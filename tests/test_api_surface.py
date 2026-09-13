"""PTrade API 面测试。

用一个策略把常用 API 全调一遍并记录返回值，然后断言语义。
这样既覆盖了实现，也把「API 的可观测行为」固定下来 —— 比纯粹的覆盖率数字有用。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


#: 在回测中调用一大批 API，把结果塞进 ``R``
PROBE = """
R = {}

def _rec(k, fn):
    try:
        R[k] = fn()
    except Exception as exc:
        R[k] = f"ERR:{type(exc).__name__}:{exc}"

def initialize(context):
    set_benchmark('000300.SS')
    set_universe(['000001.SZ', '600000.SS'])
    set_commission(commission_ratio=0.0002, min_commission=5.0, type='STOCK')
    set_slippage(slippage=0.001)
    set_fixed_slippage(0.01)
    set_volume_ratio(0.25)
    set_limit_mode('UNLIMITED')
    set_yesterday_position([])
    set_parameters({})
    run_daily(context, probe_a, time='09:31')
    run_daily(context, probe_b, time='14:00')

def probe_a(context):
    # 只在最后一个交易日探测：get_history/get_price 需要足够的前置历史，
    # 在区间首日调用会因「历史不足」返回空表，那是正确行为而非缺陷。
    if context.blotter.current_dt.strftime('%Y%m%d') != '20250108':
        return
    if R:
        return
    _rec('freq', lambda: get_frequency())
    _rec('biz_type', lambda: get_business_type())
    _rec('is_trade', lambda: is_trade())
    _rec('kline_count', lambda: get_current_kline_count())
    _rec('name', lambda: get_stock_name('000001.SZ'))
    _rec('info', lambda: get_stock_info('000001.SZ'))
    _rec('status_st', lambda: get_stock_status(['000001.SZ'], 'ST'))
    _rec('status_halt', lambda: get_stock_status(['000001.SZ'], 'HALT'))
    _rec('filter', lambda: filter_stock_by_status(['000001.SZ'], ['ST']))
    _rec('ashares_len', lambda: len(get_Ashares()))
    _rec('trade_days', lambda: list(get_trade_days(count=3)))
    _rec('all_trade_days', lambda: len(get_all_trades_days()))
    _rec('trading_day', lambda: get_trading_day(0))
    _rec('trading_day_by_date', lambda: get_trading_day_by_date('2025-01-02'))
    _rec('limit', lambda: check_limit(['000001.SZ', '600000.SS']))
    _rec('limit_str', lambda: check_limit('000001.SZ'))
    _rec('limit_hist', lambda: check_limit(['000001.SZ'], query_date='20250102'))
    _rec('snapshot', lambda: get_snapshot('000001.SZ'))
    _rec('research', lambda: get_research_path())
    _rec('val', lambda: get_fundamentals(['000001.SZ'], 'valuation', date='20250102'))
    _rec('index', lambda: len(get_index_stocks('000300', '20250102')))
    _rec('trend', lambda: get_trend_data(stocks=['000001.SZ']))
    # 占位接口：应可调用且不抛
    _rec('exrights', lambda: get_stock_exrights('000001.SZ'))
    _rec('blocks', lambda: get_stock_blocks('000001.SZ'))
    _rec('industry', lambda: get_industry_stocks('银行'))
    _rec('reits', lambda: get_reits_list())
    _rec('market_list', lambda: get_market_list())
    _rec('market_detail', lambda: get_market_detail('000001.SZ'))
    # 历史
    _rec('hist_1d', lambda: get_history(3, '1d', 'close', '000001.SZ', fq=None).shape)
    _rec('hist_1d_preclose', lambda: list(get_history(2, '1d', 'preclose', '000001.SZ', fq=None)['preclose']))
    _rec('hist_1m', lambda: get_history(5, '1m', 'close', '000001.SZ', fq=None).shape)
    _rec('hist_fields', lambda: list(get_history(1, '1d', ['open','high','low','close','volume','money','preclose'], '000001.SZ', fq=None).columns))
    _rec('price_1d', lambda: get_price('000001.SZ', end_date=None, frequency='1d', count=3).shape)
    _rec('price_multi', lambda: get_price(['000001.SZ','600000.SS'], end_date=None, frequency='1d', count=2).shape)
    # 下单族（不成交也要走通）
    _rec('order_zero', lambda: order('000001.SZ', 0))
    _rec('order_buy', lambda: order('000001.SZ', 1000))
    _rec('order_value', lambda: order_value('600000.SS', 5000))
    _rec('order_target', lambda: order_target('000001.SZ', 500))
    _rec('order_target_value', lambda: order_target_value('600000.SS', 10000))
    _rec('orders', lambda: len(get_orders()))
    _rec('trades', lambda: len(get_trades()))
    _rec('pos', lambda: get_position('000001.SZ').amount)
    _rec('positions', lambda: sorted(get_positions().keys()))
    _rec('all_positions', lambda: len(get_all_positions()))
    _rec('cash', lambda: context.portfolio.cash)
    _rec('create_dir', lambda: create_dir(get_research_path() + '/sub'))
    _rec('user', lambda: get_user_name())

