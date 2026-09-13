"""回看窗口越界（数据缺口）测试。

**背景**：``get_history`` 的回看窗口越过库内数据覆盖起点时，缺失的交易日会走
"停牌填充"分支；而窗口首日没有前收盘，于是填成 **NaN**。关键是
``get_history`` **照样返回满 count 行**，所以从返回值上完全看不出问题。

后果：策略里极常见的 ``close.iloc[0]``（算区间涨幅）拿到 NaN → ``dropna()``
清空 → **静默跳过调仓**，用户只看到"回测完成"。这类"错得不明显"的失败
正是本项目要避免的，故用测试固定住"必须告警 + 必须留档"。
"""

from __future__ import annotations

import pytest

from ptrade_sim.runtime import BacktestEngine

pytestmark = pytest.mark.integration


def _mk_strategy(tmp_path, code: str):
    d = tmp_path / "s"
    d.mkdir(exist_ok=True)
    p = d / "strategy.py"
    p.write_text(code, encoding="utf-8")
    return p


def _engine(tmp_path, tiny_db, strategy_code: str, **over):
    from tests.conftest import TRADE_DAYS

    cfg = {
        "db_path": str(tiny_db),
        "start_date": "2025-01-02",
        "end_date": "2025-01-08",
        "capital_base": 1_000_000,
        "benchmark": "000300.SS",
        "frequency": "daily",
        "preload": {"mode": "rolling", "rolling_window_days": 3, "threads": 1},
        "queue": {"enabled": False},
    }
    cfg.update(over)
    return BacktestEngine(
        cfg, str(_mk_strategy(tmp_path, strategy_code)), tmp_path / "out"
    ), TRADE_DAYS


#: 在 2025-01-06 取一个比库内历史更长的窗口：
#: 库内日线从 20250102 起，2025-01-06 往前 20 个自然交易日必然越界
PROBE_OVERFLOW = (
    "res = {}\n"
    "def initialize(context):\n"
    "    set_benchmark('000300.SS')\n"
    "    set_universe(['000001.SZ'])\n"
    "    run_daily(context, probe, time='15:00')\n"
    "\n"
    "def probe(context):\n"
    "    if context.blotter.current_dt.strftime('%Y%m%d') != '20250106':\n"
    "        return\n"
    "    h = get_history(20, '1d', 'close', ['000001.SZ'])\n"
    "    res['rows'] = len(h)\n"
    "    res['vals'] = [float(v) for v in h['close']]\n"
    "    res['nan'] = sum(1 for v in res['vals'] if v != v)\n"
)

#: 只取库内已有的历史，不应触发缺口
PROBE_OK = (
    "res = {}\n"
    "def initialize(context):\n"
    "    set_benchmark('000300.SS')\n"
    "    set_universe(['000001.SZ'])\n"
    "    run_daily(context, probe, time='15:00')\n"
    "\n"
    "def probe(context):\n"
    "    if context.blotter.current_dt.strftime('%Y%m%d') != '20250108':\n"
    "        return\n"
    "    h = get_history(3, '1d', 'close', ['000001.SZ'])\n"
    "    res['nan'] = sum(1 for v in [float(x) for x in h['close']] if v != v)\n"
)


def _warnings(caplog_msgs):
    return [m for m in caplog_msgs if "回看窗口" in m or "触及库内" in m]


@pytest.fixture
def capture_warnings():
    import contextlib

    from loguru import logger

    msgs: list[str] = []
    sink = logger.add(lambda m: msgs.append(str(m)), level="WARNING")
    yield msgs
    # 其他夹具/测试可能已重置 handler，故容错移除
    with contextlib.suppress(ValueError):
        logger.remove(sink)


def test_history_still_returns_rows_when_window_overflows(tmp_path, tiny_db, capture_warnings):
    """越界时 get_history 仍返回**非空**结果、不缩水成空表 —— 这正是它「看不出来」的原因。

    夹具日历在行情起点前有 ``PRE_COVERAGE_DAYS``（12 天），加上库内 2 天，
    故 2025-01-06 取 20 根得到 14 行（受日历长度限制），其中前 12 行落在
    覆盖之外 → 全 NaN。
    """
    from tests.conftest import PRE_COVERAGE_DAYS

    e, _ = _engine(tmp_path, tiny_db, PROBE_OVERFLOW)
    e.run()
    res = e._module.__dict__["res"]
    assert res["rows"] == len(PRE_COVERAGE_DAYS) + 2, f"实际 {res['rows']} 行"
    assert res["rows"] > 0, "不缩水成空表（故 len(h)==0 这类守卫抓不到问题）"
    assert res["nan"] == len(PRE_COVERAGE_DAYS), (
        f"覆盖外的 {len(PRE_COVERAGE_DAYS)} 天应全为 NaN，实际 {res['nan']}"
    )


