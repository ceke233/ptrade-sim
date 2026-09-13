"""``history.py`` / ``resources.py`` 的补充测试：停牌填充、复权基准、跨日窗口、
重采样、平台资源探查与准入判定。

**为什么这些分支值得单独测**：它们出错的共同特征是「跑完了、也不报错，结果悄悄错掉」。
所以断言尽量落到**具体数值**上，而不是"不抛异常"：

- 复权基准算错 → 收益率虚高/虚低（回测"很赚钱"）；
- 停牌填充忘了把量置 0 → 停牌日被当成有成交，量价类指标全歪；
- 重采样把 open/close 取反、量丢失 → 成交量/区间高低点失真；
- 跨日窗口少取/多取一根 → 区间涨幅整体偏移；
- 准入判定算错 → 要么 OOM，要么任务**永远排不上队**。

约定：**不碰共享夹具 ``tiny_db``**（并行测试会互相污染）。需要「停牌 / 因子变化」
这类数据形态时，把它复制到 ``tmp_path`` 再改，共享夹具保持只读。

本文件最初以 ``xfail`` 登记了两个真实缺陷，二者均已修复并**摘掉 xfail**，
现由普通断言持续守护：

1. ``today_partial_row`` 曾用 ``float(md.open[s])`` 标量化 1 元素切片
   —— numpy>=2.5 直接抛 TypeError，导致分钟频率下 ``include=True`` 取数必崩
   （且被引擎吞掉、策略函数被永久跳过）。现取首分钟标量 ``md.open[s.start]``。
2. ``resample_1m`` 曾用 ``int(freq[:-2])`` 解析频率 —— ``'5m'`` 抛 ValueError，
   ``'15m/30m/60m'`` **静默**按 1/3/6 分钟聚合（均线周期悄悄变 1/15）。
   现为 ``int(freq[:-1])``。
"""

from __future__ import annotations

import io
import itertools
import os
import shutil
import sys
import types
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from ptrade_sim import resources as R
from ptrade_sim.history import Clock, HistoryProvider
from ptrade_sim.runtime import DataFeed

TRADE_START = "20250102"
TRADE_END = "20250108"
MB = 1024 * 1024
GB = 1024**3


# ============================================================
# 夹具：派生库 + Provider
# ============================================================


def _derived_db(src: Path, tmp_path: Path, tag: str, *sql: str) -> Path:
    """复制共享合成库再改（**绝不动共享夹具**：并行测试会读到一半的中间状态）。"""
    d = tmp_path / tag
    d.mkdir(parents=True, exist_ok=True)
    dst = d / "copy.duckdb"
    shutil.copyfile(src, dst)
    con = duckdb.connect(str(dst))
    try:
        for stmt in sql:
            con.execute(stmt)
    finally:
        con.close()
    return dst


@pytest.fixture
def provider_factory(tiny_db, tmp_path):
    """``make(day, slot, sql=..., db=...)`` -> ``(HistoryProvider, Clock)``。

    ``sql`` 非空时先把库复制到 ``tmp_path`` 再执行，用于造停牌/因子变化。
    时钟与 Provider **共享同一个 Clock 实例**（与 runtime.py 里引擎的做法一致）。
    """
    seq = itertools.count()

    def make(day: str = "20250106", slot: int = -1, *, sql=(), db=None, daily_mode=False):
        target = Path(db or tiny_db)
        if sql:
            target = _derived_db(target, tmp_path, f"db{next(seq)}", *sql)
        feed = DataFeed(str(target), TRADE_START, TRADE_END, preload_mode="rolling", threads=2)
        clock = Clock(day=day, slot=slot)
        return HistoryProvider(feed, clock, daily_mode=daily_mode), clock

    return make


#: 000001.SZ 的逐日复权因子（夹具里其余股票恒为 1.0）
FQ_FACTORS = {"20250102": 1.0, "20250103": 1.1, "20250106": 1.2, "20250107": 1.3, "20250108": 1.4}
#: 基准日因子：pre 复权以「当前回测日」的因子为基准
FQ_BASE = FQ_FACTORS["20250108"]


@pytest.fixture
def fq_provider(provider_factory):
    """带真实复权因子序列的 Provider（基准日 = 20250108，因子 1.4）。"""
    case = "CASE date " + " ".join(f"WHEN '{d}' THEN {f}" for d, f in FQ_FACTORS.items()) + " END"
    sql = f"UPDATE ashare_1d_stock SET adj_factor = {case} WHERE code='000001.SZ'"

    def make(day: str = "20250108", slot: int = 240):
        return provider_factory(day=day, slot=slot, sql=(sql,))

    return make


@pytest.fixture
def loguru_warnings():
    """捕获 loguru WARNING（conftest 不提供，故在本文件内自建）。"""
    import contextlib

    from loguru import logger

    msgs: list[str] = []
    sink = logger.add(lambda m: msgs.append(str(m)), level="WARNING")
    yield msgs
    with contextlib.suppress(ValueError):
        logger.remove(sink)


def _days(df: pd.DataFrame) -> list[str]:
    return list(df.index.strftime("%Y%m%d"))


def _stamps(df: pd.DataFrame) -> list[str]:
    return [str(t) for t in df.index]


def _minute_frame(codes=("T1",), n: int = 20, start: str = "09:31") -> pd.DataFrame:
    """合成连续 1 分钟数据：OHLC 四列各走一条独立直线，便于逐列验聚合口径。"""
    hh, mm = (int(x) for x in start.split(":"))
    t0 = hh * 60 + mm
    rows = []
    for ci, code in enumerate(codes):
        for i in range(n):
            t = t0 + i
            rows.append(
                {
                    "ts": f"2025-01-02 {t // 60:02d}:{t % 60:02d}:00",
                    "code": code,
                    "open": 100.0 + 10 * ci + i,
                    "high": 200.0 + 10 * ci + i,
                    "low": 50.0 + 10 * ci + i,
                    "close": 150.0 + 10 * ci + i,
                    "volume": 1.0,
                    "money": 2.0,
                }
            )
    return pd.DataFrame(rows)


# ============================================================
# history.daily：窗口边界、停牌填充、include=True
# ============================================================


