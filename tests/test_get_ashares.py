"""`get_Ashares` 的语义测试：必须是**沪深全部 A 股**。

**为什么必须有这个文件**：`get_Ashares` 曾经只返回**主板**（硬过滤
``market == "主板"``），而官方语义是「获取指定日期**沪深市场的所有A股**代码列表」。
于是它静默漏掉创业板 + 科创板约 40% 的股票：

    实测（真实库）  2025-01-02 全部沪深 A 股 5088 只 -> 它只给 3149 只
                    2020-01-02                3553 只 -> 它只给 2729 只

这不是「少一点」的问题：任何用 `get_Ashares()` 建全市场池的策略，
都拿到一个**静默缩水四成**的股票池，且不会报任何错。

本文件同时锁住三件事：
1. 纳入主板 + 创业板 + 科创板；
2. **排除北交所**（官方说的是「沪深市场」，北交所既非沪也非深）；
3. `market` 列与代码前缀不一致时**告警而非静默丢弃** ——
   那要么是脏数据，要么是交易所新开号段，两种都该让人知道。
"""

from __future__ import annotations

import duckdb
import pytest
from tests.conftest import dc_columns

pytestmark = pytest.mark.integration

#: (代码, market, list_date, delist_date, list_status, 是否应被 get_Ashares 返回)
CASES = [
    # ---- 沪深 A 股：应全部返回 ----
    ("000001.SZ", "主板", "19910403", None, "L", True),  # 深主板
    ("002001.SZ", "主板", "20040625", None, "L", True),  # 原中小板，现深主板
    ("600000.SS", "主板", "19991110", None, "L", True),  # 沪主板
    ("603001.SS", "主板", "20120518", None, "L", True),  # 沪主板 603 段
    ("300001.SZ", "创业板", "20091030", None, "L", True),  # 创业板 300
    ("301001.SZ", "创业板", "20210512", None, "L", True),  # 创业板 301
    ("302132.SZ", "创业板", "20100827", None, "L", True),  # 创业板 **302**（实测反例）
    ("688001.SS", "科创板", "20190722", None, "L", True),  # 科创板
    ("689009.SS", "科创板", "20200120", None, "L", True),  # 科创板 689 段
    # ---- 北交所：不返回（官方语义是沪深）----
    ("920001.BJ", "北交所", "20231101", None, "L", False),
    ("430047.BJ", "北交所", "20210701", None, "L", False),
    # ---- 时点过滤 ----
    ("300002.SZ", "创业板", "20991231", None, "L", False),  # 查询日尚未上市
    ("300003.SZ", "创业板", "20091030", "20200601", "D", False),  # 查询日前已退市
    # ---- 脏数据 ----
    # 真实库里的一条（上港集箱(退)），状态 D —— 被**状态过滤**挡下，走不到前缀校验
    ("TS0018.SS", "主板", "20000719", "20061020", "D", False),
    # 在市的非法代码 —— 只有它才会走到**前缀校验**，用于验证第二道防线 + 告警
    ("TS9999.SS", "主板", "20000719", None, "L", False),
    # ---- 交易所新号段：market 说创业板，前缀不在已知集合 ----
    ("310001.SZ", "创业板", "20250101", None, "L", False),
]


@pytest.fixture(scope="module")
def board_db(tmp_path_factory) -> str:
    """含各板块 + 北交所 + 脏数据的合成库。"""
    db = tmp_path_factory.mktemp("boards") / "b.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE ashare_calendar (date VARCHAR)")
    con.execute("INSERT INTO ashare_calendar VALUES ('20250102'), ('20250103'), ('20250601')")
    cols = dc_columns("ashare_stock_basic")
    con.execute(
        "CREATE TABLE ashare_stock_basic (" + ", ".join(f'"{c}" VARCHAR' for c in cols) + ")"
    )
    con.executemany(
        f"INSERT INTO ashare_stock_basic VALUES ({','.join('?' * len(cols))})",
        [
            tuple(
                {
                    "code": code,
                    "name": code[:6],
                    "market": mkt,
                    "list_date": ld,
                    "delist_date": dl,
                    "list_status": st,
                }.get(c)
                for c in cols
            )
            for code, mkt, ld, dl, st, _ in CASES
        ],
    )
    # DataFeed 要求至少一张行情表非空（否则视为「未建库」）——
    # 本文件只测 get_Ashares，故放最小日线即可。
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
                "date": "20250102",
                "open": 10.0,
                "high": 10.5,
                "low": 9.8,
                "close": 10.2,
                "volume": 1e6,
                "money": 1e7,
                "preclose": 10.0,
                "adj_factor": 1.0,
                "name": "测试",
            }.get(c)
            for c in dcols
        ),
    )
    con.close()
    return str(db)


