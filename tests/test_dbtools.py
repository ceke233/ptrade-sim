"""建库工具测试（来源解析 / 投影生成 / 契约校验 / 尾缀统一）。

这些测试直接针对「建库逻辑」——它决定了库能不能建出来。
此前的真实缺陷（非分区表被当 hive 表导致静默建不出表）就发生在这里。
"""

from __future__ import annotations

import pytest

from ptrade_sim import data_contract as dc
from ptrade_sim import dbtools

pytestmark = pytest.mark.unit


# ============================================================
# 投影生成
# ============================================================


def test_select_expr_renames_source_columns():
    """vol → volume、amount → money、pre_close → preclose。"""
    expr = dbtools._select_expr(dc.DAILY_STOCK)
    assert '"vol" AS "volume"' in expr
    assert '"amount" AS "money"' in expr
    assert '"pre_close" AS "preclose"' in expr


def test_select_expr_unifies_code_suffix():
    """所有含 code 的表都要在入库时把 .SH 统一为 .SS。"""
    for t in dc.ALL_TABLES:
        if "code" in t.columns:
            expr = dbtools._select_expr(t)
            assert "replace(" in expr and "'.SH'" in expr and "'.SS'" in expr, (
                f"{t.name} 未统一代码尾缀"
            )


def test_select_expr_includes_derived_columns():
    """1m_index 的 date 必须由 trade_time 派生（源无该列）。"""
    expr = dbtools._select_expr(dc.MINUTE_INDEX)
    assert "trade_time" in expr
    assert 'AS "date"' in expr


def test_select_expr_covers_all_contract_columns():
    for t in dc.ALL_TABLES:
        expr = dbtools._select_expr(t)
        for c in t.columns:
            assert f'AS "{c}"' in expr, f"{t.name} 投影缺列 {c}"


def test_select_expr_applies_derive_before_rename_lookup():
    """derive 的表达式引用**源列名**（rename 尚未生效），不能反查契约名。"""
    for t in dc.ALL_TABLES:
        for col, sqlexpr in t.derive.items():
            assert f'AS "{col}"' in dbtools._select_expr(t)
            assert sqlexpr  # 非空


# ============================================================
# 来源解析
# ============================================================


def test_source_sql_none_when_missing(tmp_path):
    assert dbtools._source_sql(dc.DAILY_STOCK, tmp_path) is None


def test_source_sql_hive_mode(tmp_path):
    (tmp_path / dc.DAILY_STOCK.name).mkdir()
    got = dbtools._source_sql(dc.DAILY_STOCK, tmp_path)
    assert got is not None
    assert got[1] == "hive"


def test_source_sql_parquet_mode_for_non_partitioned(tmp_path):
    """回归防护：非分区表必须走 single 分支。

    早期 source_kind 默认写死 'hive'，导致 calendar / stock_basic /
    index_weight 等非分区表被当成分区表，建库时**静默什么都不建**。
    """
    d = tmp_path / dc.INDEX_WEIGHT.name
    d.mkdir()
    (d / "data.parquet").write_bytes(b"")
    got = dbtools._source_sql(dc.INDEX_WEIGHT, tmp_path)
    assert got is not None
    assert got[1] == "single", "非分区表必须走 single（一次性 CTAS）"


def test_source_sql_file_kind(tmp_path):
    (tmp_path / dc.L2_AUCTION.source).write_bytes(b"")
    got = dbtools._source_sql(dc.L2_AUCTION, tmp_path)
    assert got is not None and got[1] == "single"
    assert dc.L2_AUCTION.source in got[0]


def test_source_sql_csv_kind(tmp_path):
    t = dc.TableContract(
        name="t",
        columns=("a",),
        key=("a",),
        partitioned=False,
        required=False,
        desc="",
        source_kind="csv",
        source="x.csv",
    )
    (tmp_path / "x.csv").write_text("a\n1\n", encoding="utf-8")
    got = dbtools._source_sql(t, tmp_path)
    assert got is not None and "read_csv_auto" in got[0]


def test_all_contract_tables_resolve_with_full_source(tmp_path):
    """给定完整源目录，每张表都应能解析出来源。"""
    for t in dc.ALL_TABLES:
        if t.resolve_source_kind() == "hive":
            (tmp_path / t.name).mkdir(parents=True, exist_ok=True)
        elif t.resolve_source_kind() == "parquet":
            d = tmp_path / t.name
            d.mkdir(parents=True, exist_ok=True)
            (d / "data.parquet").write_bytes(b"")
        else:
            (tmp_path / t.source).write_bytes(b"")
        assert dbtools._source_sql(t, tmp_path) is not None, f"{t.name} 无法解析来源"


# ============================================================
# 工具函数
# ============================================================


@pytest.mark.parametrize(
    "n,expect",
    [(0, "0.0B"), (512, "512.0B"), (2048, "2.0KB"), (5 * 1024**2, "5.0MB")],
)
def test_human(n, expect):
    assert dbtools._human(n) == expect


def test_fmt_secs():
    assert dbtools._fmt_secs(5) == "5.0s"
    assert dbtools._fmt_secs(65).startswith("1m")
    assert dbtools._fmt_secs(3700).startswith("1h")


# ============================================================
# 契约校验（对合成库）
# ============================================================


