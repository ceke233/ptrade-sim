"""DuckDB 物理库的构建与等价性校验（命令行入口见 ``ptrade-sim db``）。

为什么用**物理表**而不是 DuckDB 视图
------------------------------------
实测（单日全市场分钟 1,312,513 行 -> polars）::

    物理表  SELECT ... WHERE date=?              47.5 ms
    视图    read_parquet(glob, hive_partitioning=1)
            WHERE date=?                       1216.9 ms   ← 慢 26 倍
    polars.read_parquet 直读                      12.9 ms

glob 视图无法把 ``WHERE date=?`` 下推到 hive 分区键（分区键是 year/month/day，
而引擎按 ``date`` 列过滤），等于每天扫描 4000+ 文件，**不可用**。
物理表按日顺序入库，DuckDB 的 row-group min/max 统计可精确裁剪单日数据。
"""

from __future__ import annotations

import time
from pathlib import Path

import polars as pl

from ptrade_sim import data_contract as dc

# ============================================================
# 工具
# ============================================================


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def _fmt_secs(s: float) -> str:
    if s < 60:
        return f"{s:.1f}s"
    m, sec = divmod(int(s), 60)
    if m < 60:
        return f"{m}m{sec:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


#: 入库时统一代码尾缀：``.SH`` → ``.SS``（PTrade 口径为 ``.SS``/``.SZ``/``.BJ``）。
#: 必须**在入库时**统一，否则同一库内会出现两种写法，直接写 SQL 的人
#: join 两表会**静默**匹配不上（引擎侧有转换兜底，但库本身应当自洽）。
CODE_SUFFIX_FROM = ".SH"
CODE_SUFFIX_TO = ".SS"


def _select_expr(t: dc.TableContract) -> str:
    """按契约列序生成投影：入库重命名 + 派生列 + 代码尾缀统一。

    - 重命名：``vol`` -> ``volume``（数据源名 -> PTrade 口径）
    - 派生：源表没有的列按 SQL 表达式现算（如 1m_index 的 date 从 trade_time 派生）
    - 尾缀：含 ``code`` 列的表统一 ``.SH`` -> ``.SS``（替换是幂等的）
    """
    parts = []
    for c in t.columns:
        if c in t.derive:
            expr = t.derive[c]
        else:
            src = c
            for k, v in t.rename.items():
                if v == c:
                    src = k
                    break
            expr = f'"{src}"'
        if c == "code":
            expr = f"replace({expr}, '{CODE_SUFFIX_FROM}', '{CODE_SUFFIX_TO}')"
        parts.append(f'{expr} AS "{c}"')
    return ", ".join(parts)


def _tables(con) -> set[str]:
    rows = con.sql(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
    ).fetchall()
    return {r[0] for r in rows}


def _rows(con, name: str) -> int | None:
    try:
        return con.sql(f'SELECT count(*) FROM "{name}"').fetchone()[0]
    except Exception:
        return None


# ============================================================
# 构建
# ============================================================


def _source_sql(t: dc.TableContract, data_dir: Path) -> tuple[str, str] | None:
    """解析某表的源数据读取表达式。

    返回 ``(read_expr, mode)``：

    - ``mode='single'``：整个表一次 CTAS（小表 / 单文件 / CSV）
    - ``mode='hive'``：逐日或逐年插入（分钟线、日线）

    返回 ``None`` 表示源数据不存在（可选表跳过、必需表报缺失）。
    """
    kind = t.resolve_source_kind()
    if kind == "file":
        p = data_dir / (t.source or "")
        if not p.exists():
            return None
        return f"read_parquet('{p.as_posix()}')", "single"
    if kind == "csv":
        p = data_dir / (t.source or "")
        if not p.exists():
            return None
        # header=true + auto 推断类型；源首列常是 csv 自带的行号（无名），
        # 投影只取契约列，故多余列会被自然丢弃。
        return f"read_csv_auto('{p.as_posix()}', header=true)", "single"
    if kind == "parquet":
        p = data_dir / t.name / "data.parquet"
        if not p.exists():
            return None
        return f"read_parquet('{p.as_posix()}')", "single"
    # hive 分区目录
    if not (data_dir / t.name).is_dir():
        return None
    return "", "hive"


