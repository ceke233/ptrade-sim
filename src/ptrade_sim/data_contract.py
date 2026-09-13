"""DuckDB 物理表契约（引擎消费 + 用户插入的**唯一权威**）。

设计原则
--------
1. **物理表**：引擎只 ``SELECT`` 消费；用户用 ``CREATE TABLE`` / ``INSERT`` / 导入自行管理数据。
   实测：glob 视图无法把 ``WHERE date=?`` 下推到 hive 分区键，单日加载慢 26 倍，故必须用物理表。
2. **字段口径对齐 PTrade**（本文件的核心约定）：行情表列名一律使用 PTrade 的
   盘口/BarData 词汇 —— ``volume``（非 vol）、``money``（非 amount）、
   ``preclose``（非 pre_close）。这样 API 层「取数即返回」，无需翻译，
   从根本上消除 ``preclose`` 与 ``pre_close`` 这类命名错位导致的静默空列。
3. **派生字段不入表**：``price`` / ``is_open`` / ``high_limit`` / ``low_limit`` /
   ``unlimited`` 由 API 层按 PTrade 规则现算（涨跌停还要看板块与除权，
   规则实现只在契约里一处，避免建表期与查询期两份逻辑漂移）。
4. **扩展列显式标注**：PTrade 不暴露但引擎必需（``adj_factor`` / ``is_st`` 等），
   列名保留但标注为「扩展列」。
5. 内部 **code 一律 PTrade 尾缀**（``.SS``/``.SZ``）；物理层允许 ``.SH``/``.BJ`` 混用，
   组装层统一 ``.SH → .SS``。
6. **日期口径**：物理列（``date``）保持 ``YYYYMMDD``（8 位），PTrade API 边界
    按官方约定输出/接受 ``YYYY-MM-DD``；引擎内部按日键一律走 8 位
    （``DataFeed.trade_days`` / ``_day_str``），仅在写 ``daily_stats`` 的
    ``date`` 列与 API 返回值时转 ISO（``conventions.day_iso``）。

字段口径对照（PTrade ↔ 常见数据源）
-----------------------------------
======================  ==================  ==========================================
PTrade（本表采用）       常见数据源          说明
======================  ==================  ==========================================
``volume``              ``vol``            成交量，单位**股**（实测 amount/vol == vwap）
``money``               ``amount``         成交额，单位**元**
``preclose``            ``pre_close``      昨收价
``open/high/low/close``  同名               OHLC
======================  ==================  ==========================================

PTrade 与数据源单位已实测一致（股票单位＝股、金额单位＝元），**无需换算**。
"""

from __future__ import annotations

from typing import NamedTuple


class TableContract(NamedTuple):
    """单表契约。"""

    name: str
    columns: tuple[str, ...]  # 物理列（建表后应存在）
    key: tuple[str, ...]  # 业务主键
    partitioned: bool  # 是否按 date 逐日/逐年入库
    required: bool  # 缺失是否报错（False = 可选，缺失即降级）
    desc: str
    rename: dict[str, str] = {}  # 入库时源列 -> 物理列 的重命名映射
    #: 入库时源表不存在的列 -> SQL 表达式。
    #: ⚠️ 表达式引用的是**源列名**（rename 尚未生效），因为整条投影是在一次
    #: SELECT 里对源表求值的。
    derive: dict[str, str] = {}
    derived: tuple[str, ...] = ()  # PTrade 会返回、但由 API 层现算、不入表的字段
    extension: tuple[str, ...] = ()  # PTrade 不暴露的引擎扩展列
    unsupported: tuple[str, ...] = ()  # PTrade 有、但本地数据源无法提供的字段
    # ---- 入库来源（决定 ptrade-sim db build 怎么读源数据）----
    #: 留空（``""``）表示**按 ``partitioned`` 自动推导**：
    #: ``partitioned=True`` → ``hive``；``partitioned=False`` → ``parquet``。
    #: 只有需要覆盖默认时才显式指定：
    #: ``hive``   = <data_dir>/<name>/year=/month=/day=/data.parquet（逐日/逐年）
    #: ``parquet``= <data_dir>/<name>/data.parquet（单文件）
    #: ``file``   = <data_dir>/<source>（指定的单个 parquet 文件）
    #: ``csv``    = <data_dir>/<source>（指定的单个 CSV 文件）
    source_kind: str = ""
    #: ``source_kind`` 为 ``file``/``csv`` 时的相对路径
    source: str = ""

    def resolve_source_kind(self) -> str:
        """解析实际入库来源类型（留空则按 ``partitioned`` 推导）。"""
        if self.source_kind:
            return self.source_kind
        return "hive" if self.partitioned else "parquet"


