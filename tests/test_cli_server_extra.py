"""``cli.py`` / ``server.py`` 的补充分支测试（已有 test_cli.py / test_server.py 之外）。

两块关注点不同，分开看：

**cli.py** —— 防的是「机器可读输出被污染」与「可预期异常把子命令打崩」：

* ``--json`` 时 stdout 上**只能有一份纯 JSON**。子调用（config.load / dbtools.verify）
  经 loguru 往 stdout 打 INFO 行，只要静音上下文漏掉一处，脚本消费方就会拿到
  「JSON + 日志」的混合文本 —— 所以这里的断言是**直接 json.loads 整段 stdout**。
* ``db verify`` 的年份区间写反（--start-year 2025 --end-year 2019）必须自动交换；
  否则年份列表为空，日期窗口 ``min(years)`` 直接抛异常。
* ``--dates`` 显式列表要去空白、丢空项（" a , ,b " -> ["a","b"]）。
* ``_db_inventory`` 里**单张表**的行数/日期查询失败只能记 error 继续盘点，
  不能让一处坏数据毁掉整个盘点结果。
* ``dashboard`` 子命令的 argv 组装（相对 root 必须转绝对）与缺可选依赖时的退出码 6。
* ``queue`` 读配置失败要退化为默认值 —— 用默认并发上限跑，总好过命令直接失败。

**server.py** —— 防的是「看板在残缺 run / 超大日志 / 端口被占」下不可用或误报：

* 不存在的 run 必须 404（既不能 500，也不能伪造空结果让前端以为跑完了）。
* run 存在但产物文件还没落盘（回测刚起步，很常见）要返回空结构而不是 500。
* trades.csv 读到一半失败（并发原子替换）要 200 + error 字段，不能 500。
* ``/log`` 的 offset 向前翻页必须**按 offset 扩读窗**，否则永远翻不到更早的日志；
  offset 越过文件头要返回空列表，而不是把读窗开头几行当「更早的日志」重复下发。
* 静态托管：命中真实文件走 FileResponse，未知路径回退 index.html（SPA 路由），
  且 ``../`` 不能读到 dist 之外。
* ``ensure_running``：已存在的看板要复用（root 不一致则放弃），需要拉起时
  Windows/POSIX 分别传对脱离终端的参数，拉起失败或超时必须返回 None 而不阻断回测。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import types
import urllib.error
from pathlib import Path

import duckdb
import pytest

import ptrade_sim
from ptrade_sim import cli, dbtools

try:  # 看板依赖（fastapi/httpx）缺失时只跳过 server 相关用例，cli 用例照跑
    from ptrade_sim import server
except ImportError:  # pragma: no cover - 依赖齐全的环境走不到
    server = None  # type: ignore[assignment]

requires_server = pytest.mark.skipif(server is None, reason="看板测试需要 fastapi")
pytestmark = pytest.mark.unit


# ============================================================
# cli.py：--json 输出纯净性
# ============================================================


def test_quiet_stdout_swallows_print_and_loguru(capsys):
    """``_quiet_stdout`` 必须同时吃掉 print 与 loguru 的控制台 sink。

    loguru 的 sink 是 ``lambda m: print(m, end="")``，其 ``sys.stdout`` 在调用时才解析——
    一旦有人把它改成绑定 ``sys.__stdout__``，本测试会立刻发现（那正是 --json 混入日志的成因）。
    """
    from loguru import logger

    cli._setup_console_logging()
    with cli._quiet_stdout() as buf:
        print("PRINTED_IN_BLOCK")
        logger.info("LOGGED_IN_BLOCK")

    assert "PRINTED_IN_BLOCK" in buf.getvalue(), "块内的 print 应被收进缓冲区"
    assert "LOGGED_IN_BLOCK" in buf.getvalue(), "块内的 loguru 输出也应被收进缓冲区"
    leaked = capsys.readouterr().out
    assert leaked == "", f"静音块内不应有任何内容落到 stdout，实际：{leaked!r}"


def test_env_json_stdout_is_pure_json(tmp_path, monkeypatch, capsys):
    """``env --json`` 的 stdout 必须是**纯 JSON**（混一行日志就解析失败）。

    ``env`` 子命令没有 ``--config``，配置来源就是分层查找：这里把 cwd 切到 tmp_path
    并放一份 ptrade_config.json（机器级），再用 PT_SIM_FREQUENCY 触发 config.load 的
    「环境变量覆盖」INFO 日志 —— 那条日志正是最容易被漏掉、混进 JSON 的输出。

    同时覆盖「db_path 指向不存在的库」：应记 db_exists=False 正常退出，而不是崩。
    """
    monkeypatch.chdir(tmp_path)
    missing = tmp_path / "no_such.duckdb"
    (tmp_path / "ptrade_config.json").write_text(
        json.dumps({"db_path": str(missing), "frequency": "minute"}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setenv("PT_SIM_FREQUENCY", "daily")

    rc = cli.main(["env", "--json"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "环境变量覆盖" not in out, "config.load 的 INFO 日志混进了 stdout，--json 约定被破坏"
    data = json.loads(out)  # 只要 stdout 上多一个字符（日志行）就会在这里抛异常

    assert data["db_path"] == str(missing)
    assert data["db_exists"] is False
    assert data["coverage"] == {} and data["tables"] is None
    assert data["coverage_error"] is None, "库不存在属于预期情况，不是读取错误"
    # 环境变量优先级在机器级配置之上
    assert data["config"]["frequency"] == "daily"
    # 资源探查落在库文件所在盘（与文本模式口径一致）
    assert data["resources"]["probe_target"] == str(missing.parent)
    # 断言**结构契约**而不是 cpu_count >= 1 —— 后者由
    # `os.cpu_count() or 1` 保证恒真，等于没断言。
    res = data["resources"]
    assert {"cpu_count", "mem_total", "mem_available", "mem_used_pct", "disk_free"} <= set(res), (
        f"resources 段缺字段：{sorted(res)}"
    )
    assert isinstance(res["cpu_count"], int) and isinstance(res["mem_total"], int)
    assert data["cache"]["describe"]


def test_dump_json_keeps_non_ascii_and_is_single_line_object(capsys):
    """``_dump_json`` 是 stdout 的唯一出口：中文不转义、整段可被 json.loads 直接消费。"""
    cli._dump_json({"名称": "示例轮动", "n": 1})
    out = capsys.readouterr().out
    assert "示例轮动" in out, "ensure_ascii=False 才能让日志/终端里直接读懂中文"
    assert json.loads(out) == {"名称": "示例轮动", "n": 1}


def test_db_verify_list_contract_json_is_pure_json(capsys):
    """``--list-contract --json`` 输出结构化契约（纯 JSON），而不是人读的文本表格。

    看板/脚本据此渲染表结构；走错分支拿到 describe() 的文本就整段解析失败。
    """
    rc = cli.main(["db", "verify", "--list-contract", "--json"])
    out = capsys.readouterr().out

    assert rc == 0
    data = json.loads(out)
    assert data["field_mapping"], "字段映射说明（.SH→.SS 之类）必须带上"
    t = next(x for x in data["tables"] if x["name"] == "ashare_1d_stock")
    assert set(t) == {
        "name",
        "required",
        "partitioned",
        "key",
        "columns",
        "description",
        "derived",
        "extension",
        "unsupported",
    }, "契约 JSON 的字段是给机器消费的，缺一个都会让下游 KeyError"
    assert t["required"] is True and t["partitioned"] is True
    assert t["key"] == ["code", "date"]
    assert t["columns"][:2] == ["code", "date"]
    assert t["description"]


# ============================================================
# cli.py：db verify 的年份/日期区间
# ============================================================


def _dummy_db(tmp_path: Path) -> Path:
    """造一个**合法但空**的 duckdb 文件（只有一张与契约无关的表）。"""
    db = tmp_path / "dummy.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE placeholder (x INTEGER)")
    con.close()
    return db


def test_verify_years_swaps_reversed_range(tmp_path, monkeypatch, capsys):
    """``--start-year`` 比 ``--end-year`` 大时必须自动交换。

    不交换的话年份列表为空（range(2025, 2020)），日期窗口 ``min(years)`` 直接抛异常 ——
    用户只是把区间写反了，不该拿到 traceback。
    """
    db = _dummy_db(tmp_path)
    seen: dict = {}
    monkeypatch.setattr(dbtools, "verify", lambda con, years: (seen.update(years=years), 0)[1])
    monkeypatch.setattr(
        dbtools,
        "verify_equivalence",
        lambda db_path, data_dir, dates: (seen.update(dates=dates), 0)[1],
    )

    rc = cli.main(
        [
            "db",
            "verify",
            "--db",
            str(db),
            "--data-dir",
            str(tmp_path / "nodata"),
            "--start-year",
            "2025",
            "--end-year",
            "2019",
            "--json",
        ]
    )
    data = json.loads(capsys.readouterr().out)

    assert rc == 0
    expect_years = [2019, 2020, 2021, 2022, 2023, 2024, 2025]
    assert seen["years"] == expect_years, "契约校验必须收到交换后的升序年份"
    assert data["years"] == expect_years
    assert data["date_range"]["window"] == {"start": "20190101", "end": "20251231"}
    # 未给 --dates 时按年抽样，每年一个已知交易日
    assert seen["dates"] == [cli._VERIFY_SAMPLE_DATES[y] for y in expect_years]
    assert data["equivalence"] == {"skipped": False, "dates": seen["dates"], "returncode": 0}


def test_verify_dates_flag_strips_blanks(tmp_path, monkeypatch, capsys):
    """``--dates " 20250102 ,,20250203 "`` 应得到干净的列表（空白/空项不能进 SQL 或 URL）。"""
    db = _dummy_db(tmp_path)
    seen: dict = {}
    monkeypatch.setattr(dbtools, "verify", lambda con, years: 0)
    monkeypatch.setattr(
        dbtools,
        "verify_equivalence",
        lambda db_path, data_dir, dates: (seen.update(dates=list(dates)), 0)[1],
    )

    rc = cli.main(
        [
            "db",
            "verify",
            "--db",
            str(db),
            "--data-dir",
            str(tmp_path / "nodata"),
            "--dates",
            " 20250102 ,,20250203 , ",
            "--json",
        ]
    )
    data = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert seen["dates"] == ["20250102", "20250203"]
    assert data["equivalence"]["dates"] == ["20250102", "20250203"]


def test_db_verify_json_reports_contract_issues(tmp_path, capsys):
    """必需表缺失时：退出码 1、issues 里带 ✗ 明细，但 **stdout 仍然只有纯 JSON**。

    这里同时钉住两件事：✗ 明细必须进 JSON（否则脚本无从知道哪里坏了），
    又必须留在静音缓冲区里（否则 json.loads 失败）。
    """
    db = _dummy_db(tmp_path)
    rc = cli.main(
        [
            "db",
            "verify",
            "--db",
            str(db),
            "--data-dir",
            str(tmp_path / "nodata"),
            "--start-year",
            "2025",
            "--end-year",
            "2025",
            "--contract-only",
            "--json",
        ]
    )
    out = capsys.readouterr().out
    data = json.loads(out)  # 只要有一行契约明细泄漏到 stdout，这里就会抛 JSONDecodeError

    assert rc == 1
    assert data["contract"]["ok"] is False
    assert data["contract"]["returncode"] == 1
    issues = data["contract"]["issues"]
    assert issues, "契约问题必须以 issues 形式交给脚本消费"
    assert all("✗" in ln for ln in issues)
    assert out.count("✗") == sum(ln.count("✗") for ln in issues), (
        "stdout 上出现了 issues 之外的 ✗ 行 —— 契约明细泄漏出了静音缓冲区"
    )
    # contract-only 时不做等价性校验，但仍要如实报告「跳过」
    assert data["equivalence"] == {"skipped": True, "dates": [], "returncode": None}
    by = {t["name"]: t for t in data["tables"]}
    assert by["ashare_1d_stock"] == {
        "name": "ashare_1d_stock",
        "required": True,
        "present": False,
        "rows": None,
        "columns_missing": [],
        "columns_extra": [],
        "date_range": None,
    }
    assert data["date_range"]["by_table"] == {}, "没有任何表在库时不该编造日期范围"


@pytest.mark.integration
def test_db_verify_json_inventory_on_complete_db(tiny_db, tmp_path, capsys):
    """契约齐全时：returncode 0、无 issues，并如实盘点每张表的行数与日期范围。"""
    rc = cli.main(
        [
            "db",
            "verify",
            "--db",
            str(tiny_db),
            "--data-dir",
            str(tmp_path / "nodata"),
            "--start-year",
            "2025",
            "--end-year",
            "2025",
            "--contract-only",
            "--json",
        ]
    )
    data = json.loads(capsys.readouterr().out)

    assert rc == 0, f"合成库应通过契约校验，issues={data['contract']['issues']}"
    assert data["contract"] == {"ok": True, "returncode": 0, "issues": []}
    by = {t["name"]: t for t in data["tables"]}
    d1 = by["ashare_1d_stock"]
    assert d1["present"] is True
    assert d1["rows"] == 15, "3 只股票 × 5 个交易日"
    assert d1["columns_missing"] == [] and d1["columns_extra"] == []
    assert d1["date_range"] == ["20250102", "20250108"]
    assert by["ashare_1m_stock"]["rows"] == 3 * 5 * 241
    assert by["ashare_1m_index"]["present"] is False and by["ashare_1m_index"]["rows"] is None
    assert data["date_range"]["window"] == {"start": "20250101", "end": "20251231"}
    assert data["date_range"]["by_table"]["ashare_1d_stock"] == ["20250102", "20250108"]


# ============================================================
# cli.py：_db_inventory 的容错
# ============================================================


class _StubResult:
    def __init__(self, all_rows=None, one=None):
        self._all = all_rows
        self._one = one

    def fetchall(self):
        return self._all

    def fetchone(self):
        return self._one


def test_db_inventory_records_error_instead_of_raising():
    """单表行数/日期查询失败时记 error 继续盘点，且列差异仍然要算出来。

    真实场景：某张表被别处的写入锁住或列类型坏了，不该让整个 ``db verify --json`` 崩掉 ——
    调用方要的是「其余表什么情况」+「这张表为什么没数据」。
    """
    from ptrade_sim import data_contract as dc

    t = dc.contract_of("ashare_1d_stock")
    assert t is not None
    # 故意缺一列（pct_chg）并多一列（legacy_b）
    cols = [c for c in t.columns if c != "pct_chg"] + ["legacy_b"]

    class _Con:
        def sql(self, q):
            if "information_schema.tables" in q:
                return _StubResult(all_rows=[("ashare_1d_stock",)])
            if "information_schema.columns" in q:
                return _StubResult(all_rows=[(c,) for c in cols])
            if "count(*)" in q:
                raise RuntimeError("count 查询失败")
            raise RuntimeError("date 查询失败")

    inv = {i["name"]: i for i in cli._db_inventory(_Con(), [2025])}
    item = inv["ashare_1d_stock"]

    assert item["present"] is True
    assert item["rows"] is None, "行数查询失败应记为 None，而不是让整段盘点抛异常"
    assert item["columns_missing"] == ["pct_chg"], "缺列必须报出来（契约漂移的直接证据）"
    assert item["columns_extra"] == ["legacy_b"]
    assert item["date_range"] is None
    assert item["error"] == "date 查询失败", "失败原因要落到 error 字段，便于定位"
    # 不在库里的表：present=False 且不该有 error（那不是错误，是没建）
    assert inv["ashare_1m_index"]["present"] is False
    assert "error" not in inv["ashare_1m_index"]


@pytest.mark.integration
def test_db_inventory_date_range_respects_year_window(tiny_db):
    """日期范围必须限定在 ``years`` 窗口内：窗口外没有数据时是 None，而不是库里全局 min/max。"""
    con = duckdb.connect(str(tiny_db), read_only=True)
    try:
        in_window = {i["name"]: i for i in cli._db_inventory(con, [2025])}
        out_window = {i["name"]: i for i in cli._db_inventory(con, [2019])}
    finally:
        con.close()

    assert in_window["ashare_1d_stock"]["date_range"] == ["20250102", "20250108"]
    assert out_window["ashare_1d_stock"]["present"] is True
    assert out_window["ashare_1d_stock"]["rows"] == 15, "行数是全表计数，不受窗口影响"
    assert out_window["ashare_1d_stock"]["date_range"] is None, "2019 年窗口内没有数据"


# ============================================================
# cli.py：dashboard / queue 的异常路径
# ============================================================


def test_run_dashboard_resolves_relative_root_and_default_port(tmp_path, monkeypatch):
    """``dashboard`` 交给 server.main 的 argv：相对 root 必须转绝对，端口缺省用默认值。

    相对 root 若原样透传，子进程/换目录后就会指向别处（结果目录找不到 → 空看板）。
    """
    if server is None:  # pragma: no cover - 无看板依赖时无意义
        pytest.skip("看板测试需要 fastapi")
    monkeypatch.chdir(tmp_path)
    captured: dict = {}
    monkeypatch.setattr(server, "main", lambda argv: (captured.update(argv=argv), 0)[1])

    assert cli.run_dashboard(cli.parse_args(["dashboard", "--root", "rel_results"])) == 0
    assert captured["argv"] == [
        "--root",
        str(tmp_path / "rel_results"),
        "--host",
        "127.0.0.1",
        "--port",
        str(cli.DEFAULT_PORT),
    ], "相对 root 应转成绝对路径，端口缺省应回落到 DEFAULT_PORT"


def test_run_dashboard_forwards_host_and_port(tmp_path, monkeypatch):
    """显式 --host/--port 必须原样透传（监听地址写错会暴露到公网或连不上）。"""
    if server is None:  # pragma: no cover
        pytest.skip("看板测试需要 fastapi")
    monkeypatch.chdir(tmp_path)
    captured: dict = {}
    monkeypatch.setattr(server, "main", lambda argv: (captured.update(argv=argv), 0)[1])

    rc = cli.run_dashboard(
        cli.parse_args(
            ["dashboard", "--root", str(tmp_path / "r"), "--host", "0.0.0.0", "--port", "9001"]
        )
    )
    assert rc == 0
    assert captured["argv"] == [
        "--root",
        str(tmp_path / "r"),
        "--host",
        "0.0.0.0",
        "--port",
        "9001",
    ]


def test_run_dashboard_missing_dependency_returns_exit_code_6(tmp_path, monkeypatch, capsys):
    """缺可选依赖时返回 6（缺依赖）并给出安装指引，而不是抛 ImportError 冒泡成 1。

    6 是 exceptions.EXIT_CODES 里的「缺可选依赖」，脚本据此提示 `pip install ...[dashboard]`。
    """
    monkeypatch.delitem(sys.modules, "ptrade_sim.server", raising=False)
    monkeypatch.delattr(ptrade_sim, "server", raising=False)
    monkeypatch.setitem(sys.modules, "ptrade_sim.server", None)  # import 时抛 ImportError

    rc = cli.run_dashboard(cli.parse_args(["dashboard", "--root", str(tmp_path)]))

    assert rc == 6
    out = capsys.readouterr().out
    assert "看板依赖缺失" in out
    assert "dashboard" in out, "提示里要写清装哪个 extra"


def test_run_queue_survives_config_load_failure(tmp_path, monkeypatch, capsys):
    """配置读不出来时 ``queue`` 必须退化为默认值继续报告，而不是整个命令失败。

    看板的「运行/排队」状态是排障入口，配置坏了恰恰是最需要它可用的时候。
    """

    def boom(*a, **k):
        raise RuntimeError("ptrade_config.json 损坏")

    monkeypatch.setattr(cli.cfgmod, "load", boom)
    rc = cli.run_queue(cli.parse_args(["queue", "--output-dir", str(tmp_path), "--json"]))
    data = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert {"cpu_count", "mem_total", "mem_available", "disk_free"} <= set(data["resources"])
    # 配置读取失败时应**降级为内置默认值**，而不是让字段缺失或变成哨兵值。
    # （原断言 `"jobs" in q or isinstance(q, dict)` 的后半恒真 → 零鉴别力：
    #   实测把退化分支改成 max_parallel=-999 它照样通过。）
    q = data["queue"]
    assert set(q) >= {
        "running",
        "queued",
        "used_count",
        "used_slots",
        "max_parallel",
        "cpu_slots_limit",
    }, f"queue 段缺字段：{sorted(q)}"
    assert isinstance(q["max_parallel"], int) and q["max_parallel"] >= 1, (
        f"配置读取失败后 max_parallel 应降级为合理的正整数，实际 {q['max_parallel']!r}"
    )
    assert isinstance(q["cpu_slots_limit"], int) and q["cpu_slots_limit"] >= 1, (
        f"cpu_slots_limit 应降级为合理的正整数，实际 {q['cpu_slots_limit']!r}"
    )
    assert q["running"] == [] and q["queued"] == [], "空队列目录应无 running/queued"


# ============================================================
# server.py 夹具
# ============================================================


def _board_root(tmp_path: Path) -> Path:
    """结果根：一个产物齐全的 run + 一个空目录 run（模拟回测刚起步）。"""
    root = tmp_path / "backtest_results"
    run = root / "demo-20250101_120000"
    run.mkdir(parents=True)
    (run / "progress.json").write_text(
        json.dumps({"status": "done", "phase": "done", "percent": 100.0}), encoding="utf-8"
    )
    (run / "summary.json").write_text(
        json.dumps({"total_return": 0.01, "config": {}}), encoding="utf-8"
    )
    (run / "daily_stats.csv").write_text(
        "date,total_value,cash,positions_value,daily_return,cum_return,drawdown,"
        "trades_count,commission\n2025-01-02,1000000,1000000,0,0.0,0.0,0.0,0,0.0\n",
        encoding="utf-8",
    )
    (run / "trades.csv").write_text(
        "time,security,side,amount,price\n2025-01-02 09:31:00,000001.SZ,buy,1000,10.5\n",
        encoding="utf-8",
    )
    (run / "output.log").write_text("l1\nl2\n", encoding="utf-8")
    (root / "empty-20250101_000000").mkdir()
    return root


@pytest.fixture
def board_root(tmp_path) -> Path:
    return _board_root(tmp_path)


@pytest.fixture
def board_client(board_root):
    pytest.importorskip("httpx", reason="TestClient 需要 httpx")
    from fastapi.testclient import TestClient

    return TestClient(server.create_app(board_root))


def _big_log_root(tmp_path: Path, n: int = 2000) -> Path:
    """一个 output.log 远超默认 64KB 读窗的 run（每行 101 字节，共 ~197KB）。"""
    root = tmp_path / "big_results"
    run = root / "bigrun-20250101_000000"
    run.mkdir(parents=True)
    (run / "output.log").write_text(
        "".join(f"L{i:05d}" + "x" * 94 + "\n" for i in range(n)), encoding="utf-8"
    )
    return root


def _log_lines(i0: int, i1: int) -> list[str]:
    return [f"L{i:05d}" + "x" * 94 for i in range(i0, i1)]


# ============================================================
# server.py：404 与「文件缺失」的边界
# ============================================================


@requires_server
def test_trades_missing_run_is_404(board_client):
    """run 不存在必须 404。

    若改成返回空结构，前端会把「名字打错/已被清理」显示成「该 run 没有成交」，
    把用户引向错误结论。
    """
    r = board_client.get("/api/run/no_such_run/trades")
    assert r.status_code == 404


@requires_server
def test_log_missing_run_is_404(board_client):
    r = board_client.get("/api/run/no_such_run/log")
    assert r.status_code == 404


@requires_server
def test_trades_without_file_returns_empty_structure(board_client):
    """run 存在但 trades.csv 还没写（回测刚起步）时返回空结构，而不是 500。"""
    r = board_client.get("/api/run/empty-20250101_000000/trades")
    assert r.status_code == 200
    assert r.json() == {"rows": [], "total": 0}


@requires_server
def test_log_without_file_returns_empty_structure(board_client):
    r = board_client.get("/api/run/empty-20250101_000000/log")
    assert r.status_code == 200
    assert r.json() == {"lines": [], "total": 0}


@requires_server
@pytest.mark.parametrize("exc", [OSError("文件正被原子替换"), ValueError("解析失败")])
def test_trades_csv_read_failure_is_tolerated(board_root, monkeypatch, exc):
    """trades.csv 读取失败（并发替换 / 半截文件）要 200 + error 提示，不能 500。

    引擎写 CSV 用的是「写临时文件再替换」，看板轮询时撞上半截文件是常态；
    500 会让整个详情页白屏，而实际上只是这一块暂时读不到。
    """
    from fastapi.testclient import TestClient

    def boom(path, *a, **k):
        raise exc

    monkeypatch.setattr(server.runstore, "_read_csv_retry", boom)
    client = TestClient(server.create_app(board_root))
    r = client.get("/api/run/demo-20250101_120000/trades")

    assert r.status_code == 200
    assert r.json() == {"rows": [], "total": 0, "error": "trades.csv 读取失败"}


@requires_server
def test_trades_paging_slices_server_side(tmp_path):
    """成交明细服务端分页：只下发当前页，但 total 报全量。

    大 run 有上千行，全量下发会让详情页卡住；而 total 若是当前页长度，
    前端的翻页控件会少算页数、看不到后面的成交。
    """
    from fastapi.testclient import TestClient

    root = tmp_path / "paged_results"
    run = root / "paged-20250101_000000"
    run.mkdir(parents=True)
    rows = "\n".join(
        f"2025-01-02 09:3{i}:00,00000{i}.SZ,buy,{100 * (i + 1)},10.5" for i in range(5)
    )
    (run / "trades.csv").write_text(
        "time,security,side,amount,price\n" + rows + "\n", encoding="utf-8"
    )
    client = TestClient(server.create_app(root))

    body = client.get(
        "/api/run/paged-20250101_000000/trades", params={"page": 2, "page_size": 2}
    ).json()

    assert body["total"] == 5, "total 是全量行数，不是当前页行数"
    assert [r["security"] for r in body["rows"]] == ["000002.SZ", "000003.SZ"]


@requires_server
def test_root_without_web_dist_reports_how_to_build(board_root, monkeypatch):
    """前端未构建时根路径返回 503 + 构建指引，而不是 500/404。

    从源码直接跑 `ptrade-sim dashboard` 的人最容易踩这个：不给出指引就只会看到白屏。
    """
    from fastapi.testclient import TestClient

    monkeypatch.setattr(server, "resolve_web_dist", lambda: None)
    client = TestClient(server.create_app(board_root))

    r = client.get("/")
    assert r.status_code == 503
    body = r.json()
    assert "前端未构建" in body["error"]
    assert "pnpm" in body["hint"] and "web/" in body["hint"], "提示要给出可直接照做的命令"
    assert body["api"] == "/api/runs", "提示里要给出可用的 API 入口"


# ============================================================
# server.py：/log 的 offset 翻页
# ============================================================


@requires_server
def test_log_offset_paging_reads_earlier_lines(tmp_path):
    """offset 翻页要能真的翻到更早的日志，越界则返回空列表。

    文件 ~197KB > 默认 64KB 读窗：
    * offset=0       -> 最后 10 行；
    * offset=1500    -> 必须扩读窗才拿得到第 491~500 行（否则 offset 越过读窗 → 空）；
    * offset=5000    -> 越过文件头，返回 []（不能把读窗开头几行当成「更早的日志」重复下发）。
    """
    from fastapi.testclient import TestClient

    root = _big_log_root(tmp_path, n=2000)
    client = TestClient(server.create_app(root))
    url = "/api/run/bigrun-20250101_000000/log"

    r0 = client.get(url, params={"lines": 10, "offset": 0})
    assert r0.status_code == 200
    assert r0.json()["total"] == 2000
    assert r0.json()["lines"] == _log_lines(1990, 2000), "offset=0 应取最后 10 行"

    r1 = client.get(url, params={"lines": 10, "offset": 1500})
    assert r1.json()["lines"] == _log_lines(490, 500), (
        "offset 越大必须扩读窗，否则翻不到更早的日志（64KB 只够约 648 行）"
    )

    r2 = client.get(url, params={"lines": 10, "offset": 5000})
    assert r2.json()["lines"] == [], "offset 越过文件头应返回空列表，而不是文件开头的行"


@requires_server
def test_log_offset_expands_read_window_with_cap(tmp_path, monkeypatch):
    """读窗按 offset 扩容且封顶 8MB：既要翻得到，也不能被超大 offset 拖去全量读盘。"""
    from fastapi.testclient import TestClient

    root = _big_log_root(tmp_path, n=10)
    client = TestClient(server.create_app(root))
    seen: list[int] = []
    monkeypatch.setattr(
        server.runstore,
        "_tail_lines",
        lambda path, n, **kw: (seen.append(kw["max_bytes"]), [])[1],
    )

    url = "/api/run/bigrun-20250101_000000/log"
    client.get(url, params={"lines": 10, "offset": 0})
    client.get(url, params={"lines": 10, "offset": 1000})
    client.get(url, params={"lines": 10, "offset": 10_000_000})

    assert seen[0] == server.TAIL_MAX_BYTES, "offset=0 用默认尾读窗"
    assert seen[1] == 200_000, "offset=1000 时按 200B/行估窗"
    assert seen[2] == 8 * 1024 * 1024, "读窗必须封顶，否则巨大 offset 会全量读盘"


# ============================================================
# server.py：静态托管 / SPA 回退 / 目录穿越
# ============================================================


@pytest.fixture
def web_dist(tmp_path) -> Path:
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text("<html>INDEX_MARKER</html>", encoding="utf-8")
    (d / "assets" / "app.js").write_text("console.log('APP_JS');", encoding="utf-8")
    (d / "hello.txt").write_text("HELLO_TXT", encoding="utf-8")
    # dist 之外的敏感文件：目录穿越若能读到它，等于把结果根之外的任意文件暴露出去
    (tmp_path / "outside.txt").write_text("SECRET_OUTSIDE", encoding="utf-8")
    return d


@requires_server
def test_static_file_and_spa_fallback(board_root, web_dist, monkeypatch):
    """命中真实文件走 FileResponse；未知路径回退 index.html（前端用 history 路由）。"""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("PTRADE_SIM_WEB_DIST", str(web_dist))
    client = TestClient(server.create_app(board_root))

    assert "INDEX_MARKER" in client.get("/").text
    assert "APP_JS" in client.get("/assets/app.js").text, "静态资源必须由 StaticFiles 挂载提供"
    assert client.get("/hello.txt").text == "HELLO_TXT", "dist 内的真实文件应原样返回"

    r = client.get("/some/spa/route")
    assert r.status_code == 200
    assert "INDEX_MARKER" in r.text, "未命中的路径要回退 index.html，否则前端刷新即 404"


@requires_server
def test_static_route_rejects_path_traversal(board_root, web_dist, monkeypatch):
    """``../`` 不能读到 dist 之外：应回退 index.html。

    HTTP 客户端会把 ``..`` 规范化掉，所以直接取路由端点、用真实的穿越字符串打它。
    ``../outside.txt`` 是**真实存在**的 dist 外文件 —— 少了 ``is_relative_to`` 这道判断，
    它就会被原样下发。
    """
    monkeypatch.setenv("PTRADE_SIM_WEB_DIST", str(web_dist))
    app = server.create_app(board_root)
    route = next(r for r in app.routes if getattr(r, "path", None) == "/{path:path}")

    for escape in ("../outside.txt", "../../outside.txt"):
        resp = route.endpoint(escape)
        assert Path(resp.path) == web_dist / "index.html", f"{escape} 必须回退 index.html"


# ============================================================
# server.py：_ping / ensure_running
# ============================================================


class _FakePingResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def _patch_urlopen(monkeypatch, payload: dict, *, first_call_fails: bool = False):
    """替换 urlopen，返回被请求的 URL 列表。"""
    calls: list[str] = []
    state = {"n": 0}

    def fake(url, timeout=None):
        calls.append(url)
        state["n"] += 1
        if first_call_fails and state["n"] == 1:
            raise urllib.error.URLError("connection refused")
        return _FakePingResponse(payload)

    monkeypatch.setattr(server.urllib.request, "urlopen", fake)
    return calls


@requires_server
def test_ping_detects_reachable_service(monkeypatch):
    """``_ping`` 是端口复用的唯一依据：非 200 与连不上都必须当作「没在跑」。"""
    monkeypatch.setattr(
        server.urllib.request,
        "urlopen",
        lambda url, timeout=None: _FakePingResponse({}, status=500),
    )
    assert server._ping("http://127.0.0.1:9") is False

    def refuse(url, timeout=None):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(server.urllib.request, "urlopen", refuse)
    assert server._ping("http://127.0.0.1:9") is False

    monkeypatch.setattr(
        server.urllib.request, "urlopen", lambda url, timeout=None: _FakePingResponse({"ok": True})
    )
    assert server._ping("http://127.0.0.1:9") is True


@requires_server
def test_ensure_running_reuses_service_with_matching_root(tmp_path, monkeypatch):
    """端口上已有看板且 root 一致 → 复用，绝不重复拉起进程（会抢端口/双份日志）。"""
    root = tmp_path / "results"
    root.mkdir()
    calls = _patch_urlopen(monkeypatch, {"ok": True, "root": str(root.resolve())})
    monkeypatch.setattr(
        server.subprocess,
        "Popen",
        lambda *a, **k: pytest.fail("已存在的看板不该被重复拉起"),
    )

    url = server.ensure_running(root, 8765)

    assert url == "http://127.0.0.1:8765"
    assert calls == ["http://127.0.0.1:8765/api/ping"] * 2, "先探测存活，再核对 root"


@requires_server
def test_ensure_running_gives_up_when_root_differs(tmp_path, monkeypatch):
    """端口被**别的** root 的看板占着 → 返回 None（不能把回测结果指到别人的看板上）。"""
    root = tmp_path / "results"
    root.mkdir()
    _patch_urlopen(monkeypatch, {"ok": True, "root": str((tmp_path / "other").resolve())})
    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **k: pytest.fail("不该抢占端口"))

    assert server.ensure_running(root, 8765) is None


@requires_server
@pytest.mark.parametrize("os_name", ["nt", "posix"])
def test_ensure_running_spawns_detached_process(tmp_path, monkeypatch, os_name):
    """拉起看板时：命令正确、stdio 丢弃、脱离终端（Windows 用 creationflags，POSIX 用新会话）。

    看板必须比回测进程活得久，且不能因父进程退出被一起杀掉 —— 这两个分支写错就会
    「回测结束看板也没了」，或反过来把终端占住。
    """
    root = tmp_path / "results"
    root.mkdir()
    _patch_urlopen(monkeypatch, {"ok": True, "root": str(root.resolve())}, first_call_fails=True)
    captured: dict = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(server.subprocess, "Popen", fake_popen)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setattr(
        server,
        "os",
        types.SimpleNamespace(name=os_name, environ=os.environ.copy(), pathsep=os.pathsep),
    )

    url = server.ensure_running(root, 8765)

    assert url == "http://127.0.0.1:8765"
    assert captured["cmd"] == [
        sys.executable,
        "-m",
        "ptrade_sim.server",
        "--root",
        str(root.resolve()),
        "--port",
        "8765",
    ]
    kw = captured["kwargs"]
    assert kw["stdin"] is subprocess.DEVNULL
    assert kw["stdout"] is subprocess.DEVNULL and kw["stderr"] is subprocess.DEVNULL
    assert kw["close_fds"] is True
    if os_name == "nt":
        assert kw["creationflags"] == (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )
        assert "start_new_session" not in kw, (
            "Windows 走 creationflags，不能同时给 start_new_session"
        )
    else:
        assert kw["start_new_session"] is True, "POSIX 必须另起会话，否则父进程退出会带走看板"
        assert "creationflags" not in kw
    pkg_parent = str(Path(server.__file__).resolve().parent.parent)
    assert kw["env"]["PYTHONPATH"].split(os.pathsep)[0] == pkg_parent, (
        "子进程要能 import ptrade_sim（editable/源码运行场景）"
    )


@requires_server
def test_ensure_running_returns_none_when_spawn_fails(tmp_path, monkeypatch):
    """拉起失败（无权限/解释器缺失）返回 None —— 看板拉不起来不该阻断回测。"""
    root = tmp_path / "results"
    root.mkdir()
    _patch_urlopen(monkeypatch, {"ok": True, "root": str(root.resolve())}, first_call_fails=True)

    def boom(*a, **k):
        raise OSError("拒绝访问")

    monkeypatch.setattr(server.subprocess, "Popen", boom)

    assert server.ensure_running(root, 8765) is None


@requires_server
def test_ensure_running_gives_up_when_root_unreadable(tmp_path, monkeypatch):
    """探到有服务、却读不出它的 root 时返回 None：宁可不复用，也不能猜着接上别人的看板。"""
    root = tmp_path / "results"
    root.mkdir()
    state = {"n": 0}

    def flaky(url, timeout=None):
        state["n"] += 1
        if state["n"] == 1:
            return _FakePingResponse({"ok": True})  # 第一次 _ping 说活着
        raise urllib.error.URLError("读取 /api/ping 正文失败")

    monkeypatch.setattr(server.urllib.request, "urlopen", flaky)
    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **k: pytest.fail("不该再拉起"))

    assert server.ensure_running(root, 8765) is None


@requires_server
def test_ensure_running_does_not_duplicate_pythonpath(tmp_path, monkeypatch):
    """PYTHONPATH 已含包的父目录时不再重复前置 —— 否则每次拉起都长一截。"""
    root = tmp_path / "results"
    root.mkdir()
    _patch_urlopen(monkeypatch, {"ok": True, "root": str(root.resolve())}, first_call_fails=True)
    captured: dict = {}
    monkeypatch.setattr(
        server.subprocess, "Popen", lambda cmd, **kw: captured.update(kw) or object()
    )
    pkg_parent = str(Path(server.__file__).resolve().parent.parent)
    existing = os.pathsep.join([pkg_parent, "OTHER"])
    monkeypatch.setenv("PYTHONPATH", existing)

    assert server.ensure_running(root, 8765) == "http://127.0.0.1:8765"

    assert captured["env"]["PYTHONPATH"] == existing, "已包含就应原样保留，不能无限膨胀"


@requires_server
def test_ensure_running_returns_none_on_startup_timeout(tmp_path, monkeypatch):
    """拉起后一直探测不到 → 超时返回 None，而不是无限等待。"""

    def refuse(url, timeout=None):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(server.urllib.request, "urlopen", refuse)
    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **k: object())
    root = tmp_path / "results"
    root.mkdir()

    assert server.ensure_running(root, 8765, timeout=0.05) is None
