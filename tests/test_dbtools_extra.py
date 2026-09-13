"""``dbtools`` 建库分支补充测试（构建主循环 / 源缺失语义 / 契约校验兜底）。

``tests/test_dbtools.py`` 覆盖了来源解析、投影生成与尾缀统一；
本文件补的是**建库主循环里那些只在特定源目录形态下才走到的分支** ——
它们共同决定了「库到底有没有被正确建出来」：

- 源目录在、但具体文件/分区不在时，``_source_sql`` 必须返回 ``None``，
  由上层决定「必需表报缺失」还是「可选表跳过」；一旦在这里抛异常，
  一个可选文件缺失就能让整次建库中断。
- ``build`` 的 ``tmp_dir`` 创建、已存在跳过（``overwrite``）语义。
- 必需表与可选表缺源的**文案不同**（``缺失`` / ``跳过（可选表源缺失）``）——
  两者混淆会让「建库成功」变成假象。
- 分钟表走 per_day（逐文件 CREATE/INSERT），日线走 yearly（整年 glob）：
  这条分叉决定 4000+ 文件表的建库内存与 row-group 对齐，写反了不报错但很难用。
- ``verify`` 的「缺少必需表」路径：契约 ``required`` 语义的最后一道闸。

全部用 ``tmp_path`` 造最小源目录（hive 目录结构 + 少量列，不必真实数据），
不触碰任何真实库。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import polars as pl
import pytest

from ptrade_sim import data_contract as dc
from ptrade_sim import dbtools

pytestmark = pytest.mark.unit


# ============================================================
# 最小源目录构造
# ============================================================
#
# 源文件的列名一律用**数据源口径**（vol / amount / pre_close），
# 而不是契约名 —— 否则 rename 映射根本没被测到（用契约名写源，映射写错也能过）。


def _write_part(src: Path, table: str, ds: str, df: pl.DataFrame) -> Path:
    """按 hive 结构写一天的源文件：``<table>/year=/month=/day=/data.parquet``。"""
    p = src / table / f"year={ds[:4]}" / f"month={ds[4:6]}" / f"day={ds[6:]}" / "data.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(p)
    return p


def _write_single(src: Path, table: str, df: pl.DataFrame) -> Path:
    """写非分区表的单文件源：``<table>/data.parquet``。"""
    d = src / table
    d.mkdir(parents=True, exist_ok=True)
    p = d / "data.parquet"
    df.write_parquet(p)
    return p


def _minute_stock_df(ds: str, code: str, n: int = 2) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "code": [code] * n,
            "date": [ds] * n,
            "trade_time": [f"{ds[:4]}-{ds[4:6]}-{ds[6:]} 09:3{i}:00" for i in range(n)],
            "open": [10.0] * n,
            "high": [10.5] * n,
            "low": [9.5] * n,
            "close": [10.2] * n,
            "vol": [100.0 * (i + 1) for i in range(n)],
            "amount": [1000.0 * (i + 1) for i in range(n)],
            "pre_close": [9.9] * n,
            "change": [0.3] * n,
            "pct_chg": [3.0] * n,
        }
    )


def _minute_index_df(ds: str, code: str, n: int = 3) -> pl.DataFrame:
    """指数分钟源**故意不含 date 列**：契约要求从 trade_time 派生。"""
    return pl.DataFrame(
        {
            "code": [code] * n,
            "trade_time": [f"{ds[:4]}-{ds[4:6]}-{ds[6:]} 09:3{i}:00" for i in range(n)],
            "open": [4000.0] * n,
            "high": [4005.0] * n,
            "low": [3995.0] * n,
            "close": [4002.0] * n,
            "vol": [1000.0] * n,
            "amount": [4_000_000.0] * n,
        }
    )


def _daily_stock_df(ds: str, code: str, close: float) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "code": [code],
            "date": [ds],
            "open": [close - 0.1],
            "high": [close + 0.2],
            "low": [close - 0.2],
            "close": [close],
            "vol": [1000.0],
            "amount": [close * 1000.0],
            "pre_close": [close - 0.05],
            "adj_factor": [1.0],
            "vwap": [close],
            "name": ["测试股"],
            "is_st": [0],
            "is_delisted": [0],
            "change": [0.05],
            "pct_chg": [0.5],
        }
    )


def _index_weight_df() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "index_code": ["000300", "000300"],
            "index_name": ["沪深300", "沪深300"],
            "code": ["600000.SH", "000001.SZ"],
            "in_date": ["20190102", "20190102"],
            "out_date": ["", ""],
            "weight": [40.0, 35.0],
            "source": ["ptrade", "ptrade"],
            "snapshot_date": ["", ""],
        }
    )


def _index_info_df() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ts_code": ["000300.SH"],
            "name": ["沪深300"],
            "market": ["SSE"],
            "publisher": ["中证"],
            "category": ["规模指数"],
            "base_date": ["20041231"],
            "base_point": [1000.0],
            "list_date": ["20050408"],
        }
    )


# ============================================================
# 工具：_human 的量级
# ============================================================


@pytest.mark.parametrize(
    "n,expect",
    [
        (0.5, "0.5B"),  # 小于 1 的余量（库文件可能极小）
        (1023, "1023.0B"),  # 进位边界下侧
        (1024, "1.0KB"),  # 进位边界：>= 1024 必须进位
        (1024**4, "1.0TB"),  # 本次补的量级：TB 分支
        (1024**5, "1.0PB"),  # PB 兜底分支（行 36）
        (2.5 * 1024**5, "2.5PB"),  # 兜底分支也要继续除 1024
    ],
)
def test_human_covers_tb_and_pb_magnitudes(n, expect):
    """防：量级表只写到 TB，PB 级库被打印成 ``1024.0TB`` 之类的错值。

    ``_human`` 的循环在 TB 之后落到兜底 ``return``；兜底若忘了再除一次 1024，
    1 PB 会打成 "1024.0PB"，日志里的库大小直接骗人。
    """
    assert dbtools._human(n) == expect, f"_human({n}) 应为 {expect}"


# ============================================================
# 来源解析：返回 None 的三种情形
# ============================================================


def test_source_sql_none_for_every_table_in_empty_dir(tmp_path):
    """防：某张表在空源目录上抛异常（异常会中断整个建库循环）。

    空目录下**每张表**都应安静地返回 ``None``，把「缺失」交给上层文案处理。
    """
    for t in dc.ALL_TABLES:
        assert dbtools._source_sql(t, tmp_path) is None, (
            f"{t.name} 在空源目录下应返回 None（由 build 决定报缺失还是跳过），而不是抛异常/给出读取表达式"
        )


def test_source_sql_none_when_file_source_absent(tmp_path):
    """file 型源（``l2_auction.parquet``）不存在 → None。

    对照组（文件存在时必须给出表达式）是必要的：否则一个「永远返回 None」的
    实现也能让本测试通过。
    """
    assert dbtools._source_sql(dc.L2_AUCTION, tmp_path) is None

    (tmp_path / dc.L2_AUCTION.source).write_bytes(b"")
    got = dbtools._source_sql(dc.L2_AUCTION, tmp_path)
    assert got is not None and got[1] == "single", "文件存在时应给出 single 读取表达式"


def test_source_sql_none_when_csv_source_absent(tmp_path):
    """csv 型源不存在 → None（同样不能抛 FileNotFoundError）。"""
    t = dc.TableContract(
        name="t",
        columns=("a",),
        key=("a",),
        partitioned=False,
        required=False,
        desc="",
        source_kind="csv",
        source="x.csv",
    )
    assert dbtools._source_sql(t, tmp_path) is None

    (tmp_path / "x.csv").write_text("a\n1\n", encoding="utf-8")
    got = dbtools._source_sql(t, tmp_path)
    assert got is not None and "read_csv_auto" in got[0]


def test_source_sql_none_when_parquet_dir_has_no_data_file(tmp_path):
    """parquet 型：目录在、``data.parquet`` 不在 → None。

    最容易写错的一种：只判断 ``is_dir()`` 就把空目录交给 ``read_parquet``，
    结果是建库中途抛 IO 错误 —— 而这本来应该只是「可选表源缺失」。
    """
    (tmp_path / dc.INDEX_WEIGHT.name).mkdir()
    assert dbtools._source_sql(dc.INDEX_WEIGHT, tmp_path) is None, (
        "目录存在但 data.parquet 缺失时必须返回 None，不能给出读取表达式"
    )

    (tmp_path / dc.INDEX_WEIGHT.name / "data.parquet").write_bytes(b"")
    got = dbtools._source_sql(dc.INDEX_WEIGHT, tmp_path)
    assert got is not None and got[1] == "single"


# ============================================================
# build：环境与入口
# ============================================================


def test_build_without_duckdb_returns_error_instead_of_raising(monkeypatch, capsys, tmp_path):
    """防：未安装 duckdb 时 ``build`` 抛 ImportError 而不是返回 1。

    CLI（``ptrade-sim db build``）靠返回值判定成败、靠打印提示用户装依赖；
    抛异常只会甩一段 traceback。
    """
    src = tmp_path / "src"
    src.mkdir()  # 源目录存在：确保失败只可能来自 import 分支，而不是「目录不存在」
    db = tmp_path / "out.duckdb"
    # sys.modules[name] = None 是让 ``import name`` 抛 ImportError 的标准手法
    monkeypatch.setitem(sys.modules, "duckdb", None)

    rc = dbtools.build(src, db)
    out = capsys.readouterr().out

    assert rc == 1, "缺 duckdb 必须返回 1（调用方按返回值判断成败）"
    assert "duckdb" in out and "pip install" in out, f"应提示安装 duckdb，实际输出：{out!r}"
    assert not db.exists(), "import 失败时不应留下半成品库文件"


def test_build_creates_tmp_dir_when_given(tmp_path, capsys):
    """防：``tmp_dir`` 只 SET 不建目录 —— DuckDB 溢写临时文件时报 "No such file"。

    大表建库必然溢写磁盘，而用户传的 ``tmp_dir``（如 ``D:/duck_tmp``）往往还不存在。
    """
    src = tmp_path / "src"
    _write_single(src, dc.INDEX_WEIGHT.name, _index_weight_df())
    cache = tmp_path / "nested" / "spill"
    assert not cache.exists()

    rc = dbtools.build(
        src,
        tmp_path / "out.duckdb",
        start_year=2025,
        end_year=2025,
        tables=[dc.INDEX_WEIGHT.name],
        tmp_dir=str(cache),
    )

    assert rc == 0, "单表建库应成功"
    assert cache.is_dir(), "tmp_dir 不存在时必须被创建（DuckDB 溢写要用它）"
    assert "建库完成" in capsys.readouterr().out


# ============================================================
# build：已存在跳过 / overwrite
# ============================================================


def test_build_skips_existing_table_unless_overwrite(tmp_path, capsys):
    """防：重复 build 无脑 DROP+重建（真实库几十亿行，重灌一次几小时）；
    以及 ``overwrite=True`` 时反而不重建。

    判据是打印文案（``[跳过] …（已存在 N 行）`` vs ``[建表] …``），
    它同时是运维判断「这次到底重灌了没有」的唯一依据。
    """
    duckdb = pytest.importorskip("duckdb")
    src = tmp_path / "src"
    _write_single(src, dc.INDEX_WEIGHT.name, _index_weight_df())
    db = tmp_path / "out.duckdb"
    tbl = dc.INDEX_WEIGHT.name
    kw = {"start_year": 2025, "end_year": 2025, "tables": [tbl]}

    assert dbtools.build(src, db, **kw) == 0
    first = capsys.readouterr().out
    assert f"[建表] {tbl}" in first, f"首次建库应建表，实际：{first!r}"

    assert dbtools.build(src, db, **kw) == 0
    second = capsys.readouterr().out
    assert f"[跳过] {tbl}（已存在 2 行）" in second, (
        f"已有表必须跳过并报告行数（运维据此确认没重灌），实际：{second!r}"
    )
    assert f"[建表] {tbl}" not in second, "未指定 overwrite 时不得重建"

    assert dbtools.build(src, db, overwrite=True, **kw) == 0
    third = capsys.readouterr().out
    assert f"[建表] {tbl}" in third, "overwrite=True 必须重建"

    con = duckdb.connect(str(db), read_only=True)
    n = con.execute(f'SELECT count(*) FROM "{tbl}"').fetchone()[0]
    codes = {r[0] for r in con.execute(f'SELECT code FROM "{tbl}"').fetchall()}
    con.close()
    assert n == 2, "重建后必须仍有数据（不能删了没建）"
    assert codes == {"600000.SS", "000001.SZ"}, "入库必须把源 .SH 统一为 .SS"


# ============================================================
# build：源缺失的文案（必需 vs 可选）
# ============================================================


def test_build_missing_source_wording_differs_for_required_and_optional(tmp_path, capsys):
    """防：必需表与可选表缺源被同样对待，让「建库成功」变成假象。

    契约语义：必需表缺源 = 建库失败（verify 返回非 0）；可选表缺源 = 正常降级。
    所以文案必须不同，且 file 型可选表要报出**缺的是哪个文件**。
    """
    duckdb = pytest.importorskip("duckdb")
    src = tmp_path / "src"
    src.mkdir()
    db = tmp_path / "out.duckdb"
    # 第三张表有真源：证明前面的缺源不会中断整个建表循环
    _write_single(src, dc.INDEX_INFO.name, _index_info_df())

    rc = dbtools.build(
        src,
        db,
        start_year=2025,
        end_year=2025,
        tables=[dc.CALENDAR.name, dc.INDEX_WEIGHT.name, dc.INDEX_INFO.name],
    )
    out = capsys.readouterr().out

    assert f"[缺失] {dc.CALENDAR.name}" in out, f"必需表缺源必须报「缺失」，实际：{out!r}"
    assert f"[跳过（可选表源缺失）] {dc.INDEX_WEIGHT.name}" in out
    assert f"[跳过（可选表源缺失）] {dc.CALENDAR.name}" not in out, "必需表不得按可选表降级处理"
    assert f"[建表] {dc.INDEX_INFO.name}" in out, "缺源的可选表不应中断后续建表"
    assert rc == 1, "请求的必需表没建出来，build 必须返回 1"

    con = duckdb.connect(str(db), read_only=True)
    n = con.execute(f'SELECT count(*) FROM "{dc.INDEX_INFO.name}"').fetchone()[0]
    con.close()
    assert n == 1, "有源的表必须真的被建出来"

    # file 型可选表：文案要带上缺失的源文件名，便于直接定位
    rc2 = dbtools.build(
        src, tmp_path / "o2.duckdb", start_year=2025, end_year=2025, tables=[dc.L2_AUCTION.name]
    )
    out2 = capsys.readouterr().out
    assert f"[跳过（可选表源缺失）] {dc.L2_AUCTION.name}（源 {dc.L2_AUCTION.source}）" in out2
    assert rc2 == 1, "子集校验：请求的表没建出来必须返回 1"


def test_build_full_mode_on_empty_source_fails_loudly(tmp_path, capsys):
    """防：不带 ``--tables`` 的全量建库在源目录为空时返回 0（假成功）。

    全量模式要对**每一张**契约表表态：必需表逐张点名「缺失」、可选表逐张提示跳过；
    最后交给全量 verify 汇总成非 0 退出码 —— 一条都不能少，否则用户拿到的
    是一个空库却以为建好了。
    """
    src = tmp_path / "src"
    src.mkdir()
    db = tmp_path / "out.duckdb"

    rc = dbtools.build(src, db, start_year=2025, end_year=2025)
    out = capsys.readouterr().out

    assert rc == 1, "源目录为空时全量建库必须失败（不能返回 0）"
    for name in dc.REQUIRED_TABLES:
        assert f"[缺失] {name}" in out, f"必需表 {name} 必须被逐张点名报缺失"
    n_optional = len(dc.ALL_TABLES) - len(dc.REQUIRED_TABLES)
    assert out.count("[跳过（可选表源缺失）]") == n_optional, (
        f"可选表缺源应逐张提示（共 {n_optional} 张），实际：{out!r}"
    )
    assert "缺少必需表" in out, "全量 verify 必须汇总缺表问题"
    assert "契约校验通过" not in out


# ============================================================
# build：hive 分区的两条分叉（逐日 / 逐年）
# ============================================================


def test_build_minute_tables_use_per_day_path(tmp_path, capsys):
    """防：分钟表走「整年 glob 一次 CTAS」。

    单年 4000+ 个文件一次读入会打爆内存，且物理表的 row-group 无法与交易日对齐
    （库的按日裁剪能力就废了）。判据：走「逐日入库」+ 每个源文件单独读入。
    """
    duckdb = pytest.importorskip("duckdb")
    src = tmp_path / "src"
    days = ["20250102", "20250103"]
    codes = ["000001.SZ", "600000.SH"]  # 含 .SH：验证入库时统一为 .SS
    per_day, idx_per_day = 2, 3
    for ds in days:
        # 一天一个文件（真实布局）：当天所有股票写在同一个 data.parquet 里
        day_df = pl.concat([_minute_stock_df(ds, c, per_day) for c in codes])
        _write_part(src, dc.MINUTE_STOCK.name, ds, day_df)
        _write_part(src, dc.MINUTE_INDEX.name, ds, _minute_index_df(ds, "000300.SH", idx_per_day))

    db = tmp_path / "out.duckdb"
    rc = dbtools.build(
        src,
        db,
        start_year=2025,
        end_year=2025,
        tables=[dc.MINUTE_STOCK.name, dc.MINUTE_INDEX.name],
    )
    out = capsys.readouterr().out

    assert rc == 0
    assert f"{dc.MINUTE_STOCK.name}（逐日入库）" in out, f"分钟表必须逐日入库，实际：{out!r}"
    assert f"{dc.MINUTE_INDEX.name}（逐日入库）" in out
    assert "逐年入库" not in out, "分钟表不得走整年 glob"
    # 逐日分支的进度行带耗时（逐年分支的同一行不带）—— 这是两条分叉唯一的外显差异
    assert re.search(r"2025:\s+2 天\s+[0-9.]+s", out), f"逐日分支应打印每天数 + 耗时，实际：{out!r}"

    con = duckdb.connect(str(db), read_only=True)
    m_tbl, i_tbl = dc.MINUTE_STOCK.name, dc.MINUTE_INDEX.name

    # 1) 天数一天不少、一天不重
    n_all = con.execute(f'SELECT count(*) FROM "{m_tbl}"').fetchone()[0]
    assert n_all == len(days) * len(codes) * per_day, "逐日入库总行数必须等于源行数合计"
    for ds in days:
        n_d = con.execute(f"SELECT count(*) FROM \"{m_tbl}\" WHERE date = '{ds}'").fetchone()[0]
        assert n_d == len(codes) * per_day, f"{ds} 的行数应等于源文件行数（逐日读入未丢天/重天）"

    # 2) 源列名 → 契约列名（vol/amount/pre_close → volume/money/preclose）
    vol, money, preclose = con.execute(
        f"SELECT sum(volume), sum(money), min(preclose) FROM \"{m_tbl}\" WHERE code = '600000.SS'"
    ).fetchone()
    assert vol == 2 * (100.0 + 200.0), "vol 必须映射为 volume 且不丢行"
    assert money == 2 * (1000.0 + 2000.0), "amount 必须映射为 money"
    assert preclose == 9.9, "pre_close 必须映射为 preclose"
    got_codes = {r[0] for r in con.execute(f'SELECT DISTINCT code FROM "{m_tbl}"').fetchall()}
    assert got_codes == {"000001.SZ", "600000.SS"}, "入库必须统一代码尾缀"

    # 3) 物理表列 == 契约列：read_parquet 会自动补 year/month/day 伪列，不得漏进表
    cols = {
        r[0]
        for r in con.execute(
            f"SELECT column_name FROM information_schema.columns WHERE table_name='{m_tbl}'"
        ).fetchall()
    }
    assert cols == set(dc.MINUTE_STOCK.columns), (
        f"物理表列必须与契约完全一致（hive 伪列不得进表），多出：{sorted(cols - set(dc.MINUTE_STOCK.columns))}"
    )

    # 4) 1m_index：源无 date 列，必须从 trade_time 派生为 8 位 YYYYMMDD
    n_idx = con.execute(f'SELECT count(*) FROM "{i_tbl}"').fetchone()[0]
    assert n_idx == len(days) * idx_per_day
    dates = {r[0] for r in con.execute(f'SELECT DISTINCT date FROM "{i_tbl}"').fetchall()}
    assert dates == set(days), f"1m_index 的 date 必须由 trade_time 派生为 8 位，实际：{dates}"
    con.close()


def test_build_hive_daily_path_appends_each_year(tmp_path, capsys):
    """防：hive 表只处理第一年（后续年份丢了 INSERT 分支 → 静默少数据），
    以及某年没有分区文件时崩溃或不吭声。

    日线等非分钟 hive 表走「整年 glob 一次」：首年 CREATE、其余年 INSERT。
    """
    duckdb = pytest.importorskip("duckdb")
    src = tmp_path / "src"
    tbl = dc.DAILY_STOCK.name
    _write_part(src, tbl, "20241230", _daily_stock_df("20241230", "600000.SH", 8.0))
    _write_part(src, tbl, "20250102", _daily_stock_df("20250102", "600000.SH", 8.5))
    _write_part(src, tbl, "20250103", _daily_stock_df("20250103", "600000.SH", 8.6))
    # 2023 年整个目录不存在 → glob 无命中，必须安静跳过

    db = tmp_path / "out.duckdb"
    rc = dbtools.build(src, db, start_year=2023, end_year=2025, tables=[tbl])
    out = capsys.readouterr().out

    assert rc == 0
    assert f"{tbl}（逐年入库）" in out, f"非分钟 hive 表必须逐年入库，实际：{out!r}"
    assert not re.search(r"2023:", out), "无分区文件的年份不应打印入库行"
    assert re.search(r"2024:\s+1 天", out) and re.search(r"2025:\s+2 天", out)

    con = duckdb.connect(str(db), read_only=True)
    got = dict(con.execute(f'SELECT date, close FROM "{tbl}"').fetchall())
    con.close()
    assert got == {"20241230": 8.0, "20250102": 8.5, "20250103": 8.6}, (
        f"第二年的 INSERT 必须真的追加（而不是覆盖/丢失），实际：{got}"
    )


def test_build_hive_source_without_partitions_does_not_fake_success(tmp_path, capsys):
    """防：源目录存在但没有 ``year=/month=/day=`` 分区时，建表循环一声不吭，
    调用方看到 exit 0 以为库建好了。

    契约里 ``ashare_1m_stock`` 是必需表，这种「空源」必须由 verify 的
    「未建出」兜住并返回非 0。
    """
    src = tmp_path / "src"
    (src / dc.MINUTE_STOCK.name).mkdir(parents=True)  # 目录在，分区文件不在
    db = tmp_path / "out.duckdb"

    rc = dbtools.build(src, db, start_year=2025, end_year=2025, tables=[dc.MINUTE_STOCK.name])
    out = capsys.readouterr().out

    assert f"[建表] {dc.MINUTE_STOCK.name}" not in out, "没有分区文件就不该报告建表成功"
    assert rc == 1, "必需表没建出来时必须返回非 0"
    assert "未建出" in out, f"必须明确指出哪张表没建出来，实际：{out!r}"


# ============================================================
# verify：缺少必需表
# ============================================================


def test_verify_reports_missing_required_tables_with_ddl(tmp_path, capsys):
    """防：缺必需表时 verify 返回 0（「契约校验通过」）—— required 语义失效。

    库是做对了列但缺了必需表的形态：这时唯一的判据就是必需表清单，
    并且要打印可直接粘贴的建表 DDL（否则用户只知道缺表、不知道缺哪些列）。
    """
    duckdb = pytest.importorskip("duckdb")
    db = tmp_path / "partial.duckdb"
    con = duckdb.connect(str(db))
    # 只放一张**列完全正确**的可选表：避免「缺列」也计成问题，从而掩盖必需表判定
    cols = ", ".join(f'"{c}" VARCHAR' for c in dc.INDEX_WEIGHT.columns)
    con.execute(f'CREATE TABLE "{dc.INDEX_WEIGHT.name}" ({cols})')

    rc = dbtools.verify(con)
    out = capsys.readouterr().out
    con.close()

    missing = dc.missing_required({dc.INDEX_WEIGHT.name})
    assert missing, "前置条件：该库应缺少必需表"
    assert rc == 1, "缺少必需表时 verify 必须返回 1"
    assert "缺少必需表" in out, f"必须报告缺少必需表，实际：{out!r}"
    assert "契约校验通过" not in out
    for name in missing:
        assert f'CREATE TABLE IF NOT EXISTS "{name}"' in out, f"{name} 的建表 DDL 未打印"
    # 「缺少必需表」聚合成 1 个问题；若 >1，说明连列检查也在报错（上面那张表没建对）
    assert "发现 1 个问题 ✗" in out, f"缺必需表应恰好聚合为 1 个问题，实际：{out!r}"
    # 可选表缺失只提示、不计入问题
    assert "（可选，未建）" in out, "可选表缺失应提示而不是当成问题"
    assert "未建出" not in out, "全量校验不应使用子集模式的文案"

    # ---- 对照：补齐全部必需表（列正确、无数据）后必须通过 ----
    # 判据只应是「必需表清单」，不能是别的什么（否则上面那半段可能只是碰巧失败）
    con = duckdb.connect(str(db))
    for t in dc.ALL_TABLES:
        if t.required:
            ddl = ", ".join(f'"{c}" VARCHAR' for c in t.columns)
            con.execute(f'CREATE TABLE IF NOT EXISTS "{t.name}" ({ddl})')
    rc2 = dbtools.verify(con)
    out2 = capsys.readouterr().out
    con.close()

    assert rc2 == 0, f"必需表齐全时应通过校验，实际：{out2!r}"
    assert "✓ 必需表齐全" in out2
    assert "契约校验通过 ✓" in out2
    assert "缺少必需表" not in out2
