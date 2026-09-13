"""CLI 测试（参数解析 / 子命令 / 退出码）。

重点是**退出码语义**：CI 与脚本依赖它判断成败。
此前 `db build --tables X` 成功却返回 1，就是这类问题。
"""

from __future__ import annotations

import json
import sys

import duckdb
import pytest

from ptrade_sim import cli, pipeline

pytestmark = pytest.mark.unit


# ============================================================
# 参数解析
# ============================================================


def test_no_command_is_usage():
    assert cli.main([]) == 1


def test_parse_backtest_defaults():
    a = cli.parse_args(["backtest"])
    assert a.command == "backtest"
    assert a.no_dashboard is False and a.no_queue is False


def test_parse_backtest_overrides():
    a = cli.parse_args(
        [
            "backtest",
            "--strategy",
            "s.py",
            "--start",
            "2025-01-01",
            "--end",
            "2025-02-01",
            "--capital",
            "50000",
            "--frequency",
            "daily",
            "--no-dashboard",
            "--no-queue",
            "--no-wait",
            "--threads",
            "4",
        ]
    )
    assert a.strategy == "s.py"
    assert a.start == "2025-01-01"
    assert a.end == "2025-02-01"
    assert a.capital == 50000
    assert a.frequency == "daily"
    assert a.no_dashboard and a.no_queue and a.no_wait
    assert a.threads == 4


def test_backtest_has_no_backend_flag():
    """数据源已统一为 DuckDB，--backend 应已移除。"""
    with pytest.raises(SystemExit):
        cli.parse_args(["backtest", "--backend", "parquet"])


def test_frequency_choices_enforced():
    with pytest.raises(SystemExit):
        cli.parse_args(["backtest", "--frequency", "weekly"])


def test_parse_queue_json():
    a = cli.parse_args(["queue", "--json"])
    assert a.command == "queue" and a.json is True


def test_parse_db_build():
    a = cli.parse_args(
        [
            "db",
            "build",
            "--db",
            "x.duckdb",
            "--data-dir",
            "data/",
            "--start-year",
            "2020",
            "--end-year",
            "2024",
            "--tables",
            "a,b",
            "--overwrite",
        ]
    )
    assert a.db_command == "build"
    assert a.start_year == 2020 and a.end_year == 2024
    assert a.tables == "a,b" and a.overwrite is True


def test_parse_db_normalize():
    a = cli.parse_args(["db", "normalize", "--dry-run", "--drop", "t1,t2"])
    assert a.db_command == "normalize"
    assert a.dry_run is True and a.drop == "t1,t2"


def test_all_subcommands_present():
    a = cli.parse_args(["env"])
    assert a.command == "env"


# ============================================================
# 配置覆盖合并
# ============================================================


def test_resolve_cli_overrides_config():
    a = cli.parse_args(["backtest", "--start", "2020-01-01", "--capital", "1"])
    cfg = pipeline._resolve(a, {"start_date": "1999-01-01", "capital_base": 999})
    assert cfg["start_date"] == "2020-01-01"
    assert cfg["capital_base"] == 1


def test_resolve_keeps_config_when_cli_empty():
    a = cli.parse_args(["backtest"])
    cfg = pipeline._resolve(a, {"start_date": "1999-01-01", "data_dir": "X"})
    assert cfg["start_date"] == "1999-01-01"
    assert cfg["data_dir"] == "X"


def test_resolve_threads_nested():
    a = cli.parse_args(["backtest", "--threads", "3"])
    cfg = pipeline._resolve(a, {"preload": {"mode": "rolling", "threads": 8}})
    assert cfg["preload"]["threads"] == 3
    assert cfg["preload"]["mode"] == "rolling", "同段其他键不应丢失"


# ============================================================
# 交易日估算（资源准入用）
# ============================================================


def test_count_trade_days_from_calendar(tiny_db):
    n = pipeline._count_trade_days(
        {
            "db_path": str(tiny_db),
            "start_date": "2025-01-02",
            "end_date": "2025-01-08",
        }
    )
    assert n == 5, f"应按日历精确计数，实际 {n}"


def test_count_trade_days_falls_back_on_bad_db(tmp_path):
    n = pipeline._count_trade_days(
        {
            "db_path": str(tmp_path / "nope.duckdb"),
            "start_date": "2025-01-01",
            "end_date": "2025-12-31",
        }
    )
    assert n > 200, "库不可用时应退化为自然日近似"


def test_minute_budget_from_config():
    assert pipeline._minute_budget({"cache": {"minute_memory_budget": "1GB"}}) == 1024**3


def test_minute_budget_tolerates_bad_config():
    assert pipeline._minute_budget({}) > 0


# ============================================================
# 子命令端到端
# ============================================================


