"""统一缓存：容量上限 + 内存预算 + LRU。

替换原先散落在 ``DataFeed`` 里的 9 个手写缓存（``_minute_cache`` 的 OrderedDict LRU、
``_daily_cache``/``_feature_cache`` 的无上限 dict、``_daily_rows``/``_basic_dict``/
``_st_cache`` 的懒构建 dict、``_ashares_cache``、``_index_members``、``_index_query_cache``）。

两种淘汰策略
------------
- **条数上限**（``capacity``）：适合小对象（日线行、估值、日历），
  ``None`` 表示不淘汰（永久缓存，用于体积小且必用的表）。
- **内存预算**（``memory_budget``）：适合大对象（分钟数据）。
  预算由 :class:`CacheConfig` 依可用内存自动推导（见设计文档 §5.2 的
  ``minute_memory_budget="auto"`` + ``minute_memory_ratio=0.25``），
  超出即按 LRU 逐出，实现"预算内最近 N 日热缓存"的流式语义。

统计（``hits``/``misses``/``evictions``/``bytes``）可用于回测结束后的资源报告，
也是"自动探查性能瓶颈"的依据。
"""

from __future__ import annotations

import sys
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

MISSING = object()

_MB = 1024 * 1024
_GB = 1024**3


# ============================================================
# 配置
# ============================================================


@dataclass
class CacheConfig:
    """缓存策略配置（可直接由 config.json 的 ``cache`` 段构造）。

    ``minute_memory_budget``：
        ``"auto"``（默认）按可用内存 × ``minute_memory_ratio`` 推导；
        也可给 ``"512MB"`` / ``"2GB"`` / 字节数；``None`` 表示不限（不推荐）。
    """

    daily_capacity: int | None = 120
    feature_capacity: int | None = 120
    minute_memory_budget: str | int | None = "auto"
    minute_memory_ratio: float = 0.25
    minute_bytes_per_day: int = 40 * _MB
    aux_capacity: int | None = None  # 静态表（basic/index_member 等）默认不淘汰
    #: DuckDB 缓冲池上限（``"2GB"`` / ``"512MB"`` / 字节数；``None`` = DuckDB 默认）。
    #:
    #: **必须设**：DuckDB 默认上限是系统内存的 80%，且缓冲池**只增不减** ——
    #: 本平台每天读不同日期的分区数据、几乎没有页复用，实测约 18 MB/天累积
    #: 长区间回测会累积到数十 GB）。2GB 实测可让占用平稳。
    duckdb_memory_limit: str | int | None = "2GB"

    @classmethod
    def from_dict(cls, d: dict | None) -> CacheConfig:
        d = d or {}
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})

    def resolve_minute_budget(self) -> int:
        """把 ``minute_memory_budget`` 解析为字节数；``None``/解析失败 → 0（表示不限）。"""
        b = self.minute_memory_budget
        if b is None:
            return 0
        if isinstance(b, (int, float)):
            return int(b)
        s = str(b).strip().upper()
        if s in ("AUTO", ""):
            try:
                from ptrade_sim.resources import probe_memory

                _, avail = probe_memory()
            except Exception:
                avail = 0
            if not avail:
                return 512 * _MB  # 探测失败回退固定默认
            return int(avail * float(self.minute_memory_ratio))
        try:
            num = float("".join(c for c in s if c.isdigit() or c == "."))
            if "GB" in s:
                return int(num * _GB)
            if "MB" in s:
                return int(num * _MB)
            if "KB" in s:
                return int(num * 1024)
            return int(num)
        except Exception:
            return 512 * _MB

    def describe(self) -> str:
        budget = self.resolve_minute_budget()
        b = f"{budget / _MB:,.0f} MB" if budget else "不限"
        days = f"{budget // self.minute_bytes_per_day} 天" if budget else "-"
        return (
            f"日线缓存 {self.daily_capacity or '∞'} 条 / "
            f"估值 {self.feature_capacity or '∞'} 条 / "
            f"分钟内存预算 {b}（约 {days}）"
        )


# ============================================================
# 缓存实现
# ============================================================


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    puts: int = 0
    bytes: int = 0  # 当前占用（仅内存预算型有意义）
    peak_bytes: int = 0
    peak_items: int = 0
    bytes_estimated: bool = False  # 是否用了估算而非实测

    def as_dict(self) -> dict:
        total = self.hits + self.misses
        return {
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 4) if total else 0.0,
            "evictions": self.evictions,
            "puts": self.puts,
            "items_bytes": self.bytes,
            "peak_bytes": self.peak_bytes,
            "peak_items": self.peak_items,
            "bytes_estimated": self.bytes_estimated,
        }


