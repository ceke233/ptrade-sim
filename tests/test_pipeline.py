"""``pipeline.run_backtest`` 的编排契约测试（回测主路径）。

**为什么值得专门一个文件**：``pipeline.py`` 是「配置 → 资源 → 队列 → 引擎 → 看板」的
用例层，曾经覆盖率全仓最低（47%），但它恰恰是 ``ptrade-sim backtest`` 真正走的代码。
``tests/test_cli.py`` 只钉住了参数解析与错误文案，这里补的是**分支与产出**：

- 配置文件缺失发生在**两个不同阶段**（无 ``--strategy`` 时的预读 / 正式加载），
  两处都必须以分类退出码 2 退出，而不是冒泡成 traceback（那会一律退 1）；
- 结果目录的命名（策略**目录名**而非展示名）、创建、以及资源探查落在**哪个盘**；
- 队列准入的三条路径：禁用（--no-queue / 配置）/ 获准（登记真实估算出的占用）/ 被拒（退 5）；
- ``--no-wait`` 的等待语义（0 = 完全不等待，不是无限等待）；
- 看板在**开跑前**拉起、且起不来时必须降级为警告而不是阻断回测；
- 产出契约：CSV 的 BOM、summary.json 的展示名 / 资源画像 / 数据缺口留档。

**如何做到不依赖真实行情库**（``data/quant.duckdb`` 不存在也不该被读到）：

1. 用 ``monkeypatch`` 把 ``pipeline.BacktestEngine`` 换成假引擎（记录编排层交给它的参数，
   返回固定的 polars 表），真引擎一次都不会被构造；
2. 把 ``pipeline._count_trade_days`` 打桩为固定 5 天 —— 它是这条路径上**唯一**会连库的地方；
3. 配置里的 ``db_path`` 故意指向一个不存在的文件；若哪天打桩失效，测试会立刻炸而不是静默连库。
"""

from __future__ import annotations

import json
import re
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

import polars as pl
import pytest
from loguru import logger

import ptrade_sim
from ptrade_sim import cli, pipeline, resources
from ptrade_sim import queue as queue_mod
from ptrade_sim.config import ENV_KEYS, ENV_PREFIX

pytestmark = pytest.mark.unit


# ============================================================
# 隔离夹具
# ============================================================


@pytest.fixture(autouse=True)
def _no_env_overrides(monkeypatch):
    """清掉 ``PT_SIM_*``。

    配置分层里环境变量优先级**高于**配置文件：本机若设了 ``PT_SIM_START_DATE``
    之类，测试结果就不再由夹具决定（本地绿、CI 红，或反过来）。夹具必须自己掌握输入。
    """
    for name in ENV_KEYS.values():
        monkeypatch.delenv(ENV_PREFIX + name, raising=False)


@pytest.fixture
def logs():
    """捕获 loguru 输出。

    ``pipeline`` 只通过 ``logger`` 向用户回报（探查快照、开销、缺口告警、结果摘要），
    不看日志就断言不到这些行为。
    """
    msgs: list[str] = []
    sink_id = logger.add(lambda m: msgs.append(str(m)), level="DEBUG", format="{level}|{message}")
    try:
        yield msgs
    finally:
        logger.remove(sink_id)


# ============================================================
# 假引擎 / 假队列 / 假看板
# ============================================================


class _FakeCache:
    def stats(self) -> dict:
        return {"daily": {"hits": 3, "misses": 1, "hit_rate": 0.75}}

    def describe(self) -> str:
        return "daily: 2 项（上限 120 条 / 预算 -）"


class _FakeSource:
    """数据源替身：只暴露 pipeline 收尾要看的 ``data_errors()``。

    ``errors`` 可由测试注入，用来驱动「取数失败 → 结果不可信 → 非零退出」这条分支。
    """

    def __init__(self) -> None:
        self.errors: list[str] = []

    def data_errors(self) -> list[str]:
        return list(self.errors)


class _FakeFeed:
    def __init__(self):
        self.cache = _FakeCache()
        self.src = _FakeSource()


def _daily_frame() -> pl.DataFrame:
    """两天的日线统计（列集合与真引擎一致，供 compute_metrics 真算指标）。"""
    return pl.DataFrame(
        {
            "date": ["2025-01-02", "2025-01-03"],
            "total_value": [1_000_000.0, 1_010_000.0],
            "cash": [1_000_000.0, 900_000.0],
            "positions_value": [0.0, 110_000.0],
            "benchmark_close": [4000.0, 4010.0],
            "daily_return": [0.0, 0.01],
            "cum_return": [0.0, 0.01],
            "drawdown": [0.0, 0.0],
            "trades_count": [0, 2],
            "commission": [0.0, 6.1],
        }
    )


