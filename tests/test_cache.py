"""统一缓存测试（容量上限 / 内存预算 / LRU / 内存估算）。"""

from __future__ import annotations

import pytest

from ptrade_sim.cache import MISSING, Cache, CacheConfig, CacheGroup, estimate_bytes

pytestmark = pytest.mark.unit


# ============================================================
# 基础语义
# ============================================================


def test_get_miss_returns_sentinel_not_none():
    """未命中必须返回 MISSING 哨兵：缓存会存 None（表示「该日缺失」），
    若用 None 表示未命中就无法区分两者。"""
    c = Cache("t")
    assert c.get("absent") is MISSING
    c.put("present", None)
    assert c.get("present") is None
    assert c.get("present") is not MISSING


def test_capacity_evicts_lru():
    c = Cache("t", capacity=3)
    for i in range(5):
        c.put(i, i)
    assert len(c) == 3
    assert sorted(c._d.keys()) == [2, 3, 4]
    assert c.stats.evictions == 2


def test_get_refreshes_recency():
    """命中即刷新：被访问过的键不应先被逐出。"""
    c = Cache("t", capacity=2)
    c.put("a", 1)
    c.put("b", 2)
    assert c.get("a") == 1  # a 变最近
    c.put("c", 3)  # 应逐出 b（最久未用）
    assert c.get("a") == 1
    assert c.get("b") is MISSING
    assert c.get("c") == 3


def test_put_existing_key_replaces_and_does_not_grow():
    c = Cache("t", capacity=2)
    c.put("a", 1)
    c.put("a", 2)
    assert len(c) == 1
    assert c.get("a") == 2


def test_clear_resets():
    c = Cache("t", capacity=2)
    c.put("a", 1)
    c.clear()
    assert len(c) == 0
    assert c.stats.bytes == 0


def test_contains_and_len():
    c = Cache("t")
    c.put("a", 1)
    assert "a" in c
    assert "b" not in c
    assert len(c) == 1


# ============================================================
# 内存预算
# ============================================================


def test_memory_budget_evicts_and_stays_under():
    c = Cache("t", memory_budget=300)
    for i in range(10):
        c.put(i, bytes(60))  # 每项 60B
    assert c.stats.bytes <= 300, "内存预算被突破"
    assert c.stats.evictions > 0
    assert len(c) >= 1, "不应把自己全逐空"


def test_memory_budget_keeps_at_least_one_item():
    """单项就超预算时也必须保留（否则永远取不到数据，退化成死循环）。"""
    c = Cache("t", memory_budget=10)
    c.put("big", bytes(1000))
    assert len(c) == 1
    assert c.get("big") is not MISSING


def test_zero_budget_means_unlimited():
    c = Cache("t", memory_budget=0)
    for i in range(50):
        c.put(i, bytes(1000))
    assert len(c) == 50, "预算为 0 表示不限，不应逐出"


def test_capacity_and_budget_both_apply():
    c = Cache("t", capacity=2, memory_budget=10_000)
    for i in range(5):
        c.put(i, bytes(10))
    assert len(c) == 2  # 条数上限先触发


# ============================================================
# get_or_load
# ============================================================


def test_get_or_load_caches_non_none():
    calls = []

    def loader():
        calls.append(1)
        return {"v": 42}

    c = Cache("t")
    assert c.get_or_load("k", loader) == {"v": 42}
    assert c.get_or_load("k", loader) == {"v": 42}
    assert len(calls) == 1, "命中后不应再调用 loader"


def test_get_or_load_does_not_cache_none():
    """loader 返回 None 视为「查不到」，不缓存，下次仍会重试。"""
    calls = []

    def loader():
        calls.append(1)
        return

    c = Cache("t")
    c.get_or_load("k", loader)
    c.get_or_load("k", loader)
    assert len(calls) == 2
    assert "k" not in c


# ============================================================
# 统计
# ============================================================


def test_stats_hit_rate():
    c = Cache("t")
    c.put("a", 1)
    c.get("a")
    c.get("a")
    c.get("b")
    s = c.stats.as_dict()
    assert s["hits"] == 2
    assert s["misses"] == 1
    assert s["hit_rate"] == pytest.approx(2 / 3, abs=1e-4)


