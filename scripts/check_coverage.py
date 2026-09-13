#!/usr/bin/env python
"""每模块覆盖率下限检查（ratchet）。

**为什么需要**：pyproject.toml 的 ``fail_under`` 是**全局**门槛。全局达标会掩盖
局部裸奔 —— 例如 cache 92% / config 98% 的余量，足以让 dbtools 58% 蒙混过关。
本脚本按模块设下限，任何模块退化都会失败。

**ratchet 语义**：下限锁在「当前实测值 -1%」，**只许升不许降**。
某模块覆盖率涨上去后，应把这里的数字跟着调高；覆盖率掉了就是 CI 失败，
而不是"整体还达标所以无所谓"。

**怎么用**::

    pytest --cov --cov-report=json:coverage.json
    python scripts/check_coverage.py            # 默认读 coverage.json
    python scripts/check_coverage.py --show     # 只打印表格，不做失败判定

CI 里在 pytest 之后跑这一步（见 .github/workflows/ci.yml）。
本地若没生成 coverage.json，本脚本会给出提示而不是报假失败。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

#: 每模块下限（百分比）。改高不改低 —— 见模块 docstring 的 ratchet 说明。
#:
#: 2026-09 第二轮上调：一批新测试把 cli / config / dbtools / history / pipeline /
#: resources / runstore / server 的实测值显著推高，下限随之上调（新实测 -1）。
#: 下调任何一项都会同时触发 ``tests/test_coverage_floors.py::test_ratchet_not_lowered``。
FLOORS: dict[str, int] = {
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

#: 全局下限，与 pyproject.toml 的 fail_under 保持一致
GLOBAL_FLOOR = 70

#: 包目录（用于发现「新增模块但忘了登记下限」）
PACKAGE = Path(__file__).resolve().parent.parent / "src" / "ptrade_sim"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="check-coverage", description="每模块覆盖率下限检查")
    ap.add_argument("--json", default="coverage.json", help="coverage json 路径")
    ap.add_argument("--show", action="store_true", help="只打印，不做失败判定")
    args = ap.parse_args(argv)

    path = Path(args.json)
    if not path.exists():
        print(f"找不到 {path}")
        print(f"请先跑：pytest --cov --cov-report=json:{path}")
        return 0 if args.show else 2

    data = json.loads(path.read_text(encoding="utf-8"))
    files = {Path(f).name: d["summary"]["percent_covered"] for f, d in data["files"].items()}

    print("=" * 74)
    print("每模块覆盖率")
    print("=" * 74)
    print("  {:<20} {:>7} {:>7}   状态".format("模块", "实测", "下限"))
    violations: list[str] = []
    for name in sorted(FLOORS):
        if name not in files:
            # **fail-closed**：登记了下限却没出现在 coverage.json 里，说明
            # [tool.coverage.run] omit 被改了、或测量范围被缩小 —— 这是绕过门禁
            # 的捷径（加一行 omit 就能让任意模块"消失"）。必须判失败而不是提示。
            print(
                "  {:<20} {:>7} {:>6}%   未测到（应从 omit 中移除）".format(name, "-", FLOORS[name])
            )
            violations.append(f"{name}: 未出现在 coverage.json（检查 [tool.coverage.run] omit）")
            continue
        got, floor = files[name], FLOORS[name]
        ok = got >= floor
        print("  {:<20} {:>6.1f}% {:>6}%   {}".format(name, got, floor, "OK" if ok else "低于下限"))
        if not ok:
            violations.append(f"{name}: {got:.1f}% < {floor}%")

    total = data["totals"]["percent_covered"]
    print("-" * 74)
    ok_total = total >= GLOBAL_FLOOR
    print(
        "  {:<20} {:>6.1f}% {:>6}%   {}".format(
            "全局", total, GLOBAL_FLOOR, "OK" if ok_total else "低于下限"
        )
    )
    if not ok_total:
        violations.append(f"全局: {total:.1f}% < {GLOBAL_FLOOR}%")

    on_disk = {p.name for p in PACKAGE.glob("*.py")} - {"__init__.py"}
    missing = sorted(on_disk - set(FLOORS))
    if missing:
        print()
        print("  [警告] 以下模块未登记下限（新增模块容易漏）：{}".format(", ".join(missing)))

    print()
    if violations:
        print("覆盖率退化：")
        for v in violations:
            print("  - " + v)
        return 0 if args.show else 1
    print("所有模块均达标。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