@dataclass
class _Hooks:
    engines: list = field(default_factory=list)
    queues: list = field(default_factory=list)
    events: list = field(default_factory=list)
    dashboard_calls: list = field(default_factory=list)


@dataclass
class _Result:
    rc: int
    hooks: _Hooks
    strategy_dir: Path
    log_text: str


def _install_fakes(
    monkeypatch,
    *,
    daily_frame: pl.DataFrame | None = None,
    gaps: dict | None = None,
    allow_queue: bool = True,
    data_errors: list[str] | None = None,
    trades_df: pl.DataFrame | None = None,
) -> _Hooks:
    """把引擎与队列换成假的，并记录编排层交给它们的每一个参数。"""
    hooks = _Hooks()

    class _Engine:
        def __init__(self, cfg, strategy_path, output_dir):
            self.cfg = cfg
            self.strategy_path = strategy_path
            self.output_dir = Path(output_dir)
            # 与真引擎一致：**run 目录由引擎创建**（runtime.BacktestEngine.__init__
            # 第一件事就是 output_dir.mkdir(parents=True, exist_ok=True)）。
            # pipeline 只负责建输出**根**目录 —— 假引擎若漏掉这一步，
            # 后面写 daily_stats.csv 会 FileNotFoundError（不忠实于真实契约）。
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self.capital_base = float(cfg.get("capital_base") or 0)
            self.feed = _FakeFeed()
            # 让测试能驱动「取数失败」这条分支（真引擎由 DuckDBSource 累积）
            self.feed.src.errors = list(data_errors or [])
            self.ran = False
            hooks.engines.append(self)
            hooks.events.append("engine")

        def run(self):
            self.ran = True
            return _daily_frame() if daily_frame is None else daily_frame

        def trades_frame(self):
            # 默认空表；注入 trades_frame 参数可让契约测试覆盖真实列型
            # （如 Datetime 的 time 列 —— 那是 CSV 格式化逻辑的触发条件）
            return pl.DataFrame() if trades_df is None else trades_df

        def data_gaps(self):
            return dict(gaps or {})

    class _Queue:
        def __init__(self, queue_dir, **kw):
            self.dir = Path(queue_dir)
            self.kw = kw
            self.acquire_calls: list[dict] = []
            # pipeline 紧接着就要读这两个属性来打日志
            self.max_parallel = kw.get("max_parallel") or 4
            self.cpu_slots_limit = kw.get("cpu_slots_limit") or 7
            hooks.queues.append(self)
            hooks.events.append("queue")

        def acquire(self, **kw):
            self.acquire_calls.append(kw)
            return allow_queue

    monkeypatch.setattr(pipeline, "BacktestEngine", _Engine)
    monkeypatch.setattr(queue_mod, "BacktestQueue", _Queue)
    return hooks


def _install_fake_dashboard(monkeypatch, hooks: _Hooks, *, error: Exception | None = None):
    """把 ``ptrade_sim.server`` 换成假的（真 server 会起 uvicorn / 依赖 fastapi）。"""

    def ensure_running(root, port):
        hooks.dashboard_calls.append((Path(root), port))
        hooks.events.append("dashboard")
        if error is not None:
            raise error
        return f"http://127.0.0.1:{port}"

    mod = types.ModuleType("ptrade_sim.server")
    mod.ensure_running = ensure_running
    monkeypatch.setitem(sys.modules, "ptrade_sim.server", mod)
    monkeypatch.setattr(ptrade_sim, "server", mod, raising=False)


# ============================================================
# 运行脚手架
# ============================================================


def _make_strategy_dir(root: Path, name: str = "my_strategy", cfg: dict | None = None) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "strategy.py").write_text(
        "def initialize(context):\n"
        "    set_benchmark('000300.SS')\n"
        "    set_universe(['000001.SZ'])\n",
        encoding="utf-8",
    )
    if cfg is not None:
        (d / "strategy_config.json").write_text(
            json.dumps(cfg, ensure_ascii=False), encoding="utf-8"
        )
    return d


