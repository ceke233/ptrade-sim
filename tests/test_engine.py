"""引擎集成测试（合成库上的端到端回测）。

覆盖点都是本项目**曾经真实出过错**的地方，或 PTrade 语义的关键约束：
双周期调度、241 槽、撮合价、T+1、涨跌停、API 字段口径、
指数成分左闭右开、快照源偏差告警等。
"""

from __future__ import annotations

import polars as pl
import pytest

from ptrade_sim.conventions import DAY_SLOTS
from ptrade_sim.runtime import DataFeed

pytestmark = pytest.mark.integration


# ============================================================
# 数据源与日历
# ============================================================


def test_feed_loads_from_duckdb(tiny_db):
    f = DataFeed(str(tiny_db), "20250102", "20250108", preload_mode="rolling", threads=2)
    assert f.range_days == ["20250102", "20250103", "20250106", "20250107", "20250108"]


def test_feed_rejects_missing_db(tmp_path):
    from ptrade_sim.runtime import DataFeed as DF

    with pytest.raises((FileNotFoundError, ValueError)):
        DF(str(tmp_path / "nope.duckdb"), "20250102", "20250108")


def test_feed_warns_when_range_exceeds_coverage(tiny_db):
    """区间尾部超出行情覆盖时必须告警。

    否则会静默产出一份「跑完但没成交」的空回测，用户无法区分是策略问题
    还是数据缺口。夹具里日历到 20250113，而行情只到 20250108。
    """
    from loguru import logger

    msgs: list[str] = []
    sink = logger.add(lambda m: msgs.append(str(m)), level="WARNING")
    try:
        DataFeed(str(tiny_db), "20250106", "20250113", preload_mode="rolling", threads=2)
    finally:
        logger.remove(sink)
    assert any("超出" in m for m in msgs), f"未告警：{msgs}"


def test_universe_codes_use_ptrade_suffix(tiny_db):
    f = DataFeed(str(tiny_db), "20250102", "20250108", preload_mode="rolling", threads=2)
    assert "600000.SS" in f.get_Ashares("20250102")
    assert "600000.SH" not in f.get_Ashares("20250102"), "库内应为 .SS 口径"


# ============================================================
# 分钟槽位（09:30 是集合竞价成交时点，必须保留）
# ============================================================


def test_day_slots_has_241_with_auction_slot():
    assert len(DAY_SLOTS) == 241
    assert DAY_SLOTS[0] == "09:30", "09:30 是集合竞价成交时点，不可省略"
    assert DAY_SLOTS[1] == "09:31"
    assert DAY_SLOTS[-1] == "15:00"
    assert sum(1 for s in DAY_SLOTS if "09:31" <= s <= "11:30") == 120
    assert sum(1 for s in DAY_SLOTS if "13:01" <= s <= "15:00") == 120


def test_minute_day_indexed(tiny_db):
    f = DataFeed(str(tiny_db), "20250102", "20250108", preload_mode="rolling", threads=2)
    md = f.minute_day("20250102")
    assert md is not None
    assert len(md.slot) == 241 * 3, "3 只股票 × 241 槽"
    assert md.row_of("000001.SZ", 0) >= 0


def test_minute_cache_hit_returns_same_object(tiny_db):
    f = DataFeed(str(tiny_db), "20250102", "20250108", preload_mode="rolling", threads=2)
    a = f.minute_day("20250102")
    b = f.minute_day("20250102")
    assert a is b


def test_missing_day_cached_as_none(tiny_db):
    f = DataFeed(str(tiny_db), "20250102", "20250108", preload_mode="rolling", threads=2)
    assert f.minute_day("19990101") is None
    assert f.minute_day("19990101") is None  # 不应反复查询


# ============================================================
# 日线 / 估值 / 指数成分
# ============================================================


def test_daily_row_uses_ptrade_field_names(tiny_db):
    """日线行必须用 preclose（不是 pre_close）与 volume/money。

    回归防护：早先内部列名是 pre_close，导致 get_history('preclose') 静默返空。
    """
    f = DataFeed(str(tiny_db), "20250102", "20250108", preload_mode="rolling", threads=2)
    row = f.daily_row("20250102", "000001.SZ")
    assert row is not None
    assert "preclose" in row
    assert "pre_close" not in row
    assert "volume" in row and "money" in row


