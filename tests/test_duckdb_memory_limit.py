"""DuckDB 缓冲池上限（`memory_limit`）的回归测试。

**为什么必须有这个文件**：这是本项目最隐蔽的一类缺陷 ——
它不改任何数值结果、不报任何错、日志里一行告警都没有，
只是**内存一路涨**，短区间完全看不出来。

根因：DuckDB 的 ``memory_limit`` 默认是**系统内存的 80%**，且缓冲池
``BASE_TABLE`` **只增不减**（读过的表页一直缓存）。本平台的负载是
「每个交易日读不同日期的分区数据、几乎没有页复用」，于是这个缓冲池纯属浪费：

    实测（长区间分钟回测）
      不设上限：duckdb 自报内存按每交易日十几 MB 持续上涨，长区间可累积数十 GB
      设 2GB  ：duckdb 自报内存封顶 1907 MB，RSS 平稳在 4.4 GB

**一个必须知道的行为**：``memory_limit`` 是**数据库实例级**的，不是连接级 ——
同一个库文件的多条连接共享它，**后设者生效**，全部连接关闭后重置为默认。
（DuckDB 在进程内按路径复用数据库实例。）这既解释了为什么本修复有效
（缓冲管理器是实例级的），也意味着测试之间会互相影响，故本文件的断言
不依赖「连接之间互相隔离」。
"""

from __future__ import annotations

import pytest

from ptrade_sim.cache import CacheConfig
from ptrade_sim.data_source import DuckDBSource, make_source
from ptrade_sim.exceptions import ConfigError

pytestmark = pytest.mark.unit


def _gib(text: str) -> float:
    """把 '953.6 MiB' / '1.8 GiB' / '50.0 GiB' 归一为 GiB 数值。"""
    num, unit = text.split()
    factor = {"B": 1 / 1024**3, "KiB": 1 / 1024**2, "MiB": 1 / 1024, "GiB": 1.0, "TiB": 1024.0}
    if unit not in factor:
        raise AssertionError(f"未知单位：{text!r}")
    return float(num) * factor[unit]


def _current_limit(src: DuckDBSource) -> float:
    got = src._con().execute("SELECT current_setting('memory_limit')").fetchone()[0]
    return _gib(got)


# ============================================================
# 默认值与校验（不需要真实库）
# ============================================================


def test_default_memory_limit_is_set():
    """**核心契约**：默认必须设上限。

    若哪天有人把它改成 None（"让 DuckDB 自己管"），长区间回测会重新开始
    无限增长 —— 而这不会让任何测试变红，只会让生产环境 OOM。
    """
    assert DuckDBSource.DEFAULT_MEMORY_LIMIT, (
        "DuckDB memory_limit 默认值不得为空 —— "
        "DuckDB 默认取系统内存 80% 且缓冲池只增不减，长回测会累计数十 GB"
    )


def test_cache_config_carries_the_limit():
    """配置对象必须带上这个字段，否则引擎侧拿不到、等于没设。"""
    assert CacheConfig().duckdb_memory_limit == DuckDBSource.DEFAULT_MEMORY_LIMIT
    assert CacheConfig.from_dict({"duckdb_memory_limit": "1GB"}).duckdb_memory_limit == "1GB"


def test_normalize_accepts_valid_forms():
    for v, want in (
        ("2GB", "2GB"),
        ("512MB", "512MB"),
        ("1.5GB", "1.5GB"),
        ("1500000000", "1500000000"),
        (1073741824, "1073741824"),
        ("  2GB  ", "2GB"),
        (None, None),
    ):
        assert DuckDBSource._norm_limit(v) == want, f"{v!r} 归一错误"


def test_normalize_rejects_sql_injection_forms():
    """该值会被拼进 ``SET memory_limit='...'``，必须挡住引号与分号。"""
    for bad in (
        "2GB; DROP TABLE ashare_1d_stock",
        "'; SET memory_limit='1TB'; --",
        "2GB'",
        "abc",
        "2 GB -- x",
        "",
    ):
        with pytest.raises(ConfigError):
            DuckDBSource._norm_limit(bad)