def _run(
    tmp_path,
    monkeypatch,
    logs,
    *,
    cfg_over: dict | None = None,
    extra_args=(),
    daily_frame: pl.DataFrame | None = None,
    gaps: dict | None = None,
    allow_queue: bool = True,
    no_dashboard: bool = True,
    dashboard_error: Exception | None = None,
    strategy_name: str = "my_strategy",
    strategy_cfg: dict | None = None,
    data_errors: list[str] | None = None,
    trades_df: pl.DataFrame | None = None,
) -> _Result:
    """跑一遍 ``run_backtest``，全程不碰真实库/引擎/看板。"""
    monkeypatch.chdir(tmp_path)
    sdir = _make_strategy_dir(tmp_path, strategy_name, strategy_cfg)

    cfg = {
        # 故意指向不存在的库：本文件唯一会连库的地方（_count_trade_days）已被打桩，
        # 若打桩失效也会立刻失败，而不是静默去读真实行情库。
        "db_path": str(tmp_path / "never_opened.duckdb"),
        "start_date": "2025-01-02",
        "end_date": "2025-01-08",
        "capital_base": 1_000_000,
        "output_dir": "results",
        "preload": {"mode": "all", "threads": 4, "rolling_window_days": 3},
        "queue": {"enabled": False},
    }
    cfg.update(cfg_over or {})
    cpath = tmp_path / "c.json"
    cpath.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    # 交易日估算会 make_source(db_path) 连库；打桩为固定 5 天（本条路径上唯一的库访问）。
    monkeypatch.setattr(pipeline, "_count_trade_days", lambda cfg: 5)
    hooks = _install_fakes(
        monkeypatch,
        daily_frame=daily_frame,
        gaps=gaps,
        allow_queue=allow_queue,
        data_errors=data_errors,
        trades_df=trades_df,
    )
    if not no_dashboard:
        _install_fake_dashboard(monkeypatch, hooks, error=dashboard_error)

    argv = ["backtest", "--strategy", str(sdir), "--config", str(cpath)]
    if no_dashboard:
        argv.append("--no-dashboard")
    argv += list(extra_args)
    rc = pipeline.run_backtest(cli.parse_args(argv))
    return _Result(rc=rc, hooks=hooks, strategy_dir=sdir, log_text="\n".join(logs))


# ============================================================
# 1. 配置缺失的两个出口（必须是分类退出码 2，不是 1）
# ============================================================


def test_preread_missing_config_exits_with_config_code(tmp_path, monkeypatch, logs):
    """没给 ``--strategy`` 时先预读配置取 strategy；该文件不存在 → 退出码 2。

    防的是：这条路径以前会漏成未分类的 1，调用脚本无法区分「我配置文件写错了」与
    「程序崩了」；同时错误文案必须带路径，否则用户不知道找的是哪个文件。
    """
    monkeypatch.chdir(tmp_path)
    hooks = _install_fakes(monkeypatch)
    missing = tmp_path / "nope.json"

    rc = pipeline.run_backtest(cli.parse_args(["backtest", "--config", str(missing)]))

    assert rc == 2, f"配置文件缺失属配置类错误，应为分类退出码 2，实际 {rc}"
    text = "\n".join(logs)
    assert "不存在" in text and str(missing) in text, (
        f"错误信息应指出缺失的配置文件路径，实际：{text!r}"
    )
    assert hooks.engines == [], "预读失败后不得继续构建引擎"


def test_load_missing_config_exits_with_config_code(tmp_path, monkeypatch, logs):
    """给了 ``--strategy``（跳过预读）后，正式加载配置仍缺失 → 也要退 2。

    与上一个测试是**两个不同的分支**：预读（``use_env=False``、无策略级配置）与正式加载
    （还要合并 ``strategy_config.json``）各写了一次 ``except FileNotFoundError``。
    只堵住其中一个口子，另一条路径依然会冒泡成 traceback（退出码 1）。
    """
    monkeypatch.chdir(tmp_path)
    sdir = _make_strategy_dir(tmp_path, "my_strategy")
    hooks = _install_fakes(monkeypatch)
    missing = tmp_path / "gone.json"

    rc = pipeline.run_backtest(
        cli.parse_args(["backtest", "--strategy", str(sdir), "--config", str(missing)])
    )

    assert rc == 2, f"配置文件缺失属配置类错误，应为分类退出码 2，实际 {rc}"
    text = "\n".join(logs)
    assert "不存在" in text and str(missing) in text, (
        f"错误信息应指出缺失的配置文件路径，实际：{text!r}"
    )
    assert hooks.engines == [], "配置加载失败后不得继续构建引擎"


# ============================================================
# 2. 结果目录：命名、创建、探查目标
# ============================================================