def build(
    data_dir: str | Path,
    db_path: str | Path,
    start_year: int = 2019,
    end_year: int = 2025,
    tables: list[str] | None = None,
    overwrite: bool = False,
    threads: int = 8,
    memory: str = "8GB",
    tmp_dir: str | None = None,
) -> int:
    """从源数据构建 DuckDB 物理库。返回 0 表示成功。"""
    try:
        import duckdb
    except ImportError:
        print("缺少 duckdb：pip install 'ptrade-sim[duckdb]'")
        return 1

    src = Path(data_dir)
    if not src.is_dir():
        print(f"数据目录不存在：{src}")
        return 1
    years = list(range(start_year, end_year + 1))

    con = duckdb.connect(str(db_path))
    con.sql(f"SET threads={int(threads)};")
    con.sql(f"SET memory_limit='{memory}';")
    if tmp_dir:
        Path(tmp_dir).mkdir(parents=True, exist_ok=True)
        con.sql(f"SET temp_directory='{tmp_dir}';")
    con.sql("SET preserve_insertion_order=true;")  # 保序：利于按日裁剪

    want = set(tables) if tables else None
    if want:
        unknown = want - {t.name for t in dc.ALL_TABLES}
        if unknown:
            print(f"未知表名：{sorted(unknown)}")
            print("可用表：", [t.name for t in dc.ALL_TABLES])
            return 1

    print("=" * 84)
    print(f"DuckDB 建库：{db_path}")
    print(f"数据源：{src}   年份：{years[0]} ~ {years[-1]}（{len(years)} 年）")
    print(f"线程 {threads}，内存上限 {memory}")
    print("=" * 84)

    t_all = time.time()
    for t in dc.ALL_TABLES:
        if want and t.name not in want:
            continue
        resolved = _source_sql(t, src)
        if resolved is None:
            lvl = "缺失" if t.required else "跳过（可选表源缺失）"
            print(f"  [{lvl}] {t.name}" + (f"（源 {t.source}）" if t.source else ""))
            continue
        if t.name in _tables(con) and not overwrite:
            print(f"  [跳过] {t.name}（已存在 {_rows(con, t.name):,} 行）")
            continue
        con.sql(f'DROP TABLE IF EXISTS "{t.name}"')
        proj = _select_expr(t)
        read_expr, mode = resolved

        if mode == "single":
            t0 = time.time()
            con.sql(f'CREATE TABLE "{t.name}" AS SELECT {proj} FROM {read_expr}')
            print(
                f"  [建表] {t.name:<24} {_rows(con, t.name):>13,} 行  {_fmt_secs(time.time() - t0)}"
            )
            continue

        # hive 分区表：分钟线逐日（控内存 + row-group 对齐），其余逐年
        per_day = t.name in ("ashare_1m_stock", "ashare_1m_index")
        dsrc = src / t.name
        t0 = time.time()
        created = False
        print(f"  {t.name}（{'逐日' if per_day else '逐年'}入库）")
        for y in years:
            files = sorted(dsrc.glob(f"year={y}/month=*/day=*/data.parquet"))
            if not files:
                continue
            y0 = time.time()
            if per_day:
                for f in files:
                    if created:
                        con.sql(
                            f'INSERT INTO "{t.name}" SELECT {proj} '
                            f"FROM read_parquet('{f.as_posix()}')"
                        )
                    else:
                        con.sql(
                            f'CREATE TABLE "{t.name}" AS SELECT {proj} '
                            f"FROM read_parquet('{f.as_posix()}')"
                        )
                        created = True
                print(f"    {y}: {len(files):>4} 天  {_fmt_secs(time.time() - y0)}")
            else:
                glob = (dsrc / f"year={y}/month=*/day=*/data.parquet").as_posix()
                if created:
                    con.sql(f"INSERT INTO \"{t.name}\" SELECT {proj} FROM read_parquet('{glob}')")
                else:
                    con.sql(
                        f"CREATE TABLE \"{t.name}\" AS SELECT {proj} FROM read_parquet('{glob}')"
                    )
                    created = True
                print(f"    {y}: {len(files):>4} 天")
        if created:
            print(
                f"  [建表] {t.name:<24} {_rows(con, t.name):>13,} 行  {_fmt_secs(time.time() - t0)}"
            )

    dur = time.time() - t_all
    size = Path(db_path).stat().st_size if Path(db_path).exists() else 0
    print()
    print("=" * 84)
    print(f"建库完成，耗时 {_fmt_secs(dur)}，库文件 {_human(size)}")
    print("=" * 84)
    rc = verify(con, years, only=want)
    con.close()
    return rc


