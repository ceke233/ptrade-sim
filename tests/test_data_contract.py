"""表契约测试。

契约是整个数据层的**唯一权威**：它同时驱动建库（ddl/投影）、引擎取数
与文档。契约一旦被无声改坏，后果是「跑完但没有成交」这类静默错误，
所以这里把关键不变量全部固定下来。
"""

from __future__ import annotations

import pytest

from ptrade_sim import data_contract as dc

pytestmark = pytest.mark.unit


# ============================================================
# 契约自洽性
# ============================================================


def test_all_tables_have_unique_names():
    names = [t.name for t in dc.ALL_TABLES]
    assert len(names) == len(set(names)), f"表名重复：{names}"


def test_required_tables_subset_of_all():
    assert set(dc.REQUIRED_TABLES) <= {t.name for t in dc.ALL_TABLES}


def test_every_table_has_key_columns():
    for t in dc.ALL_TABLES:
        assert t.key, f"{t.name} 未定义主键"
        assert set(t.key) <= set(t.columns), f"{t.name} 主键不在列内：{t.key}"


def test_derive_keys_must_be_contract_columns():
    """``derive`` 的键必须是要建出来的列，否则该列会被静默漏掉。"""
    for t in dc.ALL_TABLES:
        assert set(t.derive) <= set(t.columns), (
            f"{t.name} derive 键不在列内：{set(t.derive) - set(t.columns)}"
        )


def test_rename_targets_must_be_contract_columns():
    for t in dc.ALL_TABLES:
        bad = {v for v in t.rename.values() if v not in t.columns}
        assert not bad, f"{t.name} rename 目标不在列内：{bad}"


def test_partitioned_tables_have_date():
    for t in dc.ALL_TABLES:
        if t.partitioned:
            assert "date" in t.columns, f"{t.name} 声明分区但无 date 列"


def test_derived_and_columns_disjoint():
    """派生不入表的字段不应同时出现在物理列里（否则语义矛盾）。"""
    for t in dc.ALL_TABLES:
        overlap = set(t.derived) & set(t.columns)
        assert not overlap, f"{t.name} 字段既声明派生又声明入表：{overlap}"


# ============================================================
# 来源类型推导（曾因默认值写死 hive 导致非分区表建不出来）
# ============================================================


def test_source_kind_inferred_from_partitioned():
    for t in dc.ALL_TABLES:
        kind = t.resolve_source_kind()
        if t.source_kind:
            assert kind == t.source_kind
        elif t.partitioned:
            assert kind == "hive", f"{t.name} 分区表应推导为 hive"
        else:
            assert kind == "parquet", f"{t.name} 非分区表应推导为 parquet（回归防护）"


def test_every_table_has_resolvable_source():
    for t in dc.ALL_TABLES:
        assert t.resolve_source_kind() in ("hive", "parquet", "file", "csv")


def test_file_and_csv_sources_declare_path():
    for t in dc.ALL_TABLES:
        if t.resolve_source_kind() in ("file", "csv"):
            assert t.source, f"{t.name} 声明 file/csv 来源但未给 source 路径"


# ============================================================
# PTrade 字段口径（本项目最易错的地方）
# ============================================================


def test_stock_tables_use_ptrade_field_names():
    """行情表必须用 PTrade 口径：volume / money / preclose。

    回归防护：早先内部用 ``pre_close`` 而官方是 ``preclose``，
    导致 ``get_history(..., 'preclose')`` 静默返回空列。
    """
    for name in ("ashare_1d_stock", "ashare_1m_stock", "ashare_1m_index"):
        t = dc.contract_of(name)
        assert t is not None
        assert "volume" in t.columns, f"{name} 缺 volume（应为 PTrade 口径，不是 vol）"
        assert "money" in t.columns, f"{name} 缺 money（应为 PTrade 口径，不是 amount）"
        assert "vol" not in t.columns
        assert "amount" not in t.columns
        if name != "ashare_1m_index":
            assert "preclose" in t.columns, f"{name} 缺 preclose（不是 pre_close）"
            assert "pre_close" not in t.columns


def test_valuation_uses_official_field_names():
    """估值表字段名对齐官方 valuation：total_value / float_value / a_floats / dividend_ratio。"""
    t = dc.contract_of("ashare_1d_feature")
    for col in ("total_value", "float_value", "total_shares", "a_floats", "dividend_ratio"):
        assert col in t.columns, f"valuation 缺官方字段 {col}"
    # 源列名不应泄漏到契约里
    assert not ({"total_mv", "circ_mv", "dv_ratio"} & set(t.columns))


def test_valuation_declares_unsupported_fields():
    """官方有、本地数据源无的字段必须显式声明，供 API 层明确报错而不是返 NaN。"""
    t = dc.contract_of("ashare_1d_feature")
    assert "roe" in t.unsupported
    assert set(t.unsupported) & set(t.columns) == set()


def test_derived_price_fields_not_stored_anywhere():
    """price/is_open/high_limit/low_limit/unlimited 由 API 层现算，不入任何表。"""
    for t in dc.ALL_TABLES:
        overlap = set(dc.PTRADE_DERIVED_FIELDS) & set(t.columns)
        assert not overlap, f"{t.name} 不应物理存储派生字段：{overlap}"


# ============================================================
# 具体表的结构锁定
# ============================================================


def test_index_weight_has_weight_column():
    """表名叫 weight 就必须有 weight 列（改名时补回，勿再丢）。"""
    t = dc.contract_of("ashare_index_weight")
    assert t is not None
    assert "weight" in t.columns
    assert "weight" in t.extension


def test_index_weight_is_left_closed_right_open():
    t = dc.contract_of("ashare_index_weight")
    assert "in_date" in t.columns and "out_date" in t.columns
    assert "out_date 空" in t.desc or "左闭右开" in t.desc


def test_minute_index_derives_date():
    """1m_index 源表没有 date 列，必须由 trade_time 派生，否则建库报 Binder Error。"""
    t = dc.contract_of("ashare_1m_index")
    assert "date" in t.derive
    assert "trade_time" in t.derive["date"]


def test_removed_tables_stay_removed():
    """已删的冗余表不应复活（ashare_1d_flag 列与日线 100% 重复）。"""
    names = {t.name for t in dc.ALL_TABLES}
    assert "ashare_1d_flag" not in names
    assert "ashare_name_change" not in names  # 已合并进日线 name
    assert "ashare_index_member" not in names  # 已改名 ashare_index_weight


def test_contract_of_and_missing():
    assert dc.contract_of("ashare_1d_stock") is not None
    assert dc.contract_of("no_such_table") is None


def test_missing_required_helper():
    have = {"ashare_calendar"}
    miss = dc.missing_required(have)
    assert "ashare_calendar" not in miss
    assert set(miss) == set(dc.REQUIRED_TABLES) - have


def test_table_ddl_contains_all_columns():
    ddl = dc.table_ddl("ashare_index_weight")
    assert ddl is not None
    for col in dc.contract_of("ashare_index_weight").columns:
        assert col in ddl


def test_describe_mentions_every_table():
    text = dc.describe()
    for t in dc.ALL_TABLES:
        assert t.name in text