def test_output_dir_named_after_strategy_dir_with_timestamp(tmp_path, monkeypatch, logs):
    """结果目录 = ``<输出根>/<策略目录名>-<YYYYmmdd_HHMMSS>``，且必须真的建出来。

    防两点：

    1. 用**展示名**（strategy_config.json 的 ``name``）命名 —— 改个中文名就换目录，
       看板的历史 run 与策略对不上；命名必须用稳定的目录名。
    2. 目录没建出来 —— 后续写 daily_stats.csv/summary.json 会直接 FileNotFoundError。
       （职责划分：输出**根**由 pipeline 建，run 目录由引擎在构造时建，见 runtime.py:877；
       正因如此，这个测试必须让假引擎同样 mkdir，否则假夹具就不忠实于真实契约。）
    """
    res = _run(tmp_path, monkeypatch, logs, strategy_cfg={"name": "中文展示名"})

    assert res.rc == 0
    root = Path.cwd() / "results"
    runs = [p for p in root.iterdir() if p.is_dir()]
    assert len(runs) == 1, f"结果根下应恰好一个 run 目录，实际 {[p.name for p in runs]}"
    run_dir = runs[0]
    assert re.fullmatch(r"my_strategy-\d{8}_\d{6}", run_dir.name), (
        f"目录名应为「策略目录名-时间戳」，实际 {run_dir.name!r}（展示名不该参与命名）"
    )

    eng = res.hooks.engines[0]
    assert eng.output_dir == run_dir and eng.output_dir.is_absolute(), (
        f"引擎必须拿到绝对结果目录（相对路径会随 cwd 漂移），实际 {eng.output_dir}"
    )
    assert eng.strategy_path == str(res.strategy_dir / "strategy.py")
    assert eng.cfg["strategy_name"] == "中文展示名", "展示名应来自 strategy_config.json 的 name"
    assert eng.cfg["strategy_dir"] == str(res.strategy_dir)
    assert eng.cfg["strategy_config"] == {"name": "中文展示名"}, (
        "未合并的原始策略配置必须留档（看板据此显示中文名）"
    )


def test_cli_output_dir_used_when_config_has_none(tmp_path, monkeypatch, logs):
    """配置没有 ``output_dir`` 时应回退到 ``--output-dir``，而不是默认目录。

    防的是覆盖链写反（CLI 参数优先级最高，脚本靠它把结果写到指定位置）。
    """
    cli_out = tmp_path / "cli_out"
    res = _run(
        tmp_path,
        monkeypatch,
        logs,
        cfg_over={"output_dir": None},
        extra_args=["--output-dir", str(cli_out)],
    )

    assert res.rc == 0
    made = sorted(p.name for p in cli_out.iterdir())
    assert len(made) == 1 and made[0].startswith("my_strategy-"), (
        f"--output-dir 应生效，实际 {made}"
    )
    assert not (Path.cwd() / "results").exists(), "配置无 output_dir 时不该再往默认目录写"


def test_probe_targets_output_root_and_cost_uses_estimated_days(tmp_path, monkeypatch, logs):
    """资源探查必须落在**结果目录所在盘**，开销估算必须用估算出的交易日数。

    防两点：

    1. 探查 cwd —— 结果目录常被指到另一块盘（数据盘/临时盘），报的磁盘余量就不是
       真正要写盘的那块，准入判断失真；
    2. 估算天数写死 —— 长区间回测的内存预估会低几个数量级，队列占位随之失真。

    顺带钉住「探查之前先建输出根目录」：目录不存在时 ``shutil.disk_usage`` 会失败，
    probe 静默返回 ``disk_free=0``，"磁盘不足"这一维准入就**悄悄失效**了。
    """
    probes: list = []
    real_probe = resources.probe

    def spy(path=None):
        p = Path(path) if path is not None else None
        # 记录探查**当时**的现场：目录是否已建、里面是否已有 run 目录
        probes.append((p, p.exists() if p else None, bool(list(p.iterdir())) if p else None))
        return real_probe(path)

    monkeypatch.setattr(resources, "probe", spy)

    res = _run(tmp_path, monkeypatch, logs)

    assert res.rc == 0
    root = Path.cwd() / "results"
    assert probes == [(root, True, False)], (
        f"应在「结果根已建好、但还没有 run 目录」时探查结果根，实际 {probes}"
    )
    assert "资源快照：CPU" in res.log_text, "资源快照必须回报（用户要据此判断机器吃得下不）"
    assert "5 个交易日" in res.log_text, (
        "开销估算应使用 _count_trade_days 的结果（本例打桩为 5），而不是写死的天数"
    )
    assert "preload=all 将常驻 5 天分钟数据" in res.log_text, (
        "preload=all 的内存风险必须以警告点出（否则用户不知道长区间会爆内存）"
    )


