"""PTrade 策略模拟回测平台运行时。

实现 PTrade API 适配层、本地账户（T+1）、任务调度与分钟级撮合核心。

数据环境：DuckDB 物理库（默认 ``data/quant.duckdb``），由 hive 按天分区的 parquet 源构建。
引擎**只连 DuckDB**取数，不再直接读 parquet。

分钟 bar 语义（已经数据探针验证）：每交易日 **241 根**，全部按结束时间标注——
09:30（集合竞价+首分钟）、09:31~11:30（120 根）、13:01~15:00（120 根）。

**为什么是 241 根而不是 240 根**：09:30 这一根对应**集合竞价的成交时点**——
实盘中集合竞价挂单就是在 9:30 撮合成交，开盘价即该次竞价的成交价。
因此 `handle_data` 必须在 09:30 触发一次，策略才能在该时点判断/买入；
若只跑到 09:31 起，等于丢掉了开盘这一最重要的一次决策点。
这不是对官方的偏差，而是对实盘竞价撮合的正确建模。
"""

from __future__ import annotations

import importlib.util
import json
import math
import uuid
from bisect import bisect_left, bisect_right
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple

import numpy as np
import pandas as pd
import polars as pl
from loguru import logger
from tqdm import tqdm

from ptrade_sim.api import build_api
from ptrade_sim.cache import MISSING, CacheConfig, CacheGroup
from ptrade_sim.config import DEFAULT_CAPITAL_BASE
from ptrade_sim.conventions import (
    DAY_SLOTS,
    day_dt_date,
    day_iso,
    limit_pct,
    limit_price,
    norm_day,
    norm_index_code,
    to_ptrade_code,
)
from ptrade_sim.data_source import make_source
from ptrade_sim.exceptions import DataError, StrategyError, StrategyImportError
from ptrade_sim.history import Clock, HistoryProvider