def probe_b(context):
    pass
"""


@pytest.fixture(scope="module")
def api_results(tiny_db, tmp_path_factory):
    from ptrade_sim.runtime import BacktestEngine

    d = tmp_path_factory.mktemp("api")
    sp = d / "probe.py"
    sp.write_text(PROBE, encoding="utf-8")
    cfg = {
        "db_path": str(tiny_db),
        "start_date": "2025-01-02",
        "end_date": "2025-01-08",
        "capital_base": 1_000_000,
        "benchmark": "000300.SS",
        "frequency": "minute",
        "preload": {"mode": "rolling", "rolling_window_days": 2, "threads": 2},
        "queue": {"enabled": False},
    }
    e = BacktestEngine(cfg, str(sp), d / "out")
    e.run()
    return e._module.__dict__["R"]


def _err(v):
    return isinstance(v, str) and v.startswith("ERR:")


# ============================================================
# 周期与业务类型
# ============================================================


def test_frequency_apis(api_results):
    assert api_results["freq"] == "minute"
    assert api_results["biz_type"] == "stock"
    assert api_results["is_trade"] is False


def test_kline_count_positive_in_minute_mode(api_results):
    assert api_results["kline_count"] >= 1


# ============================================================
# 证券信息
# ============================================================


def test_stock_name_returns_dict(api_results):
    """官方 ``get_stock_name`` 返回 ``dict[str:str]``（即使传入单个代码）。"""
    nm = api_results["name"]
    assert isinstance(nm, dict)
    assert nm.get("000001.SZ") == "平安银行"


def test_stock_info_returns_per_code_dict(api_results):
    """``get_stock_info`` 按代码分组返回。"""
    info = api_results["info"]
    assert isinstance(info, dict)
    assert "000001.SZ" in info
    assert isinstance(info["000001.SZ"], dict)
    assert info["000001.SZ"].get("stock_name") == "平安银行"


def test_stock_status_and_filter(api_results):
    assert isinstance(api_results["status_st"], dict)
    assert isinstance(api_results["status_halt"], dict)
    assert isinstance(api_results["filter"], list)


def test_ashares_nonempty(api_results):
    assert api_results["ashares_len"] >= 1


# ============================================================
# 日历
# ============================================================


def test_trade_day_apis(api_results):
    # get_trade_days 默认到今天为止，夹具第 5 天时至少能取到 3 天
    assert len(api_results["trade_days"]) == 3
    assert api_results["all_trade_days"] >= 5
    assert api_results["trading_day"] is not None
    assert api_results["trading_day_by_date"] is not None


# ============================================================
# 历史与行情
# ============================================================


def test_get_history_daily_supports_preclose(api_results):
    """回归防护：字段名必须是 preclose，且能取到真实值（此前静默返空列）。"""
    vals = api_results["hist_1d_preclose"]
    assert not _err(vals), vals
    assert len(vals) == 2
    assert all(v > 0 for v in vals), f"preclose 取到空/零：{vals}"


def test_get_history_daily_shapes(api_results):
    assert api_results["hist_1d"][0] == 3
    assert api_results["hist_1m"][0] == 5


def test_get_history_field_list(api_results):
    cols = api_results["hist_fields"]
    assert {"open", "high", "low", "close", "volume", "money", "preclose"} <= set(cols)


def test_get_price_single_and_multi(api_results):
    assert api_results["price_1d"][0] == 3
    assert api_results["price_multi"][0] == 4  # 2 股 × 2 日


def test_snapshot(api_results):
    assert isinstance(api_results["snapshot"], dict)


def test_trend_data(api_results):
    assert isinstance(api_results["trend"], dict)


# ============================================================
# 涨跌停与状态
# ============================================================


def test_check_limit_accepts_list_and_returns_status_dict(api_results):
    """官方 ``check_limit``：接受 str 或 list[str]，返回 ``dict[str:int]``。

    回归防护：此前只接受单个 str 且返回 bool，传列表直接
    ``TypeError: unhashable type: 'list'``。
    """
    lim = api_results["limit"]
    assert isinstance(lim, dict), f"官方返回 dict[str:int]，实际 {type(lim)}"
    assert lim, "不应为空"
    for k, v in lim.items():
        assert isinstance(k, str)
        assert v in (1, 0, -1), f"状态码应为 1/0/-1，实际 {v}"


def test_check_limit_accepts_single_string(api_results):
    """官方允许传单个 str（返回仍是 dict）。"""
    lim = api_results["limit_str"]
    assert isinstance(lim, dict)
    assert lim.get("000001.SZ") in (1, 0, -1)


def test_check_limit_accepts_query_date(api_results):
    """历史日期查询以该日收盘价判断，应可正常返回状态码。"""
    lim = api_results["limit_hist"]
    assert isinstance(lim, dict)
    assert lim.get("000001.SZ") in (1, 0, -1)


# ============================================================
# 基本面 / 指数
# ============================================================


def test_fundamentals_valuation(api_results):
    v = api_results["val"]
    assert not _err(v), v
    assert "total_value" in v.columns
    assert "float_value" in v.columns


def test_index_stocks_nonempty(api_results):
    assert api_results["index"] >= 1


# ============================================================
# 占位接口（不得抛 NameError）
# ============================================================


@pytest.mark.parametrize(
    "key", ["exrights", "blocks", "industry", "reits", "market_list", "market_detail"]
)
def test_placeholder_apis_do_not_raise(api_results, key):
    assert not _err(api_results[key]), f"{key} 抛异常：{api_results[key]}"


# ============================================================
# 下单族
# ============================================================


def test_order_zero_returns_none(api_results):
    assert api_results["order_zero"] is None


def test_order_family_executes(api_results):
    assert not _err(api_results["order_buy"]), api_results["order_buy"]
    assert not _err(api_results["order_value"])
    assert not _err(api_results["order_target"])
    assert not _err(api_results["order_target_value"])


def test_orders_and_trades_queryable(api_results):
    assert isinstance(api_results["orders"], int)
    assert isinstance(api_results["trades"], int)


def test_positions_apis(api_results):
    assert isinstance(api_results["pos"], int)
    assert isinstance(api_results["positions"], list)
    assert isinstance(api_results["all_positions"], int)


def test_cash_positive(api_results):
    assert api_results["cash"] >= 0


# ============================================================
# 杂项
# ============================================================


def test_create_dir(api_results):
    assert api_results["create_dir"] is True


def test_user_name(api_results):
    assert isinstance(api_results["user"], str)


def test_setters_do_not_raise(api_results):
    """set_* 系列在 initialize 里已调用；到这里没炸即通过。"""
    assert not _err(api_results["cash"])
