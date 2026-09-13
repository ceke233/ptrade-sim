"""`StrategyBundle.stem` 的回归测试：结果目录名必须是**策略名**。

**为什么单列**：结果目录名是用户直接看到的东西（看板里、文件系统里）。
早期实现写作 ``dir.name or py.stem``，而**单文件形态**的 ``dir`` 是父目录 ——
于是 ``examples/yijin2_5x892.py`` 跑出来的结果目录叫 ``examples-<时间戳>``：
名不副实，且同目录下多个单文件策略的结果目录**互无法区分**（只差时间戳）。

本文件锁住两种形态的命名，防止再次回退到「从 dir 推导」。
"""

from __future__ import annotations

import json

import pytest

from ptrade_sim.config import resolve_strategy

pytestmark = pytest.mark.unit


def test_single_file_stem_is_file_name_not_parent_dir(tmp_path):
    """单文件策略的结果目录前缀应为**文件名**，而不是它所在的目录名。"""
    folder = tmp_path / "strategies"
    folder.mkdir()
    sp = folder / "yijin2_5x892.py"
    sp.write_text("def initialize(context):\n    pass\n", encoding="utf-8")

    b = resolve_strategy(sp)
    assert b.stem == "yijin2_5x892", (
        f"结果目录前缀应为文件名，实际 {b.stem!r} —— "
        f"若为 {folder.name!r} 说明又退回了「取父目录名」"
    )
    assert b.dir == folder, "dir 仍应是父目录（取数/相对路径要用）"
    assert b.name == "yijin2_5x892"


def test_two_single_files_in_same_folder_get_distinct_stems(tmp_path):
    """同一目录下多个单文件策略必须能区分 —— 这正是旧行为的要害。

    旧实现下两者 stem 都是目录名，结果目录只差时间戳，无法分辨谁是谁。
    """
    folder = tmp_path / "strategies"
    folder.mkdir()
    a = folder / "alpha.py"
    b = folder / "beta.py"
    for f in (a, b):
        f.write_text("def initialize(context):\n    pass\n", encoding="utf-8")

    sa, sb = resolve_strategy(a).stem, resolve_strategy(b).stem
    assert sa == "alpha" and sb == "beta"
    assert sa != sb, "同目录两个单文件策略的结果目录前缀不得相同"


def test_folder_stem_is_folder_name(tmp_path):
    """目录形态仍取目录名（含 strategy.py 的标准布局）。"""
    d = tmp_path / "demo_rotation"
    d.mkdir()
    (d / "strategy.py").write_text("def initialize(context):\n    pass\n", encoding="utf-8")

    b = resolve_strategy(d)
    assert b.stem == "demo_rotation"
    assert b.dir == d


def test_strategy_config_name_does_not_change_stem(tmp_path):
    """``strategy_config.json`` 的 ``name`` 只影响**展示名**，不影响结果目录前缀。

    两者用途不同：name 是给人看的（可含中文/空格），
    stem 是给文件系统与看板 URL 用的（必须稳定且安全）。
    """
    folder = tmp_path / "strategies"
    folder.mkdir()
    sp = folder / "yijin2.py"
    sp.write_text("def initialize(context):\n    pass\n", encoding="utf-8")
    (folder / "strategy_config.json").write_text(
        json.dumps({"name": "一进二 · 创业板"}, ensure_ascii=False), encoding="utf-8"
    )

    b = resolve_strategy(sp)
    assert b.name == "一进二 · 创业板", "展示名应取配置里的 name"
    assert b.stem == "yijin2", f"结果目录前缀仍应是文件名，实际 {b.stem!r}"


def test_stem_is_a_plain_string_attribute(tmp_path):
    """``stem`` 是显式字段而非属性推导 —— 防止有人改回从 dir 猜。"""
    folder = tmp_path / "s"
    folder.mkdir()
    sp = folder / "x.py"
    sp.write_text("def initialize(context):\n    pass\n", encoding="utf-8")
    b = resolve_strategy(sp)
    assert "stem" in b._fields, f"stem 应是 NamedTuple 字段，实际字段：{b._fields}"
    assert isinstance(b.stem, str)