def _atomic_write_text(path: Path, text: str) -> None:
    """原子写文本文件：先写临时文件再替换，避免读者读到半截内容。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


# ============================================================
# 代码格式映射：PTrade(.SS/.SZ) <-> 数据源(.SH/.SZ)
# ============================================================


# ============================================================
# 日期格式：YYYYMMDD <-> YYYY-MM-DD
# ============================================================


# ============================================================
# 分钟时间轴：241 槽/天，全部结束时间标注
# ============================================================


_SLOT_MINUTES = np.array(sorted(int(s[:2]) * 60 + int(s[3:]) for s in DAY_SLOTS), dtype=np.int32)

# 主板 A 股代码前缀（get_Ashares 双保险用）
_MAINBOARD_PREFIX = ("000", "001", "002", "003")  # 深主板（含原中小板）
_MAINBOARD_PREFIX_SH = ("600", "601", "603", "605")  # 沪主板


# ============================================================
# 单日分钟数据（行按 code+slot 排序，列式 numpy 数组）
# ============================================================


class DayMinuteData:
    """某交易日全市场分钟数据的内存索引。"""

    __slots__ = ("amount", "close", "high", "low", "open", "slot", "starts", "vol")

    def __init__(self, slot: np.ndarray, o, h, low_v, c, v, a, starts: dict):
        self.slot = slot  # int32 [N] 槽位号（升序 within code）
        self.open = o
        self.high = h
        self.low = low_v
        self.close = c
        self.vol = v
        self.amount = a
        self.starts = starts  # code -> (row_start, row_end)

    def _bounds(self, code: str):
        return self.starts.get(code, (-1, -1))

    def row_of(self, code: str, slot_idx: int) -> int:
        """精确查找某槽位 bar 的行号，不存在返回 -1。"""
        s, e = self._bounds(code)
        if s < 0:
            return -1
        i = np.searchsorted(self.slot[s:e], slot_idx)
        if i < e - s and self.slot[s + i] == slot_idx:
            return s + i
        return -1

    def rows_upto(self, code: str, slot_idx: int, include: bool) -> slice:
        """返回槽位号 <= slot_idx（含/不含当前槽）的行切片。"""
        s, e = self._bounds(code)
        if s < 0:
            return slice(0, 0)
        n = np.searchsorted(self.slot[s:e], slot_idx, side="right" if include else "left")
        return slice(s, s + n)


def _load_minute_day(df: pl.DataFrame | None) -> DayMinuteData | None:
    """把某日分钟 DataFrame 构建为内存索引（线程安全，纯函数）。

    ``df`` 由数据源层提供，列名已统一为 PTrade 口径
    （``volume``/``money``），``code`` 已统一为 ``.SS``。
    """
    if df is None or df.height == 0:
        return None
    # trade_time "YYYY-MM-DD HH:MM:SS" -> 分钟数 -> 槽位号
    hh = df["trade_time"].str.slice(11, 2).cast(pl.Int32)
    mm = df["trade_time"].str.slice(14, 2).cast(pl.Int32)
    minutes = (hh * 60 + mm).to_numpy()
    slot: np.ndarray = np.searchsorted(_SLOT_MINUTES, minutes).astype(np.int32)
    df = df.with_columns(pl.Series("slot", slot)).filter(
        pl.col("slot") < len(DAY_SLOTS)  # 排除北交所 15:00 后等非标准 bar
    )
    df = df.sort(["code", "slot"])

    codes = df["code"].to_numpy()
    uniq, first_idx = np.unique(codes, return_index=True)
    starts = {
        c: (int(s), int(e))
        for c, s, e in zip(uniq, first_idx, [*list(first_idx[1:]), len(codes)], strict=False)
    }
    return DayMinuteData(
        slot=df["slot"].to_numpy(),
        o=df["open"].to_numpy(),
        h=df["high"].to_numpy(),
        low_v=df["low"].to_numpy(),
        c=df["close"].to_numpy(),
        v=df["volume"].to_numpy(),
        a=df["money"].to_numpy(),
        starts=starts,
    )


# ============================================================
# 数据源：预热整载 + 内存索引（polars，无查询引擎）
# ============================================================


class IndexQuery(NamedTuple):
    """``DataFeed.index_query`` 的返回值。

    NamedTuple 使其**既可解包又可属性访问**，调用方按需取用::

        codes, reason = feed.index_query("000300", "20200102")   # 解包
        q = feed.index_query("399006", "20150105"); q.reason     # 属性
    """

    codes: list[str]
    reason: str


class DataFeed:
    """本地行情数据源。分钟数据按日整载（mode=all 预热 / rolling LRU）。"""

    def __init__(
        self,
        db_path: str,
        start_day: str,
        end_day: str,
        preload_mode: str = "rolling",
        rolling_window: int = 10,
        threads: int = 8,
        cache_config: CacheConfig | None = None,
    ):
        """数据源为 DuckDB 物理库（引擎不再读 parquet）。

        L2 竞价在库内表 ``ashare_l2_auction``；更名历史**已合并进日线**
        （``ashare_1d_stock.name``，时点正确），无独立表，也无外挂文件。
        """
        self.db_path = str(db_path)
        self.dir = Path(str(db_path)).parent
        self.start_day = start_day
        self.end_day = end_day
        self.preload_mode = preload_mode
        self.rolling_window = rolling_window
        self.threads = threads
        # 数据源：只连 DuckDB 物理库。
        # memory_limit 必须显式设小：DuckDB 默认取系统内存的 80% 且缓冲池只增不减，
        # 而本负载每天读不同日期、几乎没有页复用 —— 不设会让长区间回测累计数十 GB。
        self.src = make_source(
            self.db_path,
            threads=threads,
            memory_limit=(cache_config or CacheConfig()).duckdb_memory_limit,
        )
        logger.info(f"数据源：{self.src.describe()}")

        # --- 交易日历 ---
        cal = self.src.reference("ashare_calendar")
        if cal is None:
            raise DataError(
                f"缺少交易日历表 ashare_calendar（{self.src.describe()}）；"
                f"请先构建数据库：ptrade-sim db build --db {self.db_path}"
            )
        self.trade_days: list[str] = sorted(cal["date"].to_list())  # YYYYMMDD
        self._day_pos = {d: i for i, d in enumerate(self.trade_days)}
        self.range_days: list[str] = [d for d in self.trade_days if start_day <= d <= end_day]
        if not self.range_days:
            # 日历是参考表（通常全量），区间无交易日多半是日期写错
            cov = self.src.coverage("ashare_calendar")
            raise DataError(
                f"区间 {start_day}~{end_day} 内无交易日"
                + (f"（日历覆盖 {cov[0]} ~ {cov[1]}）" if cov else "")
            )

        # --- 数据覆盖校验 ---
        # 库里只灌了部分年份时，区间超出覆盖会**静默取不到行情**（每日返回 None），
        # 最后得到一份"跑完但没交易"的空回测。这里显式拦截。
        minute_cov = self.src.coverage("ashare_1m_stock")
        daily_cov = self.src.coverage("ashare_1d_stock")
        self.data_coverage = {"minute": minute_cov, "daily": daily_cov}
        lo, hi = self.range_days[0], self.range_days[-1]
        if daily_cov is None and minute_cov is None:
            raise DataError(
                f"DuckDB 库内没有行情数据（ashare_1d_stock / ashare_1m_stock 均为空）；"
                f"请先构建：ptrade-sim db build --db {self.db_path}"
            )
        for name, cov in (("日线", daily_cov), ("分钟", minute_cov)):
            if cov is None:
                continue
            if lo < cov[0] or hi > cov[1]:
                logger.warning(
                    f"回测区间 {lo}~{hi} 超出库内{name}数据覆盖 {cov[0]}~{cov[1]}，"
                    f"超出部分将取不到行情（该段不会有成交）"
                )

        # --- 股票基础信息 ---
        sb = self.src.reference(
            "ashare_stock_basic",
            columns=[
                "code",
                "name",
                "market",
                "list_date",
                "delist_date",
                "list_status",
            ],
        )
        if sb is None:
            raise DataError(f"缺少股票基础表 ashare_stock_basic（{self.src.describe()}）")
        # 全程 polars：仅在 API 边界（get_stock_name 等）按需转换
        self.basic = sb

        # --- 更名历史：直接从日线 name 列派生（已合并，无独立表）---
        # 源日线 name 本身即「时点正确」的简称 —— 实测对 2,939,341 行做全量比对，
        # 与独立更名表推导结果一致率 100.0000%，故独立表冗余并已移除。
        # 这里只抽出「名称发生变化」的时点，用于覆盖停牌等无日线行情的日期：
        # 否则会回退到基本表的**当前**简称，让历史日期显示未来才生效的名字。
        self.name_changes: dict[str, list[tuple[str, str]]] = {}
        nt = self.src.name_timeline(self.start_day)
        if nt is not None and nt.height:
            for sym, d, nm in zip(nt["code"], nt["date"], nt["name"], strict=False):
                # 统一存成 YYYY-MM-DD，便于与 stock_name 的比较日同格式比较
                s = str(d)
                self.name_changes.setdefault(str(sym), []).append((day_iso(s), str(nm)))
            for v in self.name_changes.values():
                v.sort()
            logger.info(
                f"更名时点已从日线派生：{len(self.name_changes):,} 只证券 / "
                f"{nt.height:,} 个变化时点"
            )

        # --- 指数日线（基准）---
        # 只需「按 (代码, 日期) 取收盘价」这一种查询，故直接摊平成
        # {代码: {日期: 收盘价}}，避免为每次基准查询做一次 DataFrame 定位。
        idx = self.src.reference("ashare_1d_index")
        if idx is None:
            logger.warning(
                f"缺少指数日线表 ashare_1d_index（{self.src.describe()}）→ 基准收益将为 NaN"
            )
            idx = pl.DataFrame({"code": [], "date": [], "close": []})
        self.index_daily: dict[str, dict[str, float]] = {}
        for c, d, cl in zip(idx["code"], idx["date"], idx["close"], strict=False):
            self.index_daily.setdefault(str(c), {})[str(d)] = float(cl)

        # --- 缓存：统一由 CacheGroup 管理（容量上限 + 内存预算 + LRU）---
        # 替代原先 9 个手写缓存。分钟数据按内存预算滚动（流式加载）；
        # preload_mode="all" 时取消预算与条数上限（全量常驻），
        # 此时应先由 resources.decide() 判定内存是否吃得下（否则会 OOM）。
        self.cache = CacheGroup(cache_config or CacheConfig())
        if preload_mode == "all":
            self.cache.minute._capacity = None
            self.cache.minute._budget = 0
            logger.warning(
                "preload_mode=all：分钟数据将全量常驻内存，长区间可能 OOM；"
                "建议改用 rolling（流式，按内存预算滚动）"
            )
        self._minute_budget = self.cache.minute._budget
        logger.info(f"缓存策略：{self.cache.describe()}")
        # --- L2 集合竞价（可选表 ashare_l2_auction）---
        # 已并入 DuckDB。**按日懒加载**：全表 571 万行，一次性建 dict 约需 850MB，
        # 改为按日拉取（约 3500 行/日）并走 daily_rows 同款缓存。
        self._has_l2 = "ashare_l2_auction" in self.src.tables()
        if self._has_l2:
            logger.debug("L2 集合竞价表可用")
        else:
            logger.debug("无 ashare_l2_auction 表 → 竞价回退 09:30 分钟 bar 近似")

    def l2_auction_day(self, ds: str) -> dict[str, tuple[float, float]]:
        """某日 L2 集合竞价 ``{code: (hq_px, business_amount)}``（懒加载 + 缓存）。"""
        if not self._has_l2:
            return {}
        v = self.cache.l2_auction.get(ds)
        if v is not MISSING:
            return v
        df = self.src.l2_auction(ds)
        out: dict[str, tuple[float, float]] = {}
        if df is not None and df.height:
            for c, p, a in zip(df["code"], df["hq_px"], df["business_amount"], strict=False):
                out[str(c)] = (float(p), float(a))
        self.cache.l2_auction.put(ds, out)
        return out

    # ---------- 日历 ----------
    def day_index(self, ds: str) -> int:
        return self._day_pos[ds]

    def prev_day(self, ds: str) -> str | None:
        i = self.day_index(ds)
        return self.trade_days[i - 1] if i > 0 else None

    def days_between(self, a: str, b: str) -> list[str]:
        ia = bisect_left(self.trade_days, a)
        ib = bisect_right(self.trade_days, b)
        return self.trade_days[ia:ib]

    # ---------- 分钟数据（流式：按内存预算滚动） ----------
    def minute_day(self, ds: str) -> DayMinuteData | None:
        """获取某日分钟数据（缓存未命中则加载）。

        流式语义：缓存按**内存预算**逐出（默认取可用内存的 25%），
        而不是按固定天数 —— 内存充裕时自然多留几天，紧张时自动少留，
        长区间回测内存占用平稳、不随区间线性增长。
        """
        v = self.cache.minute.get(ds)
        if v is not MISSING:
            return v  # 命中（含「该日缺失」的 None 记录）
        md = _load_minute_day(self.src.minute_day(ds))
        self.cache.minute.put(ds, md)
        return md

    def preload(self, progress: bool = True) -> None:
        """预热加载（**非默认**）：并行预读区间内全部分钟日。

        ⚠️ 流式模式下不调用本方法；仅在明确需要"全量常驻"时使用，
        且应先用 :func:`ptrade_sim.resources.decide` 确认内存吃得下。
        """
        days = self.range_days
        logger.info(f"开始预热加载 {len(days)} 个交易日的分钟数据（threads={self.threads}）...")
        t0 = datetime.now()

        def _work(ds):
            return ds, _load_minute_day(self.src.minute_day(ds))

        with ThreadPoolExecutor(max_workers=self.threads) as ex:
            futs = [ex.submit(_work, ds) for ds in days]
            it = as_completed(futs)
            if progress:
                it = tqdm(it, total=len(futs), desc="预热分钟数据", unit="天")
            for fut in it:
                ds, md = fut.result()
                self.cache.minute.put(ds, md)
        secs = (datetime.now() - t0).total_seconds()
        logger.info(
            f"预热完成：{len(days)} 天，耗时 {secs:.1f}s，"
            f"缓存 {len(self.cache.minute)} 项 / "
            f"{self.cache.minute.stats.bytes / 1048576:,.0f}MB"
        )

    def cache_report(self) -> dict:
        """缓存统计（供回测结束后的资源报告）。"""
        return {
            "config": self.cache.describe(),
            "caches": self.cache.report(),
            "stats": self.cache.stats(),
        }

    # ---------- 日线数据（内部全程 polars） ----------
    def ensure_daily(self, ds: str) -> pl.DataFrame | None:
        """某日全市场日线（**polars**，列为 PTrade 口径）。"""
        v = self.cache.daily.get(ds)
        if v is not MISSING:
            return v
        d = self.src.daily(ds)
        df = d if (d is not None and d.height) else None
        self.cache.daily.put(ds, df)
        return df

    def daily_rows(self, ds: str) -> dict[str, dict] | None:
        """某日全市场日线 -> {code: row_dict}，懒构建一次并缓存。

        这是**最热**的取数路径（每次下单/涨跌停判定都会调到）。
        直接从 polars 列构建行字典，省掉「polars → pandas → dict」两次转换。
        """
        v = self.cache.daily_rows.get(ds)
        if v is not MISSING:
            return v
        df = self.ensure_daily(ds)
        if df is None:
            return None
        cols = df.columns
        rows = {row[0]: dict(zip(cols, row, strict=False)) for row in df.iter_rows()}
        self.cache.daily_rows.put(ds, rows)
        return rows

    def basic_dict(self) -> dict[str, dict]:
        """基本表 -> {code: row_dict}，懒构建一次（替代逐行定位）。"""
        v = self.cache.static.get("basic_dict")
        if v is MISSING:
            cols = self.basic.columns
            v = (
                {row[0]: dict(zip(cols, row, strict=False)) for row in self.basic.iter_rows()}
                if self.basic.height
                else {}
            )
            self.cache.static.put("basic_dict", v)
        return v

    def is_st_map(self, ds: str) -> dict[str, int]:
        """某日 {code: is_st}，懒构建一次并缓存。"""
        v = self.cache.st_feat.get(ds)
        if v is not MISSING:
            return v
        df = self.ensure_daily(ds)
        m = (
            {str(c): int(s) for c, s in zip(df["code"], df["is_st"], strict=False)}
            if df is not None and "is_st" in df.columns
            else {}
        )
        self.cache.st_feat.put(ds, m)
        return m

    def ensure_feature(self, ds: str) -> pl.DataFrame | None:
        """某日全市场估值/股本数据（**polars**）。

        列名已由数据源层统一为 PTrade valuation 口径
        （``total_value``/``float_value``/``a_floats``/``total_shares``）。
        """
        v = self.cache.feature.get(ds)
        if v is not MISSING:
            return v
        d = self.src.feature(ds)
        df = d if (d is not None and d.height) else None
        self.cache.feature.put(ds, df)
        return df

    def valuation_frame(self, codes: list[str], ds: str) -> pl.DataFrame:
        """估值数据（**polars**）：columns=[code, total_value, float_value]（单位：元）。

        仅保留 ``total_value`` 非空的行 —— 与旧 pandas 版 `reindex + notna` 语义一致。
        """
        empty = pl.DataFrame(
            schema={"code": pl.String, "total_value": pl.Float64, "float_value": pl.Float64}
        )
        feat = self.ensure_feature(ds)
        if feat is None:
            return empty
        sub = feat.filter(pl.col("code").is_in(codes)).select(
            ["code", "total_value", "float_value"]
        )
        sub = sub.filter(pl.col("total_value").is_not_null())
        return sub if sub.height else empty

    def daily_row(self, ds: str, code: str) -> dict | None:
        rows = self.daily_rows(ds)
        if rows is None:
            return None
        return rows.get(code)

    def daily_close(self, ds: str, code: str) -> float | None:
        row = self.daily_row(ds, code)
        return float(row["close"]) if row else None

    def adj_factor(self, ds: str, code: str) -> float | None:
        row = self.daily_row(ds, code)
        if not row:
            return None
        # 注意：row 存在但该列可能为 NULL（契约未标 NOT NULL）。
        # 原写法 float(row["adj_factor"]) 对 None 会抛 TypeError，
        # 进而让 fq='pre'/'post' 的取数整段失败。
        v = row.get("adj_factor")
        return float(v) if v is not None else None

    def benchmark_close(self, code: str, ds: str) -> float | None:
        """基准指数日线收盘（code 为 PTrade 代码）。"""
        per_day = self.index_daily.get(code)
        if not per_day:
            return None
        return per_day.get(ds)

    # ---------- 证券信息 ----------
    def stock_name(self, code: str, cur_day: str) -> str | None:
        """按回测日生效的证券简称（三点优先）：

        1. **当日日线 ``name``** —— 最准（源日线 name 已实测为时点正确）
        2. 日线派生的**更名时点**中 ``<= cur_day`` 的最近一条 ——
           覆盖停牌等当日无日线行情的情况（避免回退到"当前简称"）
        3. 基本表 ``name`` —— 兜底（无任何日线数据时，如覆盖区间之前）
        """
        row = self.daily_row(cur_day, code)
        if row and row.get("name"):
            return str(row["name"])
        cur = day_iso(cur_day)
        for chg, nm in reversed(self.name_changes.get(code, [])):
            if chg <= cur:
                return nm
        b = self.basic_dict().get(code)
        if b and b.get("name"):
            return str(b["name"])
        return None

    def get_Ashares(self, cur_day: str) -> list[str]:
        """指定日主板 A 股列表（缓存）。"""
        ck = ("ashares", cur_day)
        v = self.cache.static.get(ck)
        if v is not MISSING:
            return v
        cur = day_iso(cur_day)
        # 全程 polars：过滤在引擎内完成，避免把 5000+ 行逐行迭代出来
        listed = self.basic.filter(
            (pl.col("market") == "主板")
            & (pl.col("list_status") == "L")
            & pl.col("list_date").is_not_null()
            & pl.col("list_date").str.slice(0, 4).str.contains(r"^\d{4}$")  # 防脏数据
        ).select(["code", "list_date", "delist_date"])
        result = []
        for code, ld, dl in zip(
            listed["code"], listed["list_date"], listed["delist_date"], strict=False
        ):
            code = str(code)
            p3 = code[:3]
            if not (
                (code.endswith(".SZ") and p3 in _MAINBOARD_PREFIX)
                or (code.endswith(".SS") and p3 in _MAINBOARD_PREFIX_SH)
            ):
                continue
            if str(ld) > cur:
                continue
            if dl is not None and str(dl) <= cur:
                continue
            result.append(code)
        result.sort()
        self.cache.static.put(ck, result)
        return result

    # ---------- 指数成分与权重（ashare_index_weight） ----------
    def index_members(self) -> dict[str, dict]:
        """指数成分与权重表（懒加载一次并缓存）。

        返回 ``{指数代码(6位): info}``，``info`` 为：

        ================  ====================================================
        ``rows``          ``[(成分股PTrade码, in_date, out_date, weight), ...]``
        ``index_name``    指数名称
        ``source``        ``ptrade``（权重拉链表）/ ``baostock``（时点精确）/ ``akshare``（仅快照）
        ``snapshot_date`` 快照源抓取日；**非空即表示该指数不具备时点查询能力**
        ``min_in``/``max_in``  区间起止，用于诊断「日期超出覆盖范围」
        ================  ====================================================

        表缺失或读取失败 → 空 dict（上层降级为空列表，不抛错）。
        由用户向 DuckDB 写入（见 ``data_contract.INDEX_WEIGHT``）。
        """
        v = self.cache.static.get("index_members")
        if v is not MISSING:
            return v
        members: dict[str, dict] = {}
        df = self.src.reference("ashare_index_weight")
        if df is None or df.height == 0:
            logger.warning(
                f"缺少指数成分权重表 ashare_index_weight（{self.src.describe()}）"
                f"→ get_index_stocks 返回空列表"
            )
            self.cache.static.put("index_members", members)
            return members

        # 兼容旧版表（无 snapshot_date / weight 列）：按 source 推断能力
        cols = set(df.columns)
        has_w = "weight" in cols
        for r in df.iter_rows(named=True):
            ic = norm_index_code(r["index_code"])
            info = members.get(ic)
            if info is None:
                info = {
                    "rows": [],
                    "index_name": str(r.get("index_name") or ic),
                    "source": str(r.get("source") or ""),
                    "snapshot_date": "",
                    "min_in": "",
                    "max_in": "",
                }
                members[ic] = info
            i_d, o_d = str(r.get("in_date") or ""), str(r.get("out_date") or "")
            w = r.get("weight") if has_w else None
            info["rows"].append((str(r["code"]), i_d, o_d, w))
            sd = str(r.get("snapshot_date") or "") if "snapshot_date" in cols else ""
            if sd:
                info["snapshot_date"] = sd
            if i_d:
                if not info["min_in"] or i_d < info["min_in"]:
                    info["min_in"] = i_d
                if not info["max_in"] or i_d > info["max_in"]:
                    info["max_in"] = i_d

        # 无 snapshot_date 列的旧表：akshare 源一律视为快照（其 out_date 恒空）
        for info in members.values():
            if not info["snapshot_date"] and info["source"] == "akshare":
                info["snapshot_date"] = info["max_in"] or "99999999"

        total = sum(len(v["rows"]) for v in members.values())
        n_snap = sum(1 for v in members.values() if v["snapshot_date"])
        logger.info(
            f"指数成分表已加载：{len(members)} 个指数 / {total} 条记录"
            f"（其中 {n_snap} 个为快照源，不具备时点查询能力）"
        )
        self.cache.static.put("index_members", members)
        return members

    def index_member_info(self, index_code: str) -> dict | None:
        """单指数元信息（供上层构造精确告警）。"""
        return self.index_members().get(norm_index_code(index_code))

    def index_stocks(self, index_code: str, ds: str) -> list[str]:
        """某指数在 ``ds``（YYYYMMDD）的成分股（简单接口，仅返回代码）。

        需要区分「空结果的原因」时用 :meth:`index_query`。
        """
        return self.index_query(index_code, ds).codes

    def index_query(self, index_code: str, ds: str) -> IndexQuery:
        """某指数在 ``ds`` 的成分股 + 诊断原因（:class:`IndexQuery`）。

        ``reason`` 取值：

        ==================  ========================================================
        ``ok``              时点查询成功（数据源具备该能力）
        ``no_table``        成分表缺失
        ``unknown_index``   指数码不在表中
        ``before_coverage`` 查询日早于表覆盖起点（源无更早数据，非「当日无成分」）
        ``snapshot_bias``   快照源 + 历史日期 → 返回的是**当前**成分，含幸存者偏差
        ==================  ========================================================

        ⚠️ **快照源（akshare）不做区间过滤**：其 ``out_date`` 恒为空，
        若按 ``in_date <= ds < out_date`` 过滤，会把「当前成分中纳入日期晚于 ds 的股票」
        全部剔除，得到**极小且看似合理**的成分数
        （实测创业板指 2015 年只返回 12 只、实际 100 只），静默产出错误股票池。
        故快照源一律返回完整当前成分，并由 ``reason`` 提示偏差。
        """
        # 结果缓存：同一 (指数, 日期) 重复查询直接命中。
        # 必要性：策略可能在 handle_data 中逐 bar 调用（241 次/日），
        # 而成分区间是「按日」变化的 —— 无缓存时 5 年回测约 6.7 亿次比较。
        ck = (norm_index_code(index_code), ds)
        hit = self.cache.index_query.get(ck)
        if hit is not MISSING:
            return hit

        if not self.index_members():
            res = IndexQuery([], "no_table")
        else:
            info = self.index_member_info(index_code)
            if info is None:
                res = IndexQuery([], "unknown_index")
            else:
                use_ds = ds or self.end_day
                if info["snapshot_date"]:
                    codes = sorted({row[0] for row in info["rows"]})
                    res = IndexQuery(
                        codes, "ok" if use_ds >= info["snapshot_date"] else "snapshot_bias"
                    )
                else:
                    codes = [
                        row[0]
                        for row in info["rows"]
                        if (not row[1] or row[1] <= use_ds) and (not row[2] or use_ds < row[2])
                    ]
                    if not codes and info["min_in"] and use_ds < info["min_in"]:
                        res = IndexQuery([], "before_coverage")
                    else:
                        res = IndexQuery(sorted(codes), "ok")
        self.cache.index_query.put(ck, res)
        return res


# ============================================================
# 账户对象：Position / Portfolio（T+1）
# ============================================================


class Position:
    """持仓对象：官方字段 + 用户要求字段的别名兼容。"""

    def __init__(self, security: str):
        self.sid = security
        self.total_amount = 0  # 总持仓
        self.today_amount = 0  # 今仓（T+1 不可卖）
        self.avg_cost = 0.0  # 持仓成本（移动加权）
        self.price = 0.0  # 最新价
        self.business_type = "STOCK"

    # ---- 别名 ----
    @property
    def security(self) -> str:
        return self.sid

    @property
    def amount(self) -> int:
        return self.total_amount

    @property
    def closeable_amount(self) -> int:
        return self.total_amount - self.today_amount

    @property
    def enable_amount(self) -> int:
        return self.closeable_amount

    @property
    def cost_basis(self) -> float:
        return self.avg_cost

    @property
    def last_sale_price(self) -> float:
        return self.price

    @property
    def value(self) -> float:
        return self.total_amount * self.price

    def __repr__(self):
        return (
            f"Position({self.sid}, total={self.total_amount}, closeable={self.closeable_amount}, "
            f"avg_cost={self.avg_cost:.3f}, price={self.price:.3f})"
        )


class Portfolio:
    """账户资产对象。cash 与 available_cash 恒等（本地回测无冻结资金）。"""

    def __init__(self, capital_base: float):
        self._cash = float(capital_base)
        self.capital_base = capital_base
        self.positions: dict[str, Position] = {}
        self.start_date = None

    @property
    def cash(self) -> float:
        return self._cash

    @property
    def available_cash(self) -> float:
        return self._cash

    @property
    def positions_value(self) -> float:
        return sum(p.value for p in self.positions.values())

    @property
    def total_value(self) -> float:
        return self._cash + self.positions_value

    @property
    def portfolio_value(self) -> float:
        return self.total_value

    @property
    def capital_used(self) -> float:
        return self.capital_base - self._cash

    @property
    def pnl(self) -> float:
        return self.total_value - self.capital_base

    @property
    def returns(self) -> float:
        return self.pnl / self.capital_base if self.capital_base else 0.0


# ============================================================
# 订单与成交
# ============================================================


@dataclass
class Order:
    id: str
    security: str
    amount: int  # 正买负卖（委托量）
    status: str  # filled / canceled / rejected
    price: float = 0.0
    filled_amount: int = 0
    commission: float = 0.0
    create_dt: datetime | None = None


@dataclass
class Trade:
    time: datetime
    security: str
    side: str  # buy / sell
    amount: int
    price: float
    turnover: float
    commission: float
    order_id: str
    trade_pnl: float = 0.0  # 卖出时相对持仓成本的盈亏


# ============================================================
# BarData（handle_data 的 data[code]）
# ============================================================


class BarData:
    """单代码单周期 K 线对象（分钟频率下 preclose/high_limit/low_limit 填 0.0）。"""

    def __init__(
        self,
        symbol: str,
        name: str,
        dt: datetime,
        is_open: int,
        o,
        h,
        low_v,
        c,
        vol,
        money,
    ):
        self.symbol = symbol
        self.name = name
        self.dt = dt
        self.datetime = dt
        self.is_open = is_open
        self.open = float(o) if o is not None else float("nan")
        self.high = float(h) if h is not None else float("nan")
        self.low = float(low_v) if low_v is not None else float("nan")
        self.close = float(c) if c is not None else float("nan")
        self.price = self.close
        self.volume = float(vol) if vol is not None else 0.0
        self.money = float(money) if money is not None else 0.0
        self.preclose = 0.0
        self.high_limit = 0.0
        self.low_limit = 0.0
        self.unlimited = 0

    def __repr__(self):
        return f"BarData({self.symbol}, {self.dt}, close={self.close:.2f})"


# ============================================================
# 引擎
# ============================================================


class BacktestEngine:
    """分钟级回测引擎：调度 + 撮合 + 策略 API 适配。"""

    def __init__(self, config: dict, strategy_path: str, output_dir: Path):
        self.config = config
        self.strategy_path = str(Path(strategy_path).resolve())
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        cost = config.get("cost", {})
        self.commission_ratio = float(cost.get("commission_ratio", 0.0003))
        self.min_commission = float(cost.get("min_commission", 5.0))
        self.handling_fee_ratio = float(cost.get("handling_fee_ratio", 0.0000487))
        self.stamp_tax = float(cost.get("stamp_tax", 0.001))
        # PTrade 默认无滑点（未调用 set_slippage 时）；config 值作为初始默认
        self.slippage_ratio = float(cost.get("slippage_ratio", 0.0))
        self.fixed_slippage: float | None = None
        self._limit_mode = "LIMITED"  # 成交数量限制模式（set_limit_mode 设置）
        self.benchmark = config.get("benchmark", "000300.SS")
        # 每交易日重置的缓存（清理见 _run_day）：
        #   _name_cache / _info_cache  —— get_stock_name / get_stock_info
        #   history._price_cache       —— get_price 的全市场批缓存（codes > 500 时启用）
        self._name_cache: dict[tuple[str, str], str | None] = {}
        self._info_cache: dict[tuple, dict] = {}

        preload = config.get("preload", {})
        self.feed = DataFeed(
            db_path=config["db_path"],
            start_day=config["start_date"].replace("-", ""),
            end_day=config["end_date"].replace("-", ""),
            preload_mode=preload.get("mode", "rolling"),
            rolling_window=int(preload.get("rolling_window_days", 10)),
            threads=int(preload.get("threads", 8)),
            cache_config=CacheConfig.from_dict(config.get("cache")),
        )

        # 兜底引用 config.DEFAULT_CAPITAL_BASE（唯一权威），避免这里再写一个魔数
        # 与 runstore / 看板的兜底值不一致。
        self.capital_base = float(config.get("capital_base", DEFAULT_CAPITAL_BASE))
        # 回测周期：minute（默认，241 槽/日）| daily（每日一次，15:00）
        freq = str(config.get("frequency", "minute")).strip().lower()
        if freq in ("1d", "d", "day"):
            freq = "daily"
        elif freq in ("1m", "m", "min"):
            freq = "minute"
        if freq not in ("minute", "daily"):
            logger.warning(f"未知 frequency={config.get('frequency')!r}，回退 minute")
            freq = "minute"
        self.frequency = freq
        self._daily_mode = freq == "daily"
        self.portfolio = Portfolio(self.capital_base)
        self.g = SimpleNamespace()
        self.blotter = SimpleNamespace(current_dt=None)
        self.context = self._make_context()

        self.universe: list[str] = []
        self.schedule: dict[str, list] = {}  # 'HH:MM' -> [func]
        self.orders: dict[str, Order] = {}
        self.trades: list[Trade] = []
        self._order_seq = 0
        # 回测时钟：与 HistoryProvider **共享同一实例**，避免双份状态。
        # _day_str / _slot_pos 是它的 property（见下方），故原有 17 处调用点不变。
        self.clock = Clock()
        self._failed_funcs: set[str] = set()
        self.daily_stats: list[dict] = []
        # 历史数据取数与组装（日线/分钟/复权/重采样/价格区间/交易日）
        self.history = HistoryProvider(self.feed, self.clock, daily_mode=self._daily_mode)
        # 一次性告警去重：(指数码, 原因) / 未实现的占位 API
        self._warned_index_stocks: set[tuple[str, str]] = set()
        self._stub_warned: set[str] = set()

        # 日志：引擎**只追加**运行日志文件，不动全局 handler。
        #
        # ⚠️ 这里刻意**不调用 logger.remove()**：
        # 1. 控制台输出是 CLI（cli._setup_console_logging）的职责，引擎重复一份会双打；
        # 2. 引擎是可被嵌入的库（测试、notebook、看板服务都会直接构造它），
        #    清空全局 handler 会把宿主自己的日志配置一起干掉 —— 曾导致
        #    测试里的告警捕获全部失效。
        logger.add(
            self.output_dir / "output.log",
            level="INFO",
            format="{time:YYYY-MM-DD HH:mm:ss} {level} {message}",
            encoding="utf-8",
        )

        # ---------- 实时进度（供看板轮询 progress.json） ----------
        self._started_at = datetime.now()
        self._progress_path = self.output_dir / "progress.json"
        self._progress = {
            "status": "running",
            "phase": "starting",
            "started_at": self._started_at.isoformat(timespec="seconds"),
            "strategy": self.strategy_path,
            "start_date": config["start_date"],
            "end_date": config["end_date"],
            "capital_base": self.capital_base,
            "benchmark": self.benchmark,
            "total_days": len(self.feed.range_days),
            "day_done": 0,
            "current_date": "",
            "elapsed_sec": 0.0,
        }
        self._write_progress()

    # ---------- 回测时钟（property 转发到共享 Clock） ----------
    # 用 property 而不是普通属性，是为了让「引擎」与「HistoryProvider」看到的是
    # 同一个交易日/槽位。若各自存一份，极易出现「引擎已翻日、取数仍按上一日算」，
    # 而这类偏差不会报错、只会让结果悄悄错掉。
    @property
    def _day_str(self) -> str:
        return self.clock.day

    @_day_str.setter
    def _day_str(self, v: str) -> None:
        self.clock.day = v

    @property
    def _slot_pos(self) -> int:
        return self.clock.slot

    @_slot_pos.setter
    def _slot_pos(self, v: int) -> None:
        self.clock.slot = v

    def data_gaps(self) -> dict:
        """回看窗口越界汇总（供 CLI 写入 summary.json 的 data_gaps）。"""
        return self.history.data_gaps()

    def _write_progress(self, **updates) -> None:
        """合并更新字段并原子写入 progress.json；失败仅告警，不打断回测。"""
        self._progress.update(updates)
        self._progress["elapsed_sec"] = round(
            (datetime.now() - self._started_at).total_seconds(), 1
        )
        try:
            _atomic_write_text(
                self._progress_path,
                json.dumps(self._progress, ensure_ascii=False, indent=2),
            )
        except Exception as exc:
            logger.warning(f"progress.json 写入失败：{exc}")

    def _save_strategy_source(self) -> None:
        """把策略源码与**生效配置**复制到 run 目录，保证 run 自含可复现信息。

        - ``strategy_source.py``：策略文件可能日后被删/改名，副本保证能回读源码
        - ``strategy_config.json``：策略级配置（含展示名 ``name``），看板据此显示，
          是展示名的唯一来源
        - ``run_config.json``：**合并后的完整生效配置**（含 CLI/env 覆盖），
          日后想复现这次回测，看这一个文件即可

        失败仅告警不中断（不能因为写副本失败就让回测挂掉）。
        """
        src = Path(self.strategy_path)
        try:
            if src.exists() and src.is_file():
                dst = self.output_dir / "strategy_source.py"
                dst.write_text(src.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
                self._progress["strategy_source"] = str(dst)
                logger.info(f"策略源码副本已保存：{dst}")
            else:
                logger.warning(f"策略源码不存在，跳过保存副本：{self.strategy_path}")
        except Exception as exc:
            logger.warning(f"策略源码副本保存失败：{exc}")

        # 策略级配置原样留档（看板读它取展示名）
        try:
            sc = self.config.get("strategy_config")
            if isinstance(sc, dict) and sc:
                (self.output_dir / "strategy_config.json").write_text(
                    json.dumps(sc, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )
        except Exception as exc:
            logger.warning(f"策略配置留档失败：{exc}")

        # 生效配置留档（去掉体积大又无信息量的 preload 与路径）
        try:
            eff = {k: v for k, v in self.config.items() if k not in ("preload",)}
            (self.output_dir / "run_config.json").write_text(
                json.dumps(eff, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning(f"生效配置留档失败：{exc}")

    # ---------- context ----------
    def _make_context(self) -> SimpleNamespace:
        return SimpleNamespace(
            capital_base=self.capital_base,
            portfolio=self.portfolio,
            blotter=self.blotter,
            sim_params=SimpleNamespace(
                capital_base=self.capital_base, data_frequency=self.frequency
            ),
            slippage=SimpleNamespace(),
            commission=SimpleNamespace(),
            recorded_vars={},
            initialized=False,
            previous_date=None,
        )

    # ---------- 策略加载 ----------
    def load_strategy(self) -> None:
        spec = importlib.util.spec_from_file_location("ptrade_strategy", self.strategy_path)
        loader = spec.loader if spec is not None else None
        if spec is None or loader is None:
            raise StrategyImportError(
                f"无法加载策略文件（不是有效的 Python 模块）：{self.strategy_path}"
            )
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        # 注入 API 到策略模块全局命名空间
        api = self._build_api()
        for name, obj in api.items():
            setattr(module, name, obj)
        self._module = module
        if not hasattr(module, "initialize"):
            raise StrategyError("策略缺少必选函数 initialize(context)")
        module.initialize(self.context)
        self.context.initialized = True
        if not hasattr(module, "handle_data"):
            logger.warning("策略未定义 handle_data（官方必选），引擎将跳过每 bar 调用")
        logger.info(f"策略加载完成：{self.strategy_path}")

    # ---------- API 构造 ----------
    def _build_api(self) -> dict:
        """把 PTrade API 装成字典 —— 实现在 :mod:`ptrade_sim.api`。

        这里只做转发。原先 55 个 API 全挤在本方法里（501 行 / 52 个闭包），
        现在按官方分类拆成 6 个工厂；``runtime`` 只负责把引擎实例交给它。
        """
        return build_api(self)

    def _empty_position(self, code: str) -> Position:
        """无持仓时的空 :class:`Position`（``amount == 0``）—— 官方语义。

        官方 ``get_position`` 在无持仓时返回**空 Position**而非 ``None``，
        策略据此可以无条件读 ``pos.amount``。构造放在引擎侧，
        使 ``api.py`` 无需 import ``runtime``（避免循环依赖）。
        """
        return Position(code)

    @staticmethod
    def _fmt_log(msg, args) -> str:
        return msg if not args else f"{msg} {list(args)}"

    def _stock_status_one(self, code: str, query_type: str, ds: str) -> bool | None:
        if query_type == "ST":
            row = self.feed.daily_row(ds, code)
            return bool(row["is_st"]) if row else None
        if query_type == "HALT":
            md = self.feed.minute_day(ds)
            if md is None:
                return True
            row = self.feed.daily_row(ds, code)
            if row is None:
                return True
            # 全天无分钟数据或全天成交量为 0 视为停牌
            s, e_ = md.starts.get(code, (-1, -1))
            if s < 0:
                return True
            return float(md.vol[s:e_].sum()) <= 0
        if query_type == "DELISTING":
            b = self.feed.basic_dict()
            if code not in b:
                return True
            row = b[code]
            dd = row.get("delist_date")
            cur = day_iso(ds)
            return (row.get("list_status") == "D") or (dd is not None and str(dd) <= cur)
        return None

    def _check_limit(self, code: str, query_date: str | None = None) -> int:
        """涨跌停**状态码**（官方语义）。

        返回 ``1`` 涨停 / ``-1`` 跌停 / ``0`` 既不涨停也不跌停。

        ``query_date`` 为 None 或当日时用**当前 bar 收盘价**判断；
        给历史日期时用**该日收盘价**与**该日 preclose** 判断
        （官方：历史日期一律以收盘价判断）。
        """
        ds = norm_day(query_date) if query_date else self._day_str
        row = self.feed.daily_row(ds, code)
        if row is None:
            return 0
        pct = limit_pct(row["is_st"], code, ds)
        up = limit_price(row["preclose"], pct)
        down = limit_price(row["preclose"], -pct)
        if query_date is None or ds == self._day_str:
            bar = self._bar_now(code)
            close = float(bar[3]) if bar is not None else float(row["close"])
        else:
            close = float(row["close"])
        if close >= up:
            return 1
        if close <= down:
            return -1
        return 0

    def _bar_now(self, code: str):
        """当前 bar 元组 (o,h,l,c,vol,amount)。

        - 分钟模式：当前槽位 bar，盘前取 09:30 竞价 bar
        - 日线模式：当日日线 bar（供 15:00 单次调度使用）
        """
        if self._daily_mode:
            return self._daily_bar(code)
        md = self.feed.minute_day(self._day_str)
        if md is None:
            return None
        slot = self._slot_pos if self._slot_pos >= 0 else 0
        r = md.row_of(code, slot)
        if r < 0:
            return None
        return md.open[r], md.high[r], md.low[r], md.close[r], md.vol[r], md.amount[r]

    def _daily_bar(self, code: str):
        """当日日线 bar 元组 (o,h,l,c,volume,money)；无则 None。"""
        row = self.feed.daily_row(self._day_str, code)
        if row is None:
            return None
        return (
            float(row["open"]),
            float(row["high"]),
            float(row["low"]),
            float(row["close"]),
            float(row["volume"]),
            float(row["money"]),
        )

    def _bar_has_volume(self, code: str) -> bool:
        """当前可撮合 bar 是否有成交量（停牌/零成交则为 False）。"""
        bar = self._bar_now(code)
        return bar is not None and float(bar[4]) > 0

    def _match_price(self, code: str) -> float | None:
        """撮合价：

        - 日线模式：当日**日线收盘价**（对应 15:00 单次调度）
        - 分钟模式·盘前任务（_slot_pos<0）：PTrade 09:26 市价单在 09:31 第一根完整分钟 bar
          收盘时撮合 → 用 09:31 bar 的 **close**
        - 分钟模式·盘中：当前槽位 bar close
        """
        if self._daily_mode:
            row = self.feed.daily_row(self._day_str, code)
            return float(row["close"]) if row else None
        md = self.feed.minute_day(self._day_str)
        if md is None:
            return None
        if self._slot_pos < 0:
            r1 = md.row_of(code, 1)  # 09:31 bar
            if r1 >= 0:
                return float(md.close[r1])
            r0 = md.row_of(code, 0)
            if r0 >= 0:
                return float(md.close[r0])
            return None
        bar = self._bar_now(code)
        return float(bar[3]) if bar else None

    # ---------- 订单撮合 ----------
    def _next_order_id(self) -> str:
        self._order_seq += 1
        return f"{self._day_str}-{self._order_seq:06d}-{uuid.uuid4().hex[:6]}"

    def _fee(self, turnover: float, is_sell: bool) -> float:
        commission = max(turnover * self.commission_ratio, self.min_commission)
        handling = turnover * self.handling_fee_ratio
        stamp = turnover * self.stamp_tax if is_sell else 0.0
        return commission + handling + stamp

    def _fill_price(self, base_price: float, is_buy: bool) -> float:
        if self.fixed_slippage is not None:
            return base_price + (self.fixed_slippage / 2 if is_buy else -self.fixed_slippage / 2)
        return base_price * (1 + self.slippage_ratio / 2 if is_buy else 1 - self.slippage_ratio / 2)

    def _reject(self, code: str, amount: int, reason: str) -> None:
        oid = self._next_order_id()
        self.orders[oid] = Order(
            id=oid,
            security=code,
            amount=amount,
            status="rejected",
            create_dt=self.blotter.current_dt,
        )
        logger.warning(f"废单：{code} 数量 {amount}，原因：{reason}")

    def _order(self, code: str, amount: int) -> str | None:
        """订单撮合内核。amount 正买负卖。返回 Order.id 或 None。"""
        if amount == 0:
            return None
        price = self._match_price(code)
        if price is None or price <= 0:
            self._reject(code, amount, "停牌/无行情")
            return None
        daily = self.feed.daily_row(self._day_str, code)
        if daily is None or int(daily["is_delisted"]):
            self._reject(code, amount, "退市/未上市")
            return None
        if not self._bar_has_volume(code):
            self._reject(code, amount, "盘中无成交（停牌或零成交）")
            return None
        # 一字板拒单（仅 LIMITED 模式；set_limit_mode("UNLIMITED") 不限制）
        pct = limit_pct(daily["is_st"], code, self._day_str)
        up = limit_price(daily["preclose"], pct)
        down = limit_price(daily["preclose"], -pct)
        bar = self._bar_now(code)
        # 一字板 = 全天最高=最低（无波动）
        if (
            self._limit_mode != "UNLIMITED"
            and bar is not None
            and float(bar[1]) == float(bar[2])
            and (float(bar[3]) >= up or float(bar[3]) <= down)
        ):
            self._reject(code, amount, "一字涨跌停无法成交")
            return None

        pos = self.portfolio.positions.get(code)
        total = pos.total_amount if pos else 0
        closeable = pos.closeable_amount if pos else 0
        amount = int(amount)

        if amount > 0:  # 买入
            amt = (amount // 100) * 100
            if amt < 100:
                self._reject(code, amount, "买入数量不足 100 股")
                return None
            buy_price = self._fill_price(price, True)
            # 按可用现金截断（含费用预留）
            ratio = self.commission_ratio + self.handling_fee_ratio
            max_amt = int(self.portfolio.cash / (buy_price * (1 + ratio)) // 100) * 100
            if amt > max_amt:
                amt = max_amt
                logger.warning(f"现金不足，{code} 买入数量调整为 {amt}")
            if amt < 100:
                self._reject(code, amount, "可用资金不足")
                return None
            while amt >= 100:
                turnover = amt * buy_price
                fee = self._fee(turnover, False)
                if turnover + fee <= self.portfolio.cash:
                    break
                amt -= 100
            if amt < 100:
                self._reject(code, amount, "可用资金不足")
                return None
            turnover = amt * buy_price
            fee = self._fee(turnover, False)
            self.portfolio._cash -= turnover + fee
            if pos is None:
                pos = Position(code)
                self.portfolio.positions[code] = pos
            new_total = pos.total_amount + amt
            pos.avg_cost = (
                (pos.avg_cost * pos.total_amount + turnover) / new_total if new_total else 0.0
            )
            pos.total_amount = new_total
            pos.today_amount += amt  # T+1：今仓不可卖
            pos.price = price
            oid = self._next_order_id()
            od = Order(
                id=oid,
                security=code,
                amount=amt,
                status="filled",
                price=buy_price,
                filled_amount=amt,
                commission=fee,
                create_dt=self.blotter.current_dt,
            )
            self.orders[oid] = od
            self.trades.append(
                Trade(
                    self.blotter.current_dt,
                    code,
                    "buy",
                    amt,
                    buy_price,
                    turnover,
                    fee,
                    oid,
                )
            )
            self._fire_trade_callback()
            return oid

        # 卖出
        # 显式判空（虽然 closeable==0 已隐含无持仓，但显式更清晰且利于类型收窄）
        if pos is None:
            self._reject(code, amount, "无持仓可卖")
            return None
        want = -amount
        if closeable <= 0:
            self._reject(code, amount, "T+1 约束：无可卖数量（当日买入不可卖）")
            return None
        if want >= total:
            amt = closeable  # 清仓允许零股，但最多卖可用
        else:
            amt = min(want, closeable)
            amt = (amt // 100) * 100
        if amt <= 0:
            self._reject(
                code,
                amount,
                "可卖数量不足" if want < total else "T+1 约束：可卖数量不足",
            )
            return None
        sell_price = self._fill_price(price, False)
        turnover = amt * sell_price
        fee = self._fee(turnover, True)
        pnl = (sell_price - pos.avg_cost) * amt
        self.portfolio._cash += turnover - fee  # 卖出资金 T+0 可用
        pos.total_amount -= amt
        if pos.total_amount <= 0:
            self.portfolio.positions.pop(code, None)
        oid = self._next_order_id()
        od = Order(
            id=oid,
            security=code,
            amount=-amt,
            status="filled",
            price=sell_price,
            filled_amount=amt,
            commission=fee,
            create_dt=self.blotter.current_dt,
        )
        self.orders[oid] = od
        self.trades.append(
            Trade(
                self.blotter.current_dt,
                code,
                "sell",
                amt,
                sell_price,
                turnover,
                fee,
                oid,
                trade_pnl=pnl,
            )
        )
        self._fire_trade_callback()
        return oid

    def _order_by_value(self, code: str, value: float) -> str | None:
        price = self._match_price(code)
        if price is None or price <= 0 or value == 0:
            return None
        if value > 0:
            amount = math.floor(value / price / 100) * 100
            return self._order(code, amount)
        # 负值 = 按金额卖出
        amount = -math.floor(abs(value) / price / 100) * 100
        return self._order(code, amount)

    def _order_target(self, code: str, target_amount: int) -> str | None:
        pos = self.portfolio.positions.get(code)
        total = pos.total_amount if pos else 0
        return self._order(code, int(target_amount) - total)

    def _order_by_target_value(self, code: str, value: float) -> str | None:
        price = self._match_price(code)
        if price is None or price <= 0:
            self._reject(code, 0, "无法获取有效价格")
            return None
        target_amount = math.floor(value / price / 100) * 100
        return self._order_target(code, target_amount)

    def _fire_trade_callback(self):
        cb = getattr(self._module, "on_trade_response", None)
        if cb:
            try:
                cb(self.context, [t.__dict__ for t in self.trades[-1:]])
            except Exception as exc:
                logger.error(f"on_trade_response 异常：{exc}")
        cb = getattr(self._module, "on_order_response", None)
        if cb:
            try:
                cb(self.context, [self.orders[self.trades[-1].order_id].__dict__])
            except Exception as exc:
                logger.error(f"on_order_response 异常：{exc}")

    # ---------- 行情 API ----------
    #
    # ⚠️ **pandas 边界**：以下 `_get_history` / `_get_price` / `_to_struct` /
    # `_assemble_daily` / `_apply_fq_daily` / `_history_1m` / `_resample_1m`
    # 是本项目**唯一**保留 pandas 的地方，因为 PTrade 官方规定
    # `get_history` / `get_price` 返回 pandas 的 DataFrame / Series，
    # 而策略普遍按 pandas 用法编写（`.reset_index()`、`.set_index()`、
    # `.dt.strftime()`、布尔掩码索引等）。
    # 改为 polars 会**破坏策略兼容性**，故不迁移；其余内部通路一律 polars。
    def _get_history(
        self,
        count: int,
        frequency: str,
        field,
        security_list,
        fq: str | None,
        include: bool,
        is_dict: bool,
    ) -> pd.DataFrame | dict:
        fields = [field] if isinstance(field, str) else list(field)
        single = isinstance(security_list, str)
        codes = (
            [to_ptrade_code(security_list)]
            if single
            else [
                to_ptrade_code(c)
                for c in (
                    security_list
                    if security_list
                    else self.universe or list(self.portfolio.positions)
                )
            ]
        )
        freq = frequency.lower()
        if freq == "1d":
            result = self.history.daily(codes, int(count), fields, fq, include, single)
        elif freq in ("1m", "5m", "15m", "30m", "60m", "120m"):
            if self._daily_mode:
                logger.warning(
                    f"get_history(frequency={frequency!r})：日线回测模式下无分钟数据，返回空"
                )
                return {} if is_dict else pd.DataFrame()
            result = self.history.minute(codes, int(count), freq, fields, fq, include, single)
        else:
            logger.warning(f"get_history 暂不支持频率 {frequency}，返回空")
            return {} if is_dict else pd.DataFrame()
        if is_dict:
            return {c: self.history.to_struct(result, c, freq) for c in codes}
        return result

    def _get_snapshot(self, code: str | None) -> dict:
        bar = self._bar_now(code) if code else None
        if bar is None:
            return {}
        o, h, low_v, c, v, a = bar
        return {
            "code": code,
            "open": float(o),
            "high": float(h),
            "low": float(low_v),
            "last_px": float(c),
            "business_amount": float(v),
            "business_balance": float(a),
        }

    # ---------- 盘前处理：除权 + T+1 恢复 ----------
    def _pre_day_process(self) -> None:
        pf = self.portfolio
        # 1) 持仓除权处理
        prev_ds = self.feed.prev_day(self._day_str)
        for code in list(pf.positions):
            pos = pf.positions[code]
            f_today = self.feed.adj_factor(self._day_str, code)
            f_prev = self.feed.adj_factor(prev_ds, code) if prev_ds else None
            if f_today and f_prev and not math.isclose(f_today, f_prev, rel_tol=1e-9):
                ratio = f_today / f_prev
                new_amount = round(pos.total_amount * ratio)
                # 现金分红：昨收 × ratio - 今日 pre_close（反推每股分红）
                y_close = self.feed.daily_close(prev_ds, code) if prev_ds else None
                today_row = self.feed.daily_row(self._day_str, code)
                if y_close and today_row:
                    dividend = y_close * ratio - float(today_row["preclose"])
                    if dividend > 0:
                        pf._cash += new_amount * dividend
                        logger.info(
                            f"除权除息：{code} 因子 {f_prev}->{f_today}，"
                            f"数量 {pos.total_amount}->{new_amount}，分红入账 {new_amount * dividend:.2f}"
                        )
                pos.total_amount = new_amount
                pos.avg_cost = pos.avg_cost / ratio if ratio else pos.avg_cost
        # 2) T+1 恢复：昨日买入今日可卖
        for pos in pf.positions.values():
            pos.today_amount = 0

    # ---------- 单日运行 ----------
    def _run_day(self, ds: str) -> None:
        self._day_str = ds
        self._slot_pos = -1
        # 每日重置「仅当日有效」的缓存。
        #
        # ``history._price_cache`` 必须一起清：它的 key 含 ``clock.day``，
        # 所以**跨日命中在构造上就不可能** —— 实际作用仅是「同一日内重复调用去重」。
        # 既然如此，清理**不损失任何命中率**；而不清则每天新增、永不释放
        # （全市场 ``get_price`` 单条目约 0.3~4.6 MB，长区间可累积数 GB，
        #  且完全绕过 ``cache.py`` 的 LRU / 容量管理）。
        self._name_cache.clear()
        self._info_cache.clear()
        self.history._price_cache.clear()
        day_dt = datetime.strptime(ds, "%Y%m%d")
        self.context.previous_date = None
        prev = self.feed.prev_day(ds)
        if prev:
            self.context.previous_date = datetime.strptime(prev, "%Y%m%d").date()

        # 盘前：除权 + T+1
        self._pre_day_process()

        # before_trading_start（09:15）
        self.blotter.current_dt = day_dt.replace(hour=9, minute=15)
        if hasattr(self._module, "before_trading_start"):
            self._call_strategy(
                "before_trading_start",
                lambda: self._module.before_trading_start(self.context, {}),
            )

        # 盘前 run_daily 任务（< 09:30），按时间先后执行
        pre_tasks = sorted(
            [(t, fns) for t, fns in self.schedule.items() if t < "09:30"],
            key=lambda x: x[0],
        )
        for t, fns in pre_tasks:
            hh, mm = map(int, t.split(":"))
            self.blotter.current_dt = day_dt.replace(hour=hh, minute=mm)
            for fn in fns:
                self._call_strategy(f"run_daily[{t}]{fn.__name__}", lambda fn=fn: fn(self.context))

        if self._daily_mode:
            # 日线模式：官方规定「日线级别策略每天执行一次，回测在 15:00 执行」，
            # 且 run_daily 无论设定值是多少都只在 15:00 触发。
            self.blotter.current_dt = day_dt.replace(hour=15, minute=0)
            self._slot_pos = -1  # 日线模式无分钟槽位
            for t, fns in sorted(self.schedule.items(), key=lambda x: x[0]):
                if t < "09:30":
                    continue  # 已作为盘前任务执行过
                for fn in fns:
                    self._call_strategy(
                        f"run_daily[{t}]{fn.__name__}", lambda fn=fn: fn(self.context)
                    )
            self._call_strategy("handle_data", self._call_handle_data)
            for code, pos in self.portfolio.positions.items():
                c = self.feed.daily_close(ds, code)
                if c is not None:
                    pos.price = c
        else:
            # 分钟循环：241 槽（含 09:30 集合竞价 bar）
            for slot in range(len(DAY_SLOTS)):
                self._slot_pos = slot
                hh, mm = map(int, DAY_SLOTS[slot].split(":"))
                self.blotter.current_dt = day_dt.replace(hour=hh, minute=mm)
                # 该时点调度任务
                for fn in self.schedule.get(DAY_SLOTS[slot], []):
                    self._call_strategy(
                        f"run_daily[{DAY_SLOTS[slot]}]{fn.__name__}",
                        lambda fn=fn: fn(self.context),
                    )
                # handle_data
                self._call_strategy("handle_data", self._call_handle_data)
                # 用当前 bar close 刷新持仓市值
                md = self.feed.minute_day(ds)
                if md:
                    for code, pos in self.portfolio.positions.items():
                        r = md.row_of(code, slot)
                        if r >= 0:
                            pos.price = float(md.close[r])

        # 盘后
        self.blotter.current_dt = day_dt.replace(hour=15, minute=0)
        if hasattr(self._module, "after_trading_end"):
            self._call_strategy(
                "after_trading_end",
                lambda: self._module.after_trading_end(self.context, {}),
            )
        # 取消未完成订单
        for od in self.orders.values():
            if od.status not in ("filled", "canceled", "rejected"):
                od.status = "canceled"
        self._record_daily_stats(ds)

    def _call_handle_data(self):
        handle = getattr(self._module, "handle_data", None)
        if handle is None:
            return
        data = self._build_bar_data()
        handle(self.context, data)

    def _build_bar_data(self) -> dict:
        codes = list(dict.fromkeys(self.universe + list(self.portfolio.positions)))
        day_dt = self.blotter.current_dt
        md = None if self._daily_mode else self.feed.minute_day(self._day_str)
        out = {}
        for code in codes:
            bar = None
            if self._daily_mode:
                db = self._daily_bar(code)
                if db is not None:
                    bar = BarData(
                        code,
                        self.feed.stock_name(code, self._day_str) or "",
                        day_dt,
                        1 if db[4] > 0 else 0,
                        db[0],
                        db[1],
                        db[2],
                        db[3],
                        db[4],
                        db[5],
                    )
            elif md:
                r = md.row_of(code, self._slot_pos)
                if r >= 0:
                    bar = BarData(
                        code,
                        self.feed.stock_name(code, self._day_str) or "",
                        day_dt,
                        1,
                        md.open[r],
                        md.high[r],
                        md.low[r],
                        md.close[r],
                        md.vol[r],
                        md.amount[r],
                    )
            if bar is None:
                pos = self.portfolio.positions.get(code)
                last = pos.price if pos else float("nan")
                bar = BarData(
                    code,
                    self.feed.stock_name(code, self._day_str) or "",
                    day_dt,
                    0,
                    last,
                    last,
                    last,
                    last,
                    0.0,
                    0.0,
                )
            out[code] = bar
        return out

    def _call_strategy(self, name: str, fn) -> None:
        if name in self._failed_funcs:
            return
        try:
            fn()
        except Exception as exc:
            import traceback

            logger.error(f"策略函数 {name} 异常：{exc}\n{traceback.format_exc()}")
            self._failed_funcs.add(name)

    def _record_daily_stats(self, ds: str) -> None:
        pf = self.portfolio
        # 日线收盘价复核持仓市值
        for code, pos in pf.positions.items():
            c = self.feed.daily_close(ds, code)
            if c is not None:
                pos.price = c
        bm = self.feed.benchmark_close(self.benchmark, ds) or float("nan")
        total = pf.total_value
        prev_total = self.daily_stats[-1]["total_value"] if self.daily_stats else self.capital_base
        cum = total / self.capital_base - 1
        peak = max([s["total_value"] for s in self.daily_stats], default=self.capital_base)
        peak = max(peak, total)
        self.daily_stats.append(
            {
                "date": day_iso(ds),
                "total_value": total,
                "cash": pf.cash,
                "positions_value": pf.positions_value,
                "benchmark_close": bm,
                "daily_return": total / prev_total - 1,
                "cum_return": cum,
                "drawdown": total / peak - 1,
                "trades_count": sum(1 for t in self.trades if t.time.date() == day_dt_date(ds)),
                "commission": sum(
                    t.commission for t in self.trades if t.time.date() == day_dt_date(ds)
                ),
            }
        )

    # ---------- 主入口 ----------
    def run(self) -> pl.DataFrame:
        """执行回测，返回逐日统计（**polars**）。"""
        t0 = datetime.now()
        self._write_progress(phase="preload")
        if self.feed.preload_mode == "all":
            self.feed.preload()
        elif self._daily_mode:
            # 日线模式只需日线/估值，无需分钟数据：跳过分钟预热（省时省内存）
            logger.info("日线回测模式：跳过分钟数据预热（仅按需读日线/估值）")
        logger.info(
            f"回测区间：{self.config['start_date']} ~ {self.config['end_date']}，"
            f"周期 {self.frequency}，初始资金 {self.capital_base:,.0f}，基准 {self.benchmark}"
        )
        self._write_progress(phase="init")
        self.load_strategy()
        # 保存策略源码副本到 run 目录（策略文件日后被删/改名也能在详情页看源码）
        self._save_strategy_source()
        self._write_progress(phase="simulate")
        self.portfolio.start_date = self.config["start_date"]
        for i, ds in enumerate(self.feed.range_days, start=1):
            self._run_day(ds)
            # 每日落盘：进度 + 当日快照（供运行中看板实时读取）
            self._write_progress(day_done=i, current_date=ds)
            try:
                _atomic_write_text(
                    self.output_dir / "daily_stats.csv",
                    frame_to_csv_text(self.daily_stats_frame()),
                )
            except Exception as exc:
                logger.warning(f"daily_stats.csv 快照写入失败：{exc}")
        self._write_progress(status="done", phase="done", day_done=len(self.feed.range_days))
        logger.info(
            f"回测完成，耗时 {(datetime.now() - t0).total_seconds():.1f}s，"
            f"期末资产 {self.portfolio.total_value:,.2f}"
        )
        return self.daily_stats_frame()

    def daily_stats_frame(self) -> pl.DataFrame:
        """逐日统计（**polars**）。"""
        return pl.DataFrame(self.daily_stats) if self.daily_stats else pl.DataFrame()

    def trades_frame(self) -> pl.DataFrame:
        """成交明细（**polars**）。"""
        return pl.DataFrame([t.__dict__ for t in self.trades]) if self.trades else pl.DataFrame()


def frame_to_csv_text(df: pl.DataFrame) -> str:
    """polars DataFrame -> CSV 文本，带 UTF-8 BOM。

    保留 BOM 是有意的：CSV 常被 Excel 打开，无 BOM 时中文会乱码
    （原 pandas 实现用 ``encoding="utf-8-sig"``，行为保持一致）。
    """
    return "\ufeff" + df.write_csv() if df is not None and df.height else "\ufeff"


# ============================================================
# 绩效指标与报告
# ============================================================


def _excess_kurtosis(x: np.ndarray) -> float:
    """无偏超额峰度 —— 与 ``pandas.Series.kurt()`` 逐位一致。

    **为什么需要自己实现**：polars 有 ``skew()`` 但**没有** ``kurt()``，
    而本项目原先用 pandas。polars 迁移时只换了数据类型、没换方法名，
    于是 ``mr.kurt()`` 在 ``pl.Series`` 上抛 AttributeError。

    **触发条件很容易被漏测**：那行有 ``len(mr) > 3`` 守卫，
    只有**回测跨度超过 3 个月**才会执行 —— 8 天区间的回归永远绕过它。
    而它是 ``compute_metrics`` 的最后一步，异常会让整个 run 死掉、
    不产出 ``summary.json``（看板里显示为「数据缺失」）。

    实测与 pandas 在**所有非退化样本上逐位一致**（Δ ≈ 1e-15）。
    唯一差异是 ``n < 4``：pandas 返回 NaN，这里返回 ``0.0``
    —— 沿用原有约定，避免 NaN 渗进 ``summary.json``。

    公式与 ``scipy.stats.kurtosis(bias=False)`` 一致（pandas 用的就是它）::

        g2 = m4 / m2**2 - 3                 # 有偏超额峰度
        G2 = ((n+1)*g2 + 6) * (n-1) / ((n-2)*(n-3))
    """
    n = x.size
    if n < 4:
        return 0.0
    d = x - x.mean()
    m2 = float((d**2).mean())
    # 「无变化」判定用**相对尺度**而非 `m2 == 0`：常量数组的残差是浮点噪声
    # （如 np.full(10, 0.02) 的 x-mean ~ 1e-18，m2 ~ 1e-36 ≠ 0），
    # 精确比较拦不住，会算出无意义的峰度。pandas 对常量同样返回 0.0，故一致。
    scale = float(np.max(np.abs(x)))
    if scale == 0.0 or m2 <= (scale * 1e-8) ** 2:
        return 0.0
    g2 = float((d**4).mean()) / m2**2 - 3.0
    return float(((n + 1) * g2 + 6.0) * (n - 1) / ((n - 2) * (n - 3)))


def compute_metrics(
    daily: pl.DataFrame, trades: pl.DataFrame, capital_base: float, config: dict
) -> dict:
    """计算 11 项核心指标 + 年度/月度收益分布（全程 polars）。

    入参为空表或缺关键列时返回零值摘要 —— 该函数被看板服务直接调用，
    不能因为「回测刚开始、daily_stats.csv 还是空的」就抛异常。
    """
    empty = (
        daily is None
        or daily.height == 0
        or not {"total_value", "daily_return", "drawdown"} <= set(daily.columns)
    )
    if empty:
        return {
            "total_return": 0.0,
            "annual_return": 0.0,
            "sharpe": 0.0,
            "max_drawdown": 0.0,
            "calmar": 0.0,
            "win_rate": 0.0,
            "profit_loss_ratio": 0.0,
            "final_value": float(capital_base),
            "benchmark_return": float("nan"),
            "trade_count": 0,
            "total_commission": 0.0,
            "annual_returns": {},
            "monthly_returns": {},
            "monthly_stats": {
                "win_rate": 0.0,
                "best_month": None,
                "worst_month": None,
                "mean": 0.0,
                "median": 0.0,
                "std": 0.0,
            },
            "config": {k: v for k, v in (config or {}).items() if k != "preload"},
            "trade_days": 0,
        }
    if trades is None:
        trades = pl.DataFrame()

    n_days = daily.height
    total_value = daily["total_value"]
    final_value = float(total_value[-1]) if n_days else capital_base
    total_return = final_value / capital_base - 1
    annual_return = (1 + total_return) ** (252 / max(n_days, 1)) - 1 if n_days else 0.0
    rets = daily["daily_return"].cast(pl.Float64)
    std = float(rets.std()) if n_days > 1 else 0.0
    sharpe = float(rets.mean() / std * np.sqrt(252)) if std and std > 0 else 0.0
    mdd = float(daily["drawdown"].min()) if n_days else 0.0
    calmar = annual_return / abs(mdd) if mdd < 0 else 0.0

    # 交易统计：胜率/盈亏比按卖出笔的盈亏
    n_trades = trades.height
    if n_trades:
        sells = trades.filter(pl.col("side") == "sell")
        wins = sells.filter(pl.col("trade_pnl") > 0)
        losses = sells.filter(pl.col("trade_pnl") <= 0)
        win_rate = wins.height / sells.height if sells.height else 0.0
        avg_win = float(wins["trade_pnl"].mean()) if wins.height else 0.0
        avg_loss = abs(float(losses["trade_pnl"].mean())) if losses.height else 0.0
        pl_ratio = avg_win / avg_loss if avg_loss > 0 else float("inf") if avg_win > 0 else 0.0
        total_commission = float(trades["commission"].sum())
    else:
        win_rate, pl_ratio, total_commission = 0.0, 0.0, 0.0

    # 基准（要求全程非空，与旧实现 notna().all() 语义一致）
    bm = daily["benchmark_close"].cast(pl.Float64)
    bm_ok = n_days > 0 and bm.null_count() == 0
    bm_total = float(bm[-1] / bm[0] - 1) if bm_ok else float("nan")

    # 年度/月度收益
    # ⚠️ ``date`` 列由引擎写为 ISO ``YYYY-MM-DD``（见 ``_record_daily_stats`` ->
    # ``_day_iso``），而下面的切分按紧凑 ``YYYYMMDD`` 取位 —— 故先去掉分隔符统一口径。
    # 否则 ``ym6`` 会取到 ``"2025-0"``，月度键变成 ``"2025--0"``，
    # 与看板 ``_monthly_extended`` 的 ``"YYYY-MM"`` 键对不上，月度图表与明细错位。
    compact = pl.col("date").cast(pl.String).str.replace_all("-", "")
    d = daily.with_columns(
        compact.str.slice(0, 4).alias("year"),
        compact.str.slice(0, 6).alias("ym6"),
    )
    annual_returns: dict[str, dict] = {}
    for (y,), g in d.group_by(["year"], maintain_order=True):
        bmg = g["benchmark_close"].cast(pl.Float64)
        bench = float(bmg[-1] / bmg[0] - 1) if bmg.null_count() == 0 and g.height else float("nan")
        strat = float((1 + g["daily_return"].cast(pl.Float64)).product() - 1)
        annual_returns[str(y)] = {
            "strategy": strat,
            "benchmark": bench,
            "excess": strat - bench,
        }
    monthly_returns: dict[str, float] = {}
    for (ym6,), g in d.group_by(["ym6"], maintain_order=True):
        s = str(ym6)
        monthly_returns[f"{s[:4]}-{s[4:]}"] = float(
            (1 + g["daily_return"].cast(pl.Float64)).product() - 1
        )
    # 注意：polars 的 arg_max/arg_min 返回**位置索引**，而 pandas 的 idxmax/idxmin
    # 返回标签，故这里需要把位置映射回月份字符串。
    months = list(monthly_returns.keys())
    mr = pl.Series("r", list(monthly_returns.values()), dtype=pl.Float64)
    monthly_stats = {
        "win_rate": float((mr > 0).mean()) if mr.len() else 0.0,
        "best_month": (
            {"month": months[mr.arg_max()], "return": float(mr.max())} if mr.len() else None
        ),
        "worst_month": (
            {"month": months[mr.arg_min()], "return": float(mr.min())} if mr.len() else None
        ),
        "mean": float(mr.mean()) if mr.len() else 0.0,
        "median": float(mr.median()) if mr.len() else 0.0,
        # 样本 <2 时 polars 的 std 返回 null（pandas 返回 NaN）——必须显式兜底，
        # 否则 float(None) 会抛 TypeError（单月回测就会触发）。
        "std": float(mr.std()) if mr.len() > 1 else 0.0,
        # 注意 polars 的 skew 默认 bias=True（有偏），而 pandas 是无偏 ——
        # 必须显式 bias=False，否则数值与历史结果不一致（静默偏差）。
        "skew": float(mr.skew(bias=False)) if mr.len() > 2 else 0.0,
        # polars **没有** kurt()，必须自己算（见 _excess_kurtosis）。
        "kurt": _excess_kurtosis(mr.to_numpy()) if mr.len() > 3 else 0.0,
    }

    return {
        "total_return": total_return,
        "annual_return": annual_return,
        "sharpe": sharpe,
        "max_drawdown": mdd,
        "calmar": calmar,
        "win_rate": win_rate,
        "profit_loss_ratio": pl_ratio,
        "final_value": final_value,
        "benchmark_return": bm_total,
        "trade_count": n_trades,
        "total_commission": total_commission,
        "annual_returns": annual_returns,
        "monthly_returns": monthly_returns,
        "monthly_stats": monthly_stats,
        "config": {k: v for k, v in config.items() if k != "preload"},
        "trade_days": n_days,
    }
