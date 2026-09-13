# ruff: noqa: F821  # PTrade API（g/set_*/get_*/order_*/run_daily/log）由回测引擎注入，静态检查无法识别
# -*- coding: utf-8 -*-
"""示例策略：主板动量轮动（原生 PTrade 结构，用于验证平台）。

每 g.period 个交易日（g.run_count 计数）从 g.pool 中按动量排序，
持有动量最强的前 g.top_n 只，用 order_target_value 调仓；
卖出前检查 closeable_amount（体现 T+1：当日买入不可卖）。

**可调参数**来自同目录 ``strategy_config.json`` 的 ``params`` 段
（``top_n`` / ``watch_days`` / ``rebalance_days``），通过
``get_strategy_params()`` 读取 —— 这是本平台的扩展（官方 ``set_parameters``
仅交易模块可用）。改参数不必改代码。
"""

from loguru import logger  # PTrade 内置 log 的替代；本地验证用


def initialize(context):
    # 参数缺失时的默认值写在代码里，配置只覆盖需要调的那几个
    g.period = int(get_strategy_params("rebalance_days", 5))  # 轮动周期（交易日）
    g.watch_days = int(get_strategy_params("watch_days", 20))  # 动量回看窗口
    g.top_n = int(get_strategy_params("top_n", 3))  # 持仓只数
    g.run_count = 0  # 调度计数
    g.pool = [
        "600519.SS",
        "000858.SZ",
        "601318.SS",
        "600036.SS",
        "000333.SZ",
        "601888.SS",
        "600900.SS",
        "603259.SS",
    ]
    set_benchmark("000300.SS")
    set_commission(commission_ratio=0.0003, min_commission=5.0)
    set_universe(g.pool)
    run_daily(context, rebalance, time="09:31")
    log.info(
        f"参数：周期 {g.period} 日 / 回看 {g.watch_days} 日 / 持仓 {g.top_n} 只"
    )


def rebalance(context):
    g.run_count += 1
    if (g.run_count - 1) % g.period != 0:
        return
    # 动量（多股票日线收盘，官方返回格式：index + [code, close]）
    his = get_history(g.watch_days, "1d", "close", g.pool)
    if his is None or len(his) == 0:
        return
    mom = his.groupby("code").apply(
        lambda df: df["close"].iloc[-1] / df["close"].iloc[0] - 1
    )
    mom = mom.dropna().sort_values(ascending=False)
    if len(mom) == 0:
        return
    targets = list(mom.index[: g.top_n])
    log.info(f"第 {g.run_count} 次调度，动量前{g.top_n}：{targets}")

    # 先卖出不在目标池的持仓（T+1：当日买入 closeable_amount=0，无法卖出）
    for code in list(context.portfolio.positions):
        if code not in targets:
            pos = context.portfolio.positions[code]
            if pos.closeable_amount > 0:
                order_target(code, 0)
                log.info(f"卖出：{code}")
            else:
                log.info(f"跳过卖出 {code}（T+1 今日买入不可卖）")

    # 等权买入目标池
    if len(targets) == 0:
        return
    value = context.portfolio.total_value * 0.3
    for code in targets:
        order_target_value(code, value)
        log.info(f"目标买入：{code} 市值 {value:,.0f}")


def handle_data(context, data):
    pass


def after_trading_end(context, data):
    pf = context.portfolio
    logger.info(
        f"[盘后] {context.blotter.current_dt:%Y-%m-%d} "
        f"总资产 {pf.portfolio_value:,.0f} 现金 {pf.cash:,.0f} 持仓 {len(pf.positions)} 只"
    )
