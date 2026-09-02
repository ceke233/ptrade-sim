# -*- coding: utf-8 -*-
"""ptrade-sim 命令行入口。

用法：
    ptrade-sim backtest [--config ptrade_config.json] [--strategy examples/xxx.py] [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--capital 100000]

所有路径相对于「当前工作目录」解析；输出写入 ./backtest_results/{策略名}-{时间戳}/。
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from loguru import logger

from ptrade_sim.runtime import BacktestEngine, compute_metrics, render_report


def parse_args():
    parser = argparse.ArgumentParser(
        prog="ptrade-sim",
        description="PTrade 策略本地模拟回测平台（分钟级撮合）",
    )
    sub = parser.add_subparsers(dest="command")

    bt = sub.add_parser("backtest", help="运行回测")
    bt.add_argument(
        "--config", default="ptrade_config.json", help="配置文件路径（默认 ./ptrade_config.json）"
    )
    bt.add_argument("--strategy", default=None, help="策略文件路径（覆盖配置；相对当前目录）")
    bt.add_argument("--start", default=None, help="回测开始日期 YYYY-MM-DD（覆盖配置）")
    bt.add_argument("--end", default=None, help="回测结束日期 YYYY-MM-DD（覆盖配置）")
    bt.add_argument("--capital", type=float, default=None, help="初始资金（覆盖配置）")
    bt.add_argument("--data-dir", default=None, help="行情数据目录（覆盖配置 data_dir）")
    bt.add_argument("--output-dir", default=None, help="输出目录（默认 ./backtest_results）")

    return parser.parse_args()


def main(argv=None):
    args = parse_args()

    if args.command != "backtest":
        print("用法: ptrade-sim backtest [--config ...] [--strategy ...] [--start ...] [--end ...] [--capital ...] [--data-dir ...] [--output-dir ...]")
        print("     所有路径相对于当前工作目录解析")
        return 1

    cwd = Path.cwd()

    # 1. 配置
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = cwd / config_path
    if not config_path.exists():
        logger.error(
            f"配置文件不存在: {config_path}。\n"
            f"请先准备配置文件（含 data_dir/start_date/end_date/strategy 等字段），"
            f"参考: data_dir 指向按 hive 分区组织的行情 parquet 目录"
        )
        return 1
    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)

    # 2. 命令行覆盖配置
    if args.strategy:
        config["strategy"] = args.strategy
    if args.start:
        config["start_date"] = args.start
    if args.end:
        config["end_date"] = args.end
    if args.capital:
        config["capital_base"] = args.capital
    if args.data_dir:
        config["data_dir"] = args.data_dir

    # 3. 策略路径相对当前工作目录解析
    strategy_path = Path(config["strategy"])
    if not strategy_path.is_absolute():
        strategy_path = cwd / strategy_path
    if not strategy_path.exists():
        logger.error(f"策略文件不存在: {strategy_path}")
        return 1

    # 4. 输出目录：{output_dir}/{策略名称}-{时间}
    out_root = Path(args.output_dir) if args.output_dir else (cwd / "backtest_results")
    if not out_root.is_absolute():
        out_root = cwd / out_root
    out_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = out_root / f"{strategy_path.stem}-{stamp}"
    config["strategy"] = str(strategy_path)

    engine = BacktestEngine(config, str(strategy_path), output_dir)
    daily = engine.run()

    # 5. 输出文件
    daily.to_csv(output_dir / "daily_stats.csv", index=False, encoding="utf-8-sig")
    trades = engine.trades_frame()
    trades.to_csv(output_dir / "trades.csv", index=False, encoding="utf-8-sig")
    summary = compute_metrics(daily, trades, engine.capital_base, config)
    with open(output_dir / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
    render_report(summary, daily, trades, output_dir / "report.html")

    # 6. 控制台摘要
    logger.info("=" * 60)
    logger.info(f"回测结果目录：{output_dir}")
    logger.info(
        f"总收益率 {summary['total_return'] * 100:.2f}% | 年化 {summary['annual_return'] * 100:.2f}% | "
        f"夏普 {summary['sharpe']:.2f} | 最大回撤 {summary['max_drawdown'] * 100:.2f}%"
    )
    logger.info(
        f"胜率 {summary['win_rate'] * 100:.1f}% | 盈亏比 {summary['profit_loss_ratio']:.2f} | "
        f"成交 {summary['trade_count']} 笔 | 佣金 {summary['total_commission']:,.2f}"
    )
    logger.info(
        f"期末资产 {summary['final_value']:,.2f} | 基准收益 "
        f"{summary['benchmark_return'] * 100 if summary['benchmark_return'] == summary['benchmark_return'] else float('nan'):.2f}%"
    )
    logger.info("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