# ============================================================
# 校验
# ============================================================


def verify(con, years: list[int] | None = None, only: set[str] | None = None) -> int:
    """契约校验：表存在性、行数、列名。

    ``only`` 给定子集时（``db build --tables ...``），**只校验这些表**，
    不再因「缺少其他必需表」报错 —— 子集构建成功却返回非 0 会误导调用方。
    """
    print("\n" + "=" * 84)
    print("契约校验" + (f"（限 {len(only)} 张表）" if only else ""))
    print("=" * 84)
    have = _tables(con)
    problems: list[str] = []

    if only:
        # 子集模式：只检查请求的表是否建出来了
        for name in sorted(only):
            if name in have:
                print(f"  ✓ {name:<24} {_rows(con, name):>13,} 行")
            else:
                problems.append(f"未建出 {name}")
                print(f"  ✗ {name} 未建出")
    else:
        miss = dc.missing_required(have)
        if miss:
            problems.append(f"缺少必需表：{miss}")
            print(f"  ✗ 缺少必需表：{miss}")
            for m in miss:
                print(f"      {dc.table_ddl(m)}")
        else:
            print("  ✓ 必需表齐全")

        for t in dc.ALL_TABLES:
            if t.name not in have:
                if not t.required:
                    print(f"  - {t.name}（可选，未建）")
                continue
            cols = {
                r[0]
                for r in con.sql(
                    f"SELECT column_name FROM information_schema.columns "
                    f"WHERE table_name='{t.name}'"
                ).fetchall()
            }
            missing = [c for c in t.columns if c not in cols]
            extra = sorted(cols - set(t.columns))
            msg = f"  {'✓' if not missing else '✗'} {t.name:<24} {_rows(con, t.name):>13,} 行"
            if missing:
                problems.append(f"{t.name} 缺列 {missing}")
                msg += f"  缺列: {missing}"
            if extra:
                msg += f"  多列: {extra}"
            print(msg)

    if years and any("date" in t.columns and t.name in have for t in dc.ALL_TABLES):
        print("\n  日期范围：")
        lo, hi = f"{min(years)}0101", f"{max(years)}1231"
        for t in dc.ALL_TABLES:
            if t.name not in have or "date" not in t.columns:
                continue
            if only and t.name not in only:
                continue
            try:
                r = con.sql(
                    f'SELECT min(date), max(date) FROM "{t.name}" '
                    f"WHERE date >= '{lo}' AND date <= '{hi}'"
                ).fetchone()
                if r and r[0]:
                    print(f"    {t.name:<24} {r[0]} ~ {r[1]}")
            except Exception as exc:
                print(f"    {t.name}: 查询失败 {exc}")

    if problems:
        print(f"\n发现 {len(problems)} 个问题 ✗")
        return 1
    print("\n契约校验通过 ✓")
    return 0


# ============================================================
# 尾缀统一（修复既有库）
# ============================================================