# ============================================================
# 行情表（字段口径对齐 PTrade BarData / get_history）
# ============================================================

DAILY_STOCK = TableContract(
    name="ashare_1d_stock",
    columns=(
        # ---- 存储键 ----
        "code",
        "date",
        # ---- PTrade 日线字段（get_history(1d) / get_price(1d)）----
        "open",
        "high",
        "low",
        "close",
        "volume",
        "money",
        "preclose",
        # ---- 扩展列（PTrade 不暴露；引擎内部用）----
        "adj_factor",
        "vwap",
        "name",
        "is_st",
        "is_delisted",
        "change",
        "pct_chg",
    ),
    key=("code", "date"),
    partitioned=True,
    required=True,
    desc=(
        "日线。volume=股，money=元，preclose=昨收；adj_factor 为复权因子（扩展列），"
        "is_st/is_delisted 供涨跌停与状态过滤（扩展列）"
    ),
    rename={"vol": "volume", "amount": "money", "pre_close": "preclose"},
    derived=("price", "is_open", "high_limit", "low_limit", "unlimited"),
    extension=("adj_factor", "vwap", "name", "is_st", "is_delisted", "change", "pct_chg"),
)

MINUTE_STOCK = TableContract(
    name="ashare_1m_stock",
    columns=(
        # ---- 存储键 ----
        "code",
        "date",
        "trade_time",
        # ---- PTrade 分钟字段（minute BarData）----
        "open",
        "high",
        "low",
        "close",
        "volume",
        "money",
        # ---- 扩展列 ----
        "preclose",
        "change",
        "pct_chg",
    ),
    key=("code", "trade_time"),
    partitioned=True,
    required=True,
    desc=(
        "分钟线（241 根/交易日，trade_time='YYYY-MM-DD HH:MM:SS'）。"
        "volume=股，money=元。date 为存储键（DuckDB 按它裁剪单日）"
    ),
    rename={"vol": "volume", "amount": "money", "pre_close": "preclose"},
    derived=("price",),
    extension=("preclose", "change", "pct_chg"),
)

DAILY_INDEX = TableContract(
    name="ashare_1d_index",
    columns=(
        "code",
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "money",
        "preclose",
        "change",
        "pct_chg",
    ),
    key=("code", "date"),
    partitioned=False,
    required=True,
    desc="指数日线（set_benchmark 基准）。字段口径同个股日线",
    rename={"vol": "volume", "amount": "money", "pre_close": "preclose"},
    derived=("price", "is_open", "high_limit", "low_limit", "unlimited"),
    extension=("change", "pct_chg"),
)

MINUTE_INDEX = TableContract(
    name="ashare_1m_index",
    columns=(
        "code",
        "date",
        "trade_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "money",
    ),
    key=("code", "trade_time"),
    partitioned=True,
    required=False,
    desc=(
        "指数分钟线（基准分钟级对比，可选）。"
        "⚠️ 源表**无 date 列**，入库时从 trade_time 前 10 位派生（供按日裁剪）"
    ),
    rename={"vol": "volume", "amount": "money"},
    # date 为 8 位 YYYYMMDD（与其他表一致）：trade_time 'YYYY-MM-DD HH:MM:SS' 取前 10 位再去横线
    derive={"date": "replace(substr(CAST(trade_time AS VARCHAR), 1, 10), '-', '')"},
    derived=("price",),
)


# ============================================================
# 元数据/静态表（非 BarData，沿用语义命名）
# ============================================================

CALENDAR = TableContract(
    name="ashare_calendar",
    columns=("date",),
    key=("date",),
    partitioned=False,
    required=True,
    desc="交易日历（date=YYYYMMDD 字符串）",
)

STOCK_BASIC = TableContract(
    name="ashare_stock_basic",
    columns=(
        "code",
        "symbol",
        "name",
        "area",
        "industry",
        "fullname",
        "enname",
        "cnspell",
        "market",
        "exchange",
        "curr_type",
        "list_status",
        "list_date",
        "delist_date",
        "is_hs",
        "act_name",
        "act_ent_type",
    ),
    key=("code",),
    partitioned=False,
    required=True,
    desc="股票基础信息（list_date/delist_date 为 YYYYMMDD 字符串）",
)

