"""策略目录读取测试（strategy.py + strategy_config.json）。

新形态：一个策略一个目录，配置与代码放在一起。旧的单文件写法仍兼容。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ptrade_sim import config as C

pytestmark = pytest.mark.unit

PY_OK = (
    "def initialize(context):\n    set_benchmark('000300.SS')\n    set_universe(['000001.SZ'])\n"
)


def _mk(tmp_path: Path, name: str = "s1", cfg: dict | None = None, py: str = "strategy.py"):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    (d / py).write_text(PY_OK, encoding="utf-8")
    if cfg is not None:
        (d / "strategy_config.json").write_text(
            json.dumps(cfg, ensure_ascii=False), encoding="utf-8"
        )
    return d


# ============================================================
# 解析
# ============================================================


def test_resolve_folder_with_strategy_py(tmp_path):
    d = _mk(tmp_path, cfg={"name": "我的策略", "start_date": "2025-01-01"})
    b = C.resolve_strategy(d)
    assert b.py == d / "strategy.py"
    assert b.dir == d
    assert b.name == "我的策略"
    assert b.config["start_date"] == "2025-01-01"
    assert b.stem == "s1"


def test_resolve_folder_without_config(tmp_path):
    d = _mk(tmp_path)
    b = C.resolve_strategy(d)
    assert b.config == {}
    assert b.name == "s1", "无 name 时退回目录名"


def test_resolve_folder_single_arbitrary_py(tmp_path):
    """目录里只有一个 .py 且不叫 strategy.py 时也能用。"""
    d = _mk(tmp_path, py="my_logic.py")
    b = C.resolve_strategy(d)
    assert b.py.name == "my_logic.py"


def test_resolve_folder_multiple_py_without_strategy_py_errors(tmp_path):
    """多个 .py 且无 strategy.py 必须报错 —— 静默挑一个会让人跑错策略。"""
    d = _mk(tmp_path, py="a.py")
    (d / "b.py").write_text(PY_OK, encoding="utf-8")
    with pytest.raises(ValueError) as e:
        C.resolve_strategy(d)
    assert "a.py" in str(e.value) and "b.py" in str(e.value)


def test_resolve_folder_multiple_py_with_strategy_py_wins(tmp_path):
    """有 strategy.py 时它优先，其余 .py 当作模块（如被 import 的工具）。"""
    d = _mk(tmp_path)
    (d / "helper.py").write_text("X = 1\n", encoding="utf-8")
    assert C.resolve_strategy(d).py.name == "strategy.py"


def test_resolve_empty_folder_errors(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    with pytest.raises(FileNotFoundError):
        C.resolve_strategy(d)


def test_resolve_single_file_backward_compatible(tmp_path):
    """旧的单文件写法仍可用。"""
    p = tmp_path / "demo.py"
    p.write_text(PY_OK, encoding="utf-8")
    b = C.resolve_strategy(p)
    assert b.py == p
    assert b.name == "demo"


def test_resolve_single_file_picks_up_sibling_config(tmp_path):
    """单文件策略若同目录有 strategy_config.json 也读取（便于渐进迁移）。"""
    p = tmp_path / "demo.py"
    p.write_text(PY_OK, encoding="utf-8")
    (tmp_path / "strategy_config.json").write_text(json.dumps({"name": "迁移中"}), encoding="utf-8")
    assert C.resolve_strategy(p).name == "迁移中"


def test_resolve_missing_path_errors(tmp_path):
    with pytest.raises(FileNotFoundError):
        C.resolve_strategy(tmp_path / "nope")


def test_resolve_non_py_file_errors(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("hi", encoding="utf-8")
    with pytest.raises(ValueError):
        C.resolve_strategy(p)


def test_resolve_relative_path_uses_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _mk(tmp_path, name="rel")
    b = C.resolve_strategy("rel")
    assert b.dir == tmp_path / "rel"


# ============================================================
# 策略配置作为配置层
# ============================================================


def test_strategy_config_overrides_local_file(tmp_path):
    d = _mk(tmp_path, cfg={"start_date": "2030-01-01", "capital_base": 7})
    base = tmp_path / "c.json"
    base.write_text(
        json.dumps({"start_date": "2001-01-01", "capital_base": 100, "db_path": "X"}),
        encoding="utf-8",
    )
    cfg = C.load(base, strategy_config=d and C.resolve_strategy(d).config)
    assert cfg["start_date"] == "2030-01-01", "策略级应覆盖机器级"
    assert cfg["capital_base"] == 7
    assert cfg["db_path"] == "X", "未在策略级出现的键应保留"


def test_env_overrides_strategy_config(tmp_path, monkeypatch):
    d = _mk(tmp_path, cfg={"start_date": "2030-01-01"})
    monkeypatch.setenv("PT_SIM_START_DATE", "2040-01-01")
    cfg = C.load(strategy_config=C.resolve_strategy(d).config)
    assert cfg["start_date"] == "2040-01-01", "env 应高于策略级"


def test_extra_overrides_strategy_config(tmp_path):
    d = _mk(tmp_path, cfg={"start_date": "2030-01-01"})
    cfg = C.load(strategy_config=C.resolve_strategy(d).config, extra={"start_date": "2050-01-01"})
    assert cfg["start_date"] == "2050-01-01", "CLI 应最高"


def test_strategy_config_deep_merges_nested(tmp_path):
    """嵌套段应逐层合并，不能整体替换掉机器级设置。"""
    d = _mk(tmp_path, cfg={"preload": {"threads": 2}})
    cfg = C.load(strategy_config=C.resolve_strategy(d).config)
    assert cfg["preload"]["threads"] == 2, "策略级覆盖该项"
    assert cfg["preload"]["mode"] == "rolling", "同段其他键应保留"


def test_strategy_config_underscore_keys_are_comments(tmp_path):
    d = _mk(tmp_path, cfg={"_comment": "说明", "name": "N", "capital_base": 5})
    b = C.resolve_strategy(d)
    assert "_comment" not in b.config
    assert "name" in b.config and b.config["capital_base"] == 5


def test_corrupt_strategy_config_warns_but_continues(tmp_path):
    d = tmp_path / "bad"
    d.mkdir()
    (d / "strategy.py").write_text(PY_OK, encoding="utf-8")
    (d / "strategy_config.json").write_text("{ not json", encoding="utf-8")
    b = C.resolve_strategy(d)  # 不应抛
    assert b.config == {}
    assert b.name == "bad"


def test_strategy_config_non_object_ignored(tmp_path):
    d = tmp_path / "arr"
    d.mkdir()
    (d / "strategy.py").write_text(PY_OK, encoding="utf-8")
    (d / "strategy_config.json").write_text("[1,2]", encoding="utf-8")
    assert C.resolve_strategy(d).config == {}


def test_load_tolerates_bad_strategy_target(tmp_path, monkeypatch):
    """策略路径坏掉时 config.load 应告警并继续，而不是让整个加载失败。"""
    monkeypatch.chdir(tmp_path)
    cfg = C.load(strategy=str(tmp_path / "nope"))
    assert cfg["capital_base"] == C.DEFAULTS["capital_base"]


# ============================================================
# 策略入参
# ============================================================


def test_strategy_params_exposed_to_strategy(engine_factory, tmp_path):
    """``strategy_config.json`` 的 params 段应可被策略通过 get_strategy_params 读取。"""
    e = engine_factory(
        "res = {}\n"
        "def initialize(context):\n"
        "    set_benchmark('000300.SS')\n"
        "    set_universe(['000001.SZ'])\n"
        "    res['all'] = get_strategy_params()\n"
        "    res['one'] = get_strategy_params('max_hold')\n"
        "    res['dflt'] = get_strategy_params('missing', 42)\n"
        "    res['mutate'] = get_strategy_params()\n"
        "    res['mutate']['max_hold'] = 999\n"
        "    res['after'] = get_strategy_params('max_hold')\n",
        params={"max_hold": 3, "threshold": 0.05},
    )
    e.run()
    res = e._module.__dict__["res"]
    assert res["all"] == {"max_hold": 3, "threshold": 0.05}
    assert res["one"] == 3
    assert res["dflt"] == 42
    assert res["after"] == 3, "返回的是副本，策略改它不应影响配置"


def test_strategy_params_empty_when_unset(engine_factory):
    e = engine_factory(
        "res = {}\n"
        "def initialize(context):\n"
        "    set_benchmark('000300.SS')\n"
        "    set_universe(['000001.SZ'])\n"
        "    res['all'] = get_strategy_params()\n"
        "    res['d'] = get_strategy_params('x', 'fallback')\n",
    )
    e.run()
    res = e._module.__dict__["res"]
    assert res["all"] == {}
    assert res["d"] == "fallback"


# ============================================================
# 留档（可复现 + 看板展示名）
# ============================================================


def test_run_dir_archives_strategy_config(engine_factory):
    """策略配置与生效配置应留档到 run 目录，保证回测自含可复现信息。"""
    e = engine_factory(
        "def initialize(context):\n"
        "    set_benchmark('000300.SS')\n"
        "    set_universe(['000001.SZ'])\n",
        strategy_config={"name": "留档测试", "params": {"k": 1}},
    )
    e.run()
    sc = e.output_dir / "strategy_config.json"
    rc = e.output_dir / "run_config.json"
    assert sc.exists(), "应留档 strategy_config.json（看板据此显示中文名）"
    assert json.loads(sc.read_text(encoding="utf-8"))["name"] == "留档测试"
    assert rc.exists(), "应留档生效配置"
    eff = json.loads(rc.read_text(encoding="utf-8"))
    assert eff["params"]["k"] == 1
    assert "preload" not in eff, "体积大又无信息量的 preload 不入档"


def test_dashboard_reads_name_from_run_config(results_root_with_name):
    """看板应从 run 自带的策略配置取展示名，不依赖任何外部配置。"""
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from ptrade_sim import server

    client = TestClient(server.create_app(results_root_with_name))
    data = client.get("/api/runs").json()
    runs = data if isinstance(data, list) else data.get("runs", [])
    named = {r["name"]: r.get("strategy_name") for r in runs}
    assert named.get("demo-20250101_120000") == "目录名策略"