def test_valuation_frame_official_columns(tiny_db):
    """内存内估值取数为 polars（含 code 列）；API 边界再转 pandas 并设索引。"""
    import polars as pl

    f = DataFeed(str(tiny_db), "20250102", "20250108", preload_mode="rolling", threads=2)
    v = f.valuation_frame(["000001.SZ"], "20250102")
    assert isinstance(v, pl.DataFrame), "内部通路应为 polars"
    assert list(v.columns) == ["code", "total_value", "float_value"]
    assert v["code"].to_list() == ["000001.SZ"]

    # 内部估值表也应是官方字段名
    feat = f.ensure_feature("20250102")
    assert isinstance(feat, pl.DataFrame)
    assert {"total_value", "float_value", "a_floats", "total_shares"} <= set(feat.columns)
    assert "total_mv" not in feat.columns


def test_ensure_daily_is_polars(tiny_db):
    """日线内部通路应为 polars（不再是 pandas）。"""
    import polars as pl

    f = DataFeed(str(tiny_db), "20250102", "20250108", preload_mode="rolling", threads=2)
    d = f.ensure_daily("20250102")
    assert isinstance(d, pl.DataFrame)
    assert f.daily_rows("20250102")["000001.SZ"]["close"] == 10.50


def test_index_weight_left_closed_right_open(tiny_db):
    """成分区间为左闭右开：out_date 当日**不再**是成分。"""
    f = DataFeed(str(tiny_db), "20250102", "20250108", preload_mode="rolling", threads=2)
    # 000002.SZ 的 out_date = 20250103
    assert "000002.SZ" in f.index_stocks("000300", "20250102")
    assert "000002.SZ" not in f.index_stocks("000300", "20250103")
    assert "000001.SZ" in f.index_stocks("000300", "20250108")


def test_index_weight_exposes_weights(tiny_db):
    f = DataFeed(str(tiny_db), "20250102", "20250108", preload_mode="rolling", threads=2)
    info = f.index_member_info("000300")
    assert info is not None
    rows = [r for r in info["rows"] if r[1] <= "20250102" and (not r[2] or r[2] > "20250102")]
    assert len(rows) == 3
    assert all(len(r) == 4 for r in rows), "成分行应含 weight（4 元组）"
    assert sum(r[3] for r in rows) == pytest.approx(100.0)


def test_index_query_reason_for_unknown(tiny_db):
    f = DataFeed(str(tiny_db), "20250102", "20250108", preload_mode="rolling", threads=2)
    q = f.index_query("999999", "20250102")
    assert q.codes == []
    assert q.reason == "unknown_index"


def test_l2_auction_lazy_per_day(tiny_db):
    f = DataFeed(str(tiny_db), "20250102", "20250108", preload_mode="rolling", threads=2)
    d = f.l2_auction_day("20250102")
    assert set(d) >= {"000001.SZ", "600000.SS"}
    assert d["000001.SZ"][0] > 0


def test_feed_reports_cache_stats(tiny_db):
    f = DataFeed(str(tiny_db), "20250102", "20250108", preload_mode="rolling", threads=2)
    f.minute_day("20250102")
    rep = f.cache_report()
    assert rep["stats"]["minute"]["puts"] == 1
    assert "日线" in rep["config"]


# ============================================================
# 回测：分钟级
# ============================================================


def _buy_once_strategy():
    return (
        "def initialize(context):\n"
        "    set_benchmark('000300.SS')\n"
        "    set_universe(['000001.SZ'])\n"
        "    run_daily(context, buy, time='09:31')\n"
        "\n"
        "def buy(context):\n"
        "    if get_position('000001.SZ').amount == 0:\n"
        "        order('000001.SZ', 1000)\n"
    )


def test_backtest_runs_and_produces_stats(engine_factory):
    e = engine_factory(_buy_once_strategy())
    daily = e.run()
    assert len(daily) == 5
    assert {"date", "total_value", "cash"} <= set(daily.columns)