INDEX_INFO = TableContract(
    name="ashare_index_info_basic",
    columns=(
        "ts_code",
        "name",
        "market",
        "publisher",
        "category",
        "base_date",
        "base_point",
        "list_date",
    ),
    key=("ts_code",),
    partitioned=False,
    required=False,
    desc="指数基础信息（仅元信息，不含成分）",
)

INDEX_WEIGHT = TableContract(
    name="ashare_index_weight",
    columns=(
        "index_code",
        "index_name",
        "code",
        "in_date",
        "out_date",
        "weight",
        "source",
        "snapshot_date",
    ),
    key=("index_code", "code", "in_date"),
    partitioned=False,
    required=False,
    desc=(
        "指数成分与权重拉链表（get_index_stocks 用）。"
        "区间**左闭右开** in_date <= d < out_date；"
        "out_date 空 = 至今仍在内；snapshot_date 非空 = 仅快照（无时点能力）。"
        "``weight`` 为成分权重（%，每期合计 ≈100）"
    ),
    extension=("weight",),
    # 无需显式声明 unsupported（省略即字段默认值 ()）：weight 由 PTrade 权重拉链表提供。
)
#: ``weight`` 仅 PTrade 权重拉链表（source='ptrade'）提供；
#: akshare / baostock 来源只有成分、无权重（该列为 NULL，属数据源限制而非缺陷）。

L2_AUCTION = TableContract(
    name="ashare_l2_auction",
    columns=("code", "date", "hq_px", "business_amount"),
    key=("code", "date"),
    partitioned=False,
    required=False,
    desc=(
        "L2 集合竞价（9:25 正式撮合的**纯竞价**成交价与量），供 _get_trend_data 精确取竞价；"
        "缺失时回退 09:30 分钟 bar 近似。源为单文件 l2_auction.parquet（date 为 YYYY-MM-DD）"
    ),
    source_kind="file",
    source="l2_auction.parquet",
    # 源 date 是 'YYYY-MM-DD'，统一为 8 位 YYYYMMDD（表达式引用**源列名**）
    derive={"date": "replace(substr(CAST(date AS VARCHAR), 1, 10), '-', '')"},
)

# 注：证券更名历史**已合并进日线**（``ashare_1d_stock.name``）。
# 实测源日线 name 列本身即「时点正确」的简称：对 2,939,341 行做全量比对，
# 与独立更名表推导结果**一致率 100.0000%**，故独立表冗余，已移除。
# 引擎改为从日线 name 派生「名称变化时点」，用于覆盖停牌等无日线行情的日期
# （见 ``data_source.DuckDBSource.name_timeline``，由 ``runtime.DataFeed`` 调用）。

DAILY_FEATURE = TableContract(
    name="ashare_1d_feature",
    columns=(
        "code",
        "date",
        # ---- PTrade valuation 字段（字段名对齐官方文档）----
        "total_value",  # A股总市值(元)   <- 源 total_mv
        "float_value",  # A股流通市值(元) <- 源 circ_mv
        "total_shares",  # 总股本          <- 源 total_share
        "a_floats",  # 可流通A股       <- 源 float_share
        "turnover_rate",  # 换手率
        "dividend_ratio",  # 滚动股息率      <- 源 dv_ratio
        "pe_ttm",
        "pb",
        "ps",
        "ps_ttm",
        # ---- 扩展列（本地数据源有、PTrade valuation 不暴露）----
        "pe",
        "dv_ttm",
        "volume_ratio",
        "turnover_rate_f",
        "free_share",
        "log_mv",
        "log_cmv",
        # ---- 源文件自带 ----
        "close",
    ),
    key=("code", "date"),
    partitioned=True,
    required=False,
    desc=(
        "估值/股本（get_fundamentals('valuation') 的数据源）。字段名对齐 PTrade 官方 valuation 表："
        "total_value=A股总市值(元)、float_value=A股流通市值(元)、total_shares=总股本、a_floats=可流通A股、"
        "dividend_ratio=滚动股息率。**API 层需把 turnover_rate/dividend_ratio 的 '%' 字符串转 float**"
        "（官方：数据源返回带 % 的字符串，需自行 /100）"
    ),
    rename={
        "total_mv": "total_value",
        "circ_mv": "float_value",
        "total_share": "total_shares",
        "float_share": "a_floats",
        "dv_ratio": "dividend_ratio",
    },
    extension=(
        "pe",
        "dv_ttm",
        "volume_ratio",
        "turnover_rate_f",
        "free_share",
        "log_mv",
        "log_cmv",
        "close",
    ),
    # 官方 valuation 有、但本地数据源（tushare 日频指标）无法提供的字段。
    # 声明出来供 API 层按「字段不可用」明确报错，而不是静默返 NaN。
    unsupported=(
        "naps",  # 每股净资产
        "pcf",  # 市现率
        "secu_abbr",  # 证券简称（可用 stock_basic.name 替代）
        "a_shares",  # A股股本
        "pe_dynamic",  # 动态市盈率
        "pe_static",  # 静态市盈率
        "b_floats",
        "b_shares",
        "h_shares",  # B/H 股股本
        "roe",  # 净资产收益率（属 profit_ability 表）
    ),
)