@pytest.mark.integration
def test_daily_window_excludes_today_by_default(provider_factory):
    """``include=False``（默认）必须取到**上一交易日**为止。

    官方语义「返回内容不包括当天数据」。若把 end_i 写成 cur_i+1，当日收盘价就会
    出现在窗口里 —— 策略在 09:30 用当日 15:00 的收盘价算信号，这是**未来函数**，
    且回测结果会好得离谱却完全不报错。
    """
    p, _ = provider_factory(day="20250106", slot=0)
    df = p.daily(["000001.SZ"], 2, ["close"], None, include=False, single=True)

    assert _days(df) == ["20250102", "20250103"], "窗口应止于上一交易日"
    assert list(df["close"]) == [10.50, 10.80], "取的应是这两天的原始收盘"
    assert df.index.name == "index"
    assert str(df.index.dtype).startswith("datetime64"), "官方返回以时间为 index"
    assert p.data_gaps() == {}, "未越界时不得产生 data_gaps（summary.json 里不该出现）"


@pytest.mark.integration
def test_daily_suspended_stock_filled_with_last_close_and_zero_volume(provider_factory):
    """个股停牌（当日日线无行）→ 用窗口内**上一根收盘价**填充，且成交量必须为 0。

    防的是：用 0 或者 NaN 当停牌价，或者忘了把 volume 清零 —— 前者让均价/涨跌幅
    在停牌日变成 ±inf，后者让停牌日被统计成有 100 万股成交。
    """
    sql = ("DELETE FROM ashare_1d_stock WHERE code='000002.SZ' AND date='20250103'",)
    p, _ = provider_factory(day="20250106", slot=0, sql=sql)
    df = p.daily(
        ["000002.SZ"], 2, ["close", "volume", "preclose", "is_open"], None, False, single=False
    )

    assert _days(df) == ["20250102", "20250103"]
    assert list(df["close"]) == [5.10, 5.10], "停牌日应填前收 5.10（0102 收盘）"
    assert list(df["volume"]) == [1_000_000.0, 0.0], "停牌日成交量为 0"
    assert list(df["is_open"]) == [1, 0], "停牌日 is_open=0"
    assert list(df["preclose"]) == [5.00, 5.10]
    assert list(df.columns) == ["code", "close", "volume", "preclose", "is_open"], (
        "list 入参（即使只含一只）也按多股票返回 [code, field]"
    )


@pytest.mark.integration
def test_daily_first_row_suspended_fills_nan(provider_factory):
    """窗口首日就停牌（没有前收盘可继承）→ 必须填 **NaN**，不能拿 0 冒充价格。

    这是 ``close.iloc[0]`` 算区间涨幅得到 NaN 的根源；填 0 会让涨幅变成 +inf 或者
    让 dropna 之后静默少一行，两种都难以察觉。
    """
    sql = ("DELETE FROM ashare_1d_stock WHERE code='000002.SZ' AND date='20250103'",)
    p, _ = provider_factory(day="20250106", slot=0, sql=sql)
    df = p.daily(["000002.SZ"], 1, ["close", "volume"], None, False, single=True)

    assert _days(df) == ["20250103"]
    assert df["close"].isna().all(), "无前收盘时必须 NaN，不能用 0 充当价格"
    assert list(df["volume"]) == [0.0]


@pytest.mark.integration
def test_daily_include_true_before_open_does_not_leak_todays_close(provider_factory):
    """``include=True`` 在盘中之前（slot=-1，即日线模式/盘前）当日只能用"占位"bar。

    当日占位 bar = 前收盘 + 量 0，**绝不能**是当日日线的完整 OHLC（含 15:00 收盘价）。
    这就是"防未来函数"的那条线：9:30 拿到 11.00（当日收盘）与拿到 10.80（前收）
    会得到完全相反的交易决策。
    """
    p, clock = provider_factory(day="20250106", slot=-1)
    df = p.daily(["000001.SZ"], 3, ["open", "low", "close", "volume", "is_open"], None, True, True)

    assert _days(df) == ["20250102", "20250103", "20250106"]
    today = df.loc[pd.Timestamp("2025-01-06")]
    assert today["close"] == 10.80, "当日占位价应为前收 10.80"
    assert today["open"] == today["low"] == 10.80
    assert today["volume"] == 0.0 and today["is_open"] == 0, "占位 bar 不应有成交量"
    assert 11.00 not in set(df["close"]), "不得出现当日日线收盘价（未来函数）"

    row = p.today_partial_row("000001.SZ")
    assert row is not None and len(row) == 9
    assert row[0] == "20250106" and row[1] == "000001.SZ"
    assert row[2:6] == (10.80, 10.80, 10.80, 10.80), "OHLC 全部取前收"
    assert row[6] == 0.0 and row[7] == 0.0, "量/额均为 0"
    assert row[8] == 10.80, "第 9 位是 preclose"

    # 引擎与 Provider 共享同一个 Clock：时钟翻日后取数必须跟着翻，
    # 否则会出现"引擎已到 0103、取数还按 0106 算"的双份状态（历史上有过的缺陷）。
    clock.day = "20250103"
    assert _days(p.daily(["000001.SZ"], 1, ["close"], None, False, True)) == ["20250102"]


@pytest.mark.integration
def test_daily_include_true_intraday_bar_is_synthesized_from_minutes(provider_factory):
    """``include=True`` 在盘中（slot>=0）当日 bar 必须由**分钟合成到当前槽位**。

    期望值按夹具分钟数据算：09:30 开盘 10.81；第 121 根（11:00 收）10.91；
    当日最高 11.11（11:00 那根触及 11.00*1.01）；最低 10.70；量 121 根 × 1000。
    与日线全量（收盘 11.00、量 1_000_000）截然不同 —— 用它才能防未来函数。
    """
    p, _ = provider_factory(day="20250106", slot=120)
    df = p.daily(
        ["000001.SZ"],
        2,
        ["open", "high", "low", "close", "volume", "money", "preclose"],
        None,
        include=True,
        single=True,
    )

    assert _days(df) == ["20250103", "20250106"]
    today = df.iloc[-1]
    assert today["open"] == 10.81, "开盘取当日分钟第一根（09:30）"
    assert today["close"] == 10.91, "收盘取到当前槽位那一根，不是 15:00"
    assert today["high"] == 11.11 and today["low"] == 10.70
    assert today["volume"] == 121_000.0, "量应只累计到当前槽位"
    assert today["preclose"] == 10.80
    assert today["close"] != 11.00, "取到当日日线收盘价即为未来函数"


