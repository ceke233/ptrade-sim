"""「取数失败不得静默」的回归测试。

**为什么必须有这个文件**：这是一个**会让回测结果算错却报「完成」**的缺陷 ——
项目明确要根治的那一类。

实测证据（长区间、分钟、全市场选股）：

    同一次回测区间、同一策略，两次运行的差异**只来自取数失败**：
      有 7 次 DuckDB 查询失败（Out of Memory，被吞成「无数据」）
          → 收益与成交与真实情况量级不符        ← **错的**
      0 次失败
          → 基准（无失败）      ← 对的

`_q` 只留了一行 `WARNING`。在 28000 行 INFO 日志里，这 7 行被完全淹没，
而 run 目录照常产出 `summary.json`、日志照常打印「回测完成」。
用户拿到的是一份**看起来很正常的错误结果**。

所以现在的契约是：**取数失败必须让整次回测非零退出，并把明细落进 summary.json。**
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ptrade_sim.data_source import DuckDBSource

pytestmark = pytest.mark.unit


# ============================================================
# 记录侧
# ============================================================


def test_no_errors_means_empty_list(tiny_db):
    """正常情况必须返回空 —— 否则每次回测都会误报失败。"""
    pytest.importorskip("duckdb")
    from ptrade_sim.data_source import make_source

    src = make_source(tiny_db, threads=2)
    assert src.data_errors() == []


def test_errors_are_recorded_and_described(tiny_db):
    """失败必须被记下来，且摘要要说清「结果不可信」而不是只说「有告警」。"""
    pytest.importorskip("duckdb")
    from ptrade_sim.data_source import make_source

    src = make_source(tiny_db, threads=2)
    src._note_error("SELECT a FROM t WHERE date = ?", RuntimeError("Out of Memory Error"))
    lines = src.data_errors()
    assert lines, "记录失败后 data_errors() 不应为空"
    joined = "\n".join(lines)
    assert "1 次" in joined, f"应报出次数：{joined}"
    assert "不可信" in joined, f"应明确说明后果，而不只是「有告警」：{joined}"
    assert "Out of Memory" in joined, "应带上原始异常信息以便排查"
    assert "SELECT a FROM t" in joined, "应带上出错的 SQL 以便定位哪类取数"


def test_recorded_errors_are_bounded(tiny_db):
    """长回测可能失败成千上万次，只留样本 —— 全留会堆爆内存。"""
    pytest.importorskip("duckdb")
    from ptrade_sim.data_source import make_source

    src = make_source(tiny_db, threads=2)
    n = DuckDBSource.MAX_RECORDED_ERRORS * 3
    for i in range(n):
        src._note_error(f"SELECT {i}", RuntimeError("boom"))
    assert src.query_error_count == n, "次数必须如实累计（摘要要报真实总数）"
    assert len(src.query_errors) == DuckDBSource.MAX_RECORDED_ERRORS, "样本条数应有上限"
    summary = "\n".join(src.data_errors())
    assert f"{n} 次" in summary, "摘要应报真实总次数而非样本数"
    assert "仅记录前" in summary, "截断时必须说明，否则用户以为只有这几条"


def test_async_failure_is_counted_not_silently_dropped(tiny_db):
    """走真实 `_q` 路径：查询失败应被计数（而不只是打日志）。"""
    pytest.importorskip("duckdb")
    from ptrade_sim.data_source import make_source

    src = make_source(tiny_db, threads=2)
    # 查一个不存在的表 -> _q 内部捕异常并返回 None
    assert src._q("SELECT * FROM no_such_table_xyz", []) is None
    assert src.query_error_count == 1, "_q 的失败必须计数，否则收尾时无法发现"
    assert src.data_errors()

    # 但正常查询不该被计数
    src2 = make_source(tiny_db, threads=2)
    src2._q("SELECT 1 AS a", [])
    assert src2.query_error_count == 0


# ============================================================
# 落盘与退出码（接线）
# ============================================================


def test_pipeline_writes_data_errors_into_summary():
    """`summary.json` 必须带 `data_errors` 字段 —— 结果不可信要能事后追溯。"""
    from ptrade_sim import pipeline

    src = Path(pipeline.__file__).read_text(encoding="utf-8")
    assert 'summary["data_errors"]' in src, (
        "pipeline 未把取数失败写进 summary.json —— 用户事后无从发现结果不可信"
    )


def test_pipeline_fails_loudly_on_data_errors():
    """接线检查：有取数失败时必须以非零码退出。

    若这里退回 `return 0`，缺陷就完全隐形了 —— 这正是本文件要防的。
    """
    from ptrade_sim import pipeline

    src = Path(pipeline.__file__).read_text(encoding="utf-8")
    assert "if data_errors:" in src, "缺少取数失败的判定分支"
    assert "return EXIT_DATA" in src, "取数失败必须非零退出，不能只打日志"
    assert pipeline.EXIT_DATA != 0


def test_exit_data_code_is_distinct():
    """取数失败的退出码要与其它类别区分开，便于脚本分支处理。"""
    from ptrade_sim import pipeline

    assert pipeline.EXIT_DATA != 0, "0 表示成功，取数失败不能占"
    assert pipeline.EXIT_DATA not in (
        pipeline.EXIT_CONFIG,
        pipeline.EXIT_RESOURCE,
        1,
    ), "取数失败应是与配置/资源/未分类都不同的独立退出码"
