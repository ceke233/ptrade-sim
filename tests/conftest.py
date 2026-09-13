"""pytest 公共夹具。

**核心设计：合成一个微型 DuckDB 库**，而不是依赖动辄几十 GB 的真实行情库。
理由：

1. 真实库在 CI 上不存在，测试会永久 skip → 等于没测；
2. 真实库数据会变，断言无法确定 → 测试变脆；
3. 微型库（3 只股票 × 数个交易日）毫秒级建好，测试可穷尽边界。

微型库遵循与真实库**完全相同的表契约**（``ptrade_sim.data_contract``），
所以契约一旦被破坏，这里的夹具构建也会失败 —— 夹具本身即是一道契约校验。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# 让测试无需安装即可 import（CI 里用 `pip install -e .` 亦可）
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# ============================================================
# 合成数据参数（集中定义，测试直接引用，避免魔法数散落）
# ============================================================

#: 交易日（2025-01-02 ~ 2025-01-08，跳过周末）
TRADE_DAYS = ["20250102", "20250103", "20250106", "20250107", "20250108"]

#: 日历额外包含「无行情」的交易日，用于验证「区间超出数据覆盖」告警
#: （真实场景：日历是全量的，而行情只灌了部分年份）
CALENDAR_ONLY_DAYS = ["20250109", "20250110", "20250113"]

#: 日历额外包含**行情起点之前**的交易日。
#: 真实库的日历覆盖 2005+，而行情只从 2019 起 —— 于是 get_history 的回看窗口
#: 会取到这些「日历里有、行情里没有」的日子，缺失时被填成 NaN，
#: 且 get_history 仍返回满 count 行。夹具必须保留这个特性，否则测不出该行为
#: （曾因此漏掉一个「策略静默跳过调仓」的缺陷）。
PRE_COVERAGE_DAYS = [
    "20241216",
    "20241217",
    "20241218",
    "20241219",
    "20241220",
    "20241223",
    "20241224",
    "20241225",
    "20241226",
    "20241227",
    "20241230",
    "20241231",
]

#: 三只股票：深主板 / 沪主板 / ST（用于验证涨跌停与 ST 分支）
STOCKS = [
    # code, name, market, list_date, preclose0
    ("000001.SZ", "平安银行", "主板", "19910403", 10.00),
    ("600000.SS", "浦发银行", "主板", "19991110", 8.00),
    ("000002.SZ", "ST万科", "主板", "19910129", 5.00),
]

#: 各股每日收盘价（用确定序列，便于断言）
CLOSES = {
    "000001.SZ": [10.50, 10.80, 11.00, 10.90, 11.20],
    "600000.SS": [8.20, 8.10, 8.40, 8.50, 8.30],
    "000002.SZ": [5.10, 5.05, 5.15, 5.20, 5.30],
}

MINUTE_SLOTS = 241


def dc_columns(table: str) -> tuple[str, ...]:
    """取契约列定义——夹具据此建表，**契约改坏时夹具会立刻失败**。"""
    from ptrade_sim import data_contract as dc

    t = dc.contract_of(table)
    assert t is not None, f"契约里没有表 {table}"
    return t.columns


def _minute_rows(ds: str, code: str, base: float, o: float, h: float, low: float, c: float):
    """生成某股某日的 241 根分钟 bar（价格线性插值，量恒定）。

    槽位与引擎一致：09:30、09:31~11:30、13:01~15:00。
    """
    times = ["09:30"]
    t = 9 * 60 + 31
    while t <= 11 * 60 + 30:
        times.append(f"{t // 60:02d}:{t % 60:02d}")
        t += 1
    t = 13 * 60 + 1
    while t <= 15 * 60:
        times.append(f"{t // 60:02d}:{t % 60:02d}")
        t += 1
    assert len(times) == MINUTE_SLOTS, len(times)

    n = len(times)
    rows = []
    for i, hm in enumerate(times):
        # 从 open 线性走到 close，途中触及 high/low
        frac = i / (n - 1)
        px = round(o + (c - o) * frac, 2)
        rows.append(
            (
                code,
                ds,  # date（表定义顺序）
                f"{ds[:4]}-{ds[4:6]}-{ds[6:]} {hm}:00",  # trade_time
                o if i == 0 else px,
                max(px, h) if i in (n // 2, n - 1) else px,
                min(px, low) if i in (n // 3, n - 1) else px,
                c if i == n - 1 else px,
                1000.0,
                1000.0 * px,
                base,
                round(px - base, 2),
                round((px / base - 1) * 100, 4),
            )
        )
    return rows


@pytest.fixture(scope="session")
def tiny_db(tmp_path_factory) -> Path:
    """构建合成 DuckDB 库（session 级，全部测试共享一份）。"""
    duckdb = pytest.importorskip("duckdb", reason="DuckDB 后端测试需要 duckdb")
    db = tmp_path_factory.mktemp("db") / "tiny.duckdb"
    con = duckdb.connect(str(db))

    # ---- 日历（含行情起点之前 + 之后的额外交易日，模拟「日历全量、行情部分」）----
    con.execute("CREATE TABLE ashare_calendar (date VARCHAR)")
    con.executemany(
        "INSERT INTO ashare_calendar VALUES (?)",
        [(d,) for d in PRE_COVERAGE_DAYS + TRADE_DAYS + CALENDAR_ONLY_DAYS],
    )

    # ---- 股票基础（列必须与契约完全一致，否则契约校验会失败）----
    sb_cols = dc_columns("ashare_stock_basic")
    con.execute(
        "CREATE TABLE ashare_stock_basic (" + ", ".join(f'"{c}" VARCHAR' for c in sb_cols) + ")"
    )
    for code, name, market, list_date, _ in STOCKS:
        row = {
            "code": code,
            "symbol": code.replace(".SS", ".SH"),
            "name": name,
            "area": "深圳",
            "industry": "银行",
            "fullname": f"{name}股份有限公司",
            "enname": "CO",
            "cnspell": "PAYH",
            "market": market,
            "exchange": "SZSE",
            "curr_type": "CNY",
            "list_status": "L",
            "list_date": list_date,
            "delist_date": None,
            "is_hs": "S",
            "act_name": "法人",
            "act_ent_type": "企业",
        }
        con.execute(
            f"INSERT INTO ashare_stock_basic VALUES ({','.join('?' * len(sb_cols))})",
            [row.get(c) for c in sb_cols],
        )

    # ---- 日线 ----
    con.execute(
        "CREATE TABLE ashare_1d_stock (code VARCHAR, date VARCHAR, open DOUBLE, high DOUBLE, "
        "low DOUBLE, close DOUBLE, volume DOUBLE, money DOUBLE, preclose DOUBLE, "
        "adj_factor DOUBLE, vwap DOUBLE, name VARCHAR, is_st INTEGER, is_delisted INTEGER, "
        "change DOUBLE, pct_chg DOUBLE)"
    )
    for code, name, _, _, base0 in STOCKS:
        is_st = 1 if name.startswith("ST") else 0
        prev = base0
        for ds, close in zip(TRADE_DAYS, CLOSES[code], strict=False):
            o = round(prev * 1.001, 2)
            h = round(max(o, close) * 1.01, 2)
            low = round(min(o, close) * 0.99, 2)
            vol = 1_000_000.0
            con.execute(
                "INSERT INTO ashare_1d_stock VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    code,
                    ds,
                    o,
                    h,
                    low,
                    close,
                    vol,
                    vol * close,
                    prev,
                    1.0,
                    close,
                    name,
                    is_st,
                    0,
                    round(close - prev, 2),
                    round((close / prev - 1) * 100, 4),
                ],
            )
            prev = close

    # ---- 分钟线 ----
    con.execute(
        "CREATE TABLE ashare_1m_stock (code VARCHAR, date VARCHAR, trade_time VARCHAR, "
        "open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE, money DOUBLE, "
        "preclose DOUBLE, change DOUBLE, pct_chg DOUBLE)"
    )
    for code, _nm, _mk, _ld, base0 in STOCKS:
        prev = base0
        for ds, close in zip(TRADE_DAYS, CLOSES[code], strict=False):
            o = round(prev * 1.001, 2)
            h = round(max(o, close) * 1.01, 2)
            low = round(min(o, close) * 0.99, 2)
            rows = _minute_rows(ds, code, prev, o, h, low, close)
            con.executemany("INSERT INTO ashare_1m_stock VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            prev = close

    # ---- 估值/股本（字段名对齐 PTrade valuation，列与契约一致）----
    fe_cols = dc_columns("ashare_1d_feature")
    con.execute(
        "CREATE TABLE ashare_1d_feature ("
        + ", ".join(f'"{c}" VARCHAR' if c in ("code", "date") else f'"{c}" DOUBLE' for c in fe_cols)
        + ")"
    )
    for code, _n, _m, _ld, _b in STOCKS:
        for ds in TRADE_DAYS:
            row = {
                "code": code,
                "date": ds,
                "total_value": 1e11,
                "float_value": 9e10,
                "total_shares": 1e10,
                "a_floats": 1e10,
                "turnover_rate": 1.5,
                "dividend_ratio": 2.0,
                "pe_ttm": 6.0,
                "pb": 0.9,
                "ps": 1.2,
                "ps_ttm": 1.1,
                "pe": 6.1,
                "dv_ttm": 2.1,
                "volume_ratio": 1.0,
                "turnover_rate_f": 1.6,
                "free_share": 9e9,
                "log_mv": 25.3,
                "log_cmv": 25.2,
                "close": 10.0,
            }
            con.execute(
                f"INSERT INTO ashare_1d_feature VALUES ({','.join('?' * len(fe_cols))})",
                [row.get(c) for c in fe_cols],
            )

    # ---- 指数日线（基准）----
    idx_cols = dc_columns("ashare_1d_index")
    con.execute(
        "CREATE TABLE ashare_1d_index ("
        + ", ".join(
            f'"{c}" VARCHAR' if c in ("code", "date") else f'"{c}" DOUBLE' for c in idx_cols
        )
        + ")"
    )
    base = 4000.0
    for i, ds in enumerate(TRADE_DAYS):
        c = base + i * 10
        row = {
            "code": "000300.SS",
            "date": ds,
            "open": c - 5,
            "high": c + 20,
            "low": c - 20,
            "close": c,
            "volume": 1e9,
            "money": 1e12,
            "preclose": c - 10,
            "change": 10.0,
            "pct_chg": 0.25,
        }
        con.execute(
            f"INSERT INTO ashare_1d_index VALUES ({','.join('?' * len(idx_cols))})",
            [row.get(c) for c in idx_cols],
        )

    # ---- 指数成分+权重（拉链，左闭右开）----
    con.execute(
        "CREATE TABLE ashare_index_weight (index_code VARCHAR, index_name VARCHAR, "
        "code VARCHAR, in_date VARCHAR, out_date VARCHAR, weight DOUBLE, "
        "source VARCHAR, snapshot_date VARCHAR)"
    )
    con.execute(
        "INSERT INTO ashare_index_weight VALUES (?,?,?,?,?,?,?,?)",
        ["000300", "沪深300", "000001.SZ", "20190102", "", 40.0, "ptrade", ""],
    )
    con.execute(
        "INSERT INTO ashare_index_weight VALUES (?,?,?,?,?,?,?,?)",
        ["000300", "沪深300", "600000.SS", "20190102", "", 35.0, "ptrade", ""],
    )
    # 已在 20250103 调出：验证「左闭右开」与调出语义
    con.execute(
        "INSERT INTO ashare_index_weight VALUES (?,?,?,?,?,?,?,?)",
        ["000300", "沪深300", "000002.SZ", "20190102", "20250103", 25.0, "ptrade", ""],
    )

    # ---- L2 集合竞价 ----
    con.execute(
        "CREATE TABLE ashare_l2_auction (code VARCHAR, date VARCHAR, hq_px DOUBLE, "
        "business_amount DOUBLE)"
    )
    for code, _, _, _, base0 in STOCKS:
        for ds in TRADE_DAYS:
            con.execute(
                "INSERT INTO ashare_l2_auction VALUES (?,?,?,?)",
                [code, ds, base0, 500_000.0],
            )

    con.close()
    return db


@pytest.fixture
def engine_factory(tiny_db, tmp_path):
    """按需构造 ``BacktestEngine`` 的工厂（默认走微型库）。

    额外的两个关键字参数用于模拟「策略目录」形态：

    - ``strategy_config``：原样存进 config 的 ``strategy_config``（引擎会留档，
      看板据此显示展示名）与 ``params``；
    - ``params``：策略入参，`get_strategy_params()` 读取。
    """
    from ptrade_sim.runtime import BacktestEngine

    created: list = []

    def make(strategy_code: str | None = None, **overrides):
        code = strategy_code or (
            "def initialize(context):\n"
            "    set_benchmark('000300.SS')\n"
            "    set_universe(['000001.SZ'])\n"
        )
        idx = len(created)
        # 模拟策略目录：strategy.py + strategy_config.json
        sdir = tmp_path / f"strat_{idx}"
        sdir.mkdir(parents=True, exist_ok=True)
        sp = sdir / "strategy.py"
        sp.write_text(code, encoding="utf-8")

        sc = overrides.pop("strategy_config", None)
        params = overrides.pop("params", None)
        if sc is not None or params is not None:
            sc = dict(sc or {})
            if params is not None:
                sc["params"] = params
            (sdir / "strategy_config.json").write_text(
                json.dumps(sc, ensure_ascii=False), encoding="utf-8"
            )

        cfg = {
            "db_path": str(tiny_db),
            "start_date": "2025-01-02",
            "end_date": "2025-01-08",
            "capital_base": 1_000_000,
            "benchmark": "000300.SS",
            "frequency": "minute",
            "preload": {"mode": "rolling", "rolling_window_days": 3, "threads": 2},
            "queue": {"enabled": False},
        }
        cfg.update(overrides)
        if sc is not None:
            cfg["strategy_config"] = sc
            cfg["strategy_name"] = sc.get("name")
            cfg["params"] = sc.get("params", {})
        cfg["strategy_dir"] = str(sdir)
        out = tmp_path / f"out_{idx}"
        e = BacktestEngine(cfg, str(sp), out)
        created.append(e)
        return e

    return make


@pytest.fixture
def results_root_with_name(tmp_path) -> Path:
    """带策略配置留档的结果目录（验证看板从 run 自身取展示名）。"""
    root = tmp_path / "results_named"
    run = root / "demo-20250101_120000"
    run.mkdir(parents=True)
    (run / "strategy_config.json").write_text(
        json.dumps({"name": "目录名策略", "params": {"k": 1}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (run / "progress.json").write_text(
        json.dumps(
            {
                "phase": "done",
                "status": "done",
                "day_index": 3,
                "total_days": 3,
                "current_day": "2025-01-06",
                "percent": 100.0,
                "config": {"start_date": "2025-01-02", "end_date": "2025-01-06"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (run / "summary.json").write_text(
        json.dumps({"total_return": 0.01, "config": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (run / "daily_stats.csv").write_text(
        "date,total_value,cash,positions_value,benchmark_close,"
        "daily_return,cum_return,drawdown,trades_count,commission\n"
        "2025-01-02,1000000,1000000,0,4000.0,0.0,0.0,0.0,0,0.0\n"
        "2025-01-03,1010000,900000,110000,4010.0,0.01,0.01,-0.005,2,6.1\n",
        encoding="utf-8",
    )
    return root
