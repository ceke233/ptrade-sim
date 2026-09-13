"""配置分层加载。

优先级由低到高::

    DEFAULTS（代码内兜底）
      ← config.example.json（公开模板：仅键结构 + 安全默认）
        ← ptrade_config.json / .local_config.json（机器级私有：真实路径与库连接，不入库）
          ← strategy_config.json（**策略级**：随策略目录走，见下）
            ← 环境变量 PT_SIM_*（临时覆盖，便于 CI）
              ← CLI 参数（最高）

为什么分层：公开仓库不能含真实数据路径与库连接串；同时又要保证
"克隆下来就能看到完整配置项"。于是把**结构与默认值**放公开模板，
把**真实值**放本地私有文件。

## 策略目录（推荐形态）

一个策略一个目录，配置与代码放在一起::

    strategies/my_strategy/
    ├── strategy_config.json    # 可选：该策略的配置（回测区间、资金、展示名、策略入参）
    └── strategy.py             # 必需：策略代码

`strategy_config.json` 的定位是**"这个策略怎么跑"**（回测区间、初始资金、基准、周期、
展示名、策略入参）；而 `db_path` / `queue` / `cache` 这类**机器级**设置留在
`ptrade_config.json` —— 换台机器不该改策略目录。

策略里的 tunable 入参放 `params`，策略代码通过 `get_strategy_params()` 读取
（官方 `set_parameters` 仅交易模块可用，回测里没有等价机制，故由本平台提供）。

也兼容旧的单文件写法：`--strategy path/to/strategy.py`。

环境变量映射（``PT_SIM_<大写键名>``）::

    PT_SIM_DB_PATH=data/quant.duckdb
    PT_SIM_START_DATE=2020-01-01
    PT_SIM_FREQUENCY=daily
    PT_SIM_THREADS=8
    PT_SIM_MINUTE_MEMORY_BUDGET=4GB
    PT_SIM_MAX_PARALLEL=2
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, NamedTuple

from loguru import logger

from ptrade_sim.exceptions import (
    ConfigFileNotFoundError,
    StrategyConfigError,
    StrategyPathError,
)

#: 回测结果根目录默认名（CLI 与看板共用；放这里避免 cli <-> server 互相 import）
DEFAULT_RESULTS_DIR = "backtest_results"

#: 实时看板默认端口
DEFAULT_PORT = 8765

#: 环境变量前缀
ENV_PREFIX = "PT_SIM_"

#: 顶层标量键 -> 环境变量名（嵌套键用 ``_`` 连接，如 cache.minute_memory_budget）
ENV_KEYS: dict[str, str] = {
    "db_path": "DB_PATH",
    "start_date": "START_DATE",
    "end_date": "END_DATE",
    "capital_base": "CAPITAL_BASE",
    "benchmark": "BENCHMARK",
    "strategy": "STRATEGY",
    "frequency": "FREQUENCY",
    "output_dir": "OUTPUT_DIR",
    "preload.mode": "PRELOAD_MODE",
    "preload.rolling_window_days": "ROLLING_WINDOW_DAYS",
    "preload.threads": "THREADS",
    "cache.minute_memory_budget": "MINUTE_MEMORY_BUDGET",
    "cache.daily_capacity": "DAILY_CACHE_CAPACITY",
    "queue.enabled": "QUEUE_ENABLED",
    "queue.max_parallel": "MAX_PARALLEL",
    "queue.poll_interval": "QUEUE_POLL_INTERVAL",
    "queue.max_wait_sec": "QUEUE_MAX_WAIT_SEC",
    "cost.commission_ratio": "COMMISSION_RATIO",
    "cost.min_commission": "MIN_COMMISSION",
    "cost.stamp_tax": "STAMP_TAX",
    "cost.slippage_ratio": "SLIPPAGE_RATIO",
}

#: 需要按数字解析的键（避免环境变量把 8 变成 "8"）
NUMERIC_KEYS = {
    "capital_base",
    "preload.rolling_window_days",
    "preload.threads",
    "cache.daily_capacity",
    "cache.feature_capacity",
    "queue.max_parallel",
    "queue.poll_interval",
    "queue.max_wait_sec",
    "cost.commission_ratio",
    "cost.min_commission",
    "cost.handling_fee_ratio",
    "cost.stamp_tax",
    "cost.slippage_ratio",
}

#: 初始资金默认值（唯一权威：runtime / runstore / 看板的兜底都引用它）
DEFAULT_CAPITAL_BASE = 100000

#: 默认值（公开模板缺失时的兜底）
DEFAULTS: dict[str, Any] = {
    "db_path": "data/quant.duckdb",
    # 回测周期：minute（241 槽/日）| daily（15:00 一次）
    # 必须在这里有默认值：否则公开模板缺失时，ENV_KEYS 里的 PT_SIM_FREQUENCY
    # 会写进一个「无默认」的键，配置结构随环境而变。
    "frequency": "minute",
    "capital_base": DEFAULT_CAPITAL_BASE,
    "benchmark": "000300.SS",
    "preload": {"mode": "rolling", "rolling_window_days": 10, "threads": 8},
    "cache": {
        "daily_capacity": 120,
        "feature_capacity": 120,
        "minute_memory_budget": "auto",
        "minute_memory_ratio": 0.25,
    },
    "queue": {
        "enabled": True,
        "max_parallel": None,
        "poll_interval": 10,
        "max_wait_sec": 3600,
    },
    # 交易成本（PTrade 默认费率）。必须在这里有默认值：
    # 否则公开模板缺失时这整段会消失，费率静默回退到引擎内部常量。
    "cost": {
        "commission_ratio": 0.0003,
        "min_commission": 5.0,
        "handling_fee_ratio": 4.87e-05,
        "stamp_tax": 0.001,
        "slippage_ratio": 0.0,
    },
}


def _deep_merge(base: dict, over: dict) -> dict:
    """递归合并：``over`` 覆盖 ``base``，dict 逐层合并而非整体替换。"""
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _set_nested(d: dict, dotted: str, value: Any) -> None:
    keys = dotted.split(".")
    for k in keys[:-1]:
        d.setdefault(k, {})
        if not isinstance(d[k], dict):
            d[k] = {}
        d = d[k]
    d[keys[-1]] = value


def _coerce(key: str, raw: str) -> Any:
    """把环境变量字符串转成合适的类型。"""
    low = raw.strip().lower()
    if low in ("true", "1", "yes", "on"):
        return True
    if low in ("false", "0", "no", "off"):
        return False
    if low in ("none", "null", ""):
        return None
    if key in NUMERIC_KEYS:
        try:
            return int(raw)
        except ValueError:
            try:
                return float(raw)
            except ValueError:
                logger.warning(f"环境变量 {ENV_PREFIX}{ENV_KEYS.get(key, '?')} 不是数字：{raw!r}")
                return raw
    return raw


def find_local_config(cwd: Path | None = None) -> Path | None:
    """按优先级找本地私有配置：ptrade_config.json → .local_config.json。"""
    cwd = cwd or Path.cwd()
    for name in ("ptrade_config.json", ".local_config.json"):
        p = cwd / name
        if p.exists():
            return p
    return None


def default_template_path() -> Path | None:
    """公开模板位置：工作目录 → 仓库根 → 包目录。"""
    here = Path(__file__).resolve().parent
    for p in (
        Path.cwd() / "config.example.json",
        here.parent.parent / "config.example.json",
        here / "config.example.json",
    ):
        if p.exists():
            return p
    return None


#: 策略目录内的固定文件名
STRATEGY_PY = "strategy.py"
STRATEGY_CONFIG = "strategy_config.json"


class StrategyBundle(NamedTuple):
    """一个策略的解析结果。"""

    #: 策略代码文件（绝对路径）
    py: Path
    #: 策略配置（``strategy_config.json``，缺省为空 dict）
    config: dict
    #: 策略目录（单文件策略时为其父目录）
    dir: Path
    #: 展示名（``strategy_config.json`` 的 ``name``，缺省为目录名/文件名）
    name: str
    #: 结果目录命名用的稳定标识。
    #: **目录形态**取目录名；**单文件形态**取文件名去后缀。
    #:
    #: 必须是显式字段而不是从 ``dir`` 推导 —— 早期实现写作
    #: ``dir.name or py.stem``，而单文件形态的 ``dir`` 是**父目录**，
    #: 于是 ``examples/demo_momentum.py`` 的结果目录叫 ``examples-<时间戳>``：
    #: 名不副实，且同目录下多个单文件策略还会撞名无法区分。
    stem: str


def _read_strategy_config(path: Path) -> dict:
    """读取 ``strategy_config.json``；缺失返回空 dict，损坏则告警并忽略。"""
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        logger.warning(f"策略配置读取失败（{path}）：{exc}")
        return {}
    if not isinstance(data, dict):
        logger.warning(f"策略配置应为 JSON 对象（{path}），实际 {type(data).__name__}")
        return {}
    # 以 _ 开头的键视为注释（与 config.example.json 的 _comments 一致）
    return {k: v for k, v in data.items() if not str(k).startswith("_")}


def resolve_strategy(target: str | Path) -> StrategyBundle:
    """把「策略目录」或「策略文件」解析为 :class:`StrategyBundle`。

    目录形态下策略文件的查找规则：

    1. 优先取 ``strategy.py``；
    2. 若无则取目录中**唯一**的 ``.py``（方便随手起名）；
    3. 若有多个 ``.py`` 且无 ``strategy.py`` → 报错并列出候选。
       静默挑一个会让人以为跑的是 A、实际跑的是 B。
    """
    p = Path(target)
    if not p.is_absolute():
        p = Path.cwd() / p

    if p.is_dir():
        py = p / STRATEGY_PY
        if not py.exists():
            cands = sorted(x for x in p.glob("*.py") if x.name != "__init__.py")
            if len(cands) == 1:
                py = cands[0]
            elif not cands:
                raise StrategyPathError(f"策略目录中没有 .py 文件：{p}\n请放入 {STRATEGY_PY}")
            else:
                names = "、".join(x.name for x in cands)
                raise StrategyConfigError(
                    f"策略目录中有多个 .py 且无 {STRATEGY_PY}：{p}\n"
                    f"候选：{names}\n请改名或显式指定要跑的文件"
                )
        cfg = _read_strategy_config(p / STRATEGY_CONFIG)
        return StrategyBundle(
            py=py, config=cfg, dir=p, name=str(cfg.get("name") or p.name), stem=p.name
        )

    if p.is_file():
        if p.suffix.lower() != ".py":
            raise StrategyConfigError(f"策略文件应为 .py：{p}")
        # 单文件形态：同目录若有 strategy_config.json 也一并读取（便于渐进迁移）
        cfg = _read_strategy_config(p.parent / STRATEGY_CONFIG)
        return StrategyBundle(
            py=p, config=cfg, dir=p.parent, name=str(cfg.get("name") or p.stem), stem=p.stem
        )

    raise StrategyPathError(f"策略路径不存在：{p}")


def load(
    path: str | Path | None = None,
    use_env: bool = True,
    extra: dict | None = None,
    strategy: str | Path | None = None,
    strategy_config: dict | None = None,
) -> dict:
    """加载配置（优先级由低到高）::

        DEFAULTS
          ← config.example.json（公开模板）
            ← ptrade_config.json / .local_config.json（机器级私有）
              ← strategy_config.json（**策略级**，随策略目录走）
                ← 环境变量 PT_SIM_*
                  ← ``extra``（CLI 参数）

    ``path`` 显式给出时只读该文件（仍叠加策略级、env 与 extra）。

    ``strategy`` 给出时自动加载其同目录的 ``strategy_config.json``；
    若 CLI 已解析过，可用 ``strategy_config`` 直接传入以免重复读盘。
    """
    cfg: dict[str, Any] = dict(DEFAULTS)

    if path is not None:
        p = Path(path)
        if not p.exists():
            raise ConfigFileNotFoundError(f"配置文件不存在：{p}")
        files = [p]
    else:
        files = []
        tpl = default_template_path()
        if tpl is not None:
            files.append(tpl)
        local = find_local_config()
        if local is not None and local not in files:
            files.append(local)

    for f in files:
        try:
            with f.open(encoding="utf-8") as fh:
                cfg = _deep_merge(cfg, json.load(fh))
            logger.debug(f"配置已加载：{f}")
        except Exception as exc:
            logger.warning(f"配置文件读取失败（{f}）：{exc}")

    # 策略级配置：只覆盖「本策略关心」的键。
    # 机器级设置（db_path / queue / cache）通常留在上层 —— 换机器不该改策略目录。
    sc = strategy_config
    if sc is None and strategy is not None:
        try:
            sc = resolve_strategy(strategy).config
        except (FileNotFoundError, ValueError) as exc:
            logger.warning(f"策略配置未加载：{exc}")
            sc = None
    if sc:
        overridden = sorted(k for k in sc if not str(k).startswith("_") and k != "name")
        if overridden:
            logger.info(f"策略配置覆盖：{', '.join(overridden)}")
        cfg = _deep_merge(cfg, sc)

    if use_env:
        applied = []
        for key, env_name in ENV_KEYS.items():
            raw = os.environ.get(ENV_PREFIX + env_name)
            if raw is None:
                continue
            _set_nested(cfg, key, _coerce(key, raw))
            applied.append(f"{ENV_PREFIX}{env_name}")
        if applied:
            logger.info(f"环境变量覆盖：{', '.join(applied)}")

    if extra:
        cfg = _deep_merge(cfg, extra)

    return cfg


def validate(cfg: dict, require_strategy: bool = True) -> list[str]:
    """校验必填项，返回问题列表（空 = 通过）。"""
    problems = []
    for k in ("start_date", "end_date"):
        if not cfg.get(k):
            problems.append(f"缺少必填项 {k}")
    if not cfg.get("db_path"):
        problems.append(
            "缺少必填项 db_path —— 引擎只连 DuckDB 物理库，"
            "需先用 `ptrade-sim db build --db <库路径>` 构建"
        )
    if require_strategy and not cfg.get("strategy"):
        problems.append("缺少 strategy（策略文件路径）")
    return problems


def guidance(problems: list[str]) -> str:
    """把校验问题转成可操作的指引。"""
    if not problems:
        return ""
    lines = ["配置有误："] + [f"  - {p}" for p in problems]
    lines += [
        "",
        "可选来源（优先级由低到高）：",
        "  1. config.example.json（公开模板，仅结构与默认值）",
        "  2. ptrade_config.json（本地私有，真实路径；不入库）",
        "  3. 环境变量 PT_SIM_*（临时覆盖），如：",
        "       PT_SIM_DB_PATH=data/quant.duckdb  PT_SIM_START_DATE=2020-01-01",
    ]
    return "\n".join(lines)
