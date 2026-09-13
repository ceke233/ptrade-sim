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
    # ---- 时点过滤（**判据是 list_date/delist_date，不是 list_status**）----
    ("300002.SZ", "创业板", "20991231", None, "L", False),  # 查询日尚未上市
    ("300003.SZ", "创业板", "20091030", "20200601", "D", False),  # 查询日前已退市
    # **幸存者偏差的核心用例**：2009-10-30 上市、2025-03-01 退市。
    # 查 2025-01-02 时它仍在市，必须返回；查 2025-06-01 时已退市，必须排除。
    # 若实现里带 list_status=='L'，它会被永久排除 —— 那正是幸存者偏差。
    ("300004.SZ", "创业板", "20091030", "20250301", "D", "PIT"),
    # ---- 脏数据 ----
    # 真实库里的一条（上港集箱(退)），状态 D —— 由**退市时点**挡下，走不到前缀校验
    ("TS0018.SS", "主板", "20000719", "20061020", "D", False),
    # 在市的非法代码 —— 只有它才会走到**前缀校验**，用于验证第二道防线 + 告警
    ("TS9999.SS", "主板", "20000719", None, "L", False),
    # ---- 交易所新号段：market 说创业板，前缀不在已知集合 ----
    ("310001.SZ", "创业板", "20250101", None, "L", False),
]


def expected(date: str) -> set[str]:
    """按**时点**语义算出该查询日应返回的集合。

    在市 ⟺ ``list_date <= 查询日`` 且（``delist_date`` 为空 或 ``> 查询日``）。
    ``list_status`` **不参与**判定 —— 它是「今天」的状态，
    用于历史查询即幸存者偏差。
    """
    out = set()
    for code, _mkt, ld, dl, _st, keep in CASES:
        if keep is False:
            continue
        if keep == "PIT":
            if ld <= date and (dl is None or dl > date):
                out.add(code)
            continue
        out.add(code)
    return out


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
    want = expected("20250102")
    assert got == want, f"缺失：{sorted(want - got)}\n多出：{sorted(got - want)}"


def test_delisted_before_query_is_excluded(feed):
    """查询日之前已退市的，不返回。"""
    assert "300003.SZ" not in feed.get_Ashares("20250102"), "2020-06-01 已退市"


def test_still_listed_at_query_is_included_despite_delisted_now(feed):
    """**幸存者偏差回归**：查询日仍在市、但**今天已退市**的股票必须返回。

    判据只能是 ``list_date`` / ``delist_date`` 与查询日的比较，
    **不能**用 ``list_status``（那是「今天」的状态）。带上它就会把
    「当年在市、后来退市」的股票永久排除 —— 回测只见幸存者，系统性高估收益。

    实测真实库被误排除的规模：2019 年 6.1%、2020 年 5.5%、2025 年 0.6%，
    且被排除的全是退市股。
    """
    got = feed.get_Ashares("20250102")
    assert "300004.SZ" in got, "2025-03-01 才退市，查 2025-01-02 时仍在市 —— 被排除即为幸存者偏差"
    # 同一个标的，换到退市后的日期就必须消失（说明是时点判定而非永久放行）
    assert "300004.SZ" not in feed.get_Ashares("20250601"), "2025-06-01 已退市"


def test_list_status_is_not_used_as_a_filter(feed):
    """防回退：``list_status`` 不得出现在 polars 过滤条件里。

    这条断言的价值在于**防回退** —— 只要有人把 ``list_status == 'L'`` 加回去，
    幸存者偏差就会静默复活。
    """
    import inspect

    from ptrade_sim import runtime

    src = inspect.getsource(runtime.DataFeed.get_Ashares)
    # 只看过滤表达式那几行（docstring 与注释里会解释为什么不看它）
    filter_part = src[src.index("self.basic.filter") : src.index(".select(")]
    assert "list_status" not in filter_part, (
        "get_Ashares 的过滤条件里出现了 list_status —— 会让历史查询产生幸存者偏差"
    )


def test_date_comparison_uses_same_format(feed):
    """防回退：日期比较必须在**同一格式**下进行。

    这曾是一个潜伏 bug：``cur`` 由 ``day_iso()`` 得到 ``YYYY-MM-DD``，
    而库内 ``list_date``/``delist_date`` 是 ``YYYYMMDD``。字符串比较在
    **同年份**时必然判错（第 5 个字符 ``'0'`` > ``'-'``）：

        '20250301' <= '2025-06-01'  ->  False（已退市却未排除）
        '20250101' >  '2025-06-01'  ->  True （已上市却被排除）

    于是「当年新上市的股票被错误排除、当年退市的被错误包含」。
    夹具里的 300004.SZ 正是同年份退市（2025-03-01）的用例。
    """
    assert "300004.SZ" not in feed.get_Ashares("20250601"), (
        "同年份退市的没被排除 —— 日期比较又在混用格式"
    )


def test_day_iso_tolerates_already_formatted_input():
    """``day_iso`` 必须能接受已带 ``-`` 的输入（原先会输出 ``'2025--0-1-02'``）。"""
    from ptrade_sim.conventions import day_iso, norm_day

    for raw in ("20250102", "2025-01-02"):
        assert day_iso(raw) == "2025-01-02", f"day_iso({raw!r}) 错误"
        assert norm_day(raw) == "20250102", f"norm_day({raw!r}) 错误"


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


def test_dirty_code_is_excluded_and_warned(feed):
    """非法代码应被排除**并告警**，而不是静默丢弃。

    ``TS0018.SS`` / ``TS9999.SS`` 的 ``market`` 列都写着「主板」——
    只用 market 过滤会把它们当正常股票放进池子；只用前缀过滤会静默丢掉。
    正确行为是：排除 + 告警让人知道。
    """
    got = feed.get_Ashares("20250102")
    for code in ("TS0018.SS", "TS9999.SS"):
        assert code not in got, f"{code} 是非法代码，不应进入股票池"
    assert {"TS0018.SS", "TS9999.SS"} <= feed._unrecognized_warned, (
        f"前缀不认识时没有记入告警，实际 {sorted(feed._unrecognized_warned)} —— "
        f"静默丢弃是本项目要根治的模式"
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
        feed.get_Ashares("20250601")
    assert len(feed._unrecognized_warned) == 3, (
        f"应只记录 3 个异常标的（TS0018.SS / TS9999.SS / 310001.SZ），实际 "
        f"{sorted(feed._unrecognized_warned)} —— 重复调用不应重复累计"
    )


def test_result_changes_with_query_date(feed):
    """不同查询日的结果应随时点变化 —— 这就是时点语义的意义。

    ``300004.SZ`` 于 2025-03-01 退市：查 2025-01-02 时在市（在内），
    查 2025-06-01 时已退市（不在）。若两天结果完全相同，
    说明时点过滤没生效 —— 那正是幸存者偏差的形态。
    """
    a = set(feed.get_Ashares("20250102"))
    b = set(feed.get_Ashares("20250601"))
    assert "300004.SZ" in a and "300004.SZ" not in b, "退市前后应不同"
    assert a - b == {"300004.SZ"}, f"两天差异应只有退市股，实际 {a - b}"
