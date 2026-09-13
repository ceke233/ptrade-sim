"""长区间冒烟测试：跨 14 个月的完整回测。

**为什么需要这个文件**：项目历史上出现过**两次**同一类缺陷 ——
代码路径只在「数据量够大 / 区间够长」时才执行，而回归验证一律用 8 天区间：

1. ``compute_metrics`` 的 ``mr.kurt()``（守卫 ``len(mr) > 3``）→ polars 没有该方法，
   长回测抛 AttributeError，**不产出 summary.json**；
2. ``history._price_cache``（守卫 ``codes > 500``）只写不清 → 长回测内存无界增长。

两次都不是「没测到那行代码」，而是**没测到那个量级**。所以本文件刻意构造
**跨 14 个月 / 2 个自然年**的数据，让所有「按量短路」的分支都真正执行一次。

**为什么不用 ``tiny_db`` 夹具**：它只有 5 个交易日，正是漏掉这类缺陷的原因。
这里自建一个 14 个月的合成库；模块级缓存，只建一次。

**速度**：日线模式 + 3 只股票 + 约 300 个交易日，整个文件应在数秒内跑完。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb
import pytest
from tests.conftest import dc_columns

pytestmark = pytest.mark.integration

#: 跨 14 个月、2 个自然年（2024-01 ~ 2025-02），确保 annual_returns 有多组
START, END = "20240102", "20250228"
CODES = ["000001.SZ", "600000.SS", "000002.SZ"]
BASE = {"000001.SZ": 10.0, "600000.SS": 8.0, "000002.SZ": 5.0}


def _trade_days() -> list[str]:
    """20240102~20250228 的工作日（不剔节假日 —— 日历是自洽的即可）。"""
    d0 = dt.date(2024, 1, 2)
    d1 = dt.date(2025, 2, 28)
    out, d = [], d0
    while d <= d1:
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
        d += dt.timedelta(days=1)
    return out


def _close(code: str, i: int) -> float:
    """确定性的价格序列（有涨有跌，避免零方差）。"""
    base = BASE[code]
    return round(base * (1.0 + 0.0009 * i + 0.02 * ((i % 17) - 8) / 8), 2)


@pytest.fixture(scope="module")
def long_db(tmp_path_factory) -> Path:
    """14 个月 × 3 只股票的合成库（模块级，只建一次）。"""
    db = tmp_path_factory.mktemp("longdb") / "long.duckdb"
    days = _trade_days()
    con = duckdb.connect(str(db))

    con.execute("CREATE TABLE ashare_calendar (date VARCHAR)")
    con.executemany("INSERT INTO ashare_calendar VALUES (?)", [(d,) for d in days])

    sb_cols = dc_columns("ashare_stock_basic")
    con.execute(
        "CREATE TABLE ashare_stock_basic (" + ", ".join(f'"{c}" VARCHAR' for c in sb_cols) + ")"
    )
    con.executemany(
        f"INSERT INTO ashare_stock_basic VALUES ({','.join('?' * len(sb_cols))})",
        [
            tuple(
                {
                    "code": c,
                    "name": f"股票{c[:6]}",
                    "market": "主板",
                    "list_date": "19910403",
                    "delist_date": None,
                    "list_status": "L",
                }.get(col)
                for col in sb_cols
            )
            for c in CODES
        ],
    )

    st_cols = dc_columns("ashare_1d_stock")
    con.execute(
        "CREATE TABLE ashare_1d_stock ("
        + ", ".join(
            f'"{c}" VARCHAR' if c in ("code", "date", "name") else f'"{c}" DOUBLE' for c in st_cols
        )
        + ")"
    )
    rows = []
    for i, ds in enumerate(days):
        for c in CODES:
            cl = _close(c, i)
            pc = _close(c, i - 1) if i else cl
            rows.append(
                tuple(
                    {
                        "code": c,
                        "date": ds,
                        "open": cl,
                        "high": cl * 1.01,
                        "low": cl * 0.99,
                        "close": cl,
                        "volume": 1_000_000.0,
                        "money": cl * 1_000_000.0,
                        "preclose": pc,
                        "adj_factor": 1.0,
                        "name": f"股票{c[:6]}",
                        "is_st": 0.0,
                        "is_delisted": 0.0,
                    }.get(col)
                    for col in st_cols
                )
            )
    con.executemany(f"INSERT INTO ashare_1d_stock VALUES ({','.join('?' * len(st_cols))})", rows)

    ix_cols = dc_columns("ashare_1d_index")
    con.execute(
        "CREATE TABLE ashare_1d_index ("
        + ", ".join(f'"{c}" VARCHAR' if c in ("code", "date") else f'"{c}" DOUBLE' for c in ix_cols)
        + ")"
    )
    ix = []
    for i, ds in enumerate(days):
        lvl = 4000.0 + i * 1.5
        ix.append(
            tuple(
                {
                    "code": "000300.SS",
                    "date": ds,
                    "open": lvl,
                    "high": lvl * 1.01,
                    "low": lvl * 0.99,
                    "close": lvl,
                    "volume": 1e9,
                    "money": 1e12,
                    "preclose": lvl - 1.5,
                }.get(col)
                for col in ix_cols
            )
        )
    con.executemany(f"INSERT INTO ashare_1d_index VALUES ({','.join('?' * len(ix_cols))})", ix)
    con.close()
    return db


@pytest.fixture(scope="module")
def long_engine(long_db, tmp_path_factory):
    """在 14 个月的库上跑一次完整回测（模块级缓存）。"""
    from ptrade_sim.runtime import BacktestEngine

    out = tmp_path_factory.mktemp("longout")
    sd = out / "s"
    sd.mkdir()
    sp = sd / "strategy.py"
    sp.write_text(
        "def initialize(context):\n"
        "    set_benchmark('000300.SS')\n"
        "    set_universe(['000001.SZ', '600000.SS', '000002.SZ'])\n"
        "    run_daily(context, buy, time='09:31')\n"
        "\n"
        "def buy(context):\n"
        "    if len(context.portfolio.positions) >= 2:\n"
        "        return\n"
        "    for c in ['000001.SZ', '600000.SS']:\n"
        "        if c not in context.portfolio.positions:\n"
        "            order_value(c, 200000)\n",
        encoding="utf-8",
    )
    cfg = {
        "db_path": str(long_db),
        "start_date": f"{START[:4]}-{START[4:6]}-{START[6:]}",
        "end_date": f"{END[:4]}-{END[4:6]}-{END[6:]}",
        "capital_base": 1_000_000,
        "benchmark": "000300.SS",
        "frequency": "daily",
        "preload": {"mode": "rolling", "rolling_window_days": 5, "threads": 2},
        "queue": {"enabled": False},
    }
    e = BacktestEngine(cfg, str(sp), out / "run")
    e.run()
    return e


# ============================================================
# 回测本身
# ============================================================


def test_long_backtest_completes_and_trades(long_engine):
    """14 个月回测必须跑完并真的成交（否则后面的指标断言都没意义）。"""
    daily = long_engine.daily_stats_frame()
    assert daily.height >= 250, f"应覆盖约 300 个交易日，实际 {daily.height}"
    assert long_engine.trades_frame().height > 0, "应有成交"


def test_long_backtest_spans_multiple_months_and_years(long_engine):
    """跨 >3 个月、>1 个自然年 —— 这正是历史缺陷漏测的量级。"""
    m = _metrics(long_engine)
    assert len(m["monthly_returns"]) > 12, f"应跨 >12 个月，实际 {len(m['monthly_returns'])}"
    assert len(m["annual_returns"]) >= 2, f"应跨 ≥2 个自然年，实际 {list(m['annual_returns'])}"


def test_kurtosis_and_skew_are_really_computed(long_engine):
    """**核心回归**：>3 个月时 kurt/skew 必须走真实计算，而不是 0.0 兜底。

    历史缺陷：``mr.kurt()`` 在 polars 上不存在 → 长回测抛 AttributeError，
    整个 run 死掉、不产出 summary.json。若哪天它又变回 0.0，说明守卫被绕过。
    """
    ms = _metrics(long_engine)["monthly_stats"]
    assert isinstance(ms["kurt"], float) and isinstance(ms["skew"], float)
    # 真实计算的值几乎不可能同时为 0（合成数据有涨有跌）
    assert ms["kurt"] != 0.0 or ms["skew"] != 0.0, (
        f"kurt/skew 都退化成 0.0 —— 可能没走真实计算路径：{ms}"
    )


def test_monthly_metrics_match_pandas(long_engine):
    """本实现的月度统计必须与 pandas 一致（它是 polars 迁移期的兼容替代）。"""
    import numpy as np
    import pandas as pd

    m = _metrics(long_engine)
    mr = pd.Series(list(m["monthly_returns"].values()))
    if len(mr) > 3:
        assert abs(m["monthly_stats"]["kurt"] - float(mr.kurt())) < 1e-9, (
            f"kurt 偏差：{m['monthly_stats']['kurt']} vs {mr.kurt()}"
        )
    if len(mr) > 2:
        assert abs(m["monthly_stats"]["skew"] - float(mr.skew())) < 1e-9, (
            f"skew 偏差：{m['monthly_stats']['skew']} vs {mr.skew()}"
        )
    assert np.isfinite(m["monthly_stats"]["std"])


def test_per_day_caches_are_bounded(long_engine):
    """长回测跑完后，按日缓存不得累积。

    历史缺陷：``history._price_cache`` 只写不清（同类问题在 >500 只取数时触发）。
    """
    assert len(long_engine.history._price_cache) <= 1, (
        f"{long_engine.daily_stats_frame().height} 天后仍有 "
        f"{len(long_engine.history._price_cache)} 条价格缓存 —— 应逐日清空"
    )
    assert len(long_engine._name_cache) <= 3, "名称缓存应逐日清空"


def test_metrics_have_no_nan(long_engine):
    """核心指标不得为 NaN —— 长回测最容易在边界（无交易月、基准对齐）渗出 NaN。"""
    import math

    m = _metrics(long_engine)
    for k in (
        "total_return",
        "annual_return",
        "sharpe",
        "max_drawdown",
        "win_rate",
        "final_value",
        "benchmark_return",
    ):
        v = m.get(k)
        assert v is not None, f"{k} 缺失"
        assert not (isinstance(v, float) and math.isnan(v)), f"{k} 为 NaN"
    assert m["trade_count"] > 0


def test_annual_returns_cover_every_year(long_engine):
    """每个出现过的自然年都应有一组年度收益，且策略/基准都在。"""
    m = _metrics(long_engine)
    years = {d[:4] for d in long_engine.feed.range_days}
    assert set(m["annual_returns"]) == years, (
        f"年度收益缺年：{sorted(years - set(m['annual_returns']))}"
    )
    for y, rec in m["annual_returns"].items():
        assert "strategy" in rec and "benchmark" in rec, f"{y} 缺字段：{sorted(rec)}"


def _metrics(engine) -> dict:
    """按 CLI 的口径算指标（与真实运行同一条路径）。"""
    from ptrade_sim.runtime import compute_metrics

    return compute_metrics(
        engine.daily_stats_frame(),
        engine.trades_frame(),
        engine.capital_base,
        {"benchmark": engine.benchmark},
    )
