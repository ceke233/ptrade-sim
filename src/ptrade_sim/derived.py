"""看板的派生指标（由 run 产物二次计算）。

**职责**：把 ``daily_stats.csv`` / ``summary.json`` 里的原始数据算出看板要展示的
派生量 —— 月度收益矩阵、β/α 回归、资金曲线序列。

**不含**：文件读取（用 ``runstore`` 的读取函数）、HTTP 路由。

依赖方向单向：``derived → runstore``（只用它的 CSV 读取与数值归一）。
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import polars as pl

from ptrade_sim import runstore


def _fmt_date(x) -> str:
    s = str(x)
    return s[:10] if len(s) >= 10 else s


def _series(d: Path) -> dict:
    """资金曲线/回撤/基准序列（benchmark 全 NaN 则忽略该列）。"""
    stats_path = d / "daily_stats.csv"
    if not stats_path.exists():
        return {"dates": [], "equity": [], "drawdown": [], "bench": []}
    try:
        daily = runstore._read_csv_retry(stats_path)
    except (OSError, ValueError):
        return {"dates": [], "equity": [], "drawdown": [], "bench": []}
    out = {
        "dates": [_fmt_date(x) for x in daily["date"].to_list()],
        "equity": [runstore._as_float(x) for x in daily["total_value"].to_list()],
        "drawdown": [runstore._as_float(x) for x in daily["drawdown"].to_list()],
        "bench": [],
    }
    if "benchmark_close" in daily.columns and daily["benchmark_close"].null_count() == 0:
        out["bench"] = [runstore._as_float(x) for x in daily["benchmark_close"].to_list()]
    return out


def _monthly_extended(d: Path) -> dict:
    """按月聚合 daily_stats.csv → 明细表扩展列。

    返回 { "YYYY-MM": {ret, bench, excess, mdd, trades, commission, beta, alpha} }（比例值）；
    基准收益 = 月末 benchmark_close / 月初 - 1；月内回撤为该月 drawdown 最小值；
    beta/alpha = 月内策略日收益对基准日收益回归（样本约 20 交易日）。
    """
    stats_path = d / "daily_stats.csv"
    if not stats_path.exists():
        return {}
    try:
        daily = runstore._read_csv_retry(stats_path)
    except (OSError, ValueError):
        return {}
    out: dict[str, dict] = {}
    if daily.height == 0:
        return out
    # polars：不修改原表，按 YYYY-MM 分组（保持出现顺序）
    daily = daily.with_columns(pl.col("date").cast(pl.String).str.slice(0, 7).alias("_ym"))
    for (ym,), g in daily.group_by(["_ym"], maintain_order=True):
        g = g.sort("date")
        total = g["total_value"].cast(pl.Float64)
        ret = float(total[-1] / total[0] - 1) if g.height else 0.0
        bench = None
        has_bench = "benchmark_close" in g.columns
        if has_bench and g["benchmark_close"].null_count() == 0:
            bc = g["benchmark_close"].cast(pl.Float64)
            bench = float(bc[-1] / bc[0] - 1)
        dd = float(g["drawdown"].cast(pl.Float64).min()) if "drawdown" in g.columns else None
        trades = int(g["trades_count"].cast(pl.Float64).sum()) if "trades_count" in g.columns else 0
        comm = float(g["commission"].cast(pl.Float64).sum()) if "commission" in g.columns else 0.0
        # 月内日收益回归 → beta/alpha（alpha 为月内年化超额，口径与整体一致）
        # 基准日收益在 polars 里用 diff 表达 pandas 的 pct_change
        rets = g["daily_return"].cast(pl.Float64).to_numpy()
        if has_bench:
            bc_all = g["benchmark_close"].cast(pl.Float64)
            bench_rets = (bc_all / bc_all.shift(1) - 1).to_numpy()
        else:
            bench_rets = np.full(len(rets), np.nan)
        beta, alpha = _regress_beta_alpha(rets, bench_rets, annualize=12)
        out[str(ym)] = {
            "ret": ret,
            "bench": bench,
            "excess": ret - bench if bench is not None else None,
            "mdd": dd if dd is not None and not math.isnan(dd) else None,
            "trades": trades,
            "commission": comm,
            "beta": beta,
            "alpha": alpha,
        }
    return out


def _regress_beta_alpha(
    rets: object, bench_rets: object, annualize: int = 252
) -> tuple[float | None, float | None]:
    """策略日收益对基准日收益 OLS 回归 → (beta, alpha 年化)。

    只取两序列都非空的配对；样本不足或基准无波动时返回 (None, None)。
    beta = cov(r, b) / var(b)；alpha = (mean(r) - beta*mean(b)) * annualize。
    """
    try:
        r = np.asarray(rets, dtype=float)
        b = np.asarray(bench_rets, dtype=float)
        mask = ~(np.isnan(r) | np.isnan(b))
        r, b = r[mask], b[mask]
        if len(r) < 2:
            return None, None
        vb = float(np.var(b, ddof=1))
        if vb <= 0 or not np.isfinite(vb):
            return None, None
        beta = float(np.cov(r, b, ddof=1)[0, 1] / vb)
        alpha = float((r.mean() - beta * b.mean()) * annualize)
        if not np.isfinite(beta) or not np.isfinite(alpha):
            return None, None
        return beta, alpha
    except Exception:
        return None, None


def _overall_alpha_beta(d: Path) -> dict:
    """整段回测的 Beta / Alpha（年度化），与 supermind 绩效口径一致。"""
    stats_path = d / "daily_stats.csv"
    if not stats_path.exists():
        return {"beta": None, "alpha": None}
    try:
        daily = runstore._read_csv_retry(stats_path)
    except (OSError, ValueError):
        return {"beta": None, "alpha": None}
    if daily.height == 0 or "benchmark_close" not in daily.columns:
        return {"beta": None, "alpha": None}
    rets = daily["daily_return"].cast(pl.Float64).to_numpy()
    bc = daily["benchmark_close"].cast(pl.Float64)
    bench_rets = (bc / bc.shift(1) - 1).to_numpy()  # polars 版 pct_change
    beta, alpha = _regress_beta_alpha(rets, bench_rets, annualize=252)
    return {"beta": beta, "alpha": alpha}