def test_env_returns_zero(capsys):
    assert cli.main(["env"]) == 0
    out = capsys.readouterr().out
    assert "db_path" in out
    assert "缓存策略" in out


def test_queue_command_json(tmp_path, capsys):
    rc = cli.main(["queue", "--output-dir", str(tmp_path), "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert "resources" in data and "queue" in data
    assert "cpu_count" in data["resources"]


def test_queue_command_text(tmp_path, capsys):
    assert cli.main(["queue", "--output-dir", str(tmp_path)]) == 0
    assert "资源" in capsys.readouterr().out


def test_db_verify_missing_db_raises(tmp_path):
    """库不存在时应抛错（duckdb 打开失败），而不是静默返回 0。"""
    with pytest.raises((duckdb.IOException, OSError, FileNotFoundError)):
        cli.main(["db", "verify", "--db", str(tmp_path / "nope.duckdb")])


def test_db_no_subcommand_is_usage():
    assert cli.main(["db"]) == 1


def test_backtest_missing_strategy_returns_error(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    p = tmp_path / "c.json"
    p.write_text(
        json.dumps(
            {
                "db_path": "data/quant.duckdb",
                "start_date": "2025-01-01",
                "end_date": "2025-01-31",
                "strategy": "no_such_strategy.py",
            }
        ),
        encoding="utf-8",
    )
    assert cli.main(["backtest", "--config", str(p)]) == 2  # 策略路径不存在 -> 配置类


def _mk_strategy_folder(tmp_path, cfg: dict | None = None, py_name: str = "strategy.py"):
    """造一个策略目录（strategy.py + 可选 strategy_config.json）。"""
    d = tmp_path / "my_strategy"
    d.mkdir(exist_ok=True)
    (d / py_name).write_text(
        "def initialize(context):\n"
        "    set_benchmark('000300.SS')\n"
        "    set_universe(['000001.SZ'])\n",
        encoding="utf-8",
    )
    if cfg is not None:
        (d / "strategy_config.json").write_text(
            json.dumps(cfg, ensure_ascii=False), encoding="utf-8"
        )
    return d


def test_backtest_invalid_config_returns_error(tmp_path, monkeypatch, capsys):
    """策略可解析、但配置缺项时应打印可操作指引并以非 0 退出。

    注意：``db_path`` 有 DEFAULTS 兜底，故这里用**必填且无默认**的 end_date 触发。
    """
    monkeypatch.chdir(tmp_path)
    d = _mk_strategy_folder(tmp_path)
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"strategy": str(d), "start_date": "2025-01-01"}), encoding="utf-8")
    # 退出码 2 = 配置 / 策略定位错误（见 exceptions.exit_code_for）
    assert cli.main(["backtest", "--config", str(p)]) == 2
    out = capsys.readouterr().out
    assert "配置有误" in out
    assert "config.example.json" in out


def test_backtest_missing_db_path_reports_guidance(tmp_path, monkeypatch, capsys):
    """显式把 db_path 置空时应给出「用 db build 构建」的指引。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PT_SIM_DB_PATH", "none")
    d = _mk_strategy_folder(tmp_path)
    p = tmp_path / "c.json"
    p.write_text(
        json.dumps(
            {
                "strategy": str(d),
                "start_date": "2025-01-01",
                "end_date": "2025-01-31",
            }
        ),
        encoding="utf-8",
    )
    assert cli.main(["backtest", "--config", str(p)]) == 2
    assert "db_path" in capsys.readouterr().out


def test_backtest_without_strategy_reports_howto(tmp_path, monkeypatch, capsys):
    """未指定策略时应给出目录形态的用法说明，而不是含糊报错。"""
    monkeypatch.chdir(tmp_path)
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"start_date": "2025-01-01"}), encoding="utf-8")
    assert cli.main(["backtest", "--config", str(p)]) == 2
    out = capsys.readouterr().out
    assert "未指定策略" in out
    assert "strategy.py" in out and "strategy_config.json" in out


def test_backtest_missing_strategy_path_reports_it(tmp_path, monkeypatch, capsys):
    """策略路径不存在时应直接指出（策略是入口，比先报配置问题更可操作）。"""
    monkeypatch.chdir(tmp_path)
    p = tmp_path / "c.json"
    p.write_text(
        json.dumps(
            {
                "strategy": str(tmp_path / "nope"),
                "start_date": "2025-01-01",
                "end_date": "2025-01-31",
                "db_path": "data/quant.duckdb",
            }
        ),
        encoding="utf-8",
    )
    assert cli.main(["backtest", "--config", str(p)]) == 2
    assert "不存在" in capsys.readouterr().out


def test_console_logging_setup_does_not_raise():
    cli._setup_console_logging()


def test_module_entrypoint_importable():
    import ptrade_sim.cli as m

    assert callable(m.main)
    assert sys.modules["ptrade_sim.cli"] is m