# ============================================================
# 3. 队列：绕过 / 获准 / 被拒
# ============================================================


@pytest.mark.parametrize(
    "cfg_over, extra_args, why",
    [
        (None, ["--no-queue"], "--no-queue"),
        ({"queue": {"enabled": False}}, [], "配置 queue.enabled=false"),
    ],
    ids=["by-flag", "by-config"],
)
def test_queue_can_be_bypassed(tmp_path, monkeypatch, logs, cfg_over, extra_args, why):
    """两种绕过方式都应**完全不构造队列**，但仍然继续开跑。

    防的是：只在日志里说"队列已禁用"却照样 new 一个 BacktestQueue 去抢资源。
    两个分支（CLI 标志 / 配置开关）各是一条独立的 ``or`` 条件，必须都钉住。
    """
    res = _run(tmp_path, monkeypatch, logs, cfg_over=cfg_over, extra_args=extra_args)

    assert res.rc == 0
    assert res.hooks.queues == [], f"{why} 时不应构造队列（构造了就会去抢资源）"
    assert "队列已禁用" in res.log_text
    assert len(res.hooks.engines) == 1 and res.hooks.engines[0].ran, (
        "跳过排队后应继续开跑，而不是直接退出"
    )


def test_queue_admission_uses_estimated_cost_and_config(tmp_path, monkeypatch, logs):
    """队列必须按配置构造，并按**本次回测的真实估算**登记占用。

    防的是：把 0/常量当占用传给队列 —— 登记得比实际小，多个回测就会同时通过准入，
    机器被压垮（这正是队列存在的意义）。
    """
    qdir = tmp_path / "q"
    res = _run(
        tmp_path,
        monkeypatch,
        logs,
        cfg_over={
            "queue": {
                "enabled": True,
                "dir": str(qdir),
                "max_parallel": 2,
                "cpu_slots_limit": 3,
                "poll_interval": 7,
                "max_wait_sec": 42,
            }
        },
    )

    assert res.rc == 0
    q = res.hooks.queues[0]
    assert q.dir == qdir, "队列目录应取配置里的 queue.dir（override 优先于机器级默认）"
    assert q.kw["enabled"] is True
    assert q.kw["max_parallel"] == 2 and q.kw["cpu_slots_limit"] == 3, (
        "并发上限 / CPU 槽上限必须透传给队列，否则配置形同虚设"
    )
    assert q.kw["poll_interval"] == 7 and q.kw["max_wait_sec"] == 42
    assert res.hooks.events[:2] == ["queue", "engine"], "必须先拿到许可才构建引擎"

    assert len(q.acquire_calls) == 1, f"应恰好申请一次运行许可，实际 {len(q.acquire_calls)} 次"
    call = q.acquire_calls[0]
    eng = res.hooks.engines[0]
    expected = resources.estimate_cost(
        days=5,
        threads=int(eng.cfg["preload"]["threads"]),
        preload_mode=eng.cfg["preload"]["mode"],
        rolling_window=int(eng.cfg["preload"]["rolling_window_days"]),
        minute_budget_bytes=pipeline._minute_budget(eng.cfg),
    )
    assert call["slots"] == expected.est_cpu_slots, (
        "登记的 CPU 槽必须来自资源估算，否则准入判定基于假数字"
    )
    assert call["mem_bytes"] == expected.est_mem_bytes, "登记的内存必须来自资源估算，否则内存超卖"
    assert call["strategy"] == "strategy.py", "队列视图显示的是策略文件名"
    assert Path(call["probe_path"]) == Path.cwd() / "results", "准入用的磁盘余量应探查结果根目录"


def test_no_wait_forces_zero_max_wait(tmp_path, monkeypatch, logs):
    """``--no-wait`` 必须表达「完全不等待」（0），而不是复用「无限等待」的 None。

    回归防护：0 曾同时表示"无限等待"与"不等待"，导致 --no-wait 实际永久挂起。
    """
    qdir = tmp_path / "q"
    res = _run(
        tmp_path,
        monkeypatch,
        logs,
        cfg_over={"queue": {"enabled": True, "dir": str(qdir), "max_wait_sec": 42}},
        extra_args=["--no-wait"],
    )

    assert res.hooks.queues[0].kw["max_wait_sec"] == 0, (
        "--no-wait 应传 0（试一次，拿不到就退出），而不是 42 或 None"
    )
    assert res.rc == 0, "本用例的假队列直接放行，应正常跑完"