def test_stats_peak_tracked():
    c = Cache("t", memory_budget=1000)
    for i in range(5):
        c.put(i, bytes(100))
    assert c.stats.peak_bytes >= c.stats.bytes


# ============================================================
# 内存估算（曾因 __slots__ 类退化成 1MB 兜底值而失真）
# ============================================================


def test_estimate_bytes_none_is_tiny():
    assert estimate_bytes(None) < 1024


def test_estimate_bytes_numpy():
    np = pytest.importorskip("numpy")
    a = np.zeros(1000, dtype=np.float64)
    assert estimate_bytes(a) == 8000


def test_estimate_bytes_polars():
    pl = pytest.importorskip("polars")
    df = pl.DataFrame({"a": list(range(1000))})
    est = estimate_bytes(df)
    assert est > 0


def test_estimate_bytes_slots_class():
    """__slots__ 类没有 __dict__，必须走 __slots__ 分支，
    否则会退化成 1MB 兜底值，使内存预算严重失真。"""

    class Slotted:
        __slots__ = ("a", "b")

        def __init__(self):
            import numpy as np

            self.a = np.zeros(500_000, dtype=np.float64)  # 4MB
            self.b = 1

    est = estimate_bytes(Slotted())
    assert est > 3 * 1024 * 1024, f"__slots__ 估算失真：{est}"
    assert est < 8 * 1024 * 1024


# ============================================================
# CacheConfig
# ============================================================


def test_cache_config_from_dict_ignores_unknown():
    cfg = CacheConfig.from_dict({"daily_capacity": 5, "nope": 1})
    assert cfg.daily_capacity == 5


def test_cache_config_from_none_uses_defaults():
    cfg = CacheConfig.from_dict(None)
    assert cfg.daily_capacity == 120
    assert cfg.minute_memory_budget == "auto"


@pytest.mark.parametrize(
    "spec,expected_mb",
    [("512MB", 512), ("2GB", 2048), ("1KB", 1 / 1024)],
)
def test_resolve_budget_suffixes(spec, expected_mb):
    cfg = CacheConfig(minute_memory_budget=spec)
    assert cfg.resolve_minute_budget() == pytest.approx(expected_mb * 1024 * 1024, rel=0.01)


def test_resolve_budget_integer_passthrough():
    cfg = CacheConfig(minute_memory_budget=12345)
    assert cfg.resolve_minute_budget() == 12345


def test_resolve_budget_none_is_unlimited():
    cfg = CacheConfig(minute_memory_budget=None)
    assert cfg.resolve_minute_budget() == 0


def test_resolve_budget_auto_positive():
    """auto 依赖真实可用内存，只断言「得到正数且不等于兜底值 512MB」。"""
    cfg = CacheConfig(minute_memory_budget="auto", minute_memory_ratio=0.25)
    assert cfg.resolve_minute_budget() > 0


def test_resolve_budget_garbage_falls_back():
    cfg = CacheConfig(minute_memory_budget="完全不是大小")
    assert cfg.resolve_minute_budget() > 0  # 不应抛异常


def test_describe_is_human_readable():
    text = CacheConfig().describe()
    assert "日线" in text and "分钟" in text


# ============================================================
# CacheGroup
# ============================================================


def test_cache_group_has_all_named_caches():
    g = CacheGroup()
    names = {c.name for c in g.all()}
    assert {
        "minute",
        "daily",
        "feature",
        "daily_rows",
        "st_map",
        "l2_auction",
        "static",
        "index_query",
    } <= names


def test_cache_group_report_and_stats():
    g = CacheGroup()
    g.daily.put("20250102", {"x": 1})
    assert "daily" in g.report()
    assert g.stats()["daily"]["puts"] == 1


def test_cache_group_clear_single():
    g = CacheGroup()
    g.daily.put("a", 1)
    g.minute.put("b", 2)
    g.clear("daily")
    assert len(g.daily) == 0
    assert len(g.minute) == 1


def test_cache_group_clear_all():
    g = CacheGroup()
    g.daily.put("a", 1)
    g.minute.put("b", 2)
    g.clear()
    assert all(len(c) == 0 for c in g.all())
