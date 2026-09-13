"""架构约束测试。

把「不该发生的事」固化成断言 —— 这类约束靠 code review 容易漏，
写成测试才能在 CI 上挡住。
"""

from __future__ import annotations

import re
from pathlib import Path

import polars as pl
import pytest

import ptrade_sim
from ptrade_sim.runtime import DataFeed

pytestmark = pytest.mark.unit

PKG = Path(ptrade_sim.__file__).parent


# ============================================================
# polars / pandas 边界
# ============================================================

#: 允许出现 pandas 的**函数**白名单 —— 全部是 PTrade API 边界。
#:
#: 官方规定 get_history / get_price / get_fundamentals / get_market_* 返回 pandas，
#: 用户策略按 pandas 用法编写（.reset_index/.dt.strftime/布尔掩码），
#: 改成 polars 会让已有策略全部报错，故边界必须保留 pandas。
#:
#: 这些函数分布在三处：
#:   api.py                  —— 55 个 API 的适配层（``get_history`` 等入口）
#:   runtime.BacktestEngine  —— ``_get_history`` 做 PTrade 参数归一（字段/freq 分发）
#:   history.HistoryProvider —— 实际组装 DataFrame（日线/分钟/复权/重采样）
PANDAS_ALLOWED = {
    # runtime：API 适配层
    "get_fundamentals",
    "get_market_list",
    "get_market_detail",
    "_get_history",
    # history：数据组装层（函数名即 HistoryProvider 的方法名）
    "daily",
    "minute",
    "resample_1m",
    "assemble_daily",
    "apply_fq_daily",
    "to_struct",
    "price",
    # 模块级 import 与构造（``import pandas as pd`` 本身）
    "__init__",
    "<module>",
}

#: 允许出现 pandas 的**文件**白名单 —— 边界只许在这两个模块里
PANDAS_ALLOWED_FILES = {"runtime.py", "history.py", "api.py"}

_PANDAS_PAT = re.compile(r"\bpd\.|import pandas|\.to_pandas\(|iterrows\(")


def test_pandas_confined_to_api_boundary():
    """pandas 不得越界到内部通路。

    内部数据通路（DataFeed / 指标 / 看板 / 取数 / 建库）一律 polars。
    这条测试防止后续改动悄悄把 pandas 引回来。
    """
    offenders: list[str] = []
    for f in sorted(PKG.glob("*.py")):
        # 文件级防线：pandas 只许出现在边界模块里
        if f.name not in PANDAS_ALLOWED_FILES and _PANDAS_PAT.search(f.read_text(encoding="utf-8")):
            offenders.append(f"{f.name} —— 整个模块都不该出现 pandas")
        cur = "<module>"
        for i, ln in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            m = re.match(r"\s*def (\w+)", ln)
            if m:
                cur = m.group(1)
            if _PANDAS_PAT.search(ln) and cur not in PANDAS_ALLOWED:
                offenders.append(f"{f.name}:{i} [{cur}] {ln.strip()[:64]}")
    assert not offenders, "pandas 越界到内部通路：\n" + "\n".join(offenders)


#: 看板层（HTTP / 读 run 产物 / 派生指标）—— 与 PTrade API 边界无关，
#: 故这一层任何一个文件都不该出现 pandas。
DASHBOARD_MODULES = ("server.py", "runstore.py", "derived.py")


def test_dashboard_layer_has_no_pandas():
    """看板层与 PTrade API 无关，应完全不含 pandas，且用 polars 读产物。"""
    uses_polars = False
    for name in DASHBOARD_MODULES:
        text = (PKG / name).read_text(encoding="utf-8")
        assert "import pandas" not in text, f"{name} 不应依赖 pandas"
        assert "pd." not in text, f"{name} 不应出现 pd."
        uses_polars = uses_polars or "pl." in text
    assert uses_polars, f"看板层应使用 polars 读产物：{DASHBOARD_MODULES}"


def test_dashboard_layer_dependency_direction():
    """看板层的依赖必须单向：``server → {derived, runstore}``、``derived → runstore``。

    这层拆成三个模块后（HTTP / 读产物 / 派生指标），若哪天 runstore 反过来 import
    server（比如为了拿某个路由常量），就会形成环 —— import 期不报错，运行到才炸。
    """

    def imports_of(name: str) -> set[str]:
        text = (PKG / name).read_text(encoding="utf-8")
        return {
            m.group(1)
            for m in re.finditer(
                r"^from ptrade_sim\.(\w+) import|^from ptrade_sim import ([\w, ]+)", text, re.M
            )
            if m.group(1)
        } | {
            x.strip()
            for m in re.finditer(r"^from ptrade_sim import ([\w, ]+)", text, re.M)
            for x in m.group(1).split(",")
        }

    rs, dv, sv = imports_of("runstore.py"), imports_of("derived.py"), imports_of("server.py")
    assert "server" not in rs and "derived" not in rs, f"runstore 不应反向依赖：{rs}"
    assert "server" not in dv, f"derived 不应依赖 server：{dv}"
    assert "runstore" in dv, "derived 应通过 runstore 读产物"
    assert {"runstore", "derived"} <= sv, f"server 应使用 runstore 与 derived：{sv}"


