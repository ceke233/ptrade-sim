"""回测流水线的编排（从 cli.py 拆出）。

**职责**：把「跑一次回测」编排起来 —— 策略解析 → 配置合并 → 资源探查 → 准入排队 →
构建引擎 → 执行 → 产出落盘 → 拉起看板。它是 CLI 与引擎之间的**用例层**。

**不含**：参数解析与分发（``cli.py``）、引擎内部逻辑（``runtime.py``）。

**为什么单独成模块**：它是全仓第二长的函数（186 行）所在，且依赖面最广
（配置 + 资源 + 队列 + 引擎 + 看板）。与 ``cli.py`` 分开后，CLI 只剩「解析与分发」，
两者可各自测试；依赖方向单向：``cli → pipeline``。
"""

from __future__ import annotations

import contextlib
import json
from datetime import datetime
from pathlib import Path

from loguru import logger

from ptrade_sim import config as cfgmod
from ptrade_sim import resources
from ptrade_sim.config import DEFAULT_PORT, DEFAULT_RESULTS_DIR
from ptrade_sim.exceptions import ConfigError, exit_code_for
from ptrade_sim.runtime import BacktestEngine, compute_metrics, frame_to_csv_text

#: CLI 退出码（与 ``exceptions.exit_code_for`` 的约定一致）：
#: 0 成功 / 1 未分类 / 2 配置·策略定位 / 3 数据 / 4 策略代码 / 5 资源·队列 / 6 缺依赖
EXIT_CONFIG = 2
EXIT_DATA = 3
EXIT_RESOURCE = 5


