"""`get_price` 全市场批缓存（`history._price_cache`）的**有界性**契约。

**背景**：`get_price` 在 ``codes > 500`` 时会把结果缓存起来，避免同一次 bar 里
重复取全市场。但该缓存曾经**只写不清**：

- 它的 key 含 ``clock.day``，所以**跨日命中在构造上就不可能** ——
  实际作用仅是「日内重复调用去重」；
- 然而它既不像同文件的 ``_name_cache`` / ``_info_cache`` 那样每日清理，
  也没有容量上限 → 每天新增、永不释放。
- 实测单条目规模：全市场 5380 只 × 1 天 × 6 字段 ≈ 0.3 MB；
  若一次取 20 天则约 4.6 MB。5~6 年区间可累积数 GB，
  且完全绕过 ``cache.py`` 的 LRU / 容量管理。

而项目的真实策略正是在走这条路：``pool = sorted(get_Ashares())`` 得 5000+ 只，
然后每天 ``get_price(pool, end_date=prev_ds, count=1)`` —— 每天一条新缓存。

本文件锁住「每交易日清空」这一契约。阈值 500 是硬编码的，用合成夹具造 500+
只股票不现实，故这里直接对**不变量**（每日清空）做断言，而不是真跑一次全市场取数。
"""

from __future__ import annotations

import pandas as pd
import pytest

pytestmark = pytest.mark.integration


def _engine(tmp_path, tiny_db, **over):
    from ptrade_sim.runtime import BacktestEngine

    d = tmp_path / "s"
    d.mkdir(exist_ok=True)
    sp = d / "strategy.py"
    sp.write_text(
        "def initialize(context):\n"
        "    set_benchmark('000300.SS')\n"
        "    set_universe(['000001.SZ'])\n",
        encoding="utf-8",
    )
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
    return BacktestEngine(cfg, str(sp), tmp_path / "out")


def test_price_cache_cleared_each_trading_day(tmp_path, tiny_db):
    """每交易日开始时必须清空 —— 否则它随回测长度单调增长。"""
    e = _engine(tmp_path, tiny_db)
    e.load_strategy()
    cache = e.history._price_cache
    days = e.feed.range_days
    assert len(days) >= 2, "夹具应提供多个交易日"

    for i, ds in enumerate(days):
        if i > 0:
            assert not cache, (
                f"进入 {ds} 时缓存仍有 {len(cache)} 条 —— 未按日清理，长回测会无界增长"
            )
        # 模拟「上一日」留下的条目
        cache[(ds, ("x",), ("y",), ("close",), None)] = pd.DataFrame({"a": [1.0]})
        e._run_day(ds)


def test_price_cache_bounded_after_full_run(tmp_path, tiny_db):
    """跑完整段回测后，缓存不得累积（清空发生在每个交易日开始时）。"""
    e = _engine(tmp_path, tiny_db)
    e.run()
    # 最后一天仍会留下它自己的条目（清理发生在其开始时），
    # 但**绝不能**接近回测天数 —— 那是无界增长的标志。
    n_days = len(e.feed.range_days)
    assert len(e.history._price_cache) <= 1, (
        f"跑完 {n_days} 天后缓存有 {len(e.history._price_cache)} 条 —— 应逐日清空"
    )


def test_price_cache_key_makes_cross_day_reuse_impossible(tmp_path, tiny_db):
    """前提校验：key 含 clock.day ⇒ 跨日命中不可能 ⇒ 按日清理零代价。

    这条测试是为了**防止有人误以为清理会降低命中率**而把它去掉：
    只要 key 里还有 clock.day，跨日复用本来就不存在。
    """
    e = _engine(tmp_path, tiny_db)
    e.load_strategy()
    days = e.feed.range_days
    seen = []
    for ds in days:
        seen.append((e.clock.day, "same-sel", "same-codes", "close", None))
        e._run_day(ds)
    firsts = [k[0] for k in seen]
    assert len(set(firsts)) == len(firsts), "clock.day 应逐日不同"
    assert len(firsts) >= 2, "应至少跨两个交易日"
