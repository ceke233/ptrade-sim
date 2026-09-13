"""`get_stock_info` 的**时点**语义测试。

**为什么必须有这个文件**：官方签名没有日期参数，但该接口在**回测模块也可用** ——
所以三个字段必须反映「回测当日」，否则就是把未来信息泄漏给策略。实测：

    002002.SZ  2021 年叫「鸿达兴业」，2023-10-12 起戴帽为「ST鸿达」，2024-03-18 退市
    stock_basic 里只有末尾快照：name='ST鸿达(退)'、delist_date='20240318'

修复前 `get_stock_info` 直接回这两个值 —— 策略在 2021 年就能看到
「这只股票后来会变成 ST、会在 2024 年退市」，**知道结局**。
修复后：

| 字段 | 语义 |
|---|---|
| `stock_name` | 走 ``feed.stock_name(code, 回测日)``，与 `get_stock_name` **同源** |
| `de_listed_date` | 回测日尚未退市 → 官方「未退市」哨兵值 ``2900-01-01``；已退市才给真实日期 |
| `listed_date` | 静态事实，不变 |

本文件同时锁住「两个取名 API 不得再分叉」—— 它们曾给出不同的名字。
"""

from __future__ import annotations

import duckdb
import pytest
from tests.conftest import dc_columns

pytestmark = pytest.mark.integration

#: 日线里的逐日 name（模拟更名），以及 stock_basic 里的「末尾快照」
#:   002002.SZ：鸿达兴业 -> ST鸿达 -> 退市
#:   000001.SZ：名字始终不变
DAILY_NAMES = [
    ("002002.SZ", "20210101", "鸿达兴业"),
    ("002002.SZ", "20231012", "ST鸿达"),
    ("002002.SZ", "20240118", "ST鸿达"),
    ("000001.SZ", "20210101", "平安银行"),
    ("000001.SZ", "20240118", "平安银行"),
]
BASIC = [
    # code, name(末尾快照), list_date, delist_date, list_status
    ("002002.SZ", "ST鸿达(退)", "20040625", "20240318", "D"),
    ("000001.SZ", "平安银行", "19910403", None, "L"),
]


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    d = tmp_path_factory.mktemp("info")
    db = d / "s.duckdb"
    con = duckdb.connect(str(db))
    days = ("20210101", "20231012", "20240118", "20240318", "20240601")
    con.execute("CREATE TABLE ashare_calendar (date VARCHAR)")
    con.executemany("INSERT INTO ashare_calendar VALUES (?)", [(x,) for x in days])

    scols = dc_columns("ashare_stock_basic")
    con.execute(
        "CREATE TABLE ashare_stock_basic (" + ", ".join(f'"{c}" VARCHAR' for c in scols) + ")"
    )
    con.executemany(
        f"INSERT INTO ashare_stock_basic VALUES ({','.join('?' * len(scols))})",
        [
            tuple(
                {
                    "code": c,
                    "name": nm,
                    "market": "主板",
                    "list_date": ld,
                    "delist_date": dd,
                    "list_status": st,
                }.get(col)
                for col in scols
            )
            for c, nm, ld, dd, st in BASIC
        ],
    )

    dcols = dc_columns("ashare_1d_stock")
    con.execute(
        "CREATE TABLE ashare_1d_stock ("
        + ", ".join(
            f'"{c}" VARCHAR' if c in ("code", "date", "name") else f'"{c}" DOUBLE' for c in dcols
        )
        + ")"
    )
    con.executemany(
        f"INSERT INTO ashare_1d_stock VALUES ({','.join('?' * len(dcols))})",
        [
            tuple(
                {
                    "code": c,
                    "date": ds,
                    "name": nm,
                    "open": 10.0,
                    "high": 10.5,
                    "low": 9.8,
                    "close": 10.2,
                    "volume": 1e6,
                    "money": 1e7,
                    "preclose": 10.0,
                    "adj_factor": 1.0,
                    "is_st": 0.0,
                }.get(col)
                for col in dcols
            )
            for c, ds, nm in DAILY_NAMES
        ],
    )
    con.close()

    from ptrade_sim.runtime import BacktestEngine

    sd = d / "s"
    sd.mkdir()
    sp = sd / "strategy.py"
    sp.write_text(
        "def initialize(context):\n    pass\ndef handle_data(context):\n    pass\n",
        encoding="utf-8",
    )
    cfg = {
        "db_path": str(db),
        "start_date": "2021-01-01",
        "end_date": "2024-06-01",
        "capital_base": 1_000_000,
        "benchmark": "000300.SS",
        "frequency": "daily",
        "preload": {"mode": "rolling", "rolling_window_days": 3, "threads": 1},
        "queue": {"enabled": False},
    }
    e = BacktestEngine(cfg, str(sp), d / "out")
    e.load_strategy()
    return e