def test_data_layer_has_no_pandas():
    """取数与建库层不应含 pandas。"""
    for name in ("data_source.py", "dbtools.py", "cache.py", "data_contract.py"):
        text = (PKG / name).read_text(encoding="utf-8")
        assert "import pandas" not in text, f"{name} 不应依赖 pandas"


# ============================================================
# 内部接口返回类型（锁住 polars 迁移结果）
# ============================================================


def test_internal_feeds_return_polars(tiny_db):
    f = DataFeed(str(tiny_db), "20250102", "20250108", preload_mode="rolling", threads=2)
    assert isinstance(f.ensure_daily("20250102"), pl.DataFrame)
    assert isinstance(f.ensure_feature("20250102"), pl.DataFrame)
    assert isinstance(f.valuation_frame(["000001.SZ"], "20250102"), pl.DataFrame)
    assert isinstance(f.basic, pl.DataFrame)


def test_engine_frames_return_polars(engine_factory):
    e = engine_factory()
    e.run()
    assert isinstance(e.daily_stats_frame(), pl.DataFrame)
    assert isinstance(e.trades_frame(), pl.DataFrame)


def test_engine_run_returns_polars(engine_factory):
    e = engine_factory()
    assert isinstance(e.run(), pl.DataFrame)


def test_api_boundary_still_returns_pandas(engine_factory):
    """边界必须仍是 pandas —— 否则已有 PTrade 策略会全部报错。"""
    import pandas as pd

    e = engine_factory(
        "res = {}\n"
        "def initialize(context):\n"
        "    set_benchmark('000300.SS')\n"
        "    set_universe(['000001.SZ'])\n"
        "    run_daily(context, probe, time='14:00')\n"
        "\n"
        "def probe(context):\n"
        "    # 只在最后一天探测：get_history/get_price 需要足够前置历史\n"
        "    if context.blotter.current_dt.strftime('%Y%m%d') != '20250108':\n"
        "        return\n"
        "    if res:\n"
        "        return\n"
        "    res['hist'] = get_history(2, '1d', 'close', '000001.SZ', fq=None)\n"
        "    res['price'] = get_price('000001.SZ', end_date=None, frequency='1d', count=2)\n"
        "    res['fund'] = get_fundamentals(['000001.SZ'], 'valuation', date='20250107')\n"
    )
    e.run()
    res = e._module.__dict__["res"]
    assert res, "探针未执行"
    for key in ("hist", "price", "fund"):
        assert isinstance(res[key], pd.DataFrame), f"{key} 应返回 pandas（官方契约）"

    # 且应支持策略常用的 pandas 用法（这正是不能改 polars 的原因）
    hist = res["hist"]
    assert list(hist.columns) == ["close"]
    assert hist.reset_index().shape[0] == 2, "策略常用的 reset_index 必须可用"
    assert res["fund"].index.name == "secu_code"


# ============================================================
# 死代码防护
# ============================================================


def test_to_data_code_is_gone():
    """`to_data_code` 已删除（改用 to_ptrade_code），不应复活。"""
    from ptrade_sim import runtime

    assert not hasattr(runtime, "to_data_code")
    assert hasattr(runtime, "to_ptrade_code")


def _code_only(text: str) -> str:
    """去掉注释与 docstring，只留可执行代码。

    否则「注释里说明该表已删除」会被误判为「仍在引用」。
    """
    import ast

    lines = text.splitlines()
    drop: set[int] = set()
    # 1) 注释行（本代码库中 # 不出现在字符串里，够用）
    for i, ln in enumerate(lines, 1):
        s = ln.strip()
        if s.startswith("#"):
            drop.add(i)
    # 2) docstring（模块/类/函数级）
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                first = body[0]
                drop.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return "\n".join(ln for i, ln in enumerate(lines, 1) if i not in drop)


def test_no_removed_tables_referenced_in_code():
    """已删表不应在**可执行代码**里被引用（注释/文档说明沿革是允许的）。"""
    for f in sorted(PKG.glob("*.py")):
        code = _code_only(f.read_text(encoding="utf-8"))
        for gone in ("ashare_1d_flag", "ashare_name_change", "ashare_index_member"):
            assert gone not in code, f"{f.name} 仍在代码里引用已删表 {gone}"