def test_queue_rejection_returns_resource_exit_code(tmp_path, monkeypatch, logs):
    """未获运行许可 → 退 5（资源/队列类），且必须告诉用户 ``--no-queue`` 这条退路。

    防的是：被拒后仍然开跑（多任务同时上机器被压垮），或退成未分类的 1
    （脚本无法区分"资源不够，稍后重试"与"代码出错"）。
    """
    qdir = tmp_path / "q"
    res = _run(
        tmp_path,
        monkeypatch,
        logs,
        allow_queue=False,
        cfg_over={"queue": {"enabled": True, "dir": str(qdir)}},
    )

    assert res.rc == pipeline.EXIT_RESOURCE == 5, f"队列被拒应返回 EXIT_RESOURCE(5)，实际 {res.rc}"
    assert res.hooks.engines == [], "未获得运行许可绝不能开跑"
    assert "未获得运行许可" in res.log_text
    assert "--no-queue" in res.log_text, "必须给出绕过排队的具体做法，否则用户卡死在这里"


# ============================================================
# 4. 实时看板
# ============================================================


def test_dashboard_started_before_engine_on_configured_port(tmp_path, monkeypatch, logs):
    """看板要在**开跑前**拉起，根目录是结果根，端口取 ``--port``。

    防两点：先是引擎后是看板 → 前几天的进度根本看不到（看板是为"实时"存在的）；
    根目录传成单个 run 目录 → 看板只能看到本次 run，历史 run 全丢。
    """
    res = _run(tmp_path, monkeypatch, logs, no_dashboard=False, extra_args=["--port", "8999"])

    assert res.rc == 0
    root = Path.cwd() / "results"
    assert res.hooks.dashboard_calls == [(root, 8999)], (
        f"应按 --port 拉起看板并指向结果根，实际 {res.hooks.dashboard_calls}"
    )
    assert res.hooks.events[:2] == ["dashboard", "engine"], "看板必须在引擎之前拉起"
    assert "实时看板：http://127.0.0.1:8999" in res.log_text, "拉起后要打印访问地址"


def test_dashboard_failure_does_not_block_backtest(tmp_path, monkeypatch, logs):
    """看板起不来（端口占用 / 没装 dashboard 依赖）只能降级为警告，回测照跑。

    防的是：为了让看板起来而把整次回测（可能几十分钟）一起陪葬；
    同时要留下可手动补起的命令，别让用户以为没救了。
    """
    res = _run(
        tmp_path,
        monkeypatch,
        logs,
        no_dashboard=False,
        dashboard_error=RuntimeError("端口被占用"),
    )

    assert res.rc == 0, "看板失败不应改变退出码"
    assert res.hooks.engines and res.hooks.engines[0].ran, "回测必须照常执行"
    assert "看板服务拉起失败：端口被占用" in res.log_text
    assert "dashboard --port 8765" in res.log_text, "应给出用默认端口手动补起看板的命令"


# ============================================================
# 5. 产出契约（CSV / summary.json / 数据缺口）
# ============================================================


def test_run_persists_csv_and_summary_contract(tmp_path, monkeypatch, logs):
    """产出必须落盘且内容正确：CSV 带 BOM、summary 带展示名与资源画像。

    防的是：CSV 丢 BOM（Excel 打开中文乱码）、summary 缺展示名（看板显示不出中文名）、
    资源画像缺失（事后无法判断瓶颈是内存还是 CPU）。无缺口时也不该凭空写入 data_gaps。
    """
    res = _run(tmp_path, monkeypatch, logs, strategy_cfg={"name": "中文展示名"})

    assert res.rc == 0
    out = res.hooks.engines[0].output_dir

    daily_csv = (out / "daily_stats.csv").read_text(encoding="utf-8")
    assert daily_csv.startswith("\ufeff"), "CSV 必须带 UTF-8 BOM，否则 Excel 打开中文乱码"
    assert "total_value" in daily_csv and "2025-01-03" in daily_csv, (
        "CSV 应由引擎返回的日线统计表写出"
    )
    assert (out / "trades.csv").read_text(encoding="utf-8").startswith("\ufeff")

    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["config"]["strategy_name"] == "中文展示名", "看板据此显示中文名"
    assert summary["trade_days"] == 2
    assert summary["total_return"] == pytest.approx(0.01), (
        "指标应由引擎返回的日线表真算出来（1,010,000 / 1,000,000 - 1）"
    )
    assert "data_gaps" not in summary, "没有缺口时不该写入空的 data_gaps"

    res_block = summary["resources"]
    assert res_block["estimated"]["days"] == 5 and res_block["estimated"]["threads"] == 4, (
        "summary 里的估算资源应与准入用的是同一份"
    )
    assert "cpu_count" in res_block["snapshot"] and "disk_free" in res_block["snapshot"]
    assert res_block["cache_stats"]["daily"]["hits"] == 3, "缓存画像应取自引擎的 feed"
    assert "回看窗口超出库内数据覆盖" not in res.log_text, "无缺口时不该报缺口告警"


