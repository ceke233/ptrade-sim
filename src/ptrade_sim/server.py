"""PTrade 模拟回测看板 —— FastAPI 后端。

用法：
    ptrade-sim dashboard                      # 默认根目录 ./backtest_results，端口 8765
    ptrade-sim dashboard --port 9000 --root /path/to/backtest_results
    python -m ptrade_sim.server --port 9000   # 等价（不经 CLI）

功能：
    - GET /                看板页面（Vite 构建产物 web/dist，未构建则返回提示）
    - GET /api/ping        健康检查 / 端口复用探测
    - GET /api/runs        run 列表（状态/进度/时间/源码元信息/实时或最终指标）
    - GET /api/run/<name>/detail   详情（资金曲线/回撤/日志尾/指标/Beta-Alpha）
    - GET /api/run/<name>/trades   交易明细（服务端分页）
    - GET /api/run/<name>/log?lines=N&offset=M  日志尾部 N 行（可向前翻页）
    - GET /api/run/<name>/source   策略源码

前端：仓库 ``web/``（Vite + Vue3 + TS），构建产物 ``web/dist`` 由本服务静态托管；
开发时可 ``cd web && pnpm dev``（vite dev server 代理 /api 到本服务）。

数据流：引擎逐日落盘 ``backtest_results/<run>/progress.json`` 与 ``daily_stats.csv``，
本服务读取并实时呈现。``ptrade-sim backtest`` 通过 ``ensure_running()`` 自动拉起本服务。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ptrade_sim import derived, runstore
from ptrade_sim.config import DEFAULT_PORT, DEFAULT_RESULTS_DIR
from ptrade_sim.runstore import TAIL_MAX_BYTES


def _web_dist_candidates() -> list[Path]:
    """前端构建产物候选目录（按优先级）：

    1. 环境变量 ``PTRADE_SIM_WEB_DIST``（显式覆盖）
    2. 包目录 ``<包>/web/dist``（随包分发前端的场景）
    3. 仓库根 ``web/dist``（开发态：src/ptrade_sim/server.py -> 上溯两级）
    4. 工作目录 ``./web/dist``
    """
    here = Path(__file__).resolve().parent
    out: list[Path] = []
    env = os.environ.get("PTRADE_SIM_WEB_DIST")
    if env:
        out.append(Path(env).expanduser())
    out.append(here / "web" / "dist")
    out.append(here.parent.parent / "web" / "dist")
    out.append(Path.cwd() / "web" / "dist")
    return out


def resolve_web_dist() -> Path | None:
    """返回首个存在的 dist 目录；均不存在返回 None。"""
    return next((p for p in _web_dist_candidates() if p.is_dir()), None)


# ============================================================
# 工具
# ============================================================


# ============================================================
# run 状态与指标
# ============================================================


# ============================================================
# FastAPI 应用
# ============================================================


def create_app(root: Path) -> FastAPI:
    """构造 FastAPI 应用：API 路由 + 前端静态托管。"""
    root = root.resolve()
    web_dist = resolve_web_dist()
    app = FastAPI(title="PTrade 回测看板", docs_url=None, redoc_url=None)

    @app.get("/api/ping")
    def ping() -> dict:
        return {"ok": True, "root": str(root)}

    @app.get("/api/runs")
    def api_runs() -> dict:
        return {"runs": runstore._json_clean(runstore.scan_runs(root))}

    @app.get("/api/run/{name}/detail")
    def api_detail(name: str) -> dict:
        d = runstore._safe_run_dir(root, name)
        if d is None:
            raise HTTPException(404, "run not found")
        status = runstore._run_status(d)
        return runstore._json_clean(
            {
                "name": name,
                "status": status["status"],
                "progress": status["progress"],
                "summary": runstore._run_metrics(d, status),
                "series": derived._series(d),
                "monthly_ext": derived._monthly_extended(d),
                "alpha_beta": derived._overall_alpha_beta(d),
                "logs": runstore._tail_lines(d / "output.log", 60),
            }
        )

    @app.get("/api/run/{name}/trades")
    def api_trades(
        name: str,
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=500),
    ) -> dict:
        d = runstore._safe_run_dir(root, name)
        if d is None:
            raise HTTPException(404, "run not found")
        t = d / "trades.csv"
        if not t.exists():
            return {"rows": [], "total": 0}
        try:
            df = runstore._read_csv_retry(t)
        except (OSError, ValueError):
            return {"rows": [], "total": 0, "error": "trades.csv 读取失败"}
        total = df.height
        # 服务端分页：大 run（上千行）不全量下发，仅取当前页
        start = (page - 1) * page_size
        chunk = df.slice(start, page_size)
        return {
            "rows": runstore._json_clean(chunk.to_dicts()),
            "total": total,
        }

    @app.get("/api/run/{name}/log")
    def api_log(
        name: str,
        lines: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> dict:
        d = runstore._safe_run_dir(root, name)
        if d is None:
            raise HTTPException(404, "run not found")
        log = d / "output.log"
        if not log.exists():
            return {"lines": [], "total": 0}
        # 向前翻页需要更大的读窗：offset 越大越要读全文件，避免 64KB 截断
        max_bytes = TAIL_MAX_BYTES
        if offset > 0:
            # 粗略按需扩容：offset 行 × 200B 估窗，封顶 8MB（避免特大日志全读）
            max_bytes = max(TAIL_MAX_BYTES, min(offset * 200, 8 * 1024 * 1024))
        total = runstore._count_lines(log)
        return {
            "lines": runstore._tail_lines(log, lines, max_bytes=max_bytes, offset=offset),
            "total": total,
        }

    @app.get("/api/run/{name}/source")
    def api_source(name: str) -> dict:
        d = runstore._safe_run_dir(root, name)
        if d is None:
            raise HTTPException(404, "run not found")
        src = runstore._strategy_source(d)
        return {"path": src["path"], "exists": src["exists"], "source": src["source"]}

    # ---------- 前端静态托管（Vite 产物） ----------
    if web_dist is not None:
        app.mount("/assets", StaticFiles(directory=web_dist / "assets"), name="assets")

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(web_dist / "index.html")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            p = (web_dist / path).resolve()
            # 防穿越：只服务 dist 内文件；否则回退 index.html（SPA 路由）
            if p.is_file() and p.is_relative_to(web_dist.resolve()):
                return FileResponse(p)
            return FileResponse(web_dist / "index.html")

    else:

        @app.get("/", include_in_schema=False)
        def no_frontend() -> JSONResponse:
            return JSONResponse(
                {
                    "error": "前端未构建",
                    "hint": "在仓库 web/ 目录执行：pnpm install && pnpm build",
                    "api": "/api/runs",
                },
                status_code=503,
            )

    return app


# ============================================================
# 端口探测与常驻拉起（供 cli.py 调用）
# ============================================================


def _ping(url: str, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/api/ping", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def ensure_running(root: Path, port: int, timeout: float = 5.0) -> str | None:
    """确保看板服务在跑：已跑则复用（校验 root 一致），否则脱离终端常驻拉起。

    返回服务 URL；拉起失败返回 None（不阻断回测）。
    """
    url = f"http://127.0.0.1:{port}"
    if _ping(url):
        try:
            with urllib.request.urlopen(f"{url}/api/ping", timeout=1) as r:
                data = json.loads(r.read().decode("utf-8"))
            if data.get("root") == str(root.resolve()):
                return url
        except Exception:
            pass
        return None  # 端口被其他程序占用（或 root 不一致）

    cmd = [
        sys.executable,
        "-m",
        "ptrade_sim.server",
        "--root",
        str(root.resolve()),
        "--port",
        str(port),
    ]
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True
    # 让子进程继承当前包的导入路径（editable/源码运行场景）
    env = os.environ.copy()
    pkg_parent = str(Path(__file__).resolve().parent.parent)
    if pkg_parent not in env.get("PYTHONPATH", "").split(os.pathsep):
        env["PYTHONPATH"] = os.pathsep.join(
            [pkg_parent] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
        )
    try:
        subprocess.Popen(cmd, close_fds=True, env=env, **kwargs)
    except Exception:
        return None

    deadline = time.time() + timeout
    while time.time() < deadline:
        if _ping(url):
            return url
        time.sleep(0.2)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ptrade-sim dashboard", description="PTrade 回测看板（FastAPI）"
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("PT_SIM_PORT", DEFAULT_PORT))
    )
    parser.add_argument("--root", type=Path, default=Path.cwd() / DEFAULT_RESULTS_DIR)
    parser.add_argument("--host", type=str, default="127.0.0.1")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if not root.is_dir():
        print(f"根目录不存在：{root}")
        print("请先运行一次回测：ptrade-sim backtest")
        return 1

    try:
        import uvicorn
    except ImportError:
        print(
            "缺少看板依赖。请安装：\n"
            "  uv tool install 'ptrade-sim[dashboard]'   # 或\n"
            "  pip install 'ptrade-sim[dashboard]'"
        )
        return 1

    dist = resolve_web_dist()
    if dist is None:
        print(
            "提示：前端尚未构建，页面将返回 503。在仓库 web/ 目录执行：\n"
            "  pnpm install && pnpm build"
        )
    print(f"PTrade 回测看板：http://{args.host}:{args.port}（根目录 {root}）")
    uvicorn.run(create_app(root), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