# ============================================================
# history.minute：跨日窗口、列裁剪、停牌填充、缺口
# ============================================================


@pytest.mark.integration
def test_minute_window_spans_previous_days_in_chronological_order(provider_factory):
    """分钟窗口不足当日根数时要**跨到上一交易日**，且严格按时间升序拼装。

    典型后果：``axis.reverse()`` 漏掉或倒序取日，会让 ``close.iloc[-1]`` 拿到旧价格，
    KDJ/均线类指标整体错位，而返回值长度、类型全都正常。
    """
    p, _ = provider_factory(day="20250106", slot=0)
    df = p.minute(["000001.SZ"], 242, "1m", ["close", "volume"], None, False, single=False)

    assert len(df) == 242, "include=False 时不含当前槽位，从上一交易日起向前取满"
    assert df.index.is_monotonic_increasing, "必须按时间升序（否则指标全部错位）"
    assert _stamps(df)[0] == "2025-01-02 15:00:00", "最早一根落在 0102 的 15:00"
    assert _stamps(df)[-1] == "2025-01-03 15:00:00", "最晚一根是 0103 的 15:00"
    assert df["close"].iloc[0] == 10.50, "0102 最后一根的收盘=当日收盘"
    assert df["close"].iloc[-1] == 10.80, "0103 最后一根的收盘=当日收盘"
    assert list(df.columns) == ["code", "close", "volume"], "只返回请求的字段"
    assert "price" not in df.columns, "未请求的 price 不得混入"
    assert df.index.name == "index"


@pytest.mark.integration
def test_minute_single_stock_shape_and_include_flag(provider_factory):
    """str 入参 → 单股票形态（无 code 列）；``include`` 决定是否含当前槽位。

    ``include`` 差一根就会让"当前 bar 已收盘"的判断错位：策略在 09:30 用
    include=False 拿到的是**昨天**最后一根，而不是刚刚集合竞价的这一根。
    """
    p, _ = provider_factory(day="20250106", slot=0)
    inc = p.minute(["000001.SZ"], 1, "1m", ["close"], None, True, single=True)
    exc = p.minute(["000001.SZ"], 1, "1m", ["close"], None, False, single=True)

    assert list(inc.columns) == ["close"], "str 入参按单股票返回（index=时间）"
    assert _stamps(inc) == ["2025-01-06 09:30:00"]
    assert inc["close"].iloc[0] == 10.81, "当前槽位（09:30 集合竞价）那一根"
    assert _stamps(exc) == ["2025-01-03 15:00:00"], "include=False 取到上一交易日最后一根"
    assert exc["close"].iloc[0] == 10.80


@pytest.mark.integration
def test_minute_suspension_uses_last_available_close_in_window(provider_factory):
    """分钟停牌填充：用**窗口内**最近一根的可成交收盘价，量 0，且不影响其它股票。

    防的是：拿当日日线 preclose 顶替（数值上可能对，但跨日窗口里窗口内价格更贴近
    实际连续行情），以及"一停牌就把整个窗口的股票全填了"这类按 code 分支写错的实现。
    """
    sql = ("DELETE FROM ashare_1m_stock WHERE code='000002.SZ' AND date='20250103'",)
    p, _ = provider_factory(day="20250106", slot=0, sql=sql)
    df = p.minute(
        ["000002.SZ", "000001.SZ"], 244, "1m", ["close", "volume"], None, False, single=False
    )

    susp = df[df["code"] == "000002.SZ"]
    assert _stamps(susp)[:3] == [
        "2025-01-02 14:58:00",
        "2025-01-02 14:59:00",
        "2025-01-02 15:00:00",
    ], "窗口前 3 根是 0102（有行情）"
    assert susp["volume"].iloc[:3].tolist() == [1000.0, 1000.0, 1000.0]
    assert susp["volume"].iloc[3:].eq(0.0).all(), "停牌段成交量为 0"
    assert susp["close"].iloc[3:].eq(5.10).all(), "停牌段填窗口内最近收盘 5.10"
    assert susp["close"].iloc[2] == 5.10, "0102 15:00 收盘 = 5.10"

    other = df[df["code"] == "000001.SZ"]
    assert other["volume"].iloc[3:].eq(1000.0).all(), "同窗口的其它股票不得被牵连填充"


@pytest.mark.integration
def test_minute_suspension_without_prior_close_fills_nan(provider_factory):
    """窗口首根即停牌 → NaN（不是 0）。0 会被当成有效价格算进均价与涨跌幅。"""
    sql = ("DELETE FROM ashare_1m_stock WHERE code='000002.SZ' AND date='20250103'",)
    p, _ = provider_factory(day="20250106", slot=0, sql=sql)
    df = p.minute(["000002.SZ"], 3, "1m", ["close", "volume"], None, False, single=True)

    assert _stamps(df) == [
        "2025-01-03 14:58:00",
        "2025-01-03 14:59:00",
        "2025-01-03 15:00:00",
    ]
    assert df["close"].isna().all(), "无前收盘时必须 NaN"
    assert list(df["volume"]) == [0.0, 0.0, 0.0]
    assert df["close"].iloc[-1] != 0.0, "0 不是合法的停牌填充价"


@pytest.mark.integration
def test_minute_lookback_overflow_keeps_full_count_and_records_gap(
    provider_factory, loguru_warnings
):
    """分钟窗口越过库内覆盖时：**仍然返回满 count 行**，落 NaN 并告警一次 + 留档。

    "返回满 count"正是它危险的地方（``len(h)==0`` 之类的守卫抓不到）。缺的是
    **整日**（非个股停牌）才告警，且要能进 summary.json 的 data_gaps。
    """
    p, _ = provider_factory(day="20250106", slot=0)
    df = p.minute(["000001.SZ"], 485, "1m", ["close"], None, False, single=True)

    assert len(df) == 485, "缺数据也要凑满 count（所以从返回值看不出问题）"
    assert _stamps(df)[0] == "2024-12-31 14:58:00", "越界段来自日历中行情之前的日子"
    assert df["close"].iloc[:3].isna().all(), "越界的 3 根全为 NaN"
    assert not df["close"].iloc[3:].isna().any(), "库内日期的价格不受影响"
    assert df["close"].iloc[-1] == 10.80

    assert p._missing_days == {"20241231": 3}, "按缺失日累计请求次数（1 日 × 3 槽）"
    hits = [m for m in loguru_warnings if "回看窗口" in m]
    assert len(hits) == 1, f"同上/跨 code 只能告警一次，实际 {len(hits)} 次：{hits}"
    assert "NaN" in hits[0] and "20250102~20250108" in hits[0], "告警要给出库内覆盖范围"

    gaps = p.data_gaps()
    assert gaps["missing_days"] == ["20241231"]
    assert gaps["request_count"] == 3, "3 个槽位各请求了一次缺失日"
    assert gaps["missing_day_count"] == 1
    assert gaps["daily_coverage"] == ["20250102", "20250108"]