def normalize_suffix(
    db_path: str | Path,
    dry_run: bool = False,
    drop_tables: list[str] | None = None,
) -> int:
    """把既有库内所有含 ``code`` 列的表统一为 PTrade 尾缀（``.SH`` → ``.SS``）。

    为什么需要单独一步：``db build`` 的规范化只对**新建的表**生效；
    历史库是用旧逻辑灌的，里面混着 ``.SH`` 与 ``.SS`` 两种写法
    （实测 ``ashare_l2_auction`` 用 ``.SS``、其余用 ``.SH``），
    直接写 SQL join 会**静默**匹配不上。本函数原地修复并做前后校验。

    ``drop_tables`` 可一并删除指定表（如已被契约移除的冗余表）。
    """
    import duckdb

    db = Path(db_path)
    if not db.exists():
        print(f"库不存在：{db}")
        return 1
    con = duckdb.connect(str(db))

    tabs = [
        r[0]
        for r in con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main' ORDER BY table_name"
        ).fetchall()
    ]
    print("=" * 88)
    print(f"代码尾缀统一：{db}   （{CODE_SUFFIX_FROM} → {CODE_SUFFIX_TO}）")
    print("=" * 88)

    # 先删表再规范化：否则被删的表会白改一遍（大表可能是上亿行）
    # ⚠️ dry_run 必须在这里也生效 —— 否则「预演」会真的删表（早先版本的缺陷）
    for t in drop_tables or []:
        if t in tabs:
            n = con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
            if dry_run:
                print(f"  [待删] {t}（{n:,} 行）")
            else:
                con.execute(f'DROP TABLE "{t}"')
                print(f"  [删除] {t}（原 {n:,} 行）")
            tabs.remove(t)
        else:
            print(f"  [跳过] {t} 不存在")
    if drop_tables:
        print()

    rc = 0
    for t in tabs:
        cols = [
            r[0]
            for r in con.execute(
                f"SELECT column_name FROM information_schema.columns WHERE table_name='{t}'"
            ).fetchall()
        ]
        if "code" not in cols:
            continue
        n_before = con.execute(
            f"SELECT count(*) FROM \"{t}\" WHERE code LIKE '%{CODE_SUFFIX_FROM}'"
        ).fetchone()[0]
        if not n_before:
            print(f"  [已是 {CODE_SUFFIX_TO}] {t}")
            continue
        if dry_run:
            print(f"  [待修] {t:<24} {n_before:>12,} 行需改")
            continue
        print(f"  [修复] {t:<24} 改写 {n_before:>12,} 行 ...", end="", flush=True)
        con.execute(
            f"UPDATE \"{t}\" SET code = replace(code, '{CODE_SUFFIX_FROM}', "
            f"'{CODE_SUFFIX_TO}') WHERE code LIKE '%{CODE_SUFFIX_FROM}'"
        )
        n_after = con.execute(
            f"SELECT count(*) FROM \"{t}\" WHERE code LIKE '%{CODE_SUFFIX_FROM}'"
        ).fetchone()[0]
        print(f" 剩余 {n_after} 行" + ("  ✓" if n_after == 0 else "  ✗"))
        if n_after:
            rc = 1

    # 同表内不得混用两种写法（防重复替换造成的脏数据）
    print()
    print("校验：是否仍有 .SH 或同表混用")
    bad = 0
    for t in tabs:
        cols = [
            r[0]
            for r in con.execute(
                f"SELECT column_name FROM information_schema.columns WHERE table_name='{t}'"
            ).fetchall()
        ]
        if "code" not in cols:
            continue
        r = con.execute(f"""
            SELECT sum(CASE WHEN code LIKE '%.SH' THEN 1 ELSE 0 END),
                   sum(CASE WHEN code LIKE '%.SS' THEN 1 ELSE 0 END)
            FROM "{t}"
        """).fetchone()
        if r[0]:
            print(f"  ✗ {t}: 仍有 {r[0]:,} 行 .SH")
            bad += 1
    print("  ✓ 全部表已统一为 .SS/.SZ/.BJ" if not bad else f"  ✗ {bad} 张表未统一")
    rc = rc or (1 if bad else 0)

    con.close()
    return rc


# ============================================================
# 与 parquet 等价性
# ============================================================


def _part(data_dir: Path, table: str, ds: str) -> Path:
    return (
        data_dir / table / f"year={ds[:4]}" / f"month={ds[4:6]}" / f"day={ds[6:]}" / "data.parquet"
    )