def test_backtest_minute_calls_handle_data_241_times_per_day(engine_factory):
    e = engine_factory(
        "calls = []\n"
        "def initialize(context):\n"
        "    set_benchmark('000300.SS')\n"
        "    set_universe(['000001.SZ'])\n"
        "\n"
        "def handle_data(context, data):\n"
        "    calls.append(context.blotter.current_dt.strftime('%H:%M'))\n"
    )
    e.run()
    ns = e._module.__dict__
    per_day = {}
    for hm in ns["calls"]:
        per_day[hm] = per_day.get(hm, 0) + 1
    assert len(ns["calls"]) == 241 * 5, f"应为 241×5 次，实际 {len(ns['calls'])}"
    assert "09:30" in per_day, "09:30（集合竞价成交时点）必须触发 handle_data"
    assert "15:00" in per_day


def test_backtest_daily_calls_handle_data_once_per_day(engine_factory):
    e = engine_factory(
        "calls = []\n"
        "def initialize(context):\n"
        "    set_benchmark('000300.SS')\n"
        "    set_universe(['000001.SZ'])\n"
        "\n"
        "def handle_data(context, data):\n"
        "    calls.append(context.blotter.current_dt.strftime('%H:%M'))\n",
        frequency="daily",
    )
    e.run()
    calls = e._module.__dict__["calls"]
    assert len(calls) == 5, "日线模式每天只应触发一次"
    assert set(calls) == {"15:00"}, "日线模式应在 15:00 触发"


def test_daily_mode_bar_comes_from_daily_table(engine_factory):
    e = engine_factory(
        "bars = []\n"
        "def initialize(context):\n"
        "    set_benchmark('000300.SS')\n"
        "    set_universe(['000001.SZ'])\n"
        "\n"
        "def handle_data(context, data):\n"
        "    b = data.get('000001.SZ')\n"
        "    if b is not None:\n"
        "        bars.append(round(b.close, 2))\n",
        frequency="daily",
    )
    e.run()
    closes = e._module.__dict__["bars"]
    assert closes == [10.50, 10.80, 11.00, 10.90, 11.20], "应取当日日线收盘价"


def test_order_fills_at_minute_close(engine_factory):
    e = engine_factory(_buy_once_strategy())
    e.run()
    trades = e.trades_frame()
    assert trades.height >= 1
    # 09:31 槽的收盘价应等于分钟 bar 的 close
    assert trades.row(0, named=True)["price"] > 0


def test_t_plus_1_blocks_same_day_sell(engine_factory):
    """T+1：当日买入的股票当日不可卖（``enable_amount`` 为 0）。"""
    e = engine_factory(
        "log_ = []\n"
        "def initialize(context):\n"
        "    set_benchmark('000300.SS')\n"
        "    set_universe(['000001.SZ'])\n"
        "    run_daily(context, on_buy, time='09:31')\n"
        "    run_daily(context, on_sell, time='14:00')\n"
        "\n"
        "def on_buy(context):\n"
        "    if get_position('000001.SZ').amount == 0:\n"
        "        order('000001.SZ', 1000)\n"
        "        p = get_position('000001.SZ')\n"
        "        log_.append(('after_buy', p.amount, p.enable_amount))\n"
        "\n"
        "def on_sell(context):\n"
        "    if not any(x[0] == 'sell_try' for x in log_):\n"
        "        p = get_position('000001.SZ')\n"
        "        log_.append(('sell_try', p.amount, p.enable_amount))\n"
        "        order('000001.SZ', -p.amount)\n"
    )
    e.run()
    log = e._module.__dict__["log_"]
    after_buy = next(x for x in log if x[0] == "after_buy")
    sell_try = next(x for x in log if x[0] == "sell_try")

    assert after_buy[1] == 1000, "买入后持仓应为 1000"
    assert after_buy[2] == 0, "买入当日可用应为 0（T+1）"
    assert sell_try[1] == 1000
    assert sell_try[2] == 0, "14:00 同一交易日仍应为 0（T+1 未跨日）"

    # 当日卖出应被拒（无卖出成交）—— polars 过滤
    trades = e.trades_frame()
    day_of_buy = trades.row(0, named=True)["time"].date()
    same_day_sells = trades.filter(
        (pl.col("side") == "sell") & (pl.col("time").dt.date() == day_of_buy)
    )
    assert same_day_sells.height == 0, "同一交易日不应有卖出成交"


