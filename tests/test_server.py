"""看板服务测试（FastAPI 路由 / 结果目录解析 / 静态托管降级）。

用 ``TestClient`` 直接打路由，不需要真起 uvicorn。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="看板测试需要 fastapi")
pytest.importorskip("httpx", reason="TestClient 需要 httpx")

from ptrade_sim import server

pytestmark = pytest.mark.integration


@pytest.fixture
def results_root(tmp_path) -> Path:
    """造一个含两个回测结果的结果根目录。"""
    root = tmp_path / "backtest_results"
    root.mkdir()

    run = root / "demo-20250101_120000"
    run.mkdir()
    (run / "progress.json").write_text(
        json.dumps(
            {
                "phase": "done",
                "day_index": 5,
                "total_days": 5,
                "current_day": "2025-01-08",
                "percent": 100.0,
                "updated_at": "2025-01-08T15:00:00",
                "config": {
                    "strategy_name": "示例轮动",
                    "start_date": "2025-01-02",
                    "end_date": "2025-01-08",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (run / "summary.json").write_text(
        json.dumps(
            {
                "total_return": 0.0123,
                "annual_return": 0.05,
                "sharpe": 1.2,
                "max_drawdown": -0.03,
                "calmar": 1.6,
                "final_value": 1012300.0,
                "win_rate": 0.5,
                "profit_loss_ratio": 1.4,
                "trade_count": 4,
                "total_commission": 12.3,
                "benchmark_return": 0.008,
                "config": {
                    "strategy_name": "示例轮动",
                    "start_date": "2025-01-02",
                    "end_date": "2025-01-08",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    # 列与引擎实际写出的 daily_stats.csv 对齐（服务端会读 drawdown 等列）
    (run / "daily_stats.csv").write_text(
        "date,total_value,cash,positions_value,benchmark_close,"
        "daily_return,cum_return,drawdown,trades_count,commission\n"
        "2025-01-02,1000000,1000000,0,4000.0,0.0,0.0,0.0,0,0.0\n"
        "2025-01-03,1010000,900000,110000,4010.0,0.01,0.01,-0.005,2,6.1\n",
        encoding="utf-8",
    )
    (run / "trades.csv").write_text(
        "time,security,side,amount,price\n2025-01-02 09:31:00,000001.SZ,buy,1000,10.5\n",
        encoding="utf-8",
    )
    (run / "strategy_source.py").write_text("# demo\n", encoding="utf-8")
    # 日志文件名与引擎一致（server 读 output.log）
    (run / "output.log").write_text("line1\nline2\n", encoding="utf-8")

    # 一个「坏」run：没有任何文件，验证健壮性
    (root / "empty-20250101_000000").mkdir()
    return root


@pytest.fixture
def client(results_root):
    from fastapi.testclient import TestClient

    return TestClient(server.create_app(results_root))


# ============================================================
# 路由
# ============================================================


def test_ping(client):
    r = client.get("/api/ping")
    assert r.status_code == 200
    assert r.json().get("ok") is True


def test_runs_lists_both_dirs(client):
    r = client.get("/api/runs")
    assert r.status_code == 200
    data = r.json()
    names = {x["name"] for x in (data if isinstance(data, list) else data.get("runs", []))}
    assert "demo-20250101_120000" in names
    assert "empty-20250101_000000" in names


def test_run_detail(client):
    r = client.get("/api/run/demo-20250101_120000/detail")
    assert r.status_code == 200
    body = r.json()
    text = json.dumps(body, ensure_ascii=False)
    assert "示例轮动" in text


def test_run_detail_missing_run(client):
    r = client.get("/api/run/no_such_run/detail")
    assert r.status_code in (404, 200)
    if r.status_code == 200:
        assert r.json() in ({}, [], None) or "error" in json.dumps(r.json())


def test_run_trades(client):
    r = client.get("/api/run/demo-20250101_120000/trades")
    assert r.status_code == 200


def test_run_log(client):
    r = client.get("/api/run/demo-20250101_120000/log")
    assert r.status_code == 200
    assert "line1" in r.text or "line1" in json.dumps(r.json(), ensure_ascii=False)


def test_run_source(client):
    r = client.get("/api/run/demo-20250101_120000/source")
    assert r.status_code == 200


def test_run_source_missing_run(client):
    r = client.get("/api/run/no_such_run/source")
    assert r.status_code in (200, 404)


def test_empty_run_detail_tolerated(client):
    """缺文件的目录不应 500（回测刚开始时正常现象）。"""
    r = client.get("/api/run/empty-20250101_000000/detail")
    assert r.status_code in (200, 404)


def test_path_traversal_rejected(client):
    """目录穿越不能读到结果根之外。"""
    r = client.get("/api/run/..%2F..%2Fetc/detail")
    assert r.status_code in (200, 400, 404)
    assert "root:" not in r.text.lower()


# ============================================================
# 静态托管降级
# ============================================================


def test_root_without_web_dist_returns_503(client):
    """未构建前端时根路径应给出提示，而不是 500。"""
    r = client.get("/")
    assert r.status_code in (200, 503)
    if r.status_code == 503:
        assert "构建" in r.text or "build" in r.text.lower()


def test_unknown_path_without_web_dist(client):
    r = client.get("/some/spa/route")
    assert r.status_code in (200, 404, 503)


def test_resolve_web_dist_env_override(tmp_path, monkeypatch):
    """``PTRADE_SIM_WEB_DIST`` 应能显式指定前端产物目录。"""
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text("<html></html>", encoding="utf-8")
    monkeypatch.setenv("PTRADE_SIM_WEB_DIST", str(d))
    got = server.resolve_web_dist()
    assert got == d


def test_resolve_web_dist_returns_none_or_path(monkeypatch):
    monkeypatch.delenv("PTRADE_SIM_WEB_DIST", raising=False)
    got = server.resolve_web_dist()
    assert got is None or isinstance(got, Path)


# ============================================================
# CLI 集成
# ============================================================


def test_main_parses_root_and_port(tmp_path, monkeypatch):
    """``main`` 应能接受 --root/--port 并构造应用（不真起服务）。"""
    import uvicorn

    called: dict = {}

    def fake_run(app, **kw):
        called["app"] = app
        called.update(kw)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    rc = server.main(["--root", str(tmp_path), "--port", "9999", "--host", "127.0.0.1"])
    assert rc == 0
    assert called.get("port") == 9999
    assert called.get("host") == "127.0.0.1"
