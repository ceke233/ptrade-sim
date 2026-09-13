"""覆盖率下限策略的元测试。

`scripts/check_coverage.py` 里的 FLOORS 是一份**手工维护**的表，容易在两种情况下失效：

1. **新增了模块却忘了登记下限** —— 新模块等于没有保护
2. **有人为了过 CI 把下限调低** —— ratchet 就白设了

这两点都没法靠"跑一次覆盖率"发现，所以在这里用元测试锁住。
（真正的覆盖率判定在 CI 里跑 `scripts/check_coverage.py`，
因为它需要 coverage.json —— 那是 pytest **结束后**才写出的。）
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "ptrade_sim"
SCRIPT = ROOT / "scripts" / "check_coverage.py"


def _load_policy():
    """从 scripts/ 加载下限策略（它不是包，故用 importlib 按路径加载）。"""
    spec = importlib.util.spec_from_file_location("_cov_policy", SCRIPT)
    assert spec and spec.loader, f"无法加载 {SCRIPT}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_policy_script_exists():
    assert SCRIPT.exists(), "缺少 scripts/check_coverage.py"


def test_every_module_has_a_floor():
    """**每个**包内模块都必须登记下限 —— 否则新模块无人保护。"""
    policy = _load_policy()
    on_disk = {p.name for p in PACKAGE.glob("*.py")} - {"__init__.py"}
    registered = set(policy.FLOORS)
    missing = sorted(on_disk - registered)
    assert not missing, (
        f"这些模块没有覆盖率下限，等于无保护：{missing}\n"
        f"请在 scripts/check_coverage.py 的 FLOORS 里补上（取当前实测值 -1）"
    )
    stale = sorted(registered - on_disk)
    assert not stale, f"下限表里有已不存在的模块：{stale}"


def test_floors_are_sane():
    """下限必须在合理区间，且不能为 0（那等于没设）。"""
    policy = _load_policy()
    for name, floor in policy.FLOORS.items():
        assert 0 < floor <= 100, f"{name} 的下限 {floor} 不合理"


def test_global_floor_matches_pyproject():
    """脚本里的全局下限必须与 pyproject 的 fail_under 一致 ——
    两处不一致时，CI 会出现"pytest 过了但脚本失败"的困惑。"""
    import re

    policy = _load_policy()
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r"fail_under\s*=\s*(\d+)", pyproject)
    assert m, "pyproject 里找不到 fail_under"
    assert int(m.group(1)) == policy.GLOBAL_FLOOR, (
        f"pyproject fail_under={m.group(1)} 与脚本 GLOBAL_FLOOR={policy.GLOBAL_FLOOR} 不一致"
    )


def test_ratchet_not_lowered():
    """**ratchet 防回退**：关键模块的下限不得低于已确立的水位。

    这些数字是「已经达到过的水平」。若有人为了过 CI 把它们调低，
    这条测试会失败 —— 正确做法是补测试，而不是降标准。
    """
    policy = _load_policy()
    # 实测水位（2026-09 第二轮上调）；只允许更高。
    # 每次覆盖率因补测试而上升时，把对应模块的水位一起提上来 ——
    # 否则"这次涨了、下次掉回去"没人拦。
    WATERMARK = {
        "api.py": 78,
        "cache.py": 91,
        "cli.py": 85,
        "config.py": 97,
        "conventions.py": 72,
        "data_contract.py": 96,
        "data_source.py": 68,
        "dbtools.py": 70,
        "derived.py": 72,
        "exceptions.py": 99,
        "history.py": 78,
        "pipeline.py": 94,
        "queue.py": 90,
        "resources.py": 99,
        "runstore.py": 78,
        "runtime.py": 75,
        "server.py": 93,
    }
    for name, low in WATERMARK.items():
        assert name in policy.FLOORS, f"{name} 的下限被删了"
        assert policy.FLOORS[name] >= low, (
            f"{name} 的下限被从 {low}% 降到 {policy.FLOORS[name]}% —— "
            f"ratchet 只许升不许降；请补测试而不是降标准"
        )


def test_watermark_covers_every_floor():
    """**水位表必须与下限表覆盖同一批模块。**

    这个漏洞是实测出来的：WATERMARK 曾漏登记 conventions / data_source / derived
    三个模块（恰好是当时覆盖率最低的三个）。把它们的下限从 72/68/72 改成 1，
    ``check_coverage.py``（exit 0）与全部元测试**一起放行** —— ratchet 形同虚设。

    注意本测试**不能**改成"从 FLOORS 自动生成 WATERMARK"：那样
    ``FLOORS >= WATERMARK`` 会平凡成立，防回退就没了。两者必须是独立的字面表，
    只有**键集合**相等。
    """
    policy = _load_policy()
    src = (ROOT / "tests" / "test_coverage_floors.py").read_text(encoding="utf-8")
    m = re.search(r"WATERMARK = \{(.*?)\n    \}", src, re.S)
    assert m, "找不到 WATERMARK 字面表"
    marked = set(re.findall(r'"([\w.]+\.py)"', m.group(1)))
    assert marked == set(policy.FLOORS), (
        f"WATERMARK 与 FLOORS 的模块集合不一致：\n"
        f"  仅在水位表（FLOORS 里没有）: {sorted(marked - set(policy.FLOORS))}\n"
        f"  仅在下限表（水位表漏登记，无防回退保护）: {sorted(set(policy.FLOORS) - marked)}"
    )