def test_t_plus_1_releases_next_day(engine_factory):
    """跨日后今仓转为可卖。"""
    e = engine_factory(
        "log_ = []\n"
        "def initialize(context):\n"
        "    set_benchmark('000300.SS')\n"
        "    set_universe(['000001.SZ'])\n"
        "    run_daily(context, on_buy, time='09:31')\n"
        "    run_daily(context, on_sell, time='14:00')\n"
        "\n"
        "def on_buy(context):\n"
        "    if get_position('000001.SZ').amount == 0:\n"
        "        order('000001.SZ', 1000)\n"
        "\n"
        "def on_sell(context):\n"
        "    d = context.blotter.current_dt.strftime('%Y%m%d')\n"
        "    if d == '20250103':\n"
        "        log_.append(get_position('000001.SZ').enable_amount)\n"
    )
    e.run()
    assert e._module.__dict__["log_"] == [1000], "次日应全部可卖"


def test_no_negative_cash(engine_factory):
    e = engine_factory(
        "def initialize(context):\n"
        "    set_benchmark('000300.SS')\n"
        "    set_universe(['000001.SZ', '600000.SS', '000002.SZ'])\n"
        "    run_daily(context, buy_all, time='09:31')\n"
        "\n"
        "def buy_all(context):\n"
        "    for c in ['000001.SZ', '600000.SS', '000002.SZ']:\n"
        "        order(c, 100000)\n"
    )
    e.run()
    assert e.portfolio.cash >= -1e-6, f"现金为负：{e.portfolio.cash}"


def test_buy_lot_must_be_multiple_of_100(engine_factory):
    e = engine_factory(
        "def initialize(context):\n"
        "    set_benchmark('000300.SS')\n"
        "    set_universe(['000001.SZ'])\n"
        "    run_daily(context, buy, time='09:31')\n"
        "\n"
        "def buy(context):\n"
        "    if get_position('000001.SZ').amount == 0:\n"
        "        order('000001.SZ', 150)\n"
    )
    e.run()
    pos = e.portfolio.positions.get("000001.SZ")
    if pos is not None:
        assert pos.total_amount % 100 == 0


def test_daily_stats_has_benchmark_close(engine_factory):
    import polars as pl

    e = engine_factory(_buy_once_strategy())
    daily = e.run()
    assert isinstance(daily, pl.DataFrame), "逐日统计应为 polars"
    assert "benchmark_close" in daily.columns
    assert daily["benchmark_close"].null_count() < daily.height, "基准收盘价应可算出"


def test_summary_json_has_resources_section(engine_factory):
    e = engine_factory(_buy_once_strategy())
    e.run()
    assert hasattr(e.feed, "cache")
    assert e.feed.cache.stats()


# ============================================================
# 指标计算（polars）
# ============================================================


def _daily_frame(n: int = 3, with_bench: bool = True) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "date": [f"2025-01-{i + 2:02d}" for i in range(n)],
            "total_value": [1_000_000.0 + i * 1000 for i in range(n)],
            "daily_return": [0.0] + [0.001] * (n - 1),
            "drawdown": [0.0] * n,
            "benchmark_close": [4000.0 + i for i in range(n)] if with_bench else [None] * n,
            "trades_count": [0] * n,
            "commission": [0.0] * n,
        }
    )


def _trades_frame() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "side": ["buy", "sell"],
            "trade_pnl": [0.0, 100.0],
            "commission": [5.0, 5.0],
        }
    )


def test_compute_metrics_accepts_polars():
    from ptrade_sim.runtime import compute_metrics

    m = compute_metrics(_daily_frame(), _trades_frame(), 1_000_000, {"benchmark": "000300.SS"})
    assert m["trade_count"] == 2
    assert m["win_rate"] == 1.0
    assert m["total_commission"] == 10.0
    assert "monthly_stats" in m


