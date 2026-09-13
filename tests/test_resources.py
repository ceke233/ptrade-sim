"""资源探查与准入决策测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from ptrade_sim import resources as R

pytestmark = pytest.mark.unit


def _snap(cpu=16, total=64 * 1024**3, avail=48 * 1024**3, disk=100 * 1024**3):
    return R.ResourceSnapshot(cpu_count=cpu, mem_total=total, mem_available=avail, disk_free=disk)


def _cost(days=250, threads=4, mode="rolling", budget=0):
    return R.estimate_cost(
        days=days,
        threads=threads,
        preload_mode=mode,
        rolling_window=10,
        minute_budget_bytes=budget,
    )


# ============================================================
# 内存探测
# ============================================================


def test_probe_memory_returns_positive():
    total, avail = R.probe_memory()
    assert total > 0, "内存探测失败（应至少有 psutil 兜底）"
    assert 0 < avail <= total


def test_probe_snapshot_fields():
    s = R.probe()
    assert s.cpu_count >= 1
    assert s.mem_total > 0
    assert 0 <= s.mem_used_pct <= 100
    assert "CPU" in s.describe() and "内存" in s.describe()


def test_probe_disk_optional():
    s = R.probe(Path.cwd().anchor)  # 当前盘符（与平台无关）
    assert s.disk_free >= 0  # 路径不存在时也不应抛异常


def test_suggest_threads_leaves_one_core():
    assert R.suggest_threads(_snap(cpu=16), prefer=32) == 15
    assert R.suggest_threads(_snap(cpu=1), prefer=8) == 1


# ============================================================
# 开销估算
# ============================================================


def test_estimate_rolling_bounds_minute_memory():
    """流式模式下**分钟数据部分**不随区间增长（这是流式的核心收益）。

    注意：总估算仍会因策略侧开销（持仓/指标）随天数温和增长，
    所以断言的是 minute_budget_bytes 恒定，而非总内存恒定。
    """
    c250 = R.estimate_cost(250, preload_mode="rolling", rolling_window=10)
    c1700 = R.estimate_cost(1700, preload_mode="rolling", rolling_window=10)
    assert c250.minute_budget_bytes == c1700.minute_budget_bytes, "分钟常驻应与区间无关"
    assert c250.minute_budget_bytes == 10 * R.BYTES_PER_MINUTE_DAY


def test_estimate_all_grows_with_range():
    c250 = R.estimate_cost(250, preload_mode="all")
    c1700 = R.estimate_cost(1700, preload_mode="all")
    assert c1700.est_mem_bytes > c250.est_mem_bytes * 5
    assert c1700.notes, "preload=all 应给出风险提示"


def test_all_mode_costs_much_more_than_rolling_for_long_range():
    rolling = R.estimate_cost(1700, preload_mode="rolling", rolling_window=10)
    allm = R.estimate_cost(1700, preload_mode="all")
    assert allm.est_mem_bytes > rolling.est_mem_bytes * 3


def test_estimate_slots_clamped_to_cpu_minus_one():
    """线程数超过核数时必须封顶，否则单任务需求超过总容量 → 永远排不上队。"""
    c = R.estimate_cost(250, threads=999)
    import os

    assert c.est_cpu_slots <= max(1, (os.cpu_count() or 1) - 1)


def test_estimate_minute_budget_caps_memory():
    big = R.estimate_cost(1700, preload_mode="all", minute_budget_bytes=0)
    capped = R.estimate_cost(1700, preload_mode="all", minute_budget_bytes=10**9)
    assert capped.est_mem_bytes < big.est_mem_bytes


def test_estimate_days_preserved():
    assert _cost(days=42).days == 42


def test_cost_describe():
    text = _cost().describe()
    assert "交易日" in text and "线程" in text


# ============================================================
# 准入决策
# ============================================================


def test_admit_ok_when_plenty():
    adm = R.decide(_cost(), _snap(), used_slots=0, used_count=0, max_parallel=4)
    assert adm.ok
    assert adm.reason == "ok"
    assert bool(adm) is True


def test_reject_on_memory():
    big = R.estimate_cost(1700, preload_mode="all")
    adm = R.decide(big, _snap(avail=2 * 1024**3), used_slots=0, used_count=0, max_parallel=4)
    assert not adm.ok
    assert adm.reason == "memory"
    assert "预留" in adm.detail


def test_reject_on_parallel_count():
    """并发数是独立维度：达上限就排队，不管 CPU 槽还剩多少。"""
    adm = R.decide(_cost(), _snap(), used_slots=0, used_count=1, max_parallel=1)
    assert not adm.ok
    assert adm.reason == "parallel"


def test_reject_on_cpu_slots():
    adm = R.decide(_cost(threads=8), _snap(cpu=16), used_slots=10, used_count=0, max_parallel=8)
    assert not adm.ok
    assert adm.reason == "cpu"


def test_reject_on_disk():
    adm = R.decide(_cost(), _snap(disk=1 * 1024**3), max_parallel=4)
    assert not adm.ok
    assert adm.reason == "disk"


def test_threads_never_exceed_available_slots():
    """回归防护：threads 设得再大，也必须能被默认槽位上限接纳，
    否则单任务需求超过总容量、准入判定恒为假 → 永久排队。"""
    import os

    cpu = os.cpu_count() or 1
    cost = R.estimate_cost(250, threads=999)
    assert cost.est_cpu_slots <= max(1, cpu - 1), "线程数未封顶"
    adm = R.decide(cost, _snap(cpu=cpu), used_slots=0, used_count=0, max_parallel=8)
    assert adm.ok, f"单任务被永久阻塞：{adm.detail}"


def test_default_max_parallel_is_half_cores():
    """未显式指定时默认 cpu//2，避免开满线程互相拖慢。"""
    adm = R.decide(_cost(), _snap(cpu=16), used_slots=0, used_count=8, max_parallel=None)
    assert not adm.ok and adm.reason == "parallel"


def test_admission_has_wait_hint():
    adm = R.decide(_cost(), _snap(avail=512 * 1024**2), max_parallel=4)
    assert adm.wait_hint_sec > 0


def test_mem_reserve_ratio_applied():
    """预留比例越大越容易拒绝（内存充裕时宽松通过、预留 90% 时被拒）。"""
    cost = R.estimate_cost(250)
    snap = _snap(avail=4 * 1024**3)
    loose = R.decide(cost, snap, mem_reserve_ratio=0.0, max_parallel=4)
    tight = R.decide(cost, snap, mem_reserve_ratio=0.9, max_parallel=4)
    assert loose.ok, f"0% 预留应通过：{loose.detail}"
    assert not tight.ok and tight.reason == "memory", f"90% 预留应被拒：{tight.detail}"