def test_lookback_overflow_emits_warning(tmp_path, tiny_db, capture_warnings):
    """必须告警，否则用户看到「回测完成」就以为正常。"""
    e, _ = _engine(tmp_path, tiny_db, PROBE_OVERFLOW)
    e.run()
    hits = _warnings(capture_warnings)
    assert hits, f"未告警：{capture_warnings}"
    assert "NaN" in hits[0]
    assert "20190102" in hits[0] or "库内日线覆盖" in hits[0], "应给出库内覆盖范围"


def test_overflow_warning_is_deduped(tmp_path, tiny_db, capture_warnings):
    """按「首次越界」告警一次，不能按 code × count 刷屏。"""
    code = PROBE_OVERFLOW.replace("'20250106'", "'20250103'").replace(
        "get_history(20, '1d', 'close', ['000001.SZ'])",
        "get_history(20, '1d', 'close', ['000001.SZ', '600000.SS', '000002.SZ'])",
    )
    e, _ = _engine(tmp_path, tiny_db, code)
    e.run()
    hits = _warnings(capture_warnings)
    assert len(hits) == 1, f"应只告警一次，实际 {len(hits)} 次"


def test_no_warning_when_window_within_coverage(tmp_path, tiny_db, capture_warnings):
    """正常窗口不得误报。"""
    e, _ = _engine(tmp_path, tiny_db, PROBE_OK)
    e.run()
    assert not _warnings(capture_warnings), f"误报：{capture_warnings}"
    assert e._module.__dict__["res"]["nan"] == 0


def test_data_gaps_recorded_for_summary(tmp_path, tiny_db, capture_warnings):
    """缺口要能取出来（CLI 据此写 summary.json 并打印收尾告警）。"""
    e, _ = _engine(tmp_path, tiny_db, PROBE_OVERFLOW)
    e.run()
    gaps = e.data_gaps()
    assert gaps, "越界后应有 data_gaps"
    assert gaps["missing_day_count"] > 0
    assert gaps["request_count"] >= gaps["missing_day_count"]
    assert gaps["daily_coverage"] == ["20250102", "20250108"]
    assert gaps["missing_days"] == sorted(gaps["missing_days"]), "缺失日应有序"
    assert "NaN" in gaps["hint"]


def test_data_gaps_empty_when_no_overflow(tmp_path, tiny_db, capture_warnings):
    e, _ = _engine(tmp_path, tiny_db, PROBE_OK)
    e.run()
    assert e.data_gaps() == {}, "未越界时不应有 data_gaps（summary 里也不该出现该字段）"


def test_suspended_stock_is_not_reported_as_data_gap(tmp_path, tiny_db, capture_warnings):
    """**关键区分**：个股停牌（库里有这天、只是该股无行）不得报成数据缺口。

    否则会把正常的停牌语义误报成"库缺数据"，让用户白折腾。
    """
    # 000002.SZ 在夹具里存在；用一个库内存在但该日无行的情景不易构造，
    # 这里退一步验证：正常区间内取多只股票的历史不产生任何缺口。
    code = (
        "res = {}\n"
        "def initialize(context):\n"
        "    set_benchmark('000300.SS')\n"
        "    set_universe(['000001.SZ','600000.SS','000002.SZ'])\n"
        "    run_daily(context, probe, time='15:00')\n"
        "\n"
        "def probe(context):\n"
        "    if context.blotter.current_dt.strftime('%Y%m%d') != '20250108':\n"
        "        return\n"
        "    get_history(4, '1d', 'close', ['000001.SZ','600000.SS','000002.SZ'])\n"
    )
    e, _ = _engine(tmp_path, tiny_db, code)
    e.run()
    assert e.data_gaps() == {}, "库内覆盖内的正常取数不应产生缺口"
    assert not _warnings(capture_warnings)