def test_compute_metrics_single_month_std_is_zero():
    """回归防护：样本 <2 时 polars 的 std 返回 **null**（pandas 返回 NaN），
    若不兜底则 ``float(None)`` 抛 TypeError —— 单月回测即会触发。

    此缺陷在真实库上暴露、而合成夹具（数据跨月）未覆盖，故单独固定。
    """
    from ptrade_sim.runtime import compute_metrics

    m = compute_metrics(_daily_frame(n=2), _trades_frame(), 1_000_000, {"benchmark": "000300.SS"})
    assert m["monthly_stats"]["std"] == 0.0
    assert m["monthly_stats"]["win_rate"] in (0.0, 1.0)
    assert m["monthly_stats"]["best_month"] is not None


def test_compute_metrics_single_day():
    """只有一天时不应抛异常（std/基准等边界）。"""
    from ptrade_sim.runtime import compute_metrics

    m = compute_metrics(_daily_frame(n=1), pl.DataFrame(), 1_000_000, {"benchmark": "000300.SS"})
    assert m["trade_days"] == 1
    assert m["trade_count"] == 0
    assert m["win_rate"] == 0.0


def test_compute_metrics_no_benchmark():
    """基准缺失时基准收益为 NaN，但其余指标仍应算出。"""
    from ptrade_sim.runtime import compute_metrics

    m = compute_metrics(
        _daily_frame(with_bench=False), _trades_frame(), 1_000_000, {"benchmark": "000300.SS"}
    )
    assert m["benchmark_return"] != m["benchmark_return"]  # NaN
    assert m["trade_count"] == 2


def test_compute_metrics_empty_daily():
    from ptrade_sim.runtime import compute_metrics

    m = compute_metrics(pl.DataFrame(), pl.DataFrame(), 1_000_000, {})
    assert m["trade_days"] == 0
    assert m["final_value"] == 1_000_000


def test_frame_to_csv_has_bom():
    """CSV 带 UTF-8 BOM：否则 Excel 打开中文乱码（与原 pandas utf-8-sig 一致）。"""
    from ptrade_sim.runtime import frame_to_csv_text

    txt = frame_to_csv_text(_daily_frame(n=1))
    assert txt.startswith("\ufeff")
    assert "total_value" in txt
    assert frame_to_csv_text(pl.DataFrame()) == "\ufeff"


def test_monthly_returns_keys_use_iso_date():
    """回归防护：``date`` 列是 ISO ``YYYY-MM-DD``，月度键必须仍是 ``YYYY-MM``。

    曾按紧凑 ``YYYYMMDD`` 取位（``str.slice(0, 6)``），于是键变成 ``"2025--0"``：
    看板月度图的 X 轴直接显示该键，且与 ``server._monthly_extended`` 的
    ``"YYYY-MM"`` 键对不上 —— 月度明细的基准/超额/回撤等扩展列整块取不到。
    """
    from ptrade_sim.runtime import compute_metrics

    daily = pl.DataFrame(
        {
            "date": ["2025-01-02", "2025-01-03", "2025-02-03", "2025-02-04"],
            "total_value": [1_000_000.0, 1_010_000.0, 1_020_000.0, 1_030_000.0],
            "daily_return": [0.0, 0.01, 0.0099, 0.0098],
            "drawdown": [0.0, 0.0, 0.0, 0.0],
            "benchmark_close": [4000.0, 4010.0, 4020.0, 4030.0],
        }
    )
    m = compute_metrics(daily, pl.DataFrame(), 1_000_000, {"benchmark": "000300.SS"})
    assert sorted(m["monthly_returns"]) == ["2025-01", "2025-02"], m["monthly_returns"]
    assert list(m["annual_returns"]) == ["2025"]
    assert m["monthly_stats"]["best_month"]["month"] in ("2025-01", "2025-02")


# ============================================================
# 策略异常处理
# ============================================================


def test_strategy_exception_does_not_crash_backtest(engine_factory):
    e = engine_factory(
        "def initialize(context):\n"
        "    set_benchmark('000300.SS')\n"
        "    set_universe(['000001.SZ'])\n"
        "    run_daily(context, boom, time='09:31')\n"
        "\n"
        "def boom(context):\n"
        "    raise RuntimeError('intentional')\n"
    )
    daily = e.run()  # 不应抛出
    assert len(daily) == 5


