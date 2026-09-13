"""历史数据取数与组装（从 runtime.py 拆出）。

**职责边界**：本模块只负责「把库里的数据按 PTrade 口径拼装出来」——
日线/分钟取数、停牌填充、复权、重采样、字典结构转换、价格区间查询、
交易日查询、集合竞价款。**不含**撮合、下单、涨跌停判定、指标计算，
也不含 PTrade API 的参数默认值处理（那部分留在引擎的 API 适配层）。

**为什么单独成模块**：这簇逻辑与撮合几乎无耦合（实测只共享一个「当前交易日 +
当前槽位」的时钟和一个数据源），但占了 runtime.py 约六分之一，且它是最容易
出错的部分（停牌填充、复权基准、跨日窗口、数据缺口），独立后便于单测与审查。

**状态共享**：引擎与 Provider 共享同一个 :class:`Clock`，避免「引擎改了自己的
``_day_str`` 而 Provider 没看到」这类双份状态错误。
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
from loguru import logger

from ptrade_sim.conventions import (
    DAY_SLOTS,
    as_codes,
    day_iso,
    limit_pct,
    limit_price,
    norm_day,
    to_ptrade_code,
)

if TYPE_CHECKING:  # pragma: no cover - 仅供类型标注，避免运行期循环导入
    # feed 是运行时的 ``runtime.DataFeed``（不是底层 DuckDBSource）：
    # Provider 需要的是「按日取数 + 缓存 + 复权因子」这层接口，而非裸连接。
    from ptrade_sim.runtime import DataFeed


@dataclass
class Clock:
    """回测时钟：当前交易日 + 当前槽位。

    引擎与 :class:`HistoryProvider` **共享同一个实例** —— 若各自维护一份，
    就会出现「引擎已翻到下一日、取数仍按上一日窗口算」的偏差。
    ``slot = -1`` 表示非盘中（盘前/盘后或日线模式）。
    """

    day: str = ""
    slot: int = -1


class HistoryProvider:
    """按 PTrade 口径组装历史行情。"""

    def __init__(
        self,
        feed: DataFeed,
        clock: Clock,
        daily_mode: bool = False,
    ) -> None:
        self.feed = feed
        self.clock = clock
        self.daily_mode = daily_mode
        #: ``get_price`` 的结果缓存（键含区间/频率/复权，故可跨日复用）
        self._price_cache: dict[tuple, pd.DataFrame] = {}
        #: 回看窗口越界记录：库中整体缺失的交易日 -> 被请求次数
        self._missing_days: dict[str, int] = {}
        self._missing_day_warned = False

    # ---- 以下为搬移进来的方法 ----
    def daily(
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
        cur_i = self.feed.day_index(self.clock.day)
        end_i = cur_i + 1 if include else cur_i
        sel_days = days[max(0, end_i - count) : end_i]
        records = []
        last_close: dict[str, float] = {}
        for ds in sel_days:
            rows = self.feed.daily_rows(ds)
            if rows is None:
                # 该交易日在库中**整体缺失**（区别于「个股当日停牌」：
                # 后者 rows 非空、只是该 code 无行）。最常见的原因是回看窗口
                # 越过了库内数据起点 —— 此时下面的填充会给出 NaN，
                # 而 get_history 仍返回满 count 行，不告警就完全看不出来。
                self.note_missing_day(ds)
            for code in codes:
                if ds == self.clock.day and include:
                    # 当日：从分钟数据合成到当前槽位（避免日线全量数据的未来函数）
                    rec = self.today_partial_row(code)
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
                            float(row["volume"]),
                            float(row["money"]),
                            float(row["preclose"]),
                        )
                    )
                else:  # 停牌：前收盘填充，量 0
                    pc = last_close.get(code, float("nan"))
                    records.append((ds, code, pc, pc, pc, pc, 0.0, 0.0, pc))
        return self.assemble_daily(records, codes, fields, fq, single)

    def minute(
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
        cur_day_i = self.feed.day_index(self.clock.day)
        # 时间轴：(day, slot)，从当前槽位向前（include=False 不含当前槽）
        axis: list[tuple[str, int]] = []
        start_slot = self.clock.slot + (1 if include else 0) - 1
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
            if md is None:
                # 同 daily()：该交易日整体缺失（非个股停牌），多为回看越界
                self.note_missing_day(ds)
            ts = f"{day_iso(ds)} {DAY_SLOTS[slot]}:00"
            for code in codes:
                r = md.row_of(code, slot) if md is not None else -1
                if r >= 0 and md is not None:
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
            df = self.apply_fq_daily(df, codes, fq)
        if freq != "1m":
            df = self.resample_1m(df, freq)
        df.index = pd.Index(pd.to_datetime(df["ts"]), name="index")
        df = df.drop(columns=["ts"])
        want = [f for f in fields if f in df.columns]
        if single:
            return df[want]
        return df[["code", *want]]

    def resample_1m(self, df: pd.DataFrame, freq: str) -> pd.DataFrame:
        """N 分钟重采样（label/closed=right，与结束时间标注语义一致）。"""
        # freq 形如 '5m' / '15m' / '60m' —— 剥掉结尾的 'm' 即可。
        # 原为 freq[:-2]，多剥了一位：'5m' -> int('') 抛 ValueError，
        # 而 '15m'/'30m'/'60m'/'120m' 会**静默**按 1/3/6/12 分钟聚合
        # —— 均线周期悄悄变成 1/15，回测照跑照出数，比崩溃更危险。
        n = int(freq[:-1])
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
        # 无需 rename：上面 set_index(pd.to_datetime(df["ts"])) 已把索引命名为 "ts"
        # （源列名即 "ts"），reset_index() 直接产出 ["code", "ts", "open", ...]。
        return agg

    def assemble_daily(
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
                "preclose",  # 官方字段名（原为 pre_close，导致 get_history('preclose') 取到空列）
            ],
        )
        df["price"] = df["close"]
        df["is_open"] = (df["volume"] > 0).astype(int)
        # 价格统一 round 到 2 位（分精度）：消除 float32 存储噪声，
        # 避免 14.9499998 < 14.95 这类误判（PTrade 行情价为精确 2 位小数）
        for col in ("open", "high", "low", "close", "preclose"):
            df[col] = df[col].round(2)
        df["price"] = df["close"]
        # 涨停/跌停价：ST ±5%，非 ST ±10%，Decimal 四舍五入（交易所规则）
        # 批量取 is_st（缓存 dict，一次构建，避免逐行 daily_row / Series.items 迭代）
        st_map: dict[str, int] = {}
        for ds in df["day"].unique():
            st_map.update(self.feed.is_st_map(ds))
        st = [st_map.get(code, 0) for code in df["code"]]
        df["high_limit"] = [
            limit_price(pc, limit_pct(s, c, d))
            for pc, s, c, d in zip(df["preclose"], st, df["code"], df["day"], strict=False)
        ]
        df["low_limit"] = [
            limit_price(pc, -limit_pct(s, c, d))
            for pc, s, c, d in zip(df["preclose"], st, df["code"], df["day"], strict=False)
        ]
        df["unlimited"] = 0
        if fq in ("pre", "dypre", "post"):
            df = self.apply_fq_daily(df, codes, fq)
        want = [f for f in fields if f in df.columns]
        df = df[["day", "code", *want]]
        df.index = pd.Index(pd.to_datetime(df["day"], format="%Y%m%d"), name="index")
        if single:
            return df[want]
        return df[["code", *want]]

    def apply_fq_daily(self, df: pd.DataFrame, codes: list[str], fq: str) -> pd.DataFrame:
        """复权：pre/dypre 以当前回测日因子为基准，post 乘以当日因子。"""
        base_factor = {}
        for code in codes:
            f = self.feed.adj_factor(self.clock.day, code)
            base_factor[code] = f if f else 1.0
        factors = []
        for ds, code in zip(df["day"], df["code"], strict=False):
            f = self.feed.adj_factor(ds, code)
            factors.append(f if f else 1.0)
        f = np.array(factors)
        if fq in ("pre", "dypre"):
            base = np.array([base_factor.get(c, 1.0) for c in df["code"]])
            ratio = f / base
        else:  # post
            ratio = f
        df = df.copy()
        for col in ("open", "high", "low", "close", "preclose"):
            if col in df.columns:
                df[col] = df[col] * ratio
        return df

    def to_struct(self, df: pd.DataFrame, code: str, freq: str):
        """is_dict=True 的返回：numpy 结构化数组（官方格式）。"""
        if "code" in df.columns:
            sub = df[df["code"] == code].drop(columns=["code"])
        else:
            sub = df
        n = len(sub)
        arr: np.ndarray = np.zeros(
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

    def today_partial_row(self, code: str) -> tuple | None:
        """当日到当前槽位的分钟合成 OHLC（include=True 时替代日线全量，防未来函数）。"""
        md = self.feed.minute_day(self.clock.day)
        daily = self.feed.daily_row(self.clock.day, code)
        pre_close = float(daily["preclose"]) if daily else float("nan")
        if md is None:
            return None
        s = md.rows_upto(code, self.clock.slot, include=True)
        if s.stop - s.start == 0:
            return (
                self.clock.day,
                code,
                pre_close,
                pre_close,
                pre_close,
                pre_close,
                0.0,
                0.0,
                pre_close,
            )
        # 取**首分钟**开盘价（与下面 close 取末分钟对称）。
        # 原写法 `o = md.open[s]` 得到的是切片（1 元素 ndarray），
        # 下面 float(o) 在 numpy>=2.5 上会抛 TypeError
        # （"only 0-dimensional arrays can be converted to Python scalars"）。
        o = float(md.open[s.start])
        h = float(np.max(md.high[s]))
        low_v = float(np.min(md.low[s]))
        c = float(md.close[s.stop - 1])
        v = float(np.sum(md.vol[s]))
        a = float(np.sum(md.amount[s]))
        return (self.clock.day, code, float(o), h, low_v, c, v, a, pre_close)

    def price(
        self, security, start_date, end_date, frequency, fields, fq, count, is_dict
    ) -> pd.DataFrame | dict:
        single = isinstance(security, str)
        codes = as_codes(security)
        freq = frequency.lower()
        if not fields:
            if freq == "1d":
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
                norm_day(end_date)
                if end_date
                else (self.feed.prev_day(self.clock.day) or self.clock.day)
            )
            limit = self.feed.prev_day(self.clock.day) or self.clock.day
            if end > limit:
                end = limit
            if start_date and count:
                logger.warning("get_price：start_date 与 count 只能二选一，忽略 count")
                count = None
            if start_date:
                sel = self.feed.days_between(norm_day(start_date), end)
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
                                float(row["volume"]),
                                float(row["money"]),
                                float(row["preclose"]),
                            )
                        )
                    else:
                        pc = last_close.get(code, float("nan"))
                        records.append((ds, code, pc, pc, pc, pc, 0.0, 0.0, pc))
            result = self.assemble_daily(records, codes, fields, fq, single)
            # 全市场日线批缓存：同一 (回测日, 区间, 股票集, 字段) 的重复调用直接命中。
            # key 含 codes 原顺序（策略可能依赖候选顺序），命中返回副本避免污染。
            if not single and len(codes) > 500:
                ck = (
                    self.clock.day,
                    tuple(sel),
                    tuple(codes),
                    tuple(fields),
                    fq,
                )
                hit = self._price_cache.get(ck)
                if hit is None:
                    # 存副本：缓存对象必须与返回对象隔离（策略可能 in-place 修改返回值）
                    self._price_cache[ck] = result.copy()
                else:
                    result = hit.copy()
        else:
            # 分钟频率：count 相对当前时刻向前
            result = self.minute(
                codes,
                int(count or 1),
                freq if freq.endswith("m") else "1m",
                fields,
                fq,
                include=False,
                single=single,
            )
            # minute() 以当前槽位为基准；get_price 不含当前，符合官方
        if is_dict:
            return {c: self.to_struct(result, c, freq) for c in codes}
        return result

    def trade_days(self, start_date, end_date, count) -> np.ndarray:
        days = self.feed.trade_days
        if start_date and count:
            logger.warning("get_trade_days：start_date 与 count 二选一，忽略 count")
            count = None
        end = norm_day(end_date) if end_date else self.clock.day
        if start_date:
            sel = self.feed.days_between(norm_day(start_date), end)
        else:
            c = int(count or 1)
            i = bisect_right(days, end)
            sel = days[max(0, i - c) : i]
        return np.array([day_iso(d) for d in sel])

    def trend_data(self, stocks) -> dict:
        """集中竞价数据：优先取 L2 竞价表（9:25 正式撮合的纯竞价量/价，精确）；
        无记录时回退 09:30 bar（hq_px=open, business_amount=vol 近似）。"""
        md = self.feed.minute_day(self.clock.day)
        l2 = self.feed.l2_auction_day(self.clock.day)
        out = {}
        for code in stocks:
            code = to_ptrade_code(code)
            snap = l2.get(code)
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

    def note_missing_day(self, ds: str) -> None:
        """记录「该交易日在库中整体缺失」并一次性告警。

        **与个股停牌区分**：停牌是「库里这天有数据，只是该 code 无行」，
        ``daily_rows(ds)`` 仍返回 dict；而这里 ``rows is None`` 说明**整个交易日**
        都不在库中 —— 最常见的成因是 ``get_history`` 的回看窗口越过了库内数据起点。

        为什么必须告警：此时引擎走"前收盘填充"分支，而窗口首日没有前收盘，
        于是填成 NaN；但 ``get_history`` **照样返回满 count 行**。
        策略里极常见的 ``close.iloc[0]``（算区间涨幅）拿到 NaN → ``dropna()`` 清空
        → **静默跳过调仓**，用户只看到"回测完成"。这类"错得不明显"的失败
        正是本平台要避免的，所以这里必须让它可见。

        按「首个缺失日」告警一次（避免 code × count 刷屏），全程缺失则汇总进
        ``summary.json`` 的 ``data_gaps``，便于事后核查。
        """
        self._missing_days[ds] = self._missing_days.get(ds, 0) + 1
        if self._missing_day_warned:
            return
        self._missing_day_warned = True
        cov = (self.feed.data_coverage or {}).get("daily")
        span = f"{cov[0]}~{cov[1]}" if cov else "（未知）"
        logger.warning(
            f"get_history 回看窗口触及库内数据覆盖之前：交易日 {ds} 在库中不存在"
            f"（库内日线覆盖 {span}）→ 该日将填为 NaN，窗口起点的价格/涨幅类计算"
            f"（如 close.iloc[0]）会得到 NaN，策略若 dropna 可能静默跳过调仓。"
            f"请把回测起点后移，或补建更早的数据。"
        )

    def data_gaps(self) -> dict:
        """回看窗口越界汇总（供 CLI 写入 summary.json）。"""
        if not self._missing_days:
            return {}
        days = sorted(self._missing_days)
        cov = (self.feed.data_coverage or {}).get("daily")
        return {
            "missing_days": days,
            "missing_day_count": len(days),
            "request_count": sum(self._missing_days.values()),
            "daily_coverage": list(cov) if cov else None,
            "hint": "回看窗口超出库内数据覆盖；超出的交易日被填为 NaN",
        }