class Cache:
    """LRU 缓存，支持「条数上限」或「内存预算」两种淘汰依据。

    - ``capacity`` 与 ``memory_budget`` 可同时给：任一超限都逐出。
    - 二者都为 ``None`` → 永久缓存（不淘汰）。
    - ``size_of`` 用于估算单个对象字节数；缺省用 :func:`estimate_bytes`。
    """

    __slots__ = (
        "_budget",
        "_bytes",
        "_capacity",
        "_d",
        "_size_of",
        "name",
        "stats",
    )

    def __init__(
        self,
        name: str,
        capacity: int | None = None,
        memory_budget: int = 0,
        size_of: Callable[[Any], int] | None = None,
    ):
        self.name = name
        self._d: OrderedDict[Any, Any] = OrderedDict()
        self._capacity = capacity
        self._budget = int(memory_budget or 0)
        self._size_of = size_of or estimate_bytes
        self._bytes = 0
        self.stats = CacheStats()

    # ---------- 基本操作 ----------
    def get(self, key: Any) -> Any:
        v = self._d.get(key, MISSING)
        if v is MISSING:
            self.stats.misses += 1
            return MISSING
        self._d.move_to_end(key)  # LRU：命中即刷新
        self.stats.hits += 1
        return v

    def put(self, key: Any, value: Any) -> None:
        if key in self._d:
            self._bytes -= self._size_of(self._d[key])
            del self._d[key]
        self._d[key] = value
        if self._budget:
            self._bytes += self._size_of(value)
        self.stats.puts += 1
        self._evict_if_needed()
        self.stats.peak_items = max(self.stats.peak_items, len(self._d))
        if self._budget:
            self.stats.bytes = self._bytes
            self.stats.peak_bytes = max(self.stats.peak_bytes, self._bytes)

    def get_or_load(self, key: Any, loader: Callable[[], Any]) -> Any:
        """命中即返回；未命中调用 ``loader()`` 并缓存其非 None 结果。"""
        v = self.get(key)
        if v is not MISSING:
            return v
        v = loader()
        if v is not None:
            self.put(key, v)
        return v

    def __contains__(self, key: Any) -> bool:
        return key in self._d

    def __len__(self) -> int:
        return len(self._d)

    def clear(self) -> None:
        self._d.clear()
        self._bytes = 0
        self.stats.bytes = 0

    # ---------- 淘汰 ----------
    def _evict_if_needed(self) -> None:
        if self._capacity is not None:
            while len(self._d) > self._capacity > 0:
                self._pop_oldest()
        if self._budget:
            while self._bytes > self._budget and len(self._d) > 1:
                self._pop_oldest()

    def _pop_oldest(self) -> None:
        try:
            _k, v = self._d.popitem(last=False)
        except KeyError:
            return
        if self._budget:
            self._bytes -= self._size_of(v)
            self.stats.bytes = self._bytes
        self.stats.evictions += 1

    # ---------- 报告 ----------
    def describe(self) -> str:
        cap = self._capacity if self._capacity is not None else "∞"
        bud = f"{self._budget / _MB:,.0f}MB" if self._budget else "-"
        s = self.stats
        return (
            f"{self.name}: {len(self._d)} 项（上限 {cap} 条 / 预算 {bud}） "
            f"命中率 {s.as_dict()['hit_rate']:.1%} 逐出 {s.evictions}"
        )


def estimate_bytes(obj: Any) -> int:
    """估算对象内存占用（字节）。

    对 polars DataFrame / numpy 数组走真实 nbytes，其余用浅层估算；
    估算失败回退 1MB。用于内存预算型缓存的逐出决策。
    """
    if obj is None:  # 缓存会记录「该日缺失」这一事实，占位极小
        return 64
    try:
        import polars as pl

        if isinstance(obj, pl.DataFrame):
            return int(obj.estimated_size())
    except Exception:
        pass
    for attr in ("nbytes", "memory_usage"):
        try:
            v = getattr(obj, attr)
            v = v() if callable(v) else v
            if isinstance(v, int):
                return int(v)
        except Exception:
            pass
    try:
        import numpy as np

        if isinstance(obj, np.ndarray):
            return int(obj.nbytes)
    except Exception:
        pass
    # 自定义对象：优先累加 numpy 数组的 nbytes。
    # 注意 __slots__ 类（如 DayMinuteData）没有 __dict__，需读 type(obj).__slots__，
    # 否则会静默退化成兜底估算值，导致内存预算严重失真。
    try:
        names = getattr(obj, "__dict__", None)
        if names:
            items = names.items()
        else:
            slots = getattr(type(obj), "__slots__", ())
            items = ((s, getattr(obj, s, None)) for s in slots)
        tot = sys.getsizeof(obj)
        for k, v in items:
            if k == "starts" and isinstance(v, dict):
                # code -> (start, end)：每项约 2 个 int + 字符串键
                tot += len(v) * 96
                continue
            n = getattr(v, "nbytes", None)
            tot += int(n) if isinstance(n, int) else 64
        return tot
    except Exception:
        pass
    return _MB


# ============================================================
# 缓存组（一次持有全部，便于统一报告与清空）
# ============================================================


class CacheGroup:
    """DataFeed 的全部缓存集合，按用途分类并按配置限流。"""

    def __init__(self, cfg: CacheConfig | None = None):
        self.cfg = cfg or CacheConfig()
        budget = self.cfg.resolve_minute_budget()
        # 大对象：分钟数据（内存预算 + LRU）
        self.minute = Cache("minute", capacity=None, memory_budget=budget, size_of=estimate_bytes)
        # 中对象：按条数限流
        self.daily = Cache("daily", capacity=self.cfg.daily_capacity)
        self.feature = Cache("feature", capacity=self.cfg.feature_capacity)
        # 派生小对象：与 daily 同容量（同一批日的行字典/ST 映射）
        self.daily_rows = Cache("daily_rows", capacity=self.cfg.daily_capacity)
        self.st_feat = Cache("st_map", capacity=self.cfg.daily_capacity)
        self.l2_auction = Cache("l2_auction", capacity=self.cfg.daily_capacity)
        # 静态/参考：默认永久
        self.static = Cache("static", capacity=self.cfg.aux_capacity)
        self.index_query = Cache("index_query", capacity=self.cfg.aux_capacity)

    def all(self) -> list[Cache]:
        return [
            self.minute,
            self.daily,
            self.feature,
            self.daily_rows,
            self.st_feat,
            self.l2_auction,
            self.static,
            self.index_query,
        ]

    def report(self) -> dict:
        return {c.name: c.describe() for c in self.all()}

    def stats(self) -> dict:
        return {c.name: c.stats.as_dict() for c in self.all()}

    def clear(self, name: str | None = None) -> None:
        for c in self.all():
            if name is None or c.name == name:
                c.clear()

    def describe(self) -> str:
        return self.cfg.describe()