def test_verify_subset_mode_passes_for_built_table(tiny_db):
    """子集模式：只校验请求的表，不因缺其他必需表而失败。

    回归防护：早期 `db build --tables X` 成功却返回 exit 1。
    """
    import duckdb

    con = duckdb.connect(str(tiny_db), read_only=True)
    rc = dbtools.verify(con, years=None, only={"ashare_index_weight"})
    con.close()
    assert rc == 0


def test_verify_subset_mode_fails_for_missing_table(tiny_db):
    import duckdb

    con = duckdb.connect(str(tiny_db), read_only=True)
    rc = dbtools.verify(con, years=None, only={"ashare_index_weight", "no_such_table"})
    con.close()
    assert rc == 1


def test_verify_full_mode_on_synthetic_db(tiny_db):
    """合成库包含全部必需表 → 整体契约校验应通过。"""
    import duckdb

    con = duckdb.connect(str(tiny_db), read_only=True)
    rc = dbtools.verify(con, years=[2025])
    con.close()
    assert rc == 0


def test_rows_returns_none_for_missing_table(tiny_db):
    import duckdb

    con = duckdb.connect(str(tiny_db), read_only=True)
    assert dbtools._rows(con, "no_such_table") is None
    con.close()


# ============================================================
# 尾缀统一（既有库修复）
# ============================================================


@pytest.fixture
def legacy_db(tmp_path):
    """造一个含 .SH 尾缀的旧库。"""
    duckdb = pytest.importorskip("duckdb")
    db = tmp_path / "legacy.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE ashare_1d_stock (code VARCHAR, close DOUBLE)")
    con.execute("INSERT INTO ashare_1d_stock VALUES ('600000.SH', 1.0), ('000001.SZ', 2.0)")
    con.execute("CREATE TABLE ashare_index_weight (code VARCHAR, weight DOUBLE)")
    con.execute("INSERT INTO ashare_index_weight VALUES ('600000.SS', 1.0)")
    con.execute("CREATE TABLE ashare_1d_flag (code VARCHAR)")
    con.execute("INSERT INTO ashare_1d_flag VALUES ('600000.SH')")
    con.close()
    return db


def test_normalize_fixes_suffix(legacy_db):
    rc = dbtools.normalize_suffix(legacy_db)
    assert rc == 0

    import duckdb

    con = duckdb.connect(str(legacy_db), read_only=True)
    n = con.execute("SELECT count(*) FROM ashare_1d_stock WHERE code LIKE '%.SH'").fetchone()[0]
    assert n == 0
    codes = {r[0] for r in con.execute("SELECT code FROM ashare_1d_stock").fetchall()}
    assert codes == {"600000.SS", "000001.SZ"}
    con.close()


def test_normalize_dry_run_does_not_mutate(legacy_db):
    """回归防护：早期 dry-run 会**真的删表**（删除逻辑漏了 dry_run 守卫）。"""
    dbtools.normalize_suffix(legacy_db, dry_run=True, drop_tables=["ashare_1d_flag"])

    import duckdb

    con = duckdb.connect(str(legacy_db), read_only=True)
    tabs = {
        r[0]
        for r in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
        ).fetchall()
    }
    assert "ashare_1d_flag" in tabs, "dry-run 不应删表"
    n = con.execute("SELECT count(*) FROM ashare_1d_stock WHERE code LIKE '%.SH'").fetchone()[0]
    assert n == 1, "dry-run 不应改数据"
    con.close()


def test_normalize_drops_requested_table(legacy_db):
    dbtools.normalize_suffix(legacy_db, drop_tables=["ashare_1d_flag"])

    import duckdb

    con = duckdb.connect(str(legacy_db), read_only=True)
    tabs = {
        r[0]
        for r in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
        ).fetchall()
    }
    assert "ashare_1d_flag" not in tabs
    con.close()


def test_normalize_drop_missing_table_is_ok(legacy_db):
    assert dbtools.normalize_suffix(legacy_db, drop_tables=["nope"]) == 0


def test_normalize_missing_db_returns_error(tmp_path):
    assert dbtools.normalize_suffix(tmp_path / "nope.duckdb") == 1


def test_normalize_is_idempotent(legacy_db):
    assert dbtools.normalize_suffix(legacy_db) == 0
    assert dbtools.normalize_suffix(legacy_db) == 0


# ============================================================
# build 入口的错误处理
# ============================================================


def test_build_rejects_missing_data_dir(tmp_path):
    assert dbtools.build(tmp_path / "nope", tmp_path / "o.duckdb") == 1


def test_build_rejects_unknown_table(tmp_path):
    (tmp_path / "data").mkdir()
    rc = dbtools.build(tmp_path / "data", tmp_path / "o.duckdb", tables=["no_such_table"])
    assert rc == 1


def test_build_single_table_returns_success(tiny_db, tmp_path):
    """只建一张可选表应返回 0（子集模式校验）。"""
    src = tmp_path / "src"
    d = src / dc.INDEX_WEIGHT.name
    d.mkdir(parents=True)
    import duckdb

    con = duckdb.connect(str(tiny_db), read_only=True)
    con.execute(
        f"COPY (SELECT * FROM ashare_index_weight) TO '{(d / 'data.parquet').as_posix()}' "
        f"(FORMAT PARQUET)"
    )
    con.close()

    out = tmp_path / "out.duckdb"
    assert dbtools.build(src, out, tables=[dc.INDEX_WEIGHT.name]) == 0

    con = duckdb.connect(str(out), read_only=True)
    assert con.execute("SELECT count(*) FROM ashare_index_weight").fetchone()[0] == 3
    con.close()