@pytest.fixture
def api(engine):
    from ptrade_sim.api import build_api

    return build_api(engine)


def _at(engine, ds, fn):
    """把回测日切到 ds 再调用（顺带清掉按日缓存，避免跨用例串味）。"""
    engine._day_str = ds
    engine._info_cache.clear()
    engine._name_cache.clear()
    return fn()


def test_stock_name_is_point_in_time(engine, api):
    """**核心回归**：简称必须是**回测当日**的名字，不是末尾快照。

    修复前 2021-04-07 会返回 ``ST鸿达(退)`` —— 同时泄漏「后来戴帽」与「后来退市」。
    """
    got = _at(engine, "20210101", lambda: api["get_stock_info"](["002002.SZ"], ["stock_name"]))
    assert got["002002.SZ"]["stock_name"] == "鸿达兴业", (
        f"2021 年应叫『鸿达兴业』，实际 {got['002002.SZ']['stock_name']!r} —— "
        f"若为『ST鸿达(退)』说明又取了 stock_basic 的末尾快照"
    )
    got2 = _at(engine, "20231012", lambda: api["get_stock_info"](["002002.SZ"], ["stock_name"]))
    assert got2["002002.SZ"]["stock_name"] == "ST鸿达", "2023-10-12 起才戴帽"


def test_stock_name_matches_get_stock_name(engine, api):
    """两个取名 API 必须**同源** —— 它们曾给出不同的名字。

    ``get_stock_name`` 一直是时点正确的；``get_stock_info`` 曾用末尾快照。
    同一个代码在同一天被两个 API 报出不同名字，是很容易误导策略的。
    """
    for ds in ("20210101", "20231012"):
        engine._day_str = ds
        engine._info_cache.clear()
        engine._name_cache.clear()
        a = api["get_stock_info"](["002002.SZ"], ["stock_name"])["002002.SZ"]["stock_name"]
        b = api["get_stock_name"](["002002.SZ"])["002002.SZ"]
        assert a == b, f"{ds}: get_stock_info={a!r} 与 get_stock_name={b!r} 不一致"


def test_de_listed_date_does_not_leak_the_future(engine, api):
    """**核心回归**：回测日尚未退市时，必须返回官方「未退市」哨兵值。

    ``002002.SZ`` 于 2024-03-18 退市。回测日 2021 / 2023 时它还在市 ——
    此时返回 ``2024-03-18`` 就等于让策略知道结局。
    """
    for ds in ("20210101", "20231012", "20240118"):
        got = _at(engine, ds, lambda: api["get_stock_info"](["002002.SZ"], ["de_listed_date"]))
        v = got["002002.SZ"]["de_listed_date"]
        assert v == "2900-01-01", (
            f"{ds} 时它尚未退市，应返回 2900-01-01，实际 {v!r} —— 退市日泄漏了未来"
        )


def test_de_listed_date_after_delisting_is_real(engine, api):
    """回测日**已过退市日**时，给真实退市日（那时它已是过去事实，不算泄漏）。"""
    got = _at(engine, "20240601", lambda: api["get_stock_info"](["002002.SZ"], ["de_listed_date"]))
    assert got["002002.SZ"]["de_listed_date"] == "2024-03-18"


def test_never_delisted_stays_sentinel(engine, api):
    """从未退市的标的恒为哨兵值。"""
    for ds in ("20210101", "20240601"):
        got = _at(engine, ds, lambda: api["get_stock_info"](["000001.SZ"], ["de_listed_date"]))
        assert got["000001.SZ"]["de_listed_date"] == "2900-01-01"


def test_listed_date_is_static(engine, api):
    """上市日是静态事实，任何回测日都应一致。"""
    vals = set()
    for ds in ("20210101", "20231012", "20240601"):
        got = _at(engine, ds, lambda: api["get_stock_info"](["002002.SZ"], ["listed_date"]))
        vals.add(got["002002.SZ"]["listed_date"])
    assert vals == {"2004-06-25"}, f"上市日不应随回测日变化，实际 {vals}"


def test_field_none_returns_only_stock_name(engine, api):
    """官方：field 不入参时默认只返回 stock_name。"""
    got = _at(engine, "20210101", lambda: api["get_stock_info"](["002002.SZ"]))
    assert set(got["002002.SZ"]) == {"stock_name"}, f"实际 {sorted(got['002002.SZ'])}"
    assert got["002002.SZ"]["stock_name"] == "鸿达兴业"


def test_unknown_code_returns_none_fields(engine, api):
    """基础表里没有的代码：三个字段都是 None（不抛异常）。"""
    got = _at(engine, "20210101", lambda: api["get_stock_info"](["999999.SZ"]))
    assert set(got) == {"999999.SZ"}
    assert got["999999.SZ"]["stock_name"] is None
