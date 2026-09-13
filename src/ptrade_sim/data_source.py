"""数据源后端：引擎**只连 DuckDB**（``DuckDBSource``）。

**统一约定**（引擎面对的唯一定义）
1. 返回 **polars DataFrame**，列名一律 **PTrade 口径**：
   行情 = ``volume``(股) / ``money``(元) / ``preclose``；
   估值 = ``total_value`` / ``float_value`` / ``total_shares`` / ``a_floats`` / ``dividend_ratio``。
2. ``code`` 一律 **PTrade 尾缀**（``.SS``/``.SZ``），库内 ``.SH`` 在 SQL 内转换。
3. ``date`` 一律 **8 位 YYYYMMDD** 字符串。
4. 缺失数据返回 ``None``（不是空 DataFrame），由上层按既有 None 容忍逻辑降级。

``ParquetSource``
    引擎**不使用**它，只剩一个取数对照用途：``ptrade-sim db verify`` 的
    「逐字段（日线抽样）」环节调 ``daily()``，与 DuckDB 侧逐字段比对口径。
    分钟对照与静态表对照**不走该类**（verify 内部直接 ``pl.scan_parquet`` / ``con.sql``）。
"""

from __future__ import annotations

import contextlib
import re
import threading
from pathlib import Path
from typing import ClassVar

import polars as pl
from loguru import logger

from ptrade_sim.exceptions import ConfigError, DatabaseNotFoundError, DependencyError


def _to_ss(df: pl.DataFrame, col: str = "code") -> pl.DataFrame:
    """数据源代码 -> PTrade 代码（.SH -> .SS）。"""
    return df.with_columns(pl.col(col).str.replace(".SH", ".SS", literal=True))


def _ren(df: pl.DataFrame, mapping: dict[str, str]) -> pl.DataFrame:
    """只重命名**实际存在**的列。

    ⚠️ polars 的 ``rename`` 对不存在的键会抛 ``ColumnNotFoundError``
    （pandas 则静默忽略）。各行情的重命名集合不同（分钟表没有 ``pre_close``），
    若共用一张映射表会直接报错，故此处先按列过滤。
    """
    m = {k: v for k, v in mapping.items() if k in df.columns}
    return df.rename(m) if m else df


class ParquetSource:
    """hive 分区 parquet 数据源（**仅剩取数对照用途**）。

    ⚠️ 引擎已不再使用它（引擎只连 DuckDB）。本类现在只暴露 ``daily()`` 一个入口，
    供 ``ptrade-sim db verify`` 抽样日线、与 DuckDB 逐字段比对口径；
    ``ptrade-sim db build`` 灌库也**不走本类**（直接读 parquet）。
    其余能力（分钟、估值、静态表、存在性探测）已在核对无调用点后删除。
    """

    kind = "parquet"

    #: 数据源列 -> PTrade 口径列
    RENAME: ClassVar[dict[str, str]] = {
        "vol": "volume",
        "amount": "money",
        "pre_close": "preclose",
    }

    def __init__(self, data_dir: str | Path, threads: int = 8):
        self.dir = Path(data_dir)
        self.threads = threads

    # ---------- 分区路径 ----------
    def _part(self, table: str, ds: str) -> Path:
        return (
            self.dir
            / table
            / f"year={ds[:4]}"
            / f"month={ds[4:6]}"
            / f"day={ds[6:]}"
            / "data.parquet"
        )

    # ---------- 行情 ----------
    def daily(self, ds: str) -> pl.DataFrame | None:
        p = self._part("ashare_1d_stock", ds)
        if not p.exists():
            return None
        df = pl.read_parquet(
            p,
            columns=[
                "code",
                "open",
                "high",
                "low",
                "close",
                "pre_close",
                "vol",
                "amount",
                "adj_factor",
                "is_st",
                "is_delisted",
                "name",
            ],
        )
        return _ren(_to_ss(df), self.RENAME)