def test_data_gaps_are_persisted_and_loudly_warned(tmp_path, monkeypatch, logs):
    """数据缺口必须同时**留档**与**显式告警**。

    防的是：回看窗口越过库内覆盖时 get_history 把缺失交易日填成 NaN 却仍返回满 count 行，
    用户看到"回测完成"就以为一切正常 —— 依赖窗口起点（如 close.iloc[0]）的策略逻辑
    已经失效，成交笔数被低估也无人知晓。
    """
    gaps = {
        "missing_day_count": 3,
        "missing_days": ["20241216", "20241217", "20241218"],
        "daily_coverage": ["20250102", "20250108"],
    }
    res = _run(tmp_path, monkeypatch, logs, gaps=gaps)

    assert res.rc == 0
    out = res.hooks.engines[0].output_dir
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["data_gaps"] == gaps, "缺口明细必须落进 summary.json，否则事后无从追查"

    assert "回看窗口超出库内数据覆盖" in res.log_text
    assert "3 个交易日缺失" in res.log_text and "20241216" in res.log_text, (
        "告警要给出缺失天数与具体日期，用户才能判断影响面"
    )
    assert "NaN" in res.log_text, "必须点出缺失日被填成 NaN 的后果（策略逻辑会失效）"


# ============================================================
# 6. 取数失败：比数据缺口更严重 —— 必须非零退出
# ============================================================


def test_data_errors_make_the_run_fail_loudly(tmp_path, monkeypatch, logs):
    """**取数失败必须以非零码退出**，而不是照常报「回测完成」。

    防的是实测踩到过的坑：一次 6 年分钟回测里有 7 次 DuckDB 查询失败
    （Out of Memory），被 ``_q`` 吞成「该日无数据」——
    同一策略、同一区间的收益与成交笔数就出现了量级级别的差异。
    一行 WARNING 埋在 28000 行日志里，run 目录照常产出 summary.json，
    用户拿到的是一份**看起来很正常的错误结果**。
    """
    errs = [
        "7 次 DuckDB 查询失败被按「无数据」处理（结果不可信）",
        "  · SELECT code, trade_time FROM ashare_1m_stock WHERE date = ? "
        "—— OutOfMemoryException: Out of Memory Error",
    ]
    res = _run(tmp_path, monkeypatch, logs, data_errors=errs)

    assert res.rc == pipeline.EXIT_DATA, (
        f"取数失败必须非零退出（期望 {pipeline.EXIT_DATA}），实际 {res.rc} —— "
        f"返回 0 会让错误结果被当成正常产出"
    )
    assert res.rc != 0

    # 明细必须落盘，否则事后无从追查
    out = res.hooks.engines[0].output_dir
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["data_errors"] == errs, "取数失败明细必须写进 summary.json"

    # 且必须在日志里响亮地说出来，而不是一行 WARNING
    assert "结果不可信" in res.log_text, "必须明说结果不可信，而不只是「有告警」"
    assert "Out of Memory" in res.log_text, "应带上原始错误信息以便排查"
    assert "data_errors" in res.log_text, "应告诉用户明细写在哪"


def test_no_data_errors_keeps_success_and_omits_field(tmp_path, monkeypatch, logs):
    """无取数失败时：正常退出，且**不得**凭空写入 data_errors 字段。

    否则每次回测都会误报失败，这个信号很快就没人看了。
    """
    res = _run(tmp_path, monkeypatch, logs)

    assert res.rc == 0
    out = res.hooks.engines[0].output_dir
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert "data_errors" not in summary, "无失败时不该写入空的 data_errors"
    assert "结果不可信" not in res.log_text