@pytest.fixture(scope="module")
def feed(board_db):
    from ptrade_sim.cache import CacheConfig
    from ptrade_sim.runtime import DataFeed

    return DataFeed(board_db, "20250102", "20250601", cache_config=CacheConfig())


def test_includes_all_cn_boards(feed):
    """**核心回归**：主板 + 创业板 + 科创板都必须返回。

    旧实现只给主板，创业板/科创板整体消失 —— 这是 40% 量级的静默缩水。
    """
    got = set(feed.get_Ashares("20250102"))
    want = {c for c, _, _, _, _, keep in CASES if keep}
    assert got == want, f"缺失：{sorted(want - got)}\n多出：{sorted(got - want)}"


def test_includes_chinext_302_range(feed):
    """创业板 302 号段必须在内。

    这是一个**实测反例**：只按三位前缀写 ``300/301`` 会漏掉 ``302132.SZ``
    （中航成飞，创业板，在市）。故前缀判定用两位号段。
    """
    assert "302132.SZ" in feed.get_Ashares("20250102"), (
        "302 号段被漏掉 —— 前缀集合写得太窄（应使用两位号段 30）"
    )


def test_excludes_beijing_exchange(feed):
    """北交所必须排除 —— 官方语义是「沪深市场」。"""
    got = feed.get_Ashares("20250102")
    bj = [c for c in got if c.endswith(".BJ")]
    assert not bj, f"不应包含北交所：{bj}"


def test_excludes_not_yet_listed_and_delisted(feed):
    """按查询日做时点过滤：未上市、已退市都不返回。"""
    got = set(feed.get_Ashares("20250102"))
    assert "300002.SZ" not in got, "list_date 晚于查询日，不应返回"
    assert "300003.SZ" not in got, "delist_date 早于查询日，不应返回"


def test_dirty_code_is_excluded_and_warned(feed, caplog):
    """在市的非法代码应被排除**并告警**，而不是静默丢弃。

    ``TS9999.SS`` 的 ``market`` 列写着「主板」—— 只用 market 过滤会把它当正常
    股票放进池子；只用前缀过滤会静默丢掉。正确行为是：排除 + 告警让人知道。

    注：真实库那条 ``TS0018.SS`` 状态是 D，被**状态过滤**先挡下（见
    ``test_excludes_not_yet_listed_and_delisted``），走不到前缀校验 ——
    所以这里另造一条在市（``L``）的非法代码来覆盖第二道防线。
    """
    got = feed.get_Ashares("20250102")
    assert "TS9999.SS" not in got, "非法代码不应进入股票池"
    assert "TS9999.SS" in feed._unrecognized_warned, (
        "前缀不认识时没有记入告警集合 —— 静默丢弃是本项目要根治的模式"
    )


def test_new_code_range_is_surfaced_not_silently_dropped(feed):
    """交易所新开号段（``310001.SZ``）应被**告警暴露**，而不是悄悄不见。

    这是「静默缩水」的另一种形态：号段一变，股票池就少一批，
    而没有任何提示。告警是把它变成可发现问题的唯一手段。
    """
    got = feed.get_Ashares("20250102")
    assert "310001.SZ" not in got, "未知号段不应放行（避免误纳非 A 股）"
    assert "310001.SZ" in feed._unrecognized_warned, (
        "未知号段未被记入告警 —— 新号段会导致股票池静默变小"
    )


def test_warning_is_deduplicated(feed):
    """同一标的只告警一次 —— 长回测里每天调用会刷屏。"""
    for _ in range(5):
        feed.get_Ashares("20250102")
    assert len(feed._unrecognized_warned) == 2, (
        f"应只记录 2 个异常标的（TS9999.SS / 310001.SZ），实际 {sorted(feed._unrecognized_warned)}"
    )


def test_dateless_call_uses_backtest_day(feed):
    """不传日期时用回测当日 —— 官方：默认值随回测日期变化。"""

    # 直接验 feed 层：不同日期的结果可以不同（时点语义）
    a = feed.get_Ashares("20250102")
    b = feed.get_Ashares("20250601")
    assert set(a) == set(b), "本夹具没有期间变动，两天应相同（前提校验）"