class DuckDBSource:
    """DuckDB 物理库数据源（由 ``ptrade-sim db build`` 构建）。

    - 列名已是 PTrade 口径，无需重命名；``.SH -> .SS`` 在 SQL 内完成。
    - 用**线程本地连接**：DuckDB 连接非线程安全，而预热走 ThreadPoolExecutor。
    - DB 只读打开，允许多进程并发（并行回测）。
    """

    kind = "duckdb"

    #: 各表取数列（与 ParquetSource 返回的列保持一致）
    COLS_MINUTE: ClassVar[list[str]] = [
        "code",
        "trade_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "money",
    ]
    COLS_DAILY: ClassVar[list[str]] = [
        "code",
        "open",
        "high",
        "low",
        "close",
        "preclose",
        "volume",
        "money",
        "adj_factor",
        "is_st",
        "is_delisted",
        "name",
    ]
    COLS_FEATURE: ClassVar[list[str]] = [
        "code",
        "total_value",
        "float_value",
        "a_floats",
        "total_shares",
        "turnover_rate",
        "dividend_ratio",
        "pe_ttm",
        "pb",
        "ps",
        "ps_ttm",
    ]

    #: DuckDB 缓冲池上限的合法写法（数字 + 可选单位）。用于校验配置，避免拼进 SQL。
    _LIMIT_RE = re.compile(r"^\s*\d+(?:\.\d+)?\s*(?:KB|MB|GB|TB|B)?\s*$", re.IGNORECASE)

    #: 最多记录多少条查询失败样本（长回测里可能有成千上万次，全留会堆爆内存）
    MAX_RECORDED_ERRORS = 20

    #: DuckDB ``memory_limit`` 默认值。
    #:
    #: **必须设**：DuckDB 默认 ``memory_limit`` 是**系统内存的 80%**，且它的缓冲池
    #: **只增不减** —— 读过的表页会一直被缓存。本平台的负载是「每天读不同日期的
    #: 分区数据、几乎没有页复用」，于是这个缓冲池纯粹是浪费，实测按约 18 MB/天
    #: 增长（6 年区间累计 26 GB+，而引擎预估只有 7 GB）。
    #:
    #: 2GB 是实测取值：足以覆盖参考表（stock_basic / calendar / index_weight）
    #: 与单日区间扫描，超限时 DuckDB 自行淘汰页（必要时落临时文件）。
    DEFAULT_MEMORY_LIMIT = "2GB"

    def __init__(
        self,
        db_path: str | Path,
        threads: int = 8,
        memory_limit: str | int | None = DEFAULT_MEMORY_LIMIT,
    ):
        self.db_path = str(db_path)
        try:
            import duckdb  # noqa: F401
        except ImportError as exc:
            raise DependencyError(
                "引擎需要 duckdb 依赖（引擎只连 DuckDB）。请安装：\n"
                "  uv pip install duckdb     # 或\n"
                "  pip install duckdb"
            ) from exc
        if not Path(self.db_path).exists():
            raise DatabaseNotFoundError(
                f"DuckDB 库不存在：{self.db_path}\n"
                f"请先构建：ptrade-sim db build --db {self.db_path}"
            )
        self.threads = threads
        self.memory_limit = self._norm_limit(memory_limit)
        self._local = threading.local()
        #: 被吞掉的查询失败（见 :meth:`data_errors`）—— 非空意味着结果不可信
        self.query_errors: list[tuple[str, str]] = []
        self.query_error_count = 0

    @classmethod
    def _norm_limit(cls, v: str | int | None) -> str | None:
        """校验并归一 ``memory_limit``；``None`` 表示用 DuckDB 默认（不推荐）。"""
        if v is None:
            return None
        if isinstance(v, (int, float)):
            if v <= 0:
                raise ConfigError(f"DuckDB memory_limit 应为正数，实际 {v!r}")
            return str(int(v))
        s = str(v).strip()
        # 只允许「数字 + 单位」这一种形态：它会被拼进 SET 语句，
        # 放开引号/分号就等于给了配置注入 SQL 的口子。
        if not cls._LIMIT_RE.match(s):
            raise ConfigError(
                f"DuckDB memory_limit 格式不合法：{v!r}\n"
                f"应形如 '2GB' / '512MB' / '1500000000'（仅数字与单位）"
            )
        return s

    # ---------- 连接 ----------
    def _con(self):
        con = getattr(self._local, "con", None)
        if con is None:
            import duckdb

            con = duckdb.connect(self.db_path, read_only=True)
            # 用 execute 而非 sql()：sql() 返回一个**未被消费的 relation**，
            # 它会一直挂在连接上（SET 语句没有结果集，没必要建 relation）。
            con.execute(f"SET threads={max(1, int(self.threads))};")
            if self.memory_limit:
                con.execute(f"SET memory_limit='{self.memory_limit}';")
            self._local.con = con
        return con

    def _q(self, sql: str, params: list) -> pl.DataFrame | None:
        """执行查询并转 polars；表缺失/查询失败返回 None（上层降级）。

        ⚠️ 这里**故意不抛异常**：调用方（``exists`` / ``minute_day`` / ``l2_auction`` …）
        依赖 None 做存在性探测与降级。但失败**必须留日志**——SQL 写错、表名拼错、
        库文件损坏若被静默吞成「无数据」，就是本项目要根治的「跑完但没交易」空回测。

        **失败同时会被记入 :attr:`query_errors`**，由上层在回测收尾时响亮报出。
        只留一行 WARNING 是不够的：实测在 28000 行日志里，7 次
        ``Out of Memory Error`` 被完全淹没，而它们让回测结果从 12620% 变成 5706%
        —— 用户看到的是「回测完成 + 一份数字」，无从察觉数据缺口。
        """
        preview = " ".join(sql.split())[:120]
        try:
            return self._con().execute(sql, params).pl()
        except Exception as exc:
            logger.warning(f"DuckDB 查询失败，按无数据返回：{preview} —— {exc}")
            self._note_error(preview, exc)
            return None

    def _note_error(self, preview: str, exc: Exception) -> None:
        """记录一次查询失败（只留样本，避免长回测里堆爆内存）。"""
        self.query_error_count += 1
        if len(self.query_errors) < self.MAX_RECORDED_ERRORS:
            self.query_errors.append((preview, f"{type(exc).__name__}: {exc}"[:200]))

    def data_errors(self) -> list[str]:
        """本次运行中「被吞掉的查询失败」摘要；空列表表示取数全程无异常。

        非空即意味着**回测结果不可信**（某些交易日的行情缺失，策略看到的
        是「无数据」而非真实行情）。
        """
        if not self.query_error_count:
            return []
        out = [
            f"{n} 次 DuckDB 查询失败被按「无数据」处理（结果不可信）"
            for n in [self.query_error_count]
        ]
        if self.query_error_count > len(self.query_errors):
            out.append(f"（以下仅记录前 {len(self.query_errors)} 条）")
        for preview, err in self.query_errors:
            out.append(f"  · {preview} —— {err}")
        return out

    def _table_exists(self, table: str) -> bool:
        try:
            r = (
                self._con()
                .execute("SELECT 1 FROM information_schema.tables WHERE table_name = ?", [table])
                .fetchone()
            )
            return r is not None
        except Exception:
            return False

    def exists(self, table: str, ds: str) -> bool:
        if not self._table_exists(table):
            return False
        r = self._q(f'SELECT 1 FROM "{table}" WHERE date = ? LIMIT 1', [ds])
        return r is not None and r.height > 0

    # ---------- 行情 ----------
    def minute_day(self, ds: str) -> pl.DataFrame | None:
        cols = ", ".join(f'"{c}"' for c in self.COLS_MINUTE)
        df = self._q(f"SELECT {cols} FROM ashare_1m_stock WHERE date = ?", [ds])
        if df is None:
            return None
        return _to_ss(df)

    def daily(self, ds: str) -> pl.DataFrame | None:
        cols = ", ".join(f'"{c}"' for c in self.COLS_DAILY)
        df = self._q(f"SELECT {cols} FROM ashare_1d_stock WHERE date = ?", [ds])
        if df is None:
            return None
        return _to_ss(df)

    def feature(self, ds: str) -> pl.DataFrame | None:
        cols = ", ".join(f'"{c}"' for c in self.COLS_FEATURE)
        df = self._q(f"SELECT {cols} FROM ashare_1d_feature WHERE date = ?", [ds])
        if df is None:
            return None
        return _to_ss(df)

    def l2_auction(self, ds: str) -> pl.DataFrame | None:
        """某日 L2 集合竞价（可选表 ``ashare_l2_auction``）；无表/无数据返回 None。

        按日取（全表 571 万行，一次性载入约 850MB，故不整体加载）。
        """
        df = self._q(
            "SELECT code, hq_px, business_amount FROM ashare_l2_auction WHERE date = ?",
            [ds],
        )
        if df is None:
            return None
        return _to_ss(df)

    def name_timeline(self, start_date: str = "") -> pl.DataFrame | None:
        """从日线 ``name`` 列派生「名称变化时点」。

        返回 ``code, date, name``（仅名称相对上一交易日发生变化的行）。
        更名历史**已合并进日线**，不再有独立表；这里抽出的时点用于覆盖
        停牌等**当日无日线行情**的日期，避免回退到「当前简称」而使历史失真。

        ⚠️ 该派生天然是**时点安全**的：只有在日线中实际出现过的名称才会被记录，
        不会把未来的简称提前泄露到过去。
        """
        sql = """
            SELECT code, date, name FROM (
                SELECT code, date, name,
                       lag(name) OVER (PARTITION BY code ORDER BY date) AS prev_name
                FROM ashare_1d_stock
                WHERE name IS NOT NULL AND name <> ''
                  AND date >= ?
            )
            WHERE prev_name IS NULL OR prev_name <> name
            ORDER BY code, date
        """
        df = self._q(sql, [start_date or ""])
        if df is None:
            return None
        return _to_ss(df)

    # ---------- 静态/参考表 ----------
    def reference(self, table: str, columns: list[str] | None = None) -> pl.DataFrame | None:
        sel = ", ".join(f'"{c}"' for c in columns) if columns else "*"
        df = self._q(f'SELECT {sel} FROM "{table}"', [])
        if df is None:
            return None
        if "code" in df.columns:
            df = _to_ss(df)
        return df

    def describe(self) -> str:
        return f"duckdb(db={self.db_path})"

    # ---------- 覆盖范围 ----------
    def coverage(self, table: str = "ashare_1d_stock") -> tuple[str, str] | None:
        """某表的日期覆盖范围 ``(min_date, max_date)``；表不存在或无数据返回 None。

        用途：库只灌了部分年份时，回测区间超出覆盖会**静默取不到数据**，
        需在启动时显式告警而不是让用户对着一份空回测发懵。
        """
        try:
            r = self._con().execute(f'SELECT min(date), max(date) FROM "{table}"').fetchone()
        except Exception:
            return None
        if not r or not r[0]:
            return None
        return str(r[0]), str(r[1])

    def tables(self) -> set[str]:
        """库内已存在的表名集合。"""
        try:
            rows = (
                self._con()
                .execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
                )
                .fetchall()
            )
            return {r[0] for r in rows}
        except Exception:
            return set()

    def close(self) -> None:
        con = getattr(self._local, "con", None)
        if con is not None:
            with contextlib.suppress(Exception):
                con.close()
            self._local.con = None


def make_source(
    db_path: str | Path,
    threads: int = 8,
    memory_limit: str | int | None = DuckDBSource.DEFAULT_MEMORY_LIMIT,
) -> DuckDBSource:
    """构造引擎数据源（**只支持 DuckDB**）。

    引擎不再读 hive 分区 parquet —— 统一走 DuckDB 物理库，
    以保证单一取数路径、单一列名口径，并让用户能直接管理库内数据。

    ``memory_limit`` 见 :attr:`DuckDBSource.DEFAULT_MEMORY_LIMIT` ——
    **不要轻易设为 None**：DuckDB 默认缓冲池上限是系统内存的 80% 且只增不减，
    长区间回测会因此累计占用数十 GB。
    """
    return DuckDBSource(db_path, threads=threads, memory_limit=memory_limit)