def run_backtest(args) -> int:
    cwd = Path.cwd()

    # 1. 定位策略：--strategy 优先，其次配置文件里的 strategy
    strategy_target = args.strategy
    if not strategy_target:
        try:
            pre = cfgmod.load(args.config, use_env=False)
        except FileNotFoundError as exc:
            logger.error(str(exc))
            return exit_code_for(exc)
        strategy_target = pre.get("strategy")
    if not strategy_target:
        logger.error(
            "未指定策略。用 --strategy <策略目录|策略.py>，或在配置里设置 strategy 字段。\n"
            "推荐目录形态：\n"
            "  strategies/my_strategy/\n"
            "  ├── strategy_config.json   # 可选：回测区间/资金/基准/展示名/策略入参\n"
            "  └── strategy.py            # 必需：策略代码"
        )
        return EXIT_CONFIG

    # 解析策略（目录 → strategy.py + strategy_config.json；或单个 .py）
    try:
        bundle = cfgmod.resolve_strategy(strategy_target)
    except (FileNotFoundError, ValueError) as exc:
        logger.error(str(exc))
        return exit_code_for(exc)

    # 2. 配置（分层：模板 ← 机器级私有 ← 策略级 ← 环境变量 ← CLI）
    try:
        cfg = cfgmod.load(args.config, strategy_config=bundle.config)
    except FileNotFoundError as exc:
        logger.error(str(exc))
        return exit_code_for(exc)
    cfg = _resolve(args, cfg)
    # 策略路径以解析结果为准（`--strategy` 优先于配置里的 strategy 字段）
    cfg["strategy"] = str(bundle.py)
    cfg["strategy_dir"] = str(bundle.dir)
    cfg["strategy_name"] = bundle.name
    # 原始策略配置（未合并）：留档到 run 目录，看板据此显示中文名
    cfg["strategy_config"] = dict(bundle.config)

    problems = cfgmod.validate(cfg)
    if problems:
        logger.error(cfgmod.guidance(problems))
        return EXIT_CONFIG

    strategy_path = bundle.py
    out_root = Path(cfg.get("output_dir") or args.output_dir or (cwd / DEFAULT_RESULTS_DIR))
    if not out_root.is_absolute():
        out_root = cwd / out_root
    out_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # 结果目录用策略**目录名**（一个策略一个目录时比文件名更稳定）
    output_dir = out_root / f"{bundle.stem}-{stamp}"
    logger.info(f"策略：{bundle.name}（{bundle.py.name} @ {bundle.dir}）")

    # 3. 资源探查 + 准入排队
    popt = cfg.get("preload", {}) or {}
    qopt = cfg.get("queue", {}) or {}
    cost = resources.estimate_cost(
        days=_count_trade_days(cfg),
        threads=int(popt.get("threads", 8)),
        preload_mode=popt.get("mode", "rolling"),
        rolling_window=int(popt.get("rolling_window_days", 10)),
        minute_budget_bytes=_minute_budget(cfg),
    )
    snap = resources.probe(out_root)
    logger.info(f"资源快照：{snap.describe()}")
    logger.info(f"本次回测：{cost.describe()}")
    for note in cost.notes:
        logger.warning(note)

    if args.no_queue or not qopt.get("enabled", True):
        logger.debug("队列已禁用，跳过准入检查")
    else:
        from ptrade_sim.queue import BacktestQueue, queue_dir_for

        q = BacktestQueue(
            queue_dir_for(out_root, override=qopt.get("dir")),
            enabled=True,
            max_parallel=qopt.get("max_parallel"),
            cpu_slots_limit=qopt.get("cpu_slots_limit"),
            poll_interval=qopt.get("poll_interval", 10),
            # 三态：None=无限等待；0=完全不等待（--no-wait）；>0=最多等这么久
            max_wait_sec=0 if args.no_wait else qopt.get("max_wait_sec", 3600),
        )
        logger.info(
            f"队列目录：{q.dir}（并发上限 {q.max_parallel} 个 / CPU 槽上限 {q.cpu_slots_limit}）"
        )
        if not q.acquire(
            strategy=strategy_path.name,
            slots=cost.est_cpu_slots,
            mem_bytes=cost.est_mem_bytes,
            probe_path=str(out_root),
        ):
            logger.error(
                "未获得运行许可，已退出（可加 --no-queue 强制直接运行，"
                "或用 --threads 降低占用后重试）"
            )
            return EXIT_RESOURCE

    # 4. 看板
    if not args.no_dashboard:
        try:
            from ptrade_sim import server

            url = server.ensure_running(out_root, args.port or DEFAULT_PORT)
        except Exception as exc:
            logger.warning(f"看板服务拉起失败：{exc}")
            url = None
        if url:
            logger.info(f"实时看板：{url}")
        else:
            logger.warning(
                f"看板不可用，可稍后手动运行 "
                f"ptrade-sim dashboard --port {args.port or DEFAULT_PORT}"
            )

    # 5. 运行
    engine = BacktestEngine(cfg, str(strategy_path), output_dir)
    daily = engine.run()

    # polars 写出（带 UTF-8 BOM，Excel 打开中文不乱码）
    (output_dir / "daily_stats.csv").write_text(frame_to_csv_text(daily), encoding="utf-8")
    trades = engine.trades_frame()
    (output_dir / "trades.csv").write_text(
        frame_to_csv_text(_csv_friendly_time(trades)), encoding="utf-8"
    )
    summary = compute_metrics(daily, trades, engine.capital_base, cfg)
    # 展示名来自策略目录的 strategy_config.json（无 name 时为目录名），见 config.resolve_strategy
    summary.setdefault("config", {})["strategy_name"] = cfg.get("strategy_name") or ""
    # 运行时资源/缓存画像：事后可判断瓶颈在哪
    with contextlib.suppress(Exception):
        summary["resources"] = {
            "snapshot": {
                "cpu_count": snap.cpu_count,
                "mem_total": snap.mem_total,
                "mem_available": snap.mem_available,
                "disk_free": snap.disk_free,
            },
            "estimated": {
                "days": cost.days,
                "threads": cost.threads,
                "mem_bytes": cost.est_mem_bytes,
                "cpu_slots": cost.est_cpu_slots,
            },
            "cache_stats": engine.feed.cache.stats(),
            "cache_config": engine.feed.cache.describe(),
        }
    # 数据缺口留档：回看窗口越过库内覆盖时，get_history 会把缺失交易日填成 NaN
    # 且仍返回满 count 行，不记录就完全看不出来。
    gaps: dict = {}
    with contextlib.suppress(Exception):
        gaps = engine.data_gaps()
        if gaps:
            summary["data_gaps"] = gaps
    # 取数失败必须在收尾显式落盘 —— 否则「结果不可信」这件事只存在于日志里
    data_errors: list[str] = []
    with contextlib.suppress(Exception):
        data_errors = engine.feed.src.data_errors()
        if data_errors:
            summary["data_errors"] = data_errors
    with (output_dir / "summary.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)

    logger.info("=" * 60)
    logger.info(f"回测结果目录：{output_dir}")
    logger.info(
        f"总收益率 {summary['total_return'] * 100:.2f}% | "
        f"年化 {summary['annual_return'] * 100:.2f}% | "
        f"夏普 {summary['sharpe']:.2f} | 最大回撤 {summary['max_drawdown'] * 100:.2f}%"
    )
    logger.info(
        f"胜率 {summary['win_rate'] * 100:.1f}% | 盈亏比 {summary['profit_loss_ratio']:.2f} | "
        f"成交 {summary['trade_count']} 笔 | 佣金 {summary['total_commission']:,.2f}"
    )
    with contextlib.suppress(Exception):
        logger.info(f"缓存画像：{engine.feed.cache.describe()}")
    # 数据缺口必须在收尾显式点出：否则用户看到"回测完成"就以为一切正常
    if gaps:
        cov = gaps.get("daily_coverage") or []
        span = f"{cov[0]}~{cov[1]}" if len(cov) >= 2 else "（未知）"
        missing = gaps.get("missing_days") or []
        head = "、".join(missing[:5]) + ("…" if len(missing) > 5 else "")
        logger.warning(
            f"⚠ 回看窗口超出库内数据覆盖：{gaps.get('missing_day_count', 0)} 个交易日缺失"
            f"（{head}），库内日线覆盖 {span}。"
            f"这些交易日被填为 NaN，依赖窗口起点的策略逻辑（如 close.iloc[0]）会失效，"
            f"成交笔数可能被低估。详见 summary.json 的 data_gaps。"
        )
    logger.info("=" * 60)

    # 取数失败比数据缺口更严重：那些交易日的行情根本没进来，策略看到的是
    # 「无数据」而不是真实行情 —— 上面的指标已经算错，只是看不出来。
    # 因此这里**不是警告而是失败**：非零退出码，让脚本/CI 无法忽略。
    if data_errors:
        logger.error("!" * 60)
        for line in data_errors:
            logger.error(line)
        logger.error(
            "本次回测**结果不可信**：上述交易日的行情缺失，策略据此做出的决策与"
            "真实行情无关。请先解决取数问题后重跑。"
        )
        logger.error(f"明细已写入：{output_dir / 'summary.json'} 的 data_errors 字段")
        logger.error("!" * 60)
        return EXIT_DATA

    return 0


def _csv_friendly_time(df):
    """把成交表的 ``time`` 列格式化成 ``YYYY-MM-DD HH:MM:SS`` 后再写 CSV。

    **为什么需要**：``trades_frame()`` 返回的是 polars ``Datetime`` 列，而
    ``write_csv`` 会把它序列化成 ``2021-01-05T14:50:00.000000`` ——
    中间带 ``T``、末尾 6 位小数秒。这个值在 Excel 里不认，看板也要额外处理。

    **为什么只在写出时改**：内存里保持 ``Datetime`` 类型，别处（如
    ``test_engine.py`` 用 ``.dt.date()`` 分组、以及任何按时间排序的调用）
    都依赖它。格式化成字符串只发生在落盘这一步。

    只认 ``Datetime`` 列；其它类型（或本就没有 trades）原样返回。
    """
    import polars as pl

    if df is None or df.height == 0 or "time" not in df.columns:
        return df
    if df.schema["time"] not in (pl.Datetime, pl.Date):
        return df
    return df.with_columns(pl.col("time").dt.strftime("%Y-%m-%d %H:%M:%S"))


def _resolve(cli, cfg) -> dict:
    """把命令行覆盖合并进配置。"""
    for cli_key, cfg_key in (
        ("strategy", "strategy"),
        ("start", "start_date"),
        ("end", "end_date"),
        ("capital", "capital_base"),
        ("db_path", "db_path"),
        ("frequency", "frequency"),
    ):
        val = getattr(cli, cli_key, None)
        if val:
            cfg[cfg_key] = val
    if cli.threads:
        cfg.setdefault("preload", {})["threads"] = int(cli.threads)
    return cfg


def _count_trade_days(cfg: dict) -> int:
    """按日历估算区间交易日数（用于资源估算；失败则用自然日近似）。"""
    try:
        from ptrade_sim.data_source import make_source

        db_path = cfg.get("db_path")
        if not db_path:
            raise ConfigError("配置缺少 db_path")
        src = make_source(db_path)
        cal = src.reference("ashare_calendar")
        if cal is not None:
            s = str(cfg["start_date"]).replace("-", "")
            e = str(cfg["end_date"]).replace("-", "")
            days = [d for d in cal["date"].to_list() if s <= d <= e]
            src.close()
            if days:
                return len(days)
    except Exception as exc:
        # 不静默：这里的失败会让资源估算用的天数**悄悄变成另一个数**，
        # 用户看不到任何提示就会以为估算基于真实交易日历。
        logger.warning(f"DB 取交易日失败，改用日历估算，天数可能与实际不符：{exc}")
    from datetime import date

    try:
        a = date.fromisoformat(str(cfg["start_date"])[:10])
        b = date.fromisoformat(str(cfg["end_date"])[:10])
        return max(1, int((b - a).days * 250 / 365))
    except Exception:
        return 250


def _minute_budget(cfg: dict) -> int:
    try:
        from ptrade_sim.cache import CacheConfig

        return CacheConfig.from_dict(cfg.get("cache")).resolve_minute_budget()
    except Exception:
        return 0