def verify_equivalence(
    db_path: str | Path,
    data_dir: str | Path,
    dates: list[str],
    codes: list[str] | None = None,
) -> int:
    """校验 DuckDB 与 parquet 口径一致：逐日行数 + 逐字段（日线）+ 分钟合计。"""
    import duckdb

    from ptrade_sim.data_source import ParquetSource

    db = Path(db_path)
    if not db.exists():
        print(f"库不存在：{db}")
        return 1
    src_dir = Path(data_dir)
    psrc = ParquetSource(src_dir)
    con = duckdb.connect(str(db), read_only=True)
    codes = codes or ["000001.SZ", "600000.SH", "000002.SZ"]
    bad = 0

    print("=" * 92)
    print("1) 按日行数（DuckDB vs parquet）")
    print("=" * 92)
    for t in dc.ALL_TABLES:
        if not t.partitioned or "date" not in t.columns:
            continue
        for ds in dates:
            p = _part(src_dir, t.name, ds)
            if not p.exists():
                continue
            n_d = con.sql(f"SELECT count(*) FROM \"{t.name}\" WHERE date='{ds}'").fetchone()[0]
            n_p = pl.scan_parquet(p).select(pl.len()).collect().item()
            ok = n_d == n_p
            bad += 0 if ok else 1
            print(
                f"  {'✓' if ok else '✗'} {t.name:<18} {ds}  duckdb={n_d:>10,}  parquet={n_p:>10,}"
            )

    print()
    print("=" * 92)
    print("2) 逐字段（日线抽样）")
    print("=" * 92)
    for ds in dates:
        sdf = psrc.daily(ds)  # 已统一为 PTrade 口径
        if sdf is None:
            continue
        for code in codes:
            row = sdf.filter(pl.col("code") == code)
            if row.height == 0:
                continue
            r = row.to_dicts()[0]
            q = con.sql(
                f"SELECT {', '.join(chr(34) + c + chr(34) for c in dc.DAILY_STOCK.columns)} "
                f"FROM ashare_1d_stock WHERE code='{code}' AND date='{ds}'"
            ).fetchone()
            if q is None:
                print(f"  ✗ 日线 {code} {ds}: DuckDB 无此行")
                bad += 1
                continue
            diffs = []
            for i, c in enumerate(dc.DAILY_STOCK.columns):
                sv, dv = r.get(c), q[i]
                if isinstance(sv, float) and isinstance(dv, (int, float)):
                    if abs(float(sv) - float(dv)) > 1e-9:
                        diffs.append(f"{c}:{sv}!={dv}")
                elif str(sv) != str(dv):
                    diffs.append(f"{c}:{sv!r}!={dv!r}")
            if diffs:
                print(f"  ✗ 日线 {code} {ds}: {diffs[:4]}")
                bad += 1
        print(f"  {'✓' if bad == 0 else '·'} 日线 {ds} 抽样 {len(codes)} 只完成")

    print()
    print("=" * 92)
    print("3) 分钟合计（float32 精度容差 1e-6）")
    print("=" * 92)
    msrc = {v: k for k, v in dc.MINUTE_STOCK.rename.items()}
    for ds in dates:
        p = _part(src_dir, "ashare_1m_stock", ds)
        if not p.exists():
            continue
        agg_p = (
            pl.read_parquet(p, columns=[msrc["volume"], msrc["money"]])
            .select([pl.col(msrc["volume"]).sum(), pl.col(msrc["money"]).sum()])
            .row(0)
        )
        agg_d = con.sql(
            f"SELECT sum(volume), sum(money) FROM ashare_1m_stock WHERE date='{ds}'"
        ).fetchone()
        ok = True
        for name, a, b in zip(("volume", "money"), agg_p, agg_d, strict=False):
            rel = abs(float(a) - float(b)) / max(abs(float(a)), 1.0)
            if rel > 1e-6:
                ok = False
                print(f"  ✗ 分钟 {ds} {name}: parquet={a:,.1f} duckdb={b:,.1f} 相对差 {rel:.2e}")
        if ok:
            print(f"  ✓ 分钟 {ds} 全市场合计一致")
        else:
            bad += 1

    con.close()
    print()
    print("=" * 92)
    if bad:
        print(f"发现 {bad} 处不一致 ✗")
        return 1
    print("全部一致 ✓（两后端口径相同）")
    return 0