@pytest.mark.integration
def test_minute_resampled_frequency_keeps_totals_through_public_api(provider_factory):
    """``minute(freq='30m')`` 走的是同一条聚合通路：量不丢、区间高低点不变、
    末根收盘仍等于当日收盘，且仍按单股票形态（index=时间）返回。

    这些量与频率**无关**，所以无论 bin 宽度是否算对都必须成立 —— 是"聚合环节
    本身有没有丢数据"的底线检查（``resample_1m`` 的 bin 宽度另有用例）。
    """
    p, _ = provider_factory(day="20250106", slot=0)
    fields = ["open", "high", "low", "close", "volume", "money"]
    m1 = p.minute(["000001.SZ"], 241, "1m", fields, None, False, True)
    m30 = p.minute(["000001.SZ"], 241, "30m", fields, None, False, True)

    assert len(m1) == 241 and len(m30) < len(m1), "聚合后根数必须变少"
    assert m30["volume"].sum() == m1["volume"].sum(), "聚合不得丢量"
    assert m30["money"].sum() == pytest.approx(m1["money"].sum())
    assert m30["high"].max() == m1["high"].max(), "当日最高价必须保持"
    assert m30["low"].min() == m1["low"].min(), "当日最低价必须保持"
    assert m30["open"].iloc[0] == m1["open"].iloc[0], "首根开盘取当日第一分钟"
    assert m30["close"].iloc[-1] == m1["close"].iloc[-1] == 10.80, "末根收盘=当日收盘"
    assert m30.index.name == "index" and str(m30.index[0]) == "2025-01-03 09:30:00"


@pytest.mark.integration
def test_daily_include_true_on_day_without_any_data_falls_back_to_nan(
    provider_factory, loguru_warnings
):
    """``include=True`` 且当日**连分钟数据都没有**（日历里有、行情里没有）时，
    必须退回"无数据"分支：NaN + 量 0，而不是抛异常或伪造一个价格。

    这是回测起点落在行情覆盖之前的典型形态（``get_history(count, '1d', include=True)``
    的第一天），处理错会直接让回测在第一天崩掉或拿到假价格。
    """
    p, _ = provider_factory(day="20250109", slot=-1)
    df = p.daily(["000001.SZ"], 1, ["close", "volume", "is_open"], None, True, True)

    assert _days(df) == ["20250109"], "当日仍在窗口内（include=True）"
    assert df["close"].isna().all(), "无任何行情时必须 NaN"
    assert list(df["volume"]) == [0.0] and list(df["is_open"]) == [0]
    assert any("回看窗口" in m for m in loguru_warnings), "整日缺失必须告警"


# ============================================================
# history.resample_1m
# ============================================================


@pytest.mark.integration
def test_resample_1m_preserves_envelope_totals_and_per_code_grouping(provider_factory):
    """重采样必须：量/额求和、区间高低点保持、首开=第一根开盘、末收=最后一根收盘，
    且**按 code 独立分组**（两只股票不能被并成一根）。

    防的是：把 open/close 的 first/last 取反（K 线方向反过来）、
    按 mean 聚合成交量（量对不上）、漏掉 groupby（两只股票的量相加，直接翻倍）。
    """
    p, _ = provider_factory(day="20250106", slot=0)
    src = _minute_frame(codes=("T1", "T2"), n=20)
    out = p.resample_1m(src, "30m")

    assert list(out.columns) == [
        "code",
        "ts",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "money",
        "price",
    ]
    assert set(out["code"]) == {"T1", "T2"}, "两只是独立分组，不得合并"
    assert (out["price"] == out["close"]).all(), "price 必须跟 close 一致"
    assert not out["open"].isna().any(), "空 bin 必须 dropna 掉（否则出现无成交的假 bar）"

    for code, g in out.groupby("code"):
        s = src[src["code"] == code]
        assert g["ts"].is_monotonic_increasing
        assert g["volume"].sum() == pytest.approx(s["volume"].sum()), "量不能丢也不能重复累加"
        assert g["money"].sum() == pytest.approx(s["money"].sum())
        assert g["high"].max() == s["high"].max(), "区间最高价必须保持"
        assert g["low"].min() == s["low"].min(), "区间最低价必须保持"
        assert g["open"].iloc[0] == s["open"].iloc[0], "首个 bin 的开盘取第一分钟"
        assert g["close"].iloc[-1] == s["close"].iloc[-1], "末个 bin 的收盘取最后一分钟"


