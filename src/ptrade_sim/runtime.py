# -*- coding: utf-8 -*-
"""PTrade 策略模拟回测平台运行时。

实现 PTrade API 适配层、本地账户（T+1）、任务调度与分钟级撮合核心。

数据环境：G:/data（hive 按天分区 parquet，1 文件 = 1 天全市场）
- 分钟 bar 语义（已经数据探针验证）：每交易日 241 根，全部按结束时间标注——
  09:30（集合竞价+首分钟，独立保留用于开盘买入与竞价判断）、
  09:31~11:30（120 根）、13:01~15:00（120 根）。
"""

from __future__ import annotations

import math
import uuid
import importlib.util
from bisect import bisect_left, bisect_right
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import polars as pl
from loguru import logger
from tqdm import tqdm


# ============================================================
# matplotlib 中文字体（跨平台）：系统字体优先，缺失时回退
# Windows 字体目录（WSL 直挂 /mnt/c/Windows/Fonts 场景）
# ============================================================
def setup_matplotlib_cn_font() -> None:
    """注册中文字体到 matplotlib，使图表中文正常渲染。

    规则：
    1. 已安装的常用中文字体（含 Windows 字体目录可访问的场景）直接可用；
    2. 否则动态 addfont 注册候选字体文件；
    3. 全部失败时保持默认（仅提示，不抛异常）。
    """
    import matplotlib.font_manager as fm
    from matplotlib import rcParams

    candidates = ["Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "WenQuanYi Zen Hei"]
    # 已注册/可用的字体名集合（初始化一次，避免反复扫描）
    available = {f.name for f in fm.fontManager.ttflist}
    hit = next((c for c in candidates if c in available), None)
    if hit is not None:
        rcParams["font.sans-serif"] = [hit]
        rcParams["axes.unicode_minus"] = False
        return

    # 系统字体未命中：尝试从常见路径注册字体文件（WSL 访问 Windows 字体）
    font_files = [
        "/mnt/c/Windows/Fonts/msyh.ttc",   # 微软雅黑
        "/mnt/c/Windows/Fonts/simhei.ttf",  # 黑体
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    ]
    for path in font_files:
        try:
            if Path(path).exists():
                fm.fontManager.addfont(path)
                available = {f.name for f in fm.fontManager.ttflist}
                hit = next((c for c in candidates if c in available), None)
                if hit is not None:
                    rcParams["font.sans-serif"] = [hit]
                    rcParams["axes.unicode_minus"] = False
                    logger.info(f"中文字体已注册：{path} -> {hit}")
                    return
        except Exception as exc:  # 单个字体失败不影响其他候选
            logger.warning(f"字体注册失败 {path}：{exc}")
    logger.warning("未找到可用中文字体，报告图表中文可能显示为方块")


# ============================================================
# 代码格式映射：PTrade(.SS/.SZ) <-> 数据源(.SH/.SZ)
# ============================================================


def to_data_code(code: str) -> str:
    """PTrade/聚宽代码 -> 数据源代码（.SS/.XSHG -> .SH，.XSHE -> .SZ）"""
    if isinstance(code, str):
        return (
            code.replace(".SS", ".SH").replace(".XSHG", ".SH").replace(".XSHE", ".SZ")
        )
    return code


def to_ptrade_code(code: str) -> str:
    """数据源代码 -> PTrade 代码（.SH -> .SS；兼容聚宽 .XSHG/.XSHE）"""
    if isinstance(code, str):
        return (
            code.replace(".XSHG", ".SS").replace(".XSHE", ".SZ").replace(".SH", ".SS")
        )
    return code


def _limit_pct(is_st: int, code: str = "", ds: str = "") -> float:
    """涨跌停比例（按板块与日期，交易所规则）：
    - 科创板（688/689）：±20%（无 ST 限制）
    - 创业板（300/301）：2020-08-24 注册制改革后 ±20%，此前 ±10%
    - 北交所（8/4 开头）：±30%
    - 主板（其余）：ST ±5%，非 ST ±10%
    """
    if code.startswith(("688", "689")):
        return 0.20
    if code.startswith(("300", "301")):
        return 0.20 if (not ds or ds >= "20200824") else 0.10
    if code.startswith(("8", "4")):
        return 0.30
    return 0.05 if int(is_st) else 0.10


def _limit_price(pre_close: float, pct: float) -> float:
    """交易所涨/跌停价：Decimal 精确四舍五入（ROUND_HALF_UP），先归一化到分再乘比例。
    Python round() 因浮点表示会把 9.185 舍成 9.18，交易所应为 9.19（601022 案例根因）。"""
    pc = Decimal(str(round(float(pre_close), 2)))
    return float((pc * Decimal(str(1 + pct))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# ============================================================
# 分钟时间轴：241 槽/天，全部结束时间标注
# ============================================================


def _build_day_slots() -> tuple[str, ...]:
    slots = ["09:30"]  # 集合竞价+首分钟 bar（独立保留）
    t = 9 * 60 + 31
    while t <= 11 * 60 + 30:
        slots.append(f"{t // 60:02d}:{t % 60:02d}")
        t += 1
    t = 13 * 60 + 1
    while t <= 15 * 60:
        slots.append(f"{t // 60:02d}:{t % 60:02d}")
        t += 1
    return tuple(slots)


DAY_SLOTS: tuple[str, ...] = _build_day_slots()
assert len(DAY_SLOTS) == 241, f"分钟槽位数应为 241，实际 {len(DAY_SLOTS)}"
SLOT_INDEX: dict[str, int] = {s: i for i, s in enumerate(DAY_SLOTS)}
_SLOT_MINUTES = np.array(
    sorted(int(s[:2]) * 60 + int(s[3:]) for s in DAY_SLOTS), dtype=np.int32
)

# 主板 A 股代码前缀（get_Ashares 双保险用）
_MAINBOARD_PREFIX = ("000", "001", "002", "003")  # 深主板（含原中小板）
_MAINBOARD_PREFIX_SH = ("600", "601", "603", "605")  # 沪主板


# ============================================================
# 单日分钟数据（行按 code+slot 排序，列式 numpy 数组）
# ============================================================


class DayMinuteData:
    """某交易日全市场分钟数据的内存索引。"""

    __slots__ = ("slot", "open", "high", "low", "close", "vol", "amount", "starts")

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
        n = np.searchsorted(
            self.slot[s:e], slot_idx, side="right" if include else "left"
        )
        return slice(s, s + n)

    def has_volume(self, code: str, slot_idx: int) -> bool:
        r = self.row_of(code, slot_idx)
        return r >= 0 and self.vol[r] > 0


def _load_minute_day(path: Path) -> DayMinuteData | None:
    """读取单个日分区分钟文件并构建内存索引（线程安全，纯函数）。"""
    if not path.exists():
        return None
    df = pl.read_parquet(
        path,
        columns=["code", "trade_time", "open", "high", "low", "close", "vol", "amount"],
    )
    # 统一为 PTrade 代码
    df = df.with_columns(pl.col("code").str.replace(".SH", ".SS", literal=True))
    # trade_time "YYYY-MM-DD HH:MM:SS" -> 分钟数 -> 槽位号
    hh = df["trade_time"].str.slice(11, 2).cast(pl.Int32)
    mm = df["trade_time"].str.slice(14, 2).cast(pl.Int32)
    minutes = (hh * 60 + mm).to_numpy()
    slot = np.searchsorted(_SLOT_MINUTES, minutes).astype(np.int32)
    df = df.with_columns(pl.Series("slot", slot)).filter(
        pl.col("slot") < len(DAY_SLOTS)  # 排除北交所 15:00 后等非标准 bar
    )
    df = df.sort(["code", "slot"])

    codes = df["code"].to_numpy()
    uniq, first_idx = np.unique(codes, return_index=True)
    starts = {
        c: (int(s), int(e))
        for c, s, e in zip(uniq, first_idx, list(first_idx[1:]) + [len(codes)])
    }
    return DayMinuteData(
        slot=df["slot"].to_numpy(),
        o=df["open"].to_numpy(),
        h=df["high"].to_numpy(),
        low_v=df["low"].to_numpy(),
        c=df["close"].to_numpy(),
        v=df["vol"].to_numpy(),
        a=df["amount"].to_numpy(),
        starts=starts,
    )


# ============================================================
# 数据源：预热整载 + 内存索引（polars，无查询引擎）
# ============================================================


class DataFeed:
    """本地行情数据源。分钟数据按日整载（mode=all 预热 / rolling LRU）。"""

    def __init__(
        self,
        data_dir: str,
        start_day: str,
        end_day: str,
        preload_mode: str = "all",
        rolling_window: int = 10,
        threads: int = 8,
    ):
        self.dir = Path(data_dir)
        self.start_day = start_day
        self.end_day = end_day
        self.preload_mode = preload_mode
        self.rolling_window = rolling_window
        self.threads = threads

        # --- 交易日历 ---
        cal = pl.read_parquet(self.dir / "ashare_calendar" / "data.parquet")
        self.trade_days: list[str] = sorted(cal["date"].to_list())  # YYYYMMDD
        self._day_pos = {d: i for i, d in enumerate(self.trade_days)}
        self.range_days: list[str] = [
            d for d in self.trade_days if start_day <= d <= end_day
        ]
        if not self.range_days:
            raise ValueError(f"区间 {start_day}~{end_day} 内无交易日")

        # --- 股票基础信息 ---
        sb = (
            pl.read_parquet(
                self.dir / "ashare_stock_basic" / "data.parquet",
                columns=[
                    "code",
                    "name",
                    "market",
                    "list_date",
                    "delist_date",
                    "list_status",
                ],
            )
            .with_columns(pl.col("code").str.replace(".SH", ".SS", literal=True))
            .rename({"code": "pcode"})
        )
        self.basic = sb.to_pandas().set_index("pcode")

        # --- 更名历史（优先当前工作目录 ./data/name_change_df.csv，回退包旁 data/）---
        nc_path = next(
            (p for p in (Path.cwd() / "data" / "name_change_df.csv", Path(__file__).resolve().parent / "data" / "name_change_df.csv") if p.exists()),
            None,
        )
        if nc_path is not None:
            nc = (
                pl.read_csv(nc_path)
                .select(
                    [
                        pl.col("name_change_symbol"),
                        pl.col("name_change_change_date"),
                        pl.col("name_change_stock_name"),
                    ]
                )
                .with_columns(
                    pl.col("name_change_symbol").str.replace(".SH", ".SS", literal=True)
                )
            )
            self.name_changes: dict[str, list[tuple[str, str]]] = {}
            for sym, chg, nm in zip(
                nc["name_change_symbol"],
                nc["name_change_change_date"],
                nc["name_change_stock_name"],
            ):
                self.name_changes.setdefault(sym, []).append((str(chg), str(nm)))
            for v in self.name_changes.values():
                v.sort()
        else:
            self.name_changes = {}

        # --- 指数日线（基准）---
        idx = pl.read_parquet(self.dir / "ashare_1d_index" / "data.parquet")
        idx = idx.with_columns(pl.col("code").str.replace(".SH", ".SS", literal=True))
        self.index_daily: dict[str, pd.DataFrame] = {}
        for key, g in idx.group_by("code"):
            c = key[0] if isinstance(key, tuple) else key  # polars group_by 键为元组
            self.index_daily[str(c)] = g.to_pandas().set_index("date")

        # --- 缓存 ---
        self._minute_cache: OrderedDict[str, DayMinuteData] = OrderedDict()
        self._daily_cache: dict[str, pd.DataFrame | None] = {}
        self._feature_cache: dict[str, pd.DataFrame | None] = {}
        self._ashares_cache: dict[str, list[str]] = {}
        # 日线行字典缓存（code -> row dict，懒构建一次，避免逐行 df.loc / 重复 to_dict）
        self._daily_rows: dict[str, dict[str, dict]] = {}
        # 基本表字典缓存（code -> row，懒构建一次）
        self._basic_dict: dict[str, dict] | None = None
        # is_st 映射缓存（code -> 0/1，懒构建一次）
        self._st_cache: dict[str, dict[str, int]] = {}
        # --- L2 集合竞价表（可选增强：优先当前工作目录 ./data/l2_auction.parquet，回退包旁 data/） ---
        self.l2_auction: dict[tuple[str, str], tuple[float, float]] = {}
        l2_path = next(
            (p for p in (Path.cwd() / "data" / "l2_auction.parquet", Path(__file__).resolve().parent / "data" / "l2_auction.parquet") if p.exists()),
            None,
        )
        if l2_path is not None:
            try:
                l2 = pl.read_parquet(l2_path, columns=["date", "code", "hq_px", "business_amount"])
                for row in l2.iter_rows():
                    self.l2_auction[(str(row[0]).replace("-", ""), str(row[1]))] = (
                        float(row[2]), float(row[3]),
                    )
                logger.info(f"L2 竞价表已加载：{len(self.l2_auction)} 条")
            except Exception as exc:
                logger.warning(f"L2 竞价表加载失败：{exc}")

    # ---------- 路径与日历 ----------
    def _minute_path(self, ds: str) -> Path:
        return (
            self.dir
            / f"ashare_1m_stock/year={ds[:4]}/month={ds[4:6]}/day={ds[6:]}/data.parquet"
        )

    def _daily_path(self, ds: str) -> Path:
        return (
            self.dir
            / f"ashare_1d_stock/year={ds[:4]}/month={ds[4:6]}/day={ds[6:]}/data.parquet"
        )

    def day_index(self, ds: str) -> int:
        return self._day_pos[ds]

    def prev_day(self, ds: str) -> str | None:
        i = self.day_index(ds)
        return self.trade_days[i - 1] if i > 0 else None

    def days_upto(self, ds: str, count: int) -> list[str]:
        i = self.day_index(ds)
        return self.trade_days[max(0, i - count + 1) : i + 1]

    def days_between(self, a: str, b: str) -> list[str]:
        ia = bisect_left(self.trade_days, a)
        ib = bisect_right(self.trade_days, b)
        return self.trade_days[ia:ib]

    # ---------- 分钟数据 ----------
    def minute_day(self, ds: str) -> DayMinuteData | None:
        """获取某日分钟数据（缓存未命中则加载）。"""
        md = self._minute_cache.get(ds)
        if md is not None or ds in self._minute_cache:
            self._minute_cache.move_to_end(ds)
            return md
        md = _load_minute_day(self._minute_path(ds))
        self._minute_cache[ds] = md
        if self.preload_mode != "all":
            while len(self._minute_cache) > self.rolling_window:
                self._minute_cache.popitem(last=False)
        return md

    def preload(self, progress: bool = True) -> None:
        """预热整载：并行预读区间内全部分钟日文件。"""
        days = self.range_days
        logger.info(
            f"开始预热加载 {len(days)} 个交易日的分钟数据（threads={self.threads}）..."
        )
        t0 = datetime.now()

        def _work(ds):
            return ds, _load_minute_day(self._minute_path(ds))

        with ThreadPoolExecutor(max_workers=self.threads) as ex:
            futs = [ex.submit(_work, ds) for ds in days]
            it = as_completed(futs)
            if progress:
                it = tqdm(it, total=len(futs), desc="预热分钟数据", unit="天")
            for fut in it:
                ds, md = fut.result()
                self._minute_cache[ds] = md
        # 保持区间顺序
        self._minute_cache = OrderedDict((d, self._minute_cache[d]) for d in days)
        secs = (datetime.now() - t0).total_seconds()
        logger.info(
            f"预热完成：{len(days)} 天，耗时 {secs:.1f}s，估算内存 {len(days) * 40:.0f}MB"
        )

    # ---------- 日线数据 ----------
    def ensure_daily(self, ds: str) -> pd.DataFrame | None:
        """某日全市场日线（索引为 PTrade 代码）。"""
        if ds in self._daily_cache:
            return self._daily_cache[ds]
        df = None
        if self._daily_path(ds).exists():
            d = pl.read_parquet(
                self._daily_path(ds),
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
            d = d.with_columns(pl.col("code").str.replace(".SH", ".SS", literal=True))
            df = d.to_pandas().set_index("code")
        self._daily_cache[ds] = df
        return df

    def daily_rows(self, ds: str) -> dict[str, dict] | None:
        """某日全市场日线 -> {code: row_dict}，懒构建一次并缓存（替代逐行 df.loc）。"""
        cached = self._daily_rows.get(ds)
        if cached is not None:
            return cached
        df = self.ensure_daily(ds)
        if df is None:
            return None
        rows = df.to_dict("index")
        self._daily_rows[ds] = rows
        return rows

    def basic_dict(self) -> dict[str, dict]:
        """基本表 -> {code: row_dict}，懒构建一次（替代逐行 basic.loc）。"""
        if self._basic_dict is None:
            self._basic_dict = (
                self.basic.to_dict("index") if len(self.basic) else {}
            )
        return self._basic_dict

    def is_st_map(self, ds: str) -> dict[str, int]:
        """某日 {code: is_st}，懒构建一次并缓存（替代 Series.items 逐项迭代）。"""
        cached = self._st_cache.get(ds)
        if cached is not None:
            return cached
        df = self.ensure_daily(ds)
        if df is None or "is_st" not in df.columns:
            self._st_cache[ds] = {}
            return {}
        self._st_cache[ds] = df["is_st"].astype(int).to_dict()
        return self._st_cache[ds]

    def _feature_path(self, ds: str) -> Path:
        return (
            self.dir
            / f"ashare_1d_feature/year={ds[:4]}/month={ds[4:6]}/day={ds[6:]}/data.parquet"
        )

    def ensure_feature(self, ds: str) -> pd.DataFrame | None:
        """某日全市场估值/股本数据（ashare_1d_feature，索引为 PTrade 代码）。"""
        if ds in self._feature_cache:
            return self._feature_cache[ds]
        df = None
        if self._feature_path(ds).exists():
            d = pl.read_parquet(
                self._feature_path(ds),
                columns=["code", "total_mv", "circ_mv", "float_share", "total_share"],
            )
            d = d.with_columns(pl.col("code").str.replace(".SH", ".SS", literal=True))
            df = d.to_pandas().set_index("code")
        self._feature_cache[ds] = df
        return df

    def valuation_frame(self, codes: list[str], ds: str) -> pd.DataFrame:
        """估值数据（get_fundamentals('valuation')）：index=code, columns=[total_value, float_value]（单位：元）。"""
        feat = self.ensure_feature(ds)
        if feat is None:
            return pd.DataFrame(columns=["total_value", "float_value"])
        sub = feat.reindex(codes)
        mask = sub["total_mv"].notna()
        if not mask.any():
            return pd.DataFrame(columns=["total_value", "float_value"])
        out = sub.loc[mask, ["total_mv", "circ_mv"]].astype(float)
        out.columns = ["total_value", "float_value"]
        return out

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
        return float(row["adj_factor"]) if row else None

    def benchmark_close(self, code: str, ds: str) -> float | None:
        """基准指数日线收盘（code 为 PTrade 代码，index_daily 键已统一为 PTrade 代码）。"""
        df = self.index_daily.get(code)
        if df is None or ds not in df.index:
            return None
        return float(df.loc[ds, "close"])

    # ---------- 证券信息 ----------
    def stock_name(self, code: str, cur_day: str) -> str | None:
        """按回测日生效的证券名称：更名历史优先，回退当日日线 name，再回退基本表。"""
        cur = f"{cur_day[:4]}-{cur_day[4:6]}-{cur_day[6:]}"
        for chg, nm in reversed(self.name_changes.get(code, [])):
            if chg <= cur:
                return nm
        row = self.daily_row(cur_day, code)
        if row and row.get("name"):
            return str(row["name"])
        if code in self.basic.index:
            return str(self.basic.loc[code, "name"])
        return None

    def get_Ashares(self, cur_day: str) -> list[str]:
        """指定日主板 A 股列表（缓存）。"""
        if cur_day in self._ashares_cache:
            return self._ashares_cache[cur_day]
        cur = f"{cur_day[:4]}-{cur_day[4:6]}-{cur_day[6:]}"
        b = self.basic
        mask = (
            (b["market"] == "主板")
            & (b["list_status"] == "L")
            & (b["list_date"].notna())
        )
        mask &= b["list_date"].str[:4].str.isnumeric()  # 防脏数据
        listed = b[mask]
        result = []
        for code, row in listed.iterrows():
            p3 = code[:3]
            if not (
                (code.endswith(".SZ") and p3 in _MAINBOARD_PREFIX)
                or (code.endswith(".SS") and p3 in _MAINBOARD_PREFIX_SH)
            ):
                continue
            if str(row["list_date"]) > cur:
                continue
            dl = row["delist_date"]
            if pd.notna(dl) and str(dl) <= cur:
                continue
            result.append(code)
        result.sort()
        self._ashares_cache[cur_day] = result
        return result


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
        # 全市场日线 get_price 批缓存（同参数重复调用命中，结果隔离副本）
        self._get_price_cache: dict[tuple, pd.DataFrame] = {}
        # 按交易日重置的证券信息缓存（get_stock_name/get_stock_info）
        self._name_cache: dict[tuple[str, str], str | None] = {}
        self._info_cache: dict[tuple, dict] = {}

        preload = config.get("preload", {})
        self.feed = DataFeed(
            config["data_dir"],
            config["start_date"].replace("-", ""),
            config["end_date"].replace("-", ""),
            preload_mode=preload.get("mode", "all"),
            rolling_window=int(preload.get("rolling_window_days", 10)),
            threads=int(preload.get("threads", 8)),
        )

        self.capital_base = float(config.get("capital_base", 1_000_000))
        self.portfolio = Portfolio(self.capital_base)
        self.g = SimpleNamespace()
        self.blotter = SimpleNamespace(current_dt=None)
        self.context = self._make_context()

        self.universe: list[str] = []
        self.schedule: dict[str, list] = {}  # 'HH:MM' -> [func]
        self.orders: dict[str, Order] = {}
        self.trades: list[Trade] = []
        self._order_seq = 0
        self._day_str: str = ""
        self._slot_pos = -1  # 当前槽位（-1 = 盘前）
        self._initialized = False
        self._failed_funcs: set[str] = set()
        self.daily_stats: list[dict] = []
        self._callbacks = {}

        # 日志双写
        logger.remove()
        logger.add(
            lambda m: print(m, end=""),
            level="INFO",
            format="{time:HH:mm:ss} {level} {message}",
        )
        logger.add(
            self.output_dir / "output.log",
            level="INFO",
            format="{time:YYYY-MM-DD HH:mm:ss} {level} {message}",
            encoding="utf-8",
        )

    # ---------- context ----------
    def _make_context(self) -> SimpleNamespace:
        return SimpleNamespace(
            capital_base=self.capital_base,
            portfolio=self.portfolio,
            blotter=self.blotter,
            sim_params=SimpleNamespace(
                capital_base=self.capital_base, data_frequency="minute"
            ),
            slippage=SimpleNamespace(),
            commission=SimpleNamespace(),
            recorded_vars={},
            initialized=False,
            previous_date=None,
        )

    # ---------- 策略加载 ----------
    def load_strategy(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "ptrade_strategy", self.strategy_path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # 注入 API 到策略模块全局命名空间
        api = self._build_api()
        for name, obj in api.items():
            setattr(module, name, obj)
        self._module = module
        if not hasattr(module, "initialize"):
            raise ValueError("策略缺少必选函数 initialize(context)")
        module.initialize(self.context)
        self.context.initialized = True
        self._initialized = True
        if not hasattr(module, "handle_data"):
            logger.warning("策略未定义 handle_data（官方必选），引擎将跳过每 bar 调用")
        logger.info(f"策略加载完成：{self.strategy_path}")

    # ---------- API 构造 ----------
    def _build_api(self) -> dict:
        e = self
        log = SimpleNamespace(
            info=lambda msg, *a: logger.info(e._fmt_log(msg, a)),
            warn=lambda msg, *a: logger.warning(e._fmt_log(msg, a)),
            warning=lambda msg, *a: logger.warning(e._fmt_log(msg, a)),
            error=lambda msg, *a: logger.error(e._fmt_log(msg, a)),
            debug=lambda msg, *a: logger.debug(e._fmt_log(msg, a)),
        )

        def set_universe(universe):
            e.universe = [
                to_ptrade_code(u)
                for u in (
                    universe if isinstance(universe, (list, tuple, set)) else [universe]
                )
            ]

        def set_benchmark(security):
            e.benchmark = to_ptrade_code(security)

        def set_commission(commission_ratio=0.0003, min_commission=5.0, type="STOCK"):
            e.commission_ratio = float(commission_ratio)
            e.min_commission = float(min_commission)

        def set_slippage(slippage=0.001):
            e.slippage_ratio = float(slippage)
            e.fixed_slippage = None

        def set_fixed_slippage(fixed_slippage=0.0):
            e.fixed_slippage = float(fixed_slippage)

        def set_limit_mode(mode="LIMITED"):
            # LIMITED：限制涨跌停成交（拒一字板）；UNLIMITED：不限制
            e._limit_mode = str(mode).upper()

        def run_daily(context, func, time="9:31"):
            t = str(time).strip()
            hh, mm = t.split(":")[:2]
            key = f"{int(hh):02d}:{int(mm):02d}"
            if key == "13:00":  # 官方：13:00 触发对应下午开盘
                key = "13:01"
            e.schedule.setdefault(key, []).append(func)

        def order(security, amount, limit_price=None):
            return e._order(to_ptrade_code(security), int(amount))

        def order_value(security, value):
            return e._order_by_value(to_ptrade_code(security), float(value))

        def order_target(security, amount):
            return e._order_target(to_ptrade_code(security), int(amount))

        def order_target_value(security, value):
            return e._order_by_target_value(to_ptrade_code(security), float(value))

        def get_history(
            count,
            frequency="1d",
            field="close",
            security_list=None,
            fq=None,
            include=False,
            fill="nan",
            is_dict=False,
        ):
            return e._get_history(
                int(count), frequency, field, security_list, fq, include, is_dict
            )

        def get_price(
            security,
            start_date=None,
            end_date=None,
            frequency="1d",
            fields=None,
            fq=None,
            count=None,
            is_dict=False,
        ):
            return e._get_price(
                security, start_date, end_date, frequency, fields, fq, count, is_dict
            )

        def get_trend_data(date=None, stocks=None):
            return e._get_trend_data(stocks)

        def get_stock_name(stocks):
            codes = [
                to_ptrade_code(s)
                for s in (stocks if isinstance(stocks, (list, tuple)) else [stocks])
            ]
            # 官方：始终返回 dict（str 入参也返回 {code: name}）
            out = {}
            for c in codes:
                key = (e._day_str, c)
                nm = e._name_cache.get(key)
                if nm is None and key not in e._name_cache:
                    nm = e.feed.stock_name(c, e._day_str)
                    e._name_cache[key] = nm
                out[c] = nm
            return out

        def get_stock_info(stocks, field=None):
            codes = [
                to_ptrade_code(s)
                for s in (stocks if isinstance(stocks, (list, tuple)) else [stocks])
            ]
            fields = field if (field is None or isinstance(field, list)) else [field]
            out = {}
            for c in codes:
                ck = (e._day_str, c, tuple(fields) if fields is not None else None)
                if ck in e._info_cache:
                    out[c] = e._info_cache[ck]
                    continue
                item = {}
                b = e.feed.basic_dict()
                row = b.get(c)
                if row:
                    ld = row.get("list_date")
                    dd = row.get("delist_date")
                    item["stock_name"] = (
                        None if pd.isna(row.get("name")) else str(row["name"])
                    )
                    item["listed_date"] = (
                        None
                        if pd.isna(ld)
                        else f"{str(ld)[:4]}-{str(ld)[4:6]}-{str(ld)[6:]}"
                    )
                    item["de_listed_date"] = (
                        "2900-01-01"
                        if pd.isna(dd)
                        else f"{str(dd)[:4]}-{str(dd)[4:6]}-{str(dd)[6:]}"
                    )
                else:
                    item = {
                        "stock_name": None,
                        "listed_date": None,
                        "de_listed_date": None,
                    }
                if fields is None:
                    # 官方：field 不入参时默认只返回 stock_name
                    res = {"stock_name": item["stock_name"]}
                else:
                    res = {k: item.get(k) for k in fields}
                e._info_cache[ck] = res
                out[c] = res
            # 官方：始终返回嵌套 dict（str 入参也返回 {code: {...}}）
            return out

        def get_stock_status(stocks, query_type="ST", query_date=None):
            codes = [
                to_ptrade_code(s)
                for s in (stocks if isinstance(stocks, (list, tuple)) else [stocks])
            ]
            if query_type == "DELISTING_SORTING":
                return {}  # 官方：仅交易场景支持当日查询
            ds = e._norm_day(query_date) if query_date else e._day_str
            out = {}
            for c in codes:
                out[c] = e._stock_status_one(c, query_type, ds)
            return out

        def get_Ashares(date=None):
            ds = e._norm_day(date) if date else e._day_str
            return list(e.feed.get_Ashares(ds))

        def get_trade_days(start_date=None, end_date=None, count=None):
            return e._get_trade_days(start_date, end_date, count)

        def get_all_trades_days(date=None):
            ds = e._norm_day(date) if date else e._day_str
            days = e.feed.trade_days[: e.feed.day_index(ds) + 1]
            return np.array([f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in days])

        def get_trading_day(day=0):
            days = e.feed.trade_days
            i = min(max(e.feed.day_index(e._day_str) + int(day), 0), len(days) - 1)
            d = days[i]
            return date(int(d[:4]), int(d[4:6]), int(d[6:]))

        def get_trading_day_by_date(query_date, day=0):
            q = e._norm_day(query_date)
            days = e.feed.trade_days
            i = bisect_left(days, q)  # 非交易日 -> 下一交易日
            i = min(max(i + int(day), 0), len(days) - 1)
            return f"{days[i][:4]}-{days[i][4:6]}-{days[i][6:]}"

        def check_limit(security):
            return e._check_limit(to_ptrade_code(security))

        def filter_stock_by_status(
            stock_list, filter_types=("ST", "HALT", "DELISTING")
        ):
            if isinstance(filter_types, str):
                filter_types = [filter_types]
            st = get_stock_status(stock_list, "ST") if "ST" in filter_types else {}
            halt = (
                get_stock_status(stock_list, "HALT") if "HALT" in filter_types else {}
            )
            de = (
                get_stock_status(stock_list, "DELISTING")
                if "DELISTING" in filter_types
                else {}
            )
            return [
                c for c in stock_list if not (st.get(c) or halt.get(c) or de.get(c))
            ]

        def get_snapshot(security=None):
            return e._get_snapshot(to_ptrade_code(security) if security else None)

        def get_research_path():
            return str(e.output_dir) + "/"

        def get_fundamentals(stocks, statement="valuation", date=None, **kwargs):
            """财务/估值数据。'valuation' 支持市值（ashare_1d_feature），其余报表本地无数据返回空。"""
            ds = (
                e._norm_day(date)
                if date
                else (e.feed.prev_day(e._day_str) or e._day_str)
            )
            codes = [
                to_ptrade_code(s)
                for s in (stocks if isinstance(stocks, (list, tuple)) else [stocks])
            ]
            if statement == "valuation":
                return e.feed.valuation_frame(codes, ds)
            logger.warning(
                f"get_fundamentals：本地无财务表（{statement}），返回空 DataFrame"
            )
            return pd.DataFrame()

        def get_index_stocks(index_code=None):
            if not getattr(e, "_warned_index_stocks", False):
                logger.warning(
                    f"get_index_stocks({index_code})：本地无指数成分数据，返回空列表"
                )
                e._warned_index_stocks = True
            return []

        def get_market_list():
            return pd.DataFrame(
                {
                    "finance_mic": ["SS", "SZ"],
                    "finance_name": ["上海证券交易所", "深圳证券交易所"],
                }
            )

        def get_market_detail(finance_mic):
            return pd.DataFrame(
                columns=["hq_type_code", "prod_code", "prod_name", "trade_time_rule"]
            )

        def cancel_order(order_or_id):
            oid = (
                order_or_id
                if isinstance(order_or_id, str)
                else getattr(order_or_id, "id", None)
            )
            od = e.orders.get(oid)
            if od and od.status in ("filled",):
                return False
            if od:
                od.status = "canceled"
            return True

        def get_open_orders():
            return {
                oid: od
                for oid, od in e.orders.items()
                if od.status not in ("filled", "canceled", "rejected")
            }

        def get_order(order_id):
            return e.orders.get(order_id)

        def get_orders():
            return dict(e.orders)

        def get_trades():
            return {i: t for i, t in enumerate(e.trades)}

        def _noop(*args, **kwargs):
            return None

        return {
            "log": log,
            "g": e.g,
            "context": e.context,
            "set_universe": set_universe,
            "set_benchmark": set_benchmark,
            "set_commission": set_commission,
            "set_slippage": set_slippage,
            "set_fixed_slippage": set_fixed_slippage,
            "set_volume_ratio": _noop,
            "set_limit_mode": set_limit_mode,
            "set_yesterday_position": _noop,
            "set_parameters": _noop,
            "run_daily": run_daily,
            "order": order,
            "order_value": order_value,
            "order_target": order_target,
            "order_target_value": order_target_value,
            "get_history": get_history,
            "get_price": get_price,
            "get_trend_data": get_trend_data,
            "get_stock_name": get_stock_name,
            "get_stock_info": get_stock_info,
            "get_stock_status": get_stock_status,
            "get_Ashares": get_Ashares,
            "get_trade_days": get_trade_days,
            "get_all_trades_days": get_all_trades_days,
            "get_trading_day": get_trading_day,
            "get_trading_day_by_date": get_trading_day_by_date,
            "check_limit": check_limit,
            "filter_stock_by_status": filter_stock_by_status,
            "get_snapshot": get_snapshot,
            "get_research_path": get_research_path,
            "get_fundamentals": get_fundamentals,
            "get_index_stocks": get_index_stocks,
            "get_market_list": get_market_list,
            "get_market_detail": get_market_detail,
            "cancel_order": cancel_order,
            "get_open_orders": get_open_orders,
            "get_order": get_order,
            "get_orders": get_orders,
            "get_trades": get_trades,
        }

    @staticmethod
    def _fmt_log(msg, args) -> str:
        return msg if not args else f"{msg} {list(args)}"

    # ---------- 工具 ----------
    def _norm_day(self, d) -> str:
        """'2025-01-02'/'20250102'/date/datetime -> 'YYYYMMDD'"""
        if isinstance(d, (datetime, date)):
            return d.strftime("%Y%m%d")
        s = str(d).replace("-", "").replace(" ", "")[:8]
        return s

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
            cur = f"{ds[:4]}-{ds[4:6]}-{ds[6:]}"
            return (row.get("list_status") == "D") or (pd.notna(dd) and str(dd) <= cur)
        return None

    def _check_limit(self, code: str) -> bool:
        """当前 bar 是否处于涨跌停价格。"""
        bar = self._bar_now(code)
        if bar is None or self._slot_pos < 0:
            return False
        daily = self.feed.daily_row(self._day_str, code)
        if daily is None:
            return False
        pct = _limit_pct(daily["is_st"], code, self._day_str)
        up = _limit_price(daily["pre_close"], pct)
        down = _limit_price(daily["pre_close"], -pct)
        return bar.close >= up or bar.close <= down

    def _bar_now(self, code: str):
        """当前槽位 bar 元组 (o,h,l,c,vol,amount)，盘前取 09:30 竞价 bar。"""
        md = self.feed.minute_day(self._day_str)
        if md is None:
            return None
        slot = self._slot_pos if self._slot_pos >= 0 else 0
        r = md.row_of(code, slot)
        if r < 0:
            return None
        return md.open[r], md.high[r], md.low[r], md.close[r], md.vol[r], md.amount[r]

    def _match_price(self, code: str) -> float | None:
        """撮合价：
        - 盘前任务（09:30 前，_slot_pos<0）：PTrade 09:26 市价单在 09:31 第一根完整分钟 bar
          收盘时撮合 → 用 09:31 bar 的 **close**（实证：000065 11.53→PTrade成本11.533、000070 11.00→11.003 完全吻合）
        - 盘中：用当前槽位 bar close
        """
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
            return base_price + (
                self.fixed_slippage / 2 if is_buy else -self.fixed_slippage / 2
            )
        return base_price * (
            1 + self.slippage_ratio / 2 if is_buy else 1 - self.slippage_ratio / 2
        )

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
        md = self.feed.minute_day(self._day_str)
        slot = self._slot_pos if self._slot_pos >= 0 else 0
        if md.has_volume(code, slot) is False:
            self._reject(code, amount, "盘中无成交（停牌或零成交）")
            return None
        # 一字板拒单（仅 LIMITED 模式；set_limit_mode("UNLIMITED") 不限制）
        pct = _limit_pct(daily["is_st"], code, self._day_str)
        up = _limit_price(daily["pre_close"], pct)
        down = _limit_price(daily["pre_close"], -pct)
        r = md.row_of(code, slot)
        if (
            self._limit_mode != "UNLIMITED"
            and md.high[r] == md.low[r]
            and (md.close[r] >= up or md.close[r] <= down)
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
                (pos.avg_cost * pos.total_amount + turnover) / new_total
                if new_total
                else 0.0
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
            result = self._history_1d(codes, int(count), fields, fq, include, single)
        elif freq in ("1m", "5m", "15m", "30m", "60m", "120m"):
            result = self._history_1m(
                codes, int(count), freq, fields, fq, include, single
            )
        else:
            logger.warning(f"get_history 暂不支持频率 {frequency}，返回空")
            return {} if is_dict else pd.DataFrame()
        if is_dict:
            return {c: self._to_struct(result, c, freq) for c in codes}
        return result

    def _to_struct(self, df: pd.DataFrame, code: str, freq: str):
        """is_dict=True 的返回：numpy 结构化数组（官方格式）。"""
        if "code" in df.columns:
            sub = df[df["code"] == code].drop(columns=["code"])
        else:
            sub = df
        n = len(sub)
        arr = np.zeros(
            n,
            dtype=[
                ("datetime", "i8"),
                ("open", "f8"),
                ("high", "f8"),
                ("low", "f8"),
                ("close", "f8"),
                ("volume", "f8"),
                ("money", "f8"),
                ("price", "f8"),
            ],
        )
        if n == 0:
            return arr
        idx = sub.index
        if freq == "1d":
            arr["datetime"] = np.array([int(ts.strftime("%Y%m%d")) for ts in idx])
        else:
            arr["datetime"] = np.array([int(ts.strftime("%Y%m%d%H%M")) for ts in idx])
        for f in ("open", "high", "low", "close"):
            if f in sub.columns:
                arr[f] = sub[f].to_numpy()
        if "volume" in sub.columns:
            arr["volume"] = sub["volume"].to_numpy()
        if "money" in sub.columns:
            arr["money"] = sub["money"].to_numpy()
        arr["price"] = arr["close"]
        return arr

    def _history_1d(
        self,
        codes: list[str],
        count: int,
        fields: list[str],
        fq: str | None,
        include: bool,
        single: bool,
    ) -> pd.DataFrame:
        """日线历史：默认不含当日；include=True 时当日用分钟数据合成（无未来数据）。
        官方语义：仅入参为 str 时按单股票返回（index=fields）；list 即使只有一只也按多股票返回（[code, field]）。"""
        days = self.feed.trade_days
        cur_i = self.feed.day_index(self._day_str)
        end_i = cur_i + 1 if include else cur_i
        sel_days = days[max(0, end_i - count) : end_i]
        records = []
        last_close: dict[str, float] = {}
        for ds in sel_days:
            rows = self.feed.daily_rows(ds)
            for code in codes:
                if ds == self._day_str and include:
                    # 当日：从分钟数据合成到当前槽位（避免日线全量数据的未来函数）
                    rec = self._today_partial_row(code)
                    if rec is not None:
                        records.append(rec)
                        last_close[code] = rec[5]
                        continue
                row = rows.get(code) if rows else None
                if row is not None:
                    last_close[code] = float(row["close"])
                    records.append(
                        (
                            ds,
                            code,
                            float(row["open"]),
                            float(row["high"]),
                            float(row["low"]),
                            float(row["close"]),
                            float(row["vol"]),
                            float(row["amount"]),
                            float(row["pre_close"]),
                        )
                    )
                else:  # 停牌：前收盘填充，量 0
                    pc = last_close.get(code, float("nan"))
                    records.append((ds, code, pc, pc, pc, pc, 0.0, 0.0, pc))
        return self._assemble_daily(records, codes, fields, fq, single)

    def _today_partial_row(self, code: str) -> tuple | None:
        """当日到当前槽位的分钟合成 OHLC（include=True 时替代日线全量，防未来函数）。"""
        md = self.feed.minute_day(self._day_str)
        daily = self.feed.daily_row(self._day_str, code)
        pre_close = float(daily["pre_close"]) if daily else float("nan")
        if md is None:
            return None
        s = md.rows_upto(code, self._slot_pos, include=True)
        if s.stop - s.start == 0:
            return (
                self._day_str,
                code,
                pre_close,
                pre_close,
                pre_close,
                pre_close,
                0.0,
                0.0,
                pre_close,
            )
        o = md.open[s]
        h = float(np.max(md.high[s]))
        low_v = float(np.min(md.low[s]))
        c = float(md.close[s.stop - 1])
        v = float(np.sum(md.vol[s]))
        a = float(np.sum(md.amount[s]))
        return (self._day_str, code, float(o), h, low_v, c, v, a, pre_close)

    def _assemble_daily(
        self, records, codes: list[str], fields: list[str], fq: str | None, single: bool
    ) -> pd.DataFrame:
        """长表记录 -> 官方返回格式。单股票(str入参)：index=时间(名'index'), columns=fields；
        多股票(list入参，即使只含一只)：columns=[code, field]。"""
        df = pd.DataFrame(
            records,
            columns=[
                "day",
                "code",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "money",
                "pre_close",
            ],
        )
        df["price"] = df["close"]
        df["is_open"] = (df["volume"] > 0).astype(int)
        # 价格统一 round 到 2 位（分精度）：消除 float32 存储噪声，
        # 避免 14.9499998 < 14.95 这类误判（PTrade 行情价为精确 2 位小数）
        for col in ("open", "high", "low", "close", "pre_close"):
            df[col] = df[col].round(2)
        df["price"] = df["close"]
        # 涨停/跌停价：ST ±5%，非 ST ±10%，Decimal 四舍五入（交易所规则）
        # 批量取 is_st（缓存 dict，一次构建，避免逐行 daily_row / Series.items 迭代）
        st_map: dict[str, int] = {}
        for ds in df["day"].unique():
            st_map.update(self.feed.is_st_map(ds))
        st = [st_map.get(code, 0) for code in df["code"]]
        df["high_limit"] = [_limit_price(pc, _limit_pct(s, c, d)) for pc, s, c, d in zip(df["pre_close"], st, df["code"], df["day"])]
        df["low_limit"] = [_limit_price(pc, -_limit_pct(s, c, d)) for pc, s, c, d in zip(df["pre_close"], st, df["code"], df["day"])]
        df["unlimited"] = 0
        if fq in ("pre", "dypre", "post"):
            df = self._apply_fq_daily(df, codes, fq)
        want = [f for f in fields if f in df.columns]
        df = df[["day", "code"] + want]
        df.index = pd.Index(pd.to_datetime(df["day"], format="%Y%m%d"), name="index")
        if single:
            return df[want]
        return df[["code"] + want]

    def _apply_fq_daily(
        self, df: pd.DataFrame, codes: list[str], fq: str
    ) -> pd.DataFrame:
        """复权：pre/dypre 以当前回测日因子为基准，post 乘以当日因子。"""
        base_factor = {}
        for code in codes:
            f = self.feed.adj_factor(self._day_str, code)
            base_factor[code] = f if f else 1.0
        factors = []
        for ds, code in zip(df["day"], df["code"]):
            f = self.feed.adj_factor(ds, code)
            factors.append(f if f else 1.0)
        f = np.array(factors)
        if fq in ("pre", "dypre"):
            base = np.array([base_factor.get(c, 1.0) for c in df["code"]])
            ratio = f / base
        else:  # post
            ratio = f
        df = df.copy()
        for col in ("open", "high", "low", "close", "pre_close"):
            if col in df.columns:
                df[col] = df[col] * ratio
        return df

    def _history_1m(
        self,
        codes: list[str],
        count: int,
        freq: str,
        fields: list[str],
        fq: str | None,
        include: bool,
        single: bool,
    ) -> pd.DataFrame:
        """分钟历史：从当前槽位向前取 count 根（跨日），官方停牌填充语义。
        单股票(str入参)：index=时间(名'index'), columns=fields；多股票(list入参，即使只含一只)：columns=[code, field]。"""
        days = self.feed.trade_days
        cur_day_i = self.feed.day_index(self._day_str)
        # 时间轴：(day, slot)，从当前槽位向前（include=False 不含当前槽）
        axis: list[tuple[str, int]] = []
        start_slot = self._slot_pos + (1 if include else 0) - 1
        d_i = cur_day_i
        while len(axis) < count and d_i >= 0:
            k = start_slot
            while k >= 0 and len(axis) < count:
                axis.append((days[d_i], k))
                k -= 1
            d_i -= 1
            start_slot = len(DAY_SLOTS) - 1
        axis.reverse()
        records = []
        last_close: dict[str, float] = {c: float("nan") for c in codes}
        for ds, slot in axis:
            md = self.feed.minute_day(ds)
            ts = f"{ds[:4]}-{ds[4:6]}-{ds[6:]} {DAY_SLOTS[slot]}:00"
            for code in codes:
                r = md.row_of(code, slot) if md else -1
                if r >= 0:
                    o, h, low_v, c = md.open[r], md.high[r], md.low[r], md.close[r]
                    v, a = md.vol[r], md.amount[r]
                    last_close[code] = float(c)
                else:  # 停牌填充：前收盘价，量 0
                    o = h = low_v = c = last_close.get(code, float("nan"))
                    v = a = 0.0
                records.append(
                    (
                        ts,
                        code,
                        float(o),
                        float(h),
                        float(low_v),
                        float(c),
                        float(v),
                        float(a),
                    )
                )
        df = pd.DataFrame(
            records,
            columns=["ts", "code", "open", "high", "low", "close", "volume", "money"],
        )
        # 价格统一 round 到 2 位（分精度），消除 float32 噪声比较误判
        for col in ("open", "high", "low", "close"):
            df[col] = df[col].round(2)
        df["price"] = df["close"]
        # 复权先于重采样（按日关联因子）
        if fq in ("pre", "dypre", "post"):
            df["day"] = df["ts"].str[:10].str.replace("-", "")
            df = self._apply_fq_daily(df, codes, fq)
        if freq != "1m":
            df = self._resample_1m(df, freq)
        df.index = pd.Index(pd.to_datetime(df["ts"]), name="index")
        df = df.drop(columns=["ts"])
        want = [f for f in fields if f in df.columns]
        if single:
            return df[want]
        return df[["code"] + want]

    def _resample_1m(self, df: pd.DataFrame, freq: str) -> pd.DataFrame:
        """N 分钟重采样（label/closed=right，与结束时间标注语义一致）。"""
        n = int(freq[:-2])
        g = df.set_index(pd.to_datetime(df["ts"])).groupby("code")
        agg = (
            g.resample(f"{n}min", label="right", closed="right")
            .agg(
                open=("open", "first"),
                high=("high", "max"),
                low=("low", "min"),
                close=("close", "last"),
                volume=("volume", "sum"),
                money=("money", "sum"),
            )
            .dropna(subset=["open"])
            .reset_index()
        )
        agg["price"] = agg["close"]
        return agg.rename(columns={"time": "ts"})

    def _get_price(
        self, security, start_date, end_date, frequency, fields, fq, count, is_dict
    ) -> pd.DataFrame | dict:
        single = isinstance(security, str)
        codes = [
            to_ptrade_code(s)
            for s in (security if isinstance(security, (list, tuple)) else [security])
        ]
        freq = frequency.lower()
        if fields:
            pass
        elif freq == "1d":
            # 日线默认输出含涨跌停价（现有策略依赖 high_limit）
            fields = [
                "open",
                "high",
                "low",
                "close",
                "volume",
                "money",
                "price",
                "preclose",
                "high_limit",
                "low_limit",
            ]
        else:
            fields = ["open", "high", "low", "close", "volume", "money", "price"]
        if isinstance(fields, str):
            fields = [fields]
        if freq == "1d":
            # start_date 与 count 二选一；官方"返回内容不包括当天数据"：
            # end_date 上限 = 上一交易日（显式传当天也会被截断到昨天，避免未来函数）
            days = self.feed.trade_days
            end = (
                self._norm_day(end_date)
                if end_date
                else (self.feed.prev_day(self._day_str) or self._day_str)
            )
            limit = self.feed.prev_day(self._day_str) or self._day_str
            if end > limit:
                end = limit
            if start_date and count:
                logger.warning("get_price：start_date 与 count 只能二选一，忽略 count")
                count = None
            if start_date:
                sel = self.feed.days_between(self._norm_day(start_date), end)
            else:
                c = int(count or 1)
                i = bisect_right(days, end)
                sel = days[max(0, i - c) : i]
            records = []
            last_close = {}
            for ds in sel:
                rows = self.feed.daily_rows(ds)
                for code in codes:
                    row = rows.get(code) if rows else None
                    if row is not None:
                        last_close[code] = float(row["close"])
                        records.append(
                            (
                                ds,
                                code,
                                float(row["open"]),
                                float(row["high"]),
                                float(row["low"]),
                                float(row["close"]),
                                float(row["vol"]),
                                float(row["amount"]),
                                float(row["pre_close"]),
                            )
                        )
                    else:
                        pc = last_close.get(code, float("nan"))
                        records.append((ds, code, pc, pc, pc, pc, 0.0, 0.0, pc))
            result = self._assemble_daily(records, codes, fields, fq, single)
            # 全市场日线批缓存：同一 (回测日, 区间, 股票集, 字段) 的重复调用直接命中。
            # key 含 codes 原顺序（策略可能依赖候选顺序），命中返回副本避免污染。
            if not single and len(codes) > 500:
                ck = (
                    self._day_str,
                    tuple(sel),
                    tuple(codes),
                    tuple(fields),
                    fq,
                )
                hit = self._get_price_cache.get(ck)
                if hit is None:
                    # 存副本：缓存对象必须与返回对象隔离（策略可能 in-place 修改返回值）
                    self._get_price_cache[ck] = result.copy()
                else:
                    result = hit.copy()
        else:
            # 分钟频率：count 相对当前时刻向前
            result = self._history_1m(
                codes,
                int(count or 1),
                freq if freq.endswith("m") else "1m",
                fields,
                fq,
                include=False,
                single=single,
            )
            # _history_1m 以当前槽位为基准；get_price 不含当前，符合官方
        if is_dict:
            return {c: self._to_struct(result, c, freq) for c in codes}
        return result

    def _get_trade_days(self, start_date, end_date, count) -> np.ndarray:
        days = self.feed.trade_days
        if start_date and count:
            logger.warning("get_trade_days：start_date 与 count 二选一，忽略 count")
            count = None
        end = self._norm_day(end_date) if end_date else self._day_str
        if start_date:
            sel = self.feed.days_between(self._norm_day(start_date), end)
        else:
            c = int(count or 1)
            i = bisect_right(days, end)
            sel = days[max(0, i - c) : i]
        return np.array([f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in sel])

    def _get_trend_data(self, stocks) -> dict:
        """集中竞价数据：优先取 L2 竞价表（9:25 正式撮合的纯竞价量/价，精确）；
        无记录时回退 09:30 bar（hq_px=open, business_amount=vol 近似）。"""
        md = self.feed.minute_day(self._day_str)
        out = {}
        for code in stocks:
            code = to_ptrade_code(code)
            snap = self.feed.l2_auction.get((self._day_str, code))
            if snap is not None:
                out[code] = {
                    "hq_px": snap[0],
                    "business_amount": snap[1],
                    "money": snap[1] * snap[0],
                }
                continue
            if md is None:
                continue
            r = md.row_of(code, 0)
            if r >= 0:
                out[code] = {
                    "hq_px": float(md.open[r]),
                    "business_amount": float(md.vol[r]),
                    "money": float(md.amount[r]),
                }
        return out

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
        for code in list(pf.positions):
            pos = pf.positions[code]
            f_today = self.feed.adj_factor(self._day_str, code)
            f_prev = (
                self.feed.adj_factor(self.feed.prev_day(self._day_str), code)
                if self.feed.prev_day(self._day_str)
                else None
            )
            if f_today and f_prev and not math.isclose(f_today, f_prev, rel_tol=1e-9):
                ratio = f_today / f_prev
                new_amount = round(pos.total_amount * ratio)
                # 现金分红：昨收 × ratio - 今日 pre_close（反推每股分红）
                prev_ds = self.feed.prev_day(self._day_str)
                y_close = self.feed.daily_close(prev_ds, code) if prev_ds else None
                today_row = self.feed.daily_row(self._day_str, code)
                if y_close and today_row:
                    dividend = y_close * ratio - float(today_row["pre_close"])
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
        # 每日重置证券信息缓存（缓存仅当日有效）
        self._name_cache.clear()
        self._info_cache.clear()
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
                self._call_strategy(
                    f"run_daily[{t}]{fn.__name__}", lambda fn=fn: fn(self.context)
                )

        # 分钟循环：241 槽
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
        md = self.feed.minute_day(self._day_str)
        day_dt = self.blotter.current_dt
        out = {}
        for code in codes:
            bar = None
            if md:
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
        prev_total = (
            self.daily_stats[-1]["total_value"]
            if self.daily_stats
            else self.capital_base
        )
        cum = total / self.capital_base - 1
        peak = max(
            [s["total_value"] for s in self.daily_stats], default=self.capital_base
        )
        peak = max(peak, total)
        self.daily_stats.append(
            {
                "date": f"{ds[:4]}-{ds[4:6]}-{ds[6:]}",
                "total_value": total,
                "cash": pf.cash,
                "positions_value": pf.positions_value,
                "benchmark_close": bm,
                "daily_return": total / prev_total - 1,
                "cum_return": cum,
                "drawdown": total / peak - 1,
                "trades_count": sum(
                    1 for t in self.trades if t.time.date() == day_dt_date(ds)
                ),
                "commission": sum(
                    t.commission
                    for t in self.trades
                    if t.time.date() == day_dt_date(ds)
                ),
            }
        )

    # ---------- 主入口 ----------
    def run(self) -> pd.DataFrame:
        t0 = datetime.now()
        if self.feed.preload_mode == "all":
            self.feed.preload()
        logger.info(
            f"回测区间：{self.config['start_date']} ~ {self.config['end_date']}，"
            f"初始资金 {self.capital_base:,.0f}，基准 {self.benchmark}"
        )
        self.load_strategy()
        self.portfolio.start_date = self.config["start_date"]
        for ds in self.feed.range_days:
            self._run_day(ds)
        logger.info(
            f"回测完成，耗时 {(datetime.now() - t0).total_seconds():.1f}s，"
            f"期末资产 {self.portfolio.total_value:,.2f}"
        )
        return self.daily_stats_frame()

    def daily_stats_frame(self) -> pd.DataFrame:
        return pd.DataFrame(self.daily_stats)

    def trades_frame(self) -> pd.DataFrame:
        return pd.DataFrame([t.__dict__ for t in self.trades])


def day_dt_date(ds: str) -> date:
    return date(int(ds[:4]), int(ds[4:6]), int(ds[6:]))


# ============================================================
# 绩效指标与报告
# ============================================================


def compute_metrics(
    daily: pd.DataFrame, trades: pd.DataFrame, capital_base: float, config: dict
) -> dict:
    """计算 11 项核心指标 + 年度/月度收益分布。"""
    total_value = daily["total_value"]
    n_days = len(daily)
    final_value = float(total_value.iloc[-1]) if n_days else capital_base
    total_return = final_value / capital_base - 1
    annual_return = (1 + total_return) ** (252 / max(n_days, 1)) - 1 if n_days else 0.0
    rets = daily["daily_return"].astype(float)
    std = rets.std()
    sharpe = float(rets.mean() / std * np.sqrt(252)) if std and std > 0 else 0.0
    mdd = float(daily["drawdown"].min()) if n_days else 0.0
    calmar = annual_return / abs(mdd) if mdd < 0 else 0.0

    # 交易统计：胜率/盈亏比按卖出笔的盈亏
    if len(trades):
        sells = trades[trades["side"] == "sell"]
        wins = sells[sells["trade_pnl"] > 0]
        losses = sells[sells["trade_pnl"] <= 0]
        win_rate = len(wins) / len(sells) if len(sells) else 0.0
        avg_win = float(wins["trade_pnl"].mean()) if len(wins) else 0.0
        avg_loss = abs(float(losses["trade_pnl"].mean())) if len(losses) else 0.0
        pl_ratio = (
            avg_win / avg_loss if avg_loss > 0 else float("inf") if avg_win > 0 else 0.0
        )
        total_commission = float(trades["commission"].sum())
        n_trades = int(len(trades))
    else:
        win_rate, pl_ratio, total_commission, n_trades = 0.0, 0.0, 0.0, 0

    # 基准
    bm = daily["benchmark_close"].astype(float)
    bm_total = (
        float(bm.iloc[-1] / bm.iloc[0] - 1)
        if n_days and bm.notna().all()
        else float("nan")
    )

    # 年度/月度收益
    d = daily.copy()
    d["date"] = pd.to_datetime(d["date"])
    d["year"] = d["date"].dt.year
    d["ym"] = d["date"].dt.strftime("%Y-%m")
    annual_returns: dict[str, dict] = {}
    for y, g in d.groupby("year"):
        bmg = g["benchmark_close"].astype(float)
        annual_returns[str(y)] = {
            "strategy": float((1 + g["daily_return"].astype(float)).prod() - 1),
            "benchmark": float(bmg.iloc[-1] / bmg.iloc[0] - 1)
            if bmg.notna().all()
            else float("nan"),
        }
        annual_returns[str(y)]["excess"] = (
            annual_returns[str(y)]["strategy"] - annual_returns[str(y)]["benchmark"]
        )
    monthly_returns: dict[str, float] = {}
    for ym, g in d.groupby("ym"):
        monthly_returns[str(ym)] = float(
            (1 + g["daily_return"].astype(float)).prod() - 1
        )
    mr = pd.Series(monthly_returns)
    monthly_stats = {
        "win_rate": float((mr > 0).mean()) if len(mr) else 0.0,
        "best_month": {"month": mr.idxmax(), "return": float(mr.max())}
        if len(mr)
        else None,
        "worst_month": {"month": mr.idxmin(), "return": float(mr.min())}
        if len(mr)
        else None,
        "mean": float(mr.mean()) if len(mr) else 0.0,
        "median": float(mr.median()) if len(mr) else 0.0,
        "std": float(mr.std()) if len(mr) else 0.0,
        "skew": float(mr.skew()) if len(mr) > 2 else 0.0,
        "kurt": float(mr.kurt()) if len(mr) > 3 else 0.0,
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


def render_report(
    summary: dict, daily: pd.DataFrame, trades: pd.DataFrame, out_path: Path
) -> None:
    """自包含 HTML 报告：指标卡片 + 图表（base64 内嵌）+ 明细表。"""
    import base64
    from io import BytesIO
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    setup_matplotlib_cn_font()

    def fig_b64(fig) -> str:
        # 性能：不用 bbox_inches='tight'（会对每个文本元素做布局度量，中文慢 10 倍+）
        buf = BytesIO()
        fig.savefig(buf, format="png", dpi=110)
        plt.close(fig)
        return base64.b64encode(buf.getvalue()).decode()

    d = daily.copy()
    d["date"] = pd.to_datetime(d["date"])
    # 1) 资金曲线 vs 基准 + 回撤
    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(11, 6.5), sharex=True, gridspec_kw={"height_ratios": [2.2, 1]}
    )
    ax1.plot(
        d["date"],
        d["total_value"] / d["total_value"].iloc[0] - 1,
        label="策略",
        color="#c0392b",
        lw=1.6,
    )
    bmn = d["benchmark_close"].astype(float) / d["benchmark_close"].iloc[0] - 1
    ax1.plot(
        d["date"],
        bmn,
        label="沪深300"
        if summary["config"].get("benchmark", "").startswith("000300")
        else "基准",
        color="#2c3e50",
        lw=1.2,
    )
    ax1.set_title("资金曲线（累计收益率）")
    ax1.legend()
    ax1.grid(alpha=0.3)
    ax2.fill_between(d["date"], d["drawdown"] * 100, 0, color="#e67e22", alpha=0.55)
    ax2.set_title("回撤（%）")
    ax2.grid(alpha=0.3)
    img_main = fig_b64(fig)

    # 2) 年度收益柱状图（策略 vs 基准）
    ar = summary["annual_returns"]
    years = list(ar.keys())
    x = np.arange(len(years))
    fig, ax = plt.subplots(figsize=(7, 3.6))
    ax.bar(
        x - 0.18,
        [ar[y]["strategy"] * 100 for y in years],
        width=0.36,
        label="策略",
        color="#c0392b",
    )
    ax.bar(
        x + 0.18,
        [ar[y]["benchmark"] * 100 for y in years],
        width=0.36,
        label="基准",
        color="#2c3e50",
    )
    for i, y in enumerate(years):
        ax.text(
            i,
            max(ar[y]["strategy"], ar[y]["benchmark"]) * 100 + 0.3,
            f"+{ar[y]['excess'] * 100:.1f}%"
            if ar[y]["excess"] >= 0
            else f"{ar[y]['excess'] * 100:.1f}%",
            ha="center",
            fontsize=9,
            color="#7f8c8d",
        )
    ax.set_xticks(x, years)
    ax.set_title("年度收益（%，含超额标注）")
    ax.legend()
    ax.grid(alpha=0.3, axis="y")
    img_annual = fig_b64(fig)

    # 3) 月度收益热力图（红涨绿跌）
    mr = pd.Series(summary["monthly_returns"])
    if len(mr):
        mr.index = pd.to_datetime(mr.index + "-01")
        pv = mr.to_frame("r")
        pv["year"] = pv.index.year
        pv["month"] = pv.index.month
        pv = pv.pivot_table(index="year", columns="month", values="r")
        fig, ax = plt.subplots(figsize=(9, max(2.2, 0.5 * len(pv) + 1.2)))
        vmax = np.nanmax(np.abs(pv.to_numpy())) or 0.01
        im = ax.imshow(
            pv.to_numpy() * 100,
            cmap=matplotlib.colors.LinearSegmentedColormap.from_list(
                "cn", ["#1a9850", "#ffffff", "#d73027"]
            ),
            vmin=-vmax * 100,
            vmax=vmax * 100,
            aspect="auto",
        )
        ax.set_xticks(range(12), [f"{m}月" for m in range(1, 13)])
        ax.set_yticks(range(len(pv.index)), pv.index)
        # 性能：中文文本度量昂贵，热力图格内数字去掉（颜色已表达数值），仅保留 colorbar
        ax.set_title("月度收益热力图（%，红涨绿跌）")
        fig.colorbar(im, ax=ax, shrink=0.8)
        img_month = fig_b64(fig)
    else:
        img_month = ""

    # 4) 月度收益分布直方图
    fig, ax = plt.subplots(figsize=(7, 3.6))
    vals = mr.values * 100 if len(mr) else []
    ax.hist(
        vals,
        bins=min(20, max(8, len(vals) * 2)),
        color="#2980b9",
        alpha=0.75,
        edgecolor="white",
    )
    if len(vals):
        ax.axvline(
            np.mean(vals), color="#c0392b", ls="--", label=f"均值 {np.mean(vals):.2f}%"
        )
        ax.legend()
    ax.set_title("月度收益分布")
    ax.grid(alpha=0.3, axis="y")
    img_hist = fig_b64(fig)

    def pct(v):
        if v is None or (isinstance(v, float) and (np.isnan(v) or np.isinf(v))):
            return "—"
        return f"{v * 100:.2f}%"

    def num(v, nd=2):
        if v is None or (isinstance(v, float) and (np.isnan(v) or np.isinf(v))):
            return "—"
        return f"{v:,.{nd}f}"

    cards = [
        ("总收益率", pct(summary["total_return"])),
        ("年化收益率", pct(summary["annual_return"])),
        ("夏普率", num(summary["sharpe"])),
        ("最大回撤", pct(summary["max_drawdown"])),
        ("卡尔玛比率", num(summary["calmar"])),
        ("胜率", pct(summary["win_rate"])),
        ("盈亏比", num(summary["profit_loss_ratio"])),
        ("期末资产", f"{summary['final_value']:,.0f}"),
        ("基准收益率", pct(summary["benchmark_return"])),
        ("成交笔数", f"{summary['trade_count']}"),
        ("累计佣金", f"{summary['total_commission']:,.2f}"),
        ("月度胜率", pct(summary["monthly_stats"]["win_rate"])),
    ]
    ms = summary["monthly_stats"]
    extra = ""
    if ms.get("best_month"):
        extra += f"<div class='card'><div class='k'>最佳月</div><div class='v'>{ms['best_month']['month']}（{ms['best_month']['return'] * 100:.2f}%）</div></div>"
    if ms.get("worst_month"):
        extra += f"<div class='card'><div class='k'>最差月</div><div class='v'>{ms['worst_month']['month']}（{ms['worst_month']['return'] * 100:.2f}%）</div></div>"

    trades_html = (
        trades.to_html(index=False, classes="tbl", border=0)
        if len(trades)
        else "<p>无成交</p>"
    )
    annual_rows = "".join(
        f"<tr><td>{y}</td><td>{ar[y]['strategy'] * 100:.2f}%</td><td>{ar[y]['benchmark'] * 100:.2f}%</td>"
        f"<td>{ar[y]['excess'] * 100:.2f}%</td></tr>"
        for y in years
    )
    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>PTrade 回测报告 - {summary["config"].get("strategy", "")}</title><style>
body{{font-family:'Microsoft YaHei',sans-serif;margin:24px;color:#2c3e50;background:#fafafa}}
h1{{font-size:20px}} h2{{font-size:16px;margin-top:28px;border-left:4px solid #c0392b;padding-left:8px}}
.cards{{display:flex;flex-wrap:wrap;gap:10px}}
.card{{background:#fff;border:1px solid #e5e5e5;border-radius:8px;padding:10px 16px;min-width:130px}}
.k{{font-size:12px;color:#7f8c8d}} .v{{font-size:18px;font-weight:600;margin-top:4px}}
img{{max-width:100%;border:1px solid #eee;border-radius:6px;background:#fff}}
.tbl{{border-collapse:collapse;font-size:12px;background:#fff}}
.tbl th,.tbl td{{border:1px solid #e5e5e5;padding:4px 8px}}
.tbl th{{background:#f4f6f7}}
.meta{{font-size:12px;color:#7f8c8d}}
</style></head><body>
<h1>PTrade 策略回测报告：{Path(summary["config"].get("strategy", "策略")).stem}</h1>
<div class="meta">区间 {summary["config"].get("start_date")} ~ {summary["config"].get("end_date")} ｜
初始资金 {summary["config"].get("capital_base", 0):,.0f} ｜ 交易日 {summary["trade_days"]} 天 ｜ 基准 {summary["config"].get("benchmark")}</div>
<h2>核心指标</h2><div class="cards">{"".join(f"<div class='card'><div class='k'>{k}</div><div class='v'>{v}</div></div>" for k, v in cards)}{extra}</div>
<h2>资金曲线与回撤</h2><img src="data:image/png;base64,{img_main}">
<h2>年度收益分布</h2><img src="data:image/png;base64,{img_annual}">
<h2>月度收益分布</h2><img src="data:image/png;base64,{img_month}">
<img src="data:image/png;base64,{img_hist}">
<h2>年度明细</h2><table class="tbl"><tr><th>年份</th><th>策略</th><th>基准</th><th>超额</th></tr>{annual_rows}</table>
<h2>交易明细（{len(trades)} 笔）</h2>{trades_html}
</body></html>"""
    out_path.write_text(html, encoding="utf-8")
    logger.info(f"报告已生成：{out_path}")
