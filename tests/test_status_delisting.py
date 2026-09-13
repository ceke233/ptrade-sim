"""`get_stock_status` / `filter_stock_by_status` 的**时点**语义测试（重点 DELISTING）。

**为什么必须有这个文件**：官方对该接口的定义是「获取**指定日期**证券的
ST、停牌、退市等属性」—— 是按时点的。而 DELISTING 分支曾有两处问题：

1. ``or row.get("list_status") == "D"`` —— ``list_status`` 是「**当前**是否退市」。
   用于历史查询会把「**未来才退市**」的股票也判为已退市。实测 2020-01-02
   它返回 319 只「已退市」，而当日实际只有 110 只 —— 多出的 209 只
   （退市锐电、乐视退…）在 2020 年还在正常交易。
   策略若据此剔除，等于**提前躲开了未来的输家** = 幸存者偏差、收益虚高。
2. ``cur = day_iso(ds)`` 得到 ``YYYY-MM-DD``，与库内 ``YYYYMMDD`` 比较时
   **同年份必然判错**（第 5 个字符 ``'0'`` > ``'-'``），
   于是「当年退市的股票，当年查不出来」。

修复后：只按 ``delist_date <= 查询日`` 判定（同为 YYYYMMDD 比较），
不看 ``list_status``。
"""

from __future__ import annotations

import duckdb
import pytest
from tests.conftest import dc_columns

pytestmark = pytest.mark.integration

#: (代码, list_date, delist_date, list_status)
STOCKS = [
    ("000001.SZ", "19910403", None, "L"),  # 常年在市
    ("600000.SS", "19991110", None, "L"),  # 常年在市
    ("000018.SZ", "19920616", "20200107", "D"),  # 2020-01-07 退市
    ("601558.SS", "20110113", "20200702", "D"),  # 2020-07-02 退市
    ("300004.SZ", "20091030", "20250301", "D"),  # **同年份**退市（格式 bug 的靶子）
]


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    """在合成库上建一个引擎（只需构造，不必 run）。"""
    d = tmp_path_factory.mktemp("st")
    db = d / "s.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE ashare_calendar (date VARCHAR)")
    con.executemany(
        "INSERT INTO ashare_calendar VALUES (?)",
        [(x,) for x in ("20190102", "20200102", "20200107", "20200702", "20250102", "20250601")],
    )
    scols = dc_columns("ashare_stock_basic")
    con.execute(
        "CREATE TABLE ashare_stock_basic (" + ", ".join(f'"{c}" VARCHAR' for c in scols) + ")"
    )
    con.executemany(
        f"INSERT INTO ashare_stock_basic VALUES ({','.join('?' * len(scols))})",
        [
            tuple(
                {
                    "code": code,
                    "name": code[:6],
                    "market": "主板",
                    "list_date": ld,
                    "delist_date": dd,
                    "list_status": st,
                }.get(c)
                for c in scols
            )
            for code, ld, dd, st in STOCKS
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
    con.execute(
        f"INSERT INTO ashare_1d_stock VALUES ({','.join('?' * len(dcols))})",
        tuple(
            {
                "code": "000001.SZ",
                "date": "20200102",
                "open": 10.0,
                "high": 10.5,
                "low": 9.8,
                "close": 10.2,
                "volume": 1e6,
                "money": 1e7,
                "preclose": 10.0,
                "adj_factor": 1.0,
                "is_st": 0.0,
                "name": "测试",
            }.get(c)
            for c in dcols
        ),
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
        "start_date": "2020-01-02",
        "end_date": "2020-01-07",
        "capital_base": 1_000_000,
        "benchmark": "000300.SS",
        "frequency": "daily",
        "preload": {"mode": "rolling", "rolling_window_days": 3, "threads": 1},
        "queue": {"enabled": False},
    }
    e = BacktestEngine(cfg, str(sp), d / "out")
    e.load_strategy()
    return e


def _delisting(engine, code: str, ds: str) -> bool | None:
    return engine._stock_status_one(code, "DELISTING", ds)


def test_not_delisted_before_delist_date(engine):
    """退市日**之前**查询，必须为「未退市」。

    这是幸存者偏差的核心：``list_status`` 现在已经是 ``D``，
    但 2020-01-02 时这些股票还在正常交易。
    """
    for code, ds in (("000018.SZ", "20200102"), ("601558.SS", "20200102")):
        assert _delisting(engine, code, ds) is False, (
            f"{code} 在 {ds} 尚未退市 —— 判为 True 就是提前剔除了未来的输家"
        )


def test_delisted_on_or_after_delist_date(engine):
    """退市日当天及之后，必须为「已退市」。"""
    assert _delisting(engine, "000018.SZ", "20200107") is True, "退市日当天应判已退市"
    assert _delisting(engine, "000018.SZ", "20200702") is True
    assert _delisting(engine, "601558.SS", "20200702") is True


def test_same_year_delisting_is_detected(engine):
    """**同年份**退市必须查得出来（日期格式 bug 的回归）。

    旧实现用 ``day_iso(ds)`` 得到 ``YYYY-MM-DD`` 去和库里的 ``YYYYMMDD`` 比，
    同年份必然判错：``'20250301' <= '2025-06-01'`` 为 ``False``。
    于是「当年退市的股票，当年查不出来」。
    """
    assert _delisting(engine, "300004.SZ", "20250102") is False, "2025-03-01 才退市"
    assert _delisting(engine, "300004.SZ", "20250601") is True, (
        "同年份退市未被识别 —— 日期比较又在混用格式"
    )


def test_never_delisted_is_false(engine):
    assert _delisting(engine, "000001.SZ", "20250601") is False
    assert _delisting(engine, "600000.SS", "20250601") is False


def test_list_status_is_not_consulted(engine):
    """防回退：判定不得依赖 ``list_status``。

    ``000018.SZ`` 的状态是 ``D``（今天已退市），但它在 2020-01-02 时在市。
    只要实现里还看 ``list_status``，这条就会失败。
    """
    assert engine.feed.basic_dict()["000018.SZ"]["list_status"] == "D", "前提校验"
    assert _delisting(engine, "000018.SZ", "20200102") is False, (
        "仍在读 list_status —— 历史查询会产生幸存者偏差"
    )


def test_filter_stock_by_status_keeps_still_listed(engine):
    """``filter_stock_by_status(['DELISTING'])`` 只该剔掉**当时**已退市的。

    回测日 2020-01-02：已退市 0 只（000018 于 01-07 才退市），
    所以三只标的都应保留。
    """
    from ptrade_sim.api import build_api

    api = build_api(engine)
    engine._day_str = "20200102"
    kept = api["filter_stock_by_status"](["000001.SZ", "000018.SZ", "601558.SS"], ["DELISTING"])
    assert set(kept) == {"000001.SZ", "000018.SZ", "601558.SS"}, (
        f"2020-01-02 时这三只都还在市，不该被剔除，实际 {kept}"
    )

    engine._day_str = "20200702"
    kept2 = api["filter_stock_by_status"](["000001.SZ", "000018.SZ", "601558.SS"], ["DELISTING"])
    assert set(kept2) == {"000001.SZ"}, f"2020-07-02 时另两只已退市，实际 {kept2}"