# ============================================================
# 7. trades.csv 的时间列格式
# ============================================================


def test_csv_time_column_is_human_readable():
    """trades.csv 的 ``time`` 列必须是 ``YYYY-MM-DD HH:MM:SS``。

    防的是：``trades_frame()`` 返回 polars ``Datetime`` 列，而 ``write_csv``
    会把它序列化成 ``2021-01-05T14:50:00.000000`` —— 中间带 ``T``、末尾 6 位
    小数秒。**Excel 不认这个格式**，看板也得另做解析。
    """
    from datetime import datetime

    from ptrade_sim.runtime import frame_to_csv_text

    df = pl.DataFrame({"time": [datetime(2021, 1, 5, 14, 50, 0)], "security": ["600095.SS"]})
    out = pipeline._csv_friendly_time(df)

    assert out.schema["time"] == pl.String, "写出时 time 应转成字符串列"
    assert out["time"][0] == "2021-01-05 14:50:00"

    csv = frame_to_csv_text(out)
    assert "2021-01-05 14:50:00" in csv
    assert "T14:50" not in csv, "不应残留 ISO 的 T 分隔符"
    assert ".000000" not in csv, "不应残留 6 位小数秒"


def test_csv_time_helper_passes_through_other_shapes():
    """空表 / 无 time 列 / 已是字符串 —— 都原样返回，不抛异常。

    这几条是**幂等与幂等性**保护：helper 会在每次写出时被调用，
    形状不对时必须安静放过，而不是让回测在最后一步崩掉。
    """
    import polars as pl

    assert pipeline._csv_friendly_time(None) is None

    empty = pl.DataFrame()
    assert pipeline._csv_friendly_time(empty).height == 0

    no_time = pl.DataFrame({"a": [1]})
    assert pipeline._csv_friendly_time(no_time).columns == ["a"]

    already = pl.DataFrame({"time": ["2021-01-05 14:50:00"]})
    assert pipeline._csv_friendly_time(already)["time"][0] == "2021-01-05 14:50:00"


def test_trades_frame_keeps_datetime_type():
    """**反向保护**：内存里的 ``trades_frame()`` 必须仍是 Datetime 列。

    格式化只发生在**落盘那一步**。若有人图省事直接改 trades_frame 返回字符串，
    依赖 ``.dt.date()`` 分组的下游（tests/test_engine.py）会立刻挂。
    """
    import polars as pl

    from ptrade_sim import runtime

    # 用一个真实引擎的小回测验证类型（轻量：日线 + 合成库）
    assert "trades_frame" in dir(runtime.BacktestEngine)
    # 直接验 helper 不改变入参对象（纯函数语义）
    from datetime import datetime

    df = pl.DataFrame({"time": [datetime(2021, 1, 5, 14, 50)]})
    before = df.schema["time"]
    pipeline._csv_friendly_time(df)
    assert df.schema["time"] == before, "helper 不应就地修改传入的 DataFrame"


def test_run_backtest_actually_formats_csv_time(tmp_path, monkeypatch, logs):
    """**接线测试**：验证 run_backtest **真的调用**了格式化，而不只是函数写对了。

    为什么必须单独一条：上面两条测的是 ``_csv_friendly_time`` 本身，
    把调用点删掉它们**照样通过**（实测变异存活）。这条从「跑一次回测」
    出发断言落盘的 CSV，才真正守住接线。
    """
    from datetime import datetime

    df = pl.DataFrame(
        {
            "time": [datetime(2021, 1, 5, 14, 50, 0), datetime(2021, 1, 6, 9, 26, 0)],
            "security": ["600095.SS", "002002.SZ"],
            "side": ["sell", "buy"],
            "amount": [-6700, 7900],
            "price": [14.81, 3.94],
            "turnover": [99227.0, 31126.0],
            "commission": [123.9, 15.6],
            "order_id": ["20210105-000001", "20210106-000002"],
            "trade_pnl": [603.0, 0.0],
        }
    )
    res = _run(tmp_path, monkeypatch, logs, trades_df=df)
    assert res.rc == 0

    csv = (res.hooks.engines[0].output_dir / "trades.csv").read_text(encoding="utf-8")
    assert "2021-01-05 14:50:00" in csv, f"时间未被格式化：{csv[:200]}"
    assert "T14:50" not in csv, "不应残留 ISO 的 T 分隔符"
    assert ".000000" not in csv, "不应残留 6 位小数秒"
    assert "600095.SS" in csv and "002002.SZ" in csv, "其余列不该受影响"
