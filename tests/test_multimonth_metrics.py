"""`compute_metrics` 的多月份路径回归测试。

**为什么单列一个文件**：这里守着两个只有「回测跨度 > 3 个月」时才执行的代码路径，
而项目历史上的回归验证**一律用 8 天区间** —— 于是它们在 polars 迁移后长期带病：

1. ``mr.kurt()``：polars **没有** ``kurt()``（pandas 有）→ 抛 AttributeError。
   它位于 ``compute_metrics`` 的最后一步，异常会让整个 run 死掉、
   **不产出 ``summary.json``**（看板里该 run 显示为「数据缺失」）。
2. ``mr.skew()``：polars 默认 ``bias=True``（有偏），pandas 无偏 →
   静默给出不同数值，指标与历史结果不一致。

两者的共同特征是**短区间不会触发**：``len(mr) > 3`` 的守卫让它们被短路。
所以这个文件刻意构造 ≥4 个月的数据。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import polars as pl
import pytest

from ptrade_sim.runtime import _excess_kurtosis, compute_metrics

pytestmark = pytest.mark.unit


def _daily_frame(n_days: int, start: str = "2020-01-02") -> pl.DataFrame:
    """造 n_days 个交易日的 daily_stats（日期连续，跨多个月）。"""
    import datetime as dt

    d0 = dt.date.fromisoformat(start)
    dates, vals = [], []
    v = 1_000_000.0
    for i in range(n_days):
        d = d0 + dt.timedelta(days=i)
        dates.append(d.isoformat())
        v *= 1.0 + (0.0008 if i % 3 else -0.0005)  # 有涨有跌，产生非零方差
        vals.append(v)
    return pl.DataFrame(
        {
            "date": dates,
            "total_value": vals,
            "cash": [x * 0.5 for x in vals],
            "positions_value": [x * 0.5 for x in vals],
            "benchmark_close": [4000.0 + i for i in range(n_days)],
            "daily_return": [0.0] + [vals[i] / vals[i - 1] - 1 for i in range(1, n_days)],
            "cum_return": [0.0] * n_days,
            "drawdown": [0.0] * n_days,
            "trades_count": [0] * n_days,
            "commission": [0.0] * n_days,
        }
    )


def test_kurtosis_matches_pandas_across_distributions():
    """``_excess_kurtosis`` 必须与 pandas 逐位一致（多种分布 × 多种样本量）。

    polars 没有 ``kurt()``，这个自实现是唯一替代 —— 数值对不上就等于
    悄悄改变了对外指标。
    """
    rng = np.random.default_rng(7)
    for n in (4, 5, 12, 72, 200):
        for data in (rng.normal(0.01, 0.05, n), rng.lognormal(0, 0.3, n)):
            mine = _excess_kurtosis(data)
            expected = float(pd.Series(data).kurt())
            assert abs(mine - expected) < 1e-9, f"n={n} 峰度与 pandas 不一致：{mine} vs {expected}"


def test_kurtosis_degenerate_inputs_return_zero():
    """零方差与样本不足都应返回 0.0（不把 NaN 渗进 summary.json）。"""
    assert _excess_kurtosis(np.full(10, 0.02)) == 0.0, "常量数组（浮点残差非零）"
    assert _excess_kurtosis(np.zeros(10)) == 0.0
    assert _excess_kurtosis(np.array([0.01, 0.02, 0.03])) == 0.0, "n<4"
    assert _excess_kurtosis(np.array([])) == 0.0


def test_skew_is_unbiased_like_pandas():
    """polars 的 skew 默认 ``bias=True``，必须显式 ``bias=False`` 才与 pandas 一致。"""
    rng = np.random.default_rng(11)
    data = rng.normal(0.01, 0.05, 72)
    assert abs(float(pl.Series(data).skew()) - float(pd.Series(data).skew())) > 1e-6, (
        "前提校验：polars 默认 skew 本就与 pandas 不同（否则本测试失去意义）"
    )
    assert abs(float(pl.Series(data).skew(bias=False)) - float(pd.Series(data).skew())) < 1e-12


def test_multimonth_metrics_do_not_raise_and_include_kurt():
    """**核心回归**：>3 个月的回测必须能算出指标，且含 kurt。

    这正是当年漏测的场景：8 天区间下 ``len(mr) > 3`` 短路，
    ``mr.kurt()`` 那行永不执行，于是 AttributeError 一直没暴露。
    """
    daily = _daily_frame(120)  # 约 4 个月
    m = compute_metrics(daily, pl.DataFrame(), 1_000_000, {"benchmark": "000300.SS"})
    assert "monthly_stats" in m
    ms = m["monthly_stats"]
    assert "kurt" in ms and "skew" in ms, f"月度统计缺字段：{sorted(ms)}"
    assert isinstance(ms["kurt"], float) and isinstance(ms["skew"], float)
    # 跨了 4 个月，kurt 走的是真实计算路径而不是 0.0 兜底
    assert len(m["monthly_returns"]) > 3, f"应跨 >3 个月，实际 {list(m['monthly_returns'])}"


def test_multimonth_skew_kurt_match_pandas_on_same_data():
    """同一份月度收益，本实现的 skew/kurt 应与 pandas 一致。"""
    daily = _daily_frame(400)  # 跨约 13 个月
    m = compute_metrics(daily, pl.DataFrame(), 1_000_000, {})
    mr = pd.Series(list(m["monthly_returns"].values()))
    if len(mr) > 3:
        assert abs(m["monthly_stats"]["kurt"] - float(mr.kurt())) < 1e-9, (
            f"kurt 偏差：{m['monthly_stats']['kurt']} vs {mr.kurt()}"
        )
    if len(mr) > 2:
        assert abs(m["monthly_stats"]["skew"] - float(mr.skew())) < 1e-9, (
            f"skew 偏差：{m['monthly_stats']['skew']} vs {mr.skew()}"
        )