# 注：``ashare_1d_flag``（日频 ST/退市标记）**已删除**。
# 其列 ``name`` / ``is_st`` / ``is_delisted`` 与 ``ashare_1d_stock`` **100.0000% 一致**
# （实测重叠 7,989,129 行全部相同），引擎也从未引用它；
# 仅多出的 192,817 行「有状态但无行情」的停牌记录不承载额外信息
# （停牌日无日线行，本就无撮合、无涨跌停判定）。
# 故整表冗余，已从契约与库中移除。


# ============================================================
# 汇总
# ============================================================

# 建库顺序：小表在前（便于快速验证），大表（分钟）在后
ALL_TABLES: tuple[TableContract, ...] = (
    CALENDAR,
    STOCK_BASIC,
    INDEX_INFO,
    INDEX_WEIGHT,
    L2_AUCTION,
    DAILY_INDEX,
    DAILY_FEATURE,
    DAILY_STOCK,
    MINUTE_INDEX,
    MINUTE_STOCK,
)

REQUIRED_TABLES: tuple[str, ...] = tuple(t.name for t in ALL_TABLES if t.required)

# PTrade 会返回、但统一由 API 层现算的字段（不入任何表）
PTRADE_DERIVED_FIELDS: tuple[str, ...] = (
    "price",
    "is_open",
    "high_limit",
    "low_limit",
    "unlimited",
)


def contract_of(name: str) -> TableContract | None:
    for t in ALL_TABLES:
        if t.name == name:
            return t
    return None


def missing_required(existing: set[str]) -> list[str]:
    """给定已存在表名集合，返回缺失的必需表。"""
    return [n for n in REQUIRED_TABLES if n not in existing]


def table_ddl(name: str) -> str | None:
    """生成建表语句（供文档/缺表指引；建库工具实际用 CTAS 保类型）。"""
    t = contract_of(name)
    if t is None:
        return None
    cols = ",\n    ".join(f'"{c}" VARCHAR' for c in t.columns)
    return f'CREATE TABLE IF NOT EXISTS "{t.name}" (\n    {cols}\n);'


def field_mapping_note() -> str:
    """PTrade ← 数据源 字段映射说明。"""
    return (
        "PTrade 口径采用：volume(股) / money(元) / preclose\n"
        "数据源映射：vol→volume、amount→money、pre_close→preclose\n"
        "派生不入表：price、is_open、high_limit、low_limit、unlimited（API 层现算）"
    )


def describe() -> str:
    """人类可读的契约摘要（``ptrade-sim db verify --list-contract``）。"""
    lines = [field_mapping_note(), ""]
    for t in ALL_TABLES:
        flag = "必需" if t.required else "可选"
        part = "按日分区" if t.partitioned else "单表"
        lines.append(f"[{flag}][{part}] {t.name}")
        lines.append(f"    主键: {', '.join(t.key)}")
        lines.append(f"    说明: {t.desc}")
        lines.append(f"    列({len(t.columns)}): {', '.join(t.columns)}")
        if t.rename:
            mp = ", ".join(f"{k}→{v}" for k, v in t.rename.items())
            lines.append(f"    入库重命名: {mp}")
        if t.derived:
            lines.append(f"    派生不入表: {', '.join(t.derived)}")
        if t.extension:
            lines.append(f"    扩展列(PTrade 不暴露): {', '.join(t.extension)}")
        lines.append("")
    return "\n".join(lines)