def test_normalize_rejects_nonpositive_numbers():
    for bad in (0, -1, -1073741824):
        with pytest.raises(ConfigError):
            DuckDBSource._norm_limit(bad)


def test_none_normalizes_to_none():
    """``None`` = 交给 DuckDB 默认（文档明示不推荐，但必须可用）。

    这里只做**归一**断言：``None`` 在连接上读到什么值取决于同实例的其他连接
    （见模块 docstring 的实例级作用域说明），不适合断言具体数值。
    """
    assert DuckDBSource._norm_limit(None) is None


# ============================================================
# 真正传到连接（需要真实库）
# ============================================================


def test_limit_actually_applied_to_connection(tiny_db):
    """不能只存在字段里 —— 必须真的 ``SET`` 到连接上。

    这是「配置写了但没接线」的防护：字段有值、连接没设，
    缺陷会以完全相同的方式复现（内存继续涨）。
    """
    pytest.importorskip("duckdb")
    src = make_source(tiny_db, threads=2, memory_limit="1GB")
    # DuckDB 把 1GB（十进制）换算成 953.6 MiB（二进制），故容差取 0.9±0.1 GiB
    assert abs(_current_limit(src) - 0.9313) < 0.05, (
        f"memory_limit 未生效，读到 {_current_limit(src):.4f} GiB（应约 0.93）"
    )


def test_limit_is_database_instance_scoped(tiny_db):
    """**记录一个反直觉行为**：``memory_limit`` 是库实例级而非连接级。

    这不是我们引入的，而是 DuckDB 的语义（进程内按路径复用数据库实例）。
    写成断言是为了防止日后有人按「连接之间互相隔离」去推理而得出错误结论。

    精确语义（两条）：
      1. ``_con()`` 是**惰性建连**，建连时把本 source 配置的值 SET 上去；
      2. 但该设置落在**库实例**上 —— 任何一条连接后来的 SET 都会覆盖它，
         包括覆盖已经存在的其他连接读到的值。

    也顺带说明本修复为什么有效：缓冲管理器是实例级的，限制它才能真正
    约束表页缓存。
    """
    pytest.importorskip("duckdb")
    a = make_source(tiny_db, threads=2, memory_limit="1GB")
    assert abs(_current_limit(a) - 0.9313) < 0.05, "a 建连时应设为自己的 1GB"

    b = make_source(tiny_db, threads=2, memory_limit="4GB")
    assert abs(_current_limit(b) - 3.7253) < 0.1, "b 建连时应设为自己的 4GB"

    # 关键：b 的 SET 覆盖了共享实例，a 这条**已存在**的连接读到的也是 4GB
    assert abs(_current_limit(a) - 3.7253) < 0.1, (
        "a 读到的应是 b 设的 4GB —— memory_limit 落在库实例上，后设者覆盖"
    )
    # 收尾：设回默认限定值，避免影响同进程内后续测试（同路径共享实例）
    a._con().execute("SET memory_limit='2GB';")


def test_threads_still_applied_alongside_limit(tiny_db):
    """加 memory_limit 时不能把原有的 threads 设置挤掉。"""
    pytest.importorskip("duckdb")
    src = make_source(tiny_db, threads=3, memory_limit="1GB")
    assert src._con().execute("SELECT current_setting('threads')").fetchone()[0] == 3


def test_engine_passes_config_limit_to_source():
    """引擎必须把 ``cache.duckdb_memory_limit`` 一路传到数据源。

    只测 `make_source` 不够 —— 真正的接线点在 `DataFeed.__init__`，
    那里漏传就会「配置有值但引擎不用」。
    """
    pytest.importorskip("duckdb")
    import inspect

    from ptrade_sim import runtime

    src = inspect.getsource(runtime.DataFeed.__init__)
    assert "duckdb_memory_limit" in src, (
        "DataFeed.__init__ 未把 cache.duckdb_memory_limit 传给 make_source —— "
        "引擎侧漏接线，长回测会重新无界增长"
    )
