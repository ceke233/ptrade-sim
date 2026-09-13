"""配置三级分层测试（默认 ← 本地私有 ← 环境变量）。"""

from __future__ import annotations

import json

import pytest

from ptrade_sim import config as C

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """清掉所有 PT_SIM_*，避免宿主环境干扰。"""
    import os

    for k in list(os.environ):
        if k.startswith(C.ENV_PREFIX):
            monkeypatch.delenv(k, raising=False)
    yield


# ============================================================
# 合并语义
# ============================================================


def test_deep_merge_is_recursive_not_replacing():
    """嵌套 dict 必须逐层合并：覆盖 preload.threads 不应丢掉 preload.mode。"""
    base = {"preload": {"mode": "rolling", "threads": 8}, "a": 1}
    over = {"preload": {"threads": 16}}
    out = C._deep_merge(base, over)
    assert out["preload"] == {"mode": "rolling", "threads": 16}
    assert out["a"] == 1


def test_deep_merge_original_untouched():
    base = {"preload": {"mode": "rolling"}}
    C._deep_merge(base, {"preload": {"mode": "all"}})
    assert base["preload"]["mode"] == "rolling", "_deep_merge 不应修改入参"


def test_set_nested_creates_path():
    d: dict = {}
    C._set_nested(d, "a.b.c", 1)
    assert d == {"a": {"b": {"c": 1}}}


def test_set_nested_overwrites_scalar_with_dict():
    d = {"a": 5}
    C._set_nested(d, "a.b", 1)
    assert d == {"a": {"b": 1}}


# ============================================================
# 类型转换
# ============================================================


@pytest.mark.parametrize("raw,expected", [("true", True), ("1", True), ("on", True), ("yes", True)])
def test_coerce_true(raw, expected):
    assert C._coerce("queue.enabled", raw) is expected


@pytest.mark.parametrize(
    "raw,expected", [("false", False), ("0", False), ("off", False), ("no", False)]
)
def test_coerce_false(raw, expected):
    assert C._coerce("queue.enabled", raw) is expected


def test_coerce_none():
    assert C._coerce("db_path", "none") is None
    assert C._coerce("db_path", "null") is None
    assert C._coerce("db_path", "") is None


def test_coerce_numeric_key_becomes_int():
    assert C._coerce("preload.threads", "8") == 8
    assert isinstance(C._coerce("preload.threads", "8"), int)


def test_coerce_float_key():
    assert C._coerce("cost.commission_ratio", "0.0003") == pytest.approx(0.0003)


def test_coerce_string_key_stays_string():
    assert C._coerce("db_path", "G:/quant.duckdb") == "G:/quant.duckdb"


def test_coerce_bad_number_warns_and_returns_raw():
    # 不抛异常：配置错误不应让进程崩在解析阶段
    assert C._coerce("preload.threads", "abc") == "abc"


# ============================================================
# 加载与优先级
# ============================================================


def test_load_returns_defaults_without_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = C.load()
    assert cfg["capital_base"] == C.DEFAULTS["capital_base"]
    assert "queue" in cfg and "preload" in cfg


def test_explicit_config_overrides_defaults(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"capital_base": 42, "start_date": "2020-01-01"}), encoding="utf-8")
    cfg = C.load(p)
    assert cfg["capital_base"] == 42
    assert cfg["start_date"] == "2020-01-01"


def test_explicit_config_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        C.load(tmp_path / "nope.json")


def test_env_overrides_file(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"preload": {"threads": 4}}), encoding="utf-8")
    import os

    os.environ["PT_SIM_THREADS"] = "16"
    try:
        cfg = C.load(p)
        assert cfg["preload"]["threads"] == 16
        assert cfg["preload"]["mode"] in ("rolling", "all"), "其他键应保留"
    finally:
        os.environ.pop("PT_SIM_THREADS", None)


def test_env_overrides_db_backend(tmp_path):
    import os

    p = tmp_path / "c.json"
    p.write_text(json.dumps({"db_path": "A.duckdb"}), encoding="utf-8")
    os.environ["PT_SIM_DB_PATH"] = "B.duckdb"
    try:
        assert C.load(p)["db_path"] == "B.duckdb"
    finally:
        os.environ.pop("PT_SIM_DB_PATH", None)


def test_env_can_be_disabled(tmp_path):
    import os

    p = tmp_path / "c.json"
    p.write_text(json.dumps({"db_path": "A.duckdb"}), encoding="utf-8")
    os.environ["PT_SIM_DB_PATH"] = "B.duckdb"
    try:
        assert C.load(p, use_env=False)["db_path"] == "A.duckdb"
    finally:
        os.environ.pop("PT_SIM_DB_PATH", None)


def test_extra_has_highest_priority(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"capital_base": 1}), encoding="utf-8")
    assert C.load(p, extra={"capital_base": 999})["capital_base"] == 999


def test_corrupt_config_warns_but_does_not_raise(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{ this is not json", encoding="utf-8")
    cfg = C.load(p)  # 不应抛
    assert cfg["capital_base"] == C.DEFAULTS["capital_base"]


def test_local_config_discovery_order(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".local_config.json").write_text("{}", encoding="utf-8")
    assert C.find_local_config().name == ".local_config.json"
    (tmp_path / "ptrade_config.json").write_text("{}", encoding="utf-8")
    assert C.find_local_config().name == "ptrade_config.json", "ptrade_config.json 应优先"


# ============================================================
# 校验
# ============================================================


def _valid() -> dict:
    return {
        "db_path": "G:/quant.duckdb",
        "start_date": "2025-01-01",
        "end_date": "2025-12-31",
        "strategy": "s.py",
    }


def test_validate_passes_on_valid():
    assert C.validate(_valid()) == []


def test_validate_requires_db_path():
    cfg = _valid()
    cfg["db_path"] = None
    prob = C.validate(cfg)
    assert any("db_path" in p for p in prob)
    assert any("duckdb" in p.lower() for p in prob), "错误信息应含构建指引"


def test_validate_requires_dates():
    cfg = _valid()
    cfg.pop("start_date")
    cfg.pop("end_date")
    prob = C.validate(cfg)
    assert len([p for p in prob if "日期" in p or "start_date" in p or "end_date" in p]) == 2


def test_validate_strategy_optional_flag():
    cfg = _valid()
    cfg.pop("strategy")
    assert C.validate(cfg, require_strategy=False) == []
    assert C.validate(cfg, require_strategy=True) != []


def test_guidance_mentions_all_sources():
    text = C.guidance(["缺少必填项 db_path"])
    assert "config.example.json" in text
    assert "ptrade_config.json" in text
    assert "PT_SIM_" in text


def test_guidance_empty_when_no_problems():
    assert C.guidance([]) == ""


def test_env_keys_reference_existing_paths():
    """ENV_KEYS 里的点号路径必须能在 DEFAULTS 里找到（除纯用户态键）。"""
    user_only = {"strategy", "start_date", "end_date", "output_dir"}
    for dotted in C.ENV_KEYS:
        if dotted in user_only:
            continue
        head = dotted.split(".")[0]
        assert head in C.DEFAULTS, f"ENV_KEYS 指向不存在的默认键：{dotted}"