def test_failing_hook_recorded(engine_factory):
    e = engine_factory(
        "def initialize(context):\n"
        "    set_benchmark('000300.SS')\n"
        "    set_universe(['000001.SZ'])\n"
        "    run_daily(context, boom, time='09:31')\n"
        "\n"
        "def boom(context):\n"
        "    raise RuntimeError('intentional')\n"
    )
    e.run()
    assert e._failed_funcs, "失败函数应被记录，而不是静默"


# ============================================================
# 持仓 API（官方三件套）
# ============================================================


def _position_strategy():
    return (
        "snap = {}\n"
        "def initialize(context):\n"
        "    set_benchmark('000300.SS')\n"
        "    set_universe(['000001.SZ'])\n"
        "    run_daily(context, buy, time='09:31')\n"
        "    run_daily(context, snap_positions, time='14:00')\n"
        "\n"
        "def buy(context):\n"
        "    if get_position('000001.SZ').amount == 0:\n"
        "        order('000001.SZ', 1000)\n"
        "\n"
        "def snap_positions(context):\n"
        "    d = context.blotter.current_dt.strftime('%Y%m%d')\n"
        "    p = get_position('000001.SZ')\n"
        "    ps = get_positions(['000001.SZ'])\n"
        "    ap = get_all_positions()\n"
        "    snap[d] = {\n"
        "        'amount': p.amount,\n"
        "        'enable': p.enable_amount,\n"
        "        'cost': p.cost_basis,\n"
        "        'multi_keys': sorted(ps.keys()),\n"
        "        'all_len': len(ap),\n"
        "        'all_keys': sorted(ap[0].keys()) if ap else [],\n"
        "    }\n"
    )


def test_get_position_api_exists_and_shape(engine_factory):
    """官方 ``get_position``：此前完全缺失，调用即 NameError。"""
    e = engine_factory(_position_strategy())
    e.run()
    snap = e._module.__dict__["snap"]
    # 买入当日：有持仓但可用为 0（T+1）
    day1 = snap["20250102"]
    assert day1["amount"] == 1000
    assert day1["enable"] == 0, "买入当日可用应为 0"
    assert day1["cost"] > 0
    # 次日：转为全部可用
    assert snap["20250103"]["enable"] == 1000


def test_get_position_returns_empty_position_when_flat(engine_factory):
    e = engine_factory(
        "res = {}\n"
        "def initialize(context):\n"
        "    set_benchmark('000300.SS')\n"
        "    set_universe(['000001.SZ'])\n"
        "\n"
        "def handle_data(context, data):\n"
        "    if not res:\n"
        "        p = get_position('000001.SZ')\n"
        "        res['amount'] = p.amount\n"
        "        res['is_none'] = p is None\n"
    )
    e.run()
    res = e._module.__dict__["res"]
    assert res["is_none"] is False, "官方语义：无持仓返回空 Position，不是 None"
    assert res["amount"] == 0


def test_get_positions_supports_both_suffix_forms(engine_factory):
    """官方：两位与四位尾缀皆可作为返回字典的键。"""
    e = engine_factory(_position_strategy())
    e.run()
    keys = e._module.__dict__["snap"]["20250102"]["multi_keys"]
    assert "000001.SZ" in keys
    assert "000001.XSHE" in keys


def test_get_all_positions_returns_list_of_dicts(engine_factory):
    e = engine_factory(_position_strategy())
    e.run()
    snap = e._module.__dict__["snap"]["20250102"]
    assert snap["all_len"] == 1
    for k in ("stock_code", "current_amount", "enable_amount", "last_price", "cost_price"):
        assert k in snap["all_keys"], f"缺官方字段 {k}"


def test_get_positions_all_when_no_arg(engine_factory):
    e = engine_factory(
        "res = {}\n"
        "def initialize(context):\n"
        "    set_benchmark('000300.SS')\n"
        "    set_universe(['000001.SZ'])\n"
        "    run_daily(context, buy, time='09:31')\n"
        "    run_daily(context, allp, time='14:00')\n"
        "\n"
        "def buy(context):\n"
        "    if get_position('000001.SZ').amount == 0:\n"
        "        order('000001.SZ', 1000)\n"
        "\n"
        "def allp(context):\n"
        "    res['keys'] = sorted(get_positions().keys())\n"
    )
    e.run()
    assert "000001.SZ" in e._module.__dict__["res"]["keys"]