@pytest.mark.parametrize("freq", ["5m", "15m", "30m", "60m"])
@pytest.mark.integration
def test_resample_1m_bin_width_matches_frequency(provider_factory, freq):
    """bin 宽度必须等于频率里的分钟数：3×n 根 1 分钟数据 → 恰好 3 根，每根量 = n。

    错在这里比崩溃更糟：策略请求 15m 却拿到 1m 数据，均线周期悄悄变成原来的 1/15，
    回测照跑、指标照出，没有任何异常。
    """
    n = int(freq[:-1])
    # 起点取「bin 边界 + 1 分钟」，使 3n 根恰好铺满 3 个整 bin（pandas 的 bin 按自然
    # 分钟边界对齐，起点不齐会多出半截 bin）
    start = 1 + n * ((9 * 60 + 30) // n)
    p, _ = provider_factory(day="20250106", slot=0)
    df = _minute_frame(codes=("T1",), n=3 * n, start=f"{start // 60:02d}:{start % 60:02d}")
    out = p.resample_1m(df, freq)

    end = start + n - 1  # label=right：第一根 bin 标在区间右端（结束时间）
    assert len(out) == 3, f"{freq} 应把 {3 * n} 根 1m 聚成 3 根，实际 {len(out)} 根"
    assert out["volume"].tolist() == [float(n)] * 3, f"{freq} 每根应含 {n} 根分钟量"
    assert str(out["ts"].iloc[0])[:19] == f"2025-01-02 {end // 60:02d}:{end % 60:02d}:00"


# ============================================================
# history 复权（apply_fq_daily / assemble_daily / minute）
# ============================================================


@pytest.mark.integration
def test_apply_fq_daily_pre_post_and_base_relationship(fq_provider):
    """复权基准的**定义**：post 乘当日因子；pre/dypre 再除以「当前回测日」因子。

    用具体数值锁死三条关系（防"乘成除""基准日取错"这类会让收益率整体放大的错误）：
    1. pre 基准日价格 == 原始价（基准日因子 1.4 / 自身 = 1）；
    2. post == 原始 × 当日因子；
    3. pre == post ÷ 基准因子（前/后复权只差一个常数基准）。
    另外：复权只能改价格，**不能改成交量**；也不得原地改调用方的 DataFrame。
    """
    p, _ = fq_provider(day="20250108", slot=240)
    raw = pd.DataFrame(
        {
            "day": ["20250102", "20250108"],
            "code": ["000001.SZ", "000001.SZ"],
            "open": [10.00, 11.00],
            "high": [11.00, 12.00],
            "low": [9.00, 10.00],
            "close": [10.50, 11.20],
            "preclose": [10.00, 11.00],
            "volume": [1_000_000.0, 1_000_000.0],
        }
    )
    pre = p.apply_fq_daily(raw, ["000001.SZ"], "pre")
    post = p.apply_fq_daily(raw, ["000001.SZ"], "post")
    dypre = p.apply_fq_daily(raw, ["000001.SZ"], "dypre")

    assert list(pre["close"]) == pytest.approx([10.50 / FQ_BASE, 11.20]), "基准日 pre 价=原始价"
    assert list(post["close"]) == pytest.approx([10.50 * 1.0, 11.20 * FQ_BASE])
    assert list(post["close"] / pre["close"]) == pytest.approx([FQ_BASE, FQ_BASE])
    assert list(dypre["close"]) == list(pre["close"]), "dypre 与 pre 同口径"
    ratio = (pre["close"] / raw["close"]).to_numpy()
    for col in ("open", "high", "low", "preclose"):
        assert (pre[col] / raw[col]).to_numpy() == pytest.approx(ratio), (
            f"{col} 的复权比例与 close 不一致，漏改一列会让 K 线自相矛盾"
        )
    assert list(pre["volume"]) == list(raw["volume"]), "成交量不随复权变化"
    assert list(raw["close"]) == [10.50, 11.20], "不得原地修改调用方的 DataFrame"


@pytest.mark.integration
def test_fq_daily_end_to_end_none_pre_post(fq_provider):
    """端到端（经 assemble_daily）三种复权口径：原始 / 前复权 / 后复权。

    因子 1.0/1.1/1.2/1.3 → 基准日 1.4：
    - fq=None : [10.5, 10.8, 11.0, 10.9]
    - pre     : 原始 ÷ 1.4 再乘当日因子 → [7.5, 8.4857, 9.4286, 10.1214]
    - post    : 原始 × 当日因子 → [10.5, 11.88, 13.2, 14.17]
    任一处把因子用反，得到的"收益率"都会成倍偏离真实值。
    """
    p, _ = fq_provider(day="20250108", slot=240)
    raw = p.daily(["000001.SZ"], 4, ["close"], None, False, True)
    pre = p.daily(["000001.SZ"], 4, ["close"], "pre", False, True)
    post = p.daily(["000001.SZ"], 4, ["close"], "post", False, True)

    assert _days(raw) == ["20250102", "20250103", "20250106", "20250107"]
    assert list(raw["close"]) == [10.50, 10.80, 11.00, 10.90]
    assert list(pre["close"]) == pytest.approx([7.5, 8.4857142857, 9.4285714286, 10.1214285714])
    assert list(post["close"]) == pytest.approx([10.50, 11.88, 13.20, 14.17])
    ratio = (post["close"] / pre["close"]).to_numpy()
    assert ratio == pytest.approx([FQ_BASE] * 4), "前后复权之间只允许差一个基准因子"


@pytest.mark.integration
def test_minute_fq_shares_base_with_daily(fq_provider):
    """分钟路径的复权必须与日线**同基准**（同一回测日的因子）。

    分钟漏复权时数值看起来"合理"（就是原始价），只有跟日线一对比才暴露；
    一旦两者不一致，跨周期策略（日线选股 + 分钟择时）就会算出两个不同的收益率。
    """
    p, _ = fq_provider(day="20250108", slot=240)
    d_pre = p.daily(["000001.SZ"], 1, ["close"], "pre", False, True)
    # 241 根：0108 的 240 个槽位 + 0107 的 15:00，故首根落在上一交易日
    m_pre = p.minute(["000001.SZ"], 241, "1m", ["close"], "pre", False, True)
    m_raw = p.minute(["000001.SZ"], 241, "1m", ["close"], None, False, True)

    assert _days(d_pre) == ["20250107"]
    assert float(d_pre["close"].iloc[0]) == pytest.approx(10.90 * 1.3 / FQ_BASE)
    assert _stamps(m_pre)[0] == "2025-01-07 15:00:00"
    assert _stamps(m_pre)[-1] == "2025-01-08 14:59:00"
    assert float(m_pre["close"].iloc[0]) == pytest.approx(float(d_pre["close"].iloc[0])), (
        "同一时点的分钟前复权价必须等于日线前复权价（同基准）"
    )
    assert float(m_raw["close"].iloc[0]) == 10.90, "fq=None 时不复权"
    assert float(m_pre["close"].iloc[0]) != 10.90, "漏复权时会等于原始价"
    assert float(m_pre["close"].iloc[-1]) == pytest.approx(11.20), "基准日因子=1，价格不变"
    assert float(m_raw["close"].iloc[-1]) == 11.20


@pytest.mark.integration
def test_fq_zero_factor_is_treated_as_one(provider_factory):
    """因子为 0（脏数据/缺失的常见写法）必须按 1.0 处理，**不能把价格乘成 0**。

    直接相乘会把当天所有价格（含涨跌停价）清零，随后撮合与收益计算要么崩、
    要么静默产出 0 收益 —— 而库里出现 0 因子是很常见的。
    """
    case = "CASE date " + " ".join(f"WHEN '{d}' THEN {f}" for d, f in FQ_FACTORS.items()) + " END"
    sql = (
        f"UPDATE ashare_1d_stock SET adj_factor = {case} WHERE code='000001.SZ'",
        "UPDATE ashare_1d_stock SET adj_factor = 0 WHERE code='000001.SZ' AND date='20250103'",
    )
    p, _ = provider_factory(day="20250108", slot=240, sql=sql)
    pre = p.daily(["000001.SZ"], 3, ["close"], "pre", False, True)
    post = p.daily(["000001.SZ"], 3, ["close"], "post", False, True)

    assert _days(pre) == ["20250103", "20250106", "20250107"]
    assert list(pre["close"]) == pytest.approx(
        [10.80 / FQ_BASE, 11.00 * 1.2 / FQ_BASE, 10.90 * 1.3 / FQ_BASE]
    )
    assert list(post["close"]) == pytest.approx([10.80, 13.20, 14.17]), "0 因子按 1.0 处理"
    assert (post["close"] > 0).all() and (pre["close"] > 0).all(), "价格不得被因子 0 清零"


# ============================================================
# resources：平台探查
# ============================================================


class _MeminfoStub:
    """假的 ``/proc/meminfo``：只实现 ``_mem_linux`` 用到的那一个方法。"""

    def __init__(self, text: str) -> None:
        self._text = text

    def open(self, *args, **kwargs):
        return io.StringIO(self._text)


@pytest.mark.unit
def test_mem_linux_parses_meminfo(monkeypatch):
    """Linux 内存探测按 kB 换算成字节，且 MemAvailable 缺失时退回 MemTotal。

    这里最容易错的是**单位**（kB → B 少乘 1024）与**只认第一行**：
    真实 /proc/meminfo 的字段顺序并不保证，MemAvailable 可能出现在 MemTotal 之前。
    """
    text = "MemAvailable:   16384000 kB\nMemTotal:       32768000 kB\nMemFree:         1234567 kB\n"
    monkeypatch.setattr(R, "Path", lambda _p: _MeminfoStub(text))
    assert R._mem_linux() == (32768000 * 1024, 16384000 * 1024)

    monkeypatch.setattr(R, "Path", lambda _p: _MeminfoStub("MemTotal:       32768000 kB\n"))
    assert R._mem_linux() == (32768000 * 1024, 32768000 * 1024), "无 MemAvailable 时退回总量"

    monkeypatch.setattr(R, "Path", lambda _p: _MeminfoStub("MemFree: 1234 kB\n"))
    with pytest.raises(OSError):
        R._mem_linux()

    monkeypatch.setattr(R, "Path", lambda _p: _MeminfoStub("MemTotal: abc kB\n"))
    with pytest.raises(ValueError):
        R._mem_linux()


@pytest.mark.unit
def test_mem_macos_parses_vm_stat(monkeypatch):
    """macOS：总量取 sysctl，可用量用 vm_stat 的 free+inactive × 页大小。

    页大小必须从 vm_stat 输出里读（Apple Silicon 上是 16384，不是 4096），
    写死 4096 会让可用内存被低估 4 倍，进而把能跑的回测错误地挡在队列外。
    """
    vm = (
        "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
        "Pages free:                               12345.\n"
        "Pages inactive:                            6789.\n"
        "Pages speculative:                          100.\n"
    )
    monkeypatch.setattr(
        R.os,
        "popen",
        lambda cmd: io.StringIO("17179869184\n" if cmd == "sysctl -n hw.memsize" else vm),
    )
    assert R._mem_macos() == (17179869184, (12345 + 6789) * 16384)


@pytest.mark.unit
def test_mem_macos_fallbacks(monkeypatch):
    """vm_stat 不可用/无字段 → 退回总量一半；sysctl 失败或为 0 → 必须报错而不是返回 0。

    静默返回 0 总量比报错危险得多：可用内存按 0 算会让所有回测被判"内存不足"。
    """

    def _popen(mem: str, vm):
        def _p(cmd):
            if cmd.startswith("sysctl"):
                return io.StringIO(mem)
            if isinstance(vm, Exception):
                raise vm
            return io.StringIO(vm)

        return _p

    monkeypatch.setattr(R.os, "popen", _popen("0\n", ""))
    with pytest.raises(OSError):
        R._mem_macos()

    monkeypatch.setattr(R.os, "popen", _popen("17179869184\n", OSError("vm_stat 不存在")))
    assert R._mem_macos() == (17179869184, 17179869184 // 2), "vm_stat 失败 → 总量一半"

    # 缺 "page size of" 行 → 页大小退回 4096；free/inactive 全缺 → 再退回总量一半
    monkeypatch.setattr(R.os, "popen", _popen("17179869184\n", "Pages free: 10.\n"))
    assert R._mem_macos() == (17179869184, 10 * 4096), "缺页大小行时默认 4096"

    monkeypatch.setattr(R.os, "popen", _popen("17179869184\n", ""))
    assert R._mem_macos() == (17179869184, 17179869184 // 2), "没有任何页统计 → 总量一半"


class _FakeKernel32:
    def __init__(self, ret: int, total: int = 8 * GB, avail: int = 5 * GB) -> None:
        self._ret = ret
        self._total = total
        self._avail = avail

    def GlobalMemoryStatusEx(self, byref_obj) -> int:
        st = byref_obj._obj
        if self._ret:
            st.ullTotalPhys = self._total
            st.ullAvailPhys = self._avail
        return self._ret


class _FakeWindll:
    def __init__(self, ret: int, **kw) -> None:
        self.kernel32 = _FakeKernel32(ret, **kw)


@pytest.mark.unit
def test_mem_windows_reads_structure_fields(monkeypatch):
    """Windows 分支：从 MEMORYSTATUSEX 结构体里取 total/avail，失败必须抛 OSError。

    用假 ``windll`` 驱动，所以任何平台都能测；关键是结构体字段**取错位**（例如把
    AvailPhys 当 TotalPhys）时总量与可用量会颠倒，准入判定随之反着来。
    """
    monkeypatch.setattr(
        R.ctypes, "windll", _FakeWindll(1, total=8 * GB, avail=5 * GB), raising=False
    )
    assert R._mem_windows() == (8 * GB, 5 * GB)

    monkeypatch.setattr(R.ctypes, "windll", _FakeWindll(0), raising=False)
    with pytest.raises(OSError):
        R._mem_windows()


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="需要真实 Windows API")
@pytest.mark.unit
def test_mem_windows_real_api_plausible():
    """真机 Windows 上再验一次：总量/可用量必须是可信的正数且 avail <= total。"""
    total, avail = R._mem_windows()
    assert total > 1 * GB, f"物理内存总量不合理：{total}"
    assert 0 < avail <= total, f"可用 {avail} 与总量 {total} 矛盾"


@pytest.mark.unit
def test_probe_memory_dispatches_by_platform(monkeypatch):
    """平台分派：win32→Windows API，darwin→sysctl，其余→/proc/meminfo。"""
    for platform, fn, value in (
        ("linux", "_mem_linux", (1, 2)),
        ("darwin", "_mem_macos", (3, 4)),
        ("win32", "_mem_windows", (5, 6)),
    ):
        monkeypatch.setattr(R.sys, "platform", platform)
        monkeypatch.setattr(R, fn, lambda v=value: v)
        assert R.probe_memory() == value, f"{platform} 分派错误"

    monkeypatch.setattr(R.sys, "platform", "linux2")  # 老式 Linux 取值也必须落到 linux
    monkeypatch.setattr(R, "_mem_linux", lambda: (7, 8))
    assert R.probe_memory() == (7, 8)


@pytest.mark.unit
def test_probe_memory_falls_back_to_psutil_then_zero(monkeypatch):
    """探测失败时的降级链：本平台探测 → psutil → ``(0, 0)``。

    ``(0, 0)`` 是"探测不到"的约定值（上层据此**跳过**内存准入而不是判定不足），
    所以这条链子必须走完而不是抛异常 —— 否则没有 psutil 的机器连回测都启动不了。
    """

    def _boom():
        raise OSError("no /proc")

    monkeypatch.setattr(R.sys, "platform", "linux")
    monkeypatch.setattr(R, "_mem_linux", _boom)

    fake = types.SimpleNamespace(
        virtual_memory=lambda: types.SimpleNamespace(total=64 * GB, available=33 * GB)
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)
    assert R.probe_memory() == (64 * GB, 33 * GB), "psutil 兜底应被采用"

    monkeypatch.setitem(sys.modules, "psutil", None)  # `import psutil` 抛 ImportError
    assert R.probe_memory() == (0, 0), "全失败时返回 (0, 0) 而不是抛异常"


@pytest.mark.unit
def test_mem_used_pct_with_zero_total_is_not_zero_division():
    """探测失败时 mem_total=0：使用率应返回 0 而不是 ZeroDivisionError，
    ``describe()`` 也要能打印 —— 一份"资源探测失败导致回测启动即崩"的报告毫无用处。
    """
    s = R.ResourceSnapshot(cpu_count=4, mem_total=0, mem_available=0)
    assert s.mem_used_pct == 0.0
    assert "0 / 0 MB" in s.describe()

    norm = R.ResourceSnapshot(
        cpu_count=8,
        mem_total=100 * MB,
        mem_available=40 * MB,
        psutil_cpu_pct=12.5,
        disk_free=5 * GB,
    )
    assert norm.mem_used_pct == pytest.approx(60.0)
    text = norm.describe()
    assert "系统占用 12%" in text, "有 psutil 时才展示系统 CPU 占用"
    assert "磁盘可用 5 GB" in text


@pytest.mark.unit
def test_probe_reads_load_avg_psutil_and_disk(monkeypatch, tmp_path):
    """``probe()`` 的可选字段：loadavg（仅 POSIX）、psutil CPU 占用、磁盘可用量。

    这三个都是"锦上添花"的字段，写错不会让回测失败，只会让准入判定用错数据；
    缺了就必须是 None/0（表示"未知"），不能凭空造一个 0% 占用来骗过准入。
    """
    monkeypatch.setattr(R, "probe_memory", lambda: (64 * GB, 32 * GB))
    monkeypatch.setattr(R.os, "getloadavg", lambda: (1.25, 2.0, 3.0), raising=False)
    monkeypatch.setitem(
        sys.modules,
        "psutil",
        types.SimpleNamespace(cpu_percent=lambda interval=0.1: 33.0),
    )

    s = R.probe(str(tmp_path))
    assert (s.mem_total, s.mem_available, s.cpu_count) == (64 * GB, 32 * GB, os.cpu_count() or 1)
    assert s.load_avg == 1.25, "只取 1 分钟负载"
    assert s.psutil_cpu_pct == 33.0
    assert s.disk_free > 0

    # 无 getloadavg（Windows）/ 无 psutil → 字段为 None，而不是 0
    monkeypatch.delattr(R.os, "getloadavg", raising=False)
    monkeypatch.setitem(sys.modules, "psutil", None)
    s2 = R.probe(str(tmp_path))
    assert s2.load_avg is None and s2.psutil_cpu_pct is None
    assert "系统占用" not in s2.describe()

    # getloadavg 抛错（容器里常见）也必须容错
    def _boom():
        raise OSError("no loadavg")

    monkeypatch.setattr(R.os, "getloadavg", _boom, raising=False)
    assert R.probe(str(tmp_path)).load_avg is None

    # 磁盘路径不存在 / 未提供 → 0（"未知"，不参与准入判定）
    monkeypatch.setattr(R, "probe_memory", lambda: (0, 0))
    assert R.probe(str(tmp_path / "nope")).disk_free == 0
    empty = R.probe()
    assert empty.disk_free == 0 and empty.mem_used_pct == 0.0


# ============================================================
# resources：估算与准入判定
# ============================================================


def _snap(cpu=8, total=64 * GB, avail=48 * GB, disk=100 * GB):
    return R.ResourceSnapshot(cpu_count=cpu, mem_total=total, mem_available=avail, disk_free=disk)


def _cost(days=10, threads=2, mem=1 * GB, slots=2):
    return R.BacktestCost(days=days, threads=threads, est_mem_bytes=mem, est_cpu_slots=slots)


@pytest.mark.unit
def test_estimate_cost_caps_threads_to_cpu_minus_one(monkeypatch):
    """线程数必须封顶到 ``cpu_count-1``：否则单任务需求 > 总容量，
    ``used + need > cap`` 恒成立 → 该任务**永远排不上队**（活锁，不报错）。
    """
    monkeypatch.setattr(R.os, "cpu_count", lambda: 8)
    c = R.estimate_cost(days=250, threads=64, preload_mode="rolling", rolling_window=3)

    assert c.threads == 64, "原始请求值要留档"
    assert c.est_cpu_slots == 7, "占用槽必须封顶到 8-1"
    assert c.minute_budget_bytes == 3 * R.BYTES_PER_MINUTE_DAY, "rolling 只常驻窗口天数"
    assert c.est_mem_bytes == (
        R.BASE_OVERHEAD_BYTES + 3 * R.BYTES_PER_MINUTE_DAY + (200 * MB + 250 * 4 * MB)
    )
    assert R.decide(c, _snap(cpu=8), used_slots=0, used_count=0).ok, "封顶后单任务必须能起跑"


@pytest.mark.unit
def test_estimate_cost_all_mode_and_minute_budget_cap(monkeypatch):
    """preload=all 要按**整个区间**算常驻内存（长区间会爆），预算上限则要能压下来。"""
    monkeypatch.setattr(R.os, "cpu_count", lambda: 8)
    allm = R.estimate_cost(days=20, threads=2, preload_mode="all")
    assert allm.minute_budget_bytes == 20 * R.BYTES_PER_MINUTE_DAY
    assert allm.notes and "常驻" in allm.notes[0], "必须提示全量常驻的风险"

    capped = R.estimate_cost(days=20, threads=2, preload_mode="all", minute_budget_bytes=100 * MB)
    assert capped.minute_budget_bytes == 100 * MB
    assert capped.est_mem_bytes == (R.BASE_OVERHEAD_BYTES + 100 * MB + (200 * MB + 20 * 4 * MB))

    zero = R.estimate_cost(days=0, preload_mode="rolling", rolling_window=10)
    assert zero.minute_budget_bytes == R.BYTES_PER_MINUTE_DAY, "d=0 也要按 1 天算"


@pytest.mark.unit
def test_decide_ok_when_resources_are_ample():
    """资源充足 → ``ok=True`` 且不该给等待暗示（否则调度器会白等）。"""
    adm = R.decide(_cost(mem=1 * GB, slots=2), _snap(cpu=8, avail=48 * GB))
    assert (adm.ok, adm.reason, adm.detail, adm.wait_hint_sec) == (True, "ok", "资源充足", 0.0)


@pytest.mark.unit
def test_decide_queues_on_memory_and_reports_numbers():
    """内存不足 → 排队（不是拒绝），并给出**可读数字**与更长的等待暗示。

    detail 里的"已扣 15% 预留"和 MB 数是用户唯一的排障线索：不给数字，
    用户只能看到"内存不足"却不知道差多少、该关掉什么。
    """
    adm = R.decide(_cost(mem=8 * GB, slots=2), _snap(cpu=8, total=16 * GB, avail=6 * GB))
    assert (adm.ok, adm.reason) == (False, "memory")
    assert adm.wait_hint_sec == 30.0, "内存类等待要给更长的重试间隔（别的任务在释放）"
    assert "15%" in adm.detail and "预留" in adm.detail
    assert "MB" in adm.detail and "8,192" in adm.detail, f"应给出需求 MB 数：{adm.detail}"


@pytest.mark.unit
def test_decide_parallel_and_cpu_are_independent_dimensions():
    """并发数（几个回测）与 CPU 槽（总共几个线程）是**两个独立维度**：
    任一超限即排队，且并发优先报出。混成一个数会让单任务线程多就永久排队。
    """
    cost = _cost(mem=1 * GB, slots=2)
    par = R.decide(cost, _snap(cpu=8), used_slots=0, used_count=4, max_parallel=4)
    assert (par.ok, par.reason, par.wait_hint_sec) == (False, "parallel", 20.0)
    assert "并发已达上限" in par.detail and "4" in par.detail

    cpu = R.decide(cost, _snap(cpu=4), used_slots=2, used_count=0)
    assert (cpu.ok, cpu.reason) == (False, "cpu"), "默认槽位上限 = 核数-1 = 3"
    assert "留 1 核给系统" in cpu.detail and "4 核" in cpu.detail

    # 两个维度同时超限时，先报并发（顺序即优先级，不能因为内存/CPU 数字大就改报）
    both = R.decide(
        _cost(mem=64 * GB, slots=2),
        _snap(cpu=4, total=16 * GB, avail=0),
        used_slots=99,
        used_count=99,
        max_parallel=1,
    )
    assert both.reason == "parallel", f"优先级错误：{both.reason}"


@pytest.mark.unit
def test_decide_skips_memory_check_when_probe_failed():
    """``mem_total == 0`` 表示**探测失败**，此时必须放行而不是判"内存不足"。

    若把 0 总量当成"内存为 0"，所有回测都会被无限期排队 —— 一台探测不支持的机器
    会完全无法使用（而且日志里只会说"内存不足"，指向完全错误的方向）。
    """
    adm = R.decide(_cost(mem=64 * GB, slots=2), _snap(cpu=8, total=0, avail=0))
    assert adm.ok, f"探测失败不应阻塞：{adm.detail}"


@pytest.mark.unit
def test_decide_disk_guard_only_when_known():
    """磁盘：< 2GB 排长队；0 表示"未知/未探测"，不参与判定。"""
    low = R.decide(_cost(), _snap(cpu=8, disk=1 * GB))
    assert (low.ok, low.reason, low.wait_hint_sec) == (False, "disk", 60.0)
    assert "1.0 GB" in low.detail and "2 GB" in low.detail

    unknown = R.decide(_cost(), _snap(cpu=8, disk=0))
    assert unknown.ok, "未探测磁盘（0）不能当成磁盘不足"

    edge = R.decide(_cost(), _snap(cpu=8, disk=2 * GB))
    assert edge.ok, "恰好 2GB 应放行（阈值是 < 2GB）"
