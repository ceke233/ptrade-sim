"""run 产物的读取与塑形（看板后端的数据层）。

**职责**：把 ``backtest_results/<run>/`` 下的产物（``progress.json`` / ``summary.json`` /
``daily_stats.csv`` / ``trades.csv`` / ``strategy_source.py`` / ``output.log``）读出来，
整理成看板需要的结构。

**不含**：HTTP 路由、派生指标（见 ``derived.py``）、看板进程管理（见 ``server.py``）。
依赖方向是单向的：``server → {derived, runstore}``，``derived → runstore``。

**为什么单独成模块**：这层是纯 IO + 结构整理，与 HTTP 无关，可独立测试与基准；
且它是看板性能的瓶颈所在（``scan_runs`` 每次轮询都要跑）。
"""

from __future__ import annotations

import json
import math
import re
import time
from datetime import date, datetime
from pathlib import Path

import polars as pl

from ptrade_sim import runtime
from ptrade_sim.config import DEFAULT_CAPITAL_BASE

RUN_RE = re.compile(r"^(.*)-(\d{8}_\d{6})$")


NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


STALE_SEC = 300


TAIL_MAX_BYTES = 64 * 1024


_LIST_METRIC_KEYS = (
    "total_return",
    "annual_return",
    "sharpe",
    "max_drawdown",
    "calmar",
    "final_value",
    "benchmark_return",
    "trade_count",
    "total_commission",
)


def _read_csv_retry(path: Path, retries: int = 3, delay: float = 0.05):
    """读取 CSV（polars），遇到并发原子替换导致的瞬时 OSError 时重试。"""
    for attempt in range(retries):
        try:
            # 兼容历史结果文件：早期由 pandas 以 utf-8-sig 写出（带 BOM）。
            # polars 默认即能识别 UTF-8 BOM，故无需特殊参数。
            return pl.read_csv(path, infer_schema_length=0, encoding="utf8")
        except (OSError, ValueError):
            if attempt == retries - 1:
                raise
            time.sleep(delay)
    return None  # pragma: no cover - 不可达，仅为类型收窄


def _json_clean(obj):
    """递归清洗 NaN/Inf -> None、datetime -> str，保证 JSON 可序列化。"""
    # ⚠️ date.isoformat() **不接受 sep 参数**（只有 datetime 接受），
    # 故两者必须分开处理；否则响应里出现纯 date 对象时会 500。
    if isinstance(obj, datetime):
        return obj.isoformat(sep=" ")
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, float):
        return None if math.isnan(obj) or math.isinf(obj) else obj
    if isinstance(obj, dict):
        return {k: _json_clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_clean(v) for v in obj]
    return obj


def _as_float(x) -> float | None:
    try:
        v = float(x)
        return None if math.isnan(v) or math.isinf(v) else v
    except (TypeError, ValueError):
        return None


def _read_progress(d: Path) -> dict | None:
    p = d / "progress.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _run_status(d: Path) -> dict:
    """返回 run 的状态信息：status/phase/进度/时间戳。"""
    progress = _read_progress(d)
    has_summary = (d / "summary.json").exists()
    stats_path = d / "daily_stats.csv"
    mtime = stats_path.stat().st_mtime if stats_path.exists() else 0.0

    if progress is None:
        if has_summary:
            return {"status": "done", "progress": None, "mtime": mtime}
        return {"status": "missing", "progress": None, "mtime": mtime}

    status = progress.get("status", "running")
    if status == "running":
        # 陈旧 running -> 中断
        age = time.time() - (d / "progress.json").stat().st_mtime
        if age > STALE_SEC:
            status = "interrupted"
    return {"status": status, "progress": progress, "mtime": mtime}


def _live_metrics(d: Path, progress: dict | None) -> dict | None:
    """用当日 daily_stats.csv 现算实时指标（与最终 compute_metrics 同口径）。"""
    stats_path = d / "daily_stats.csv"
    if not stats_path.exists():
        return None
    try:
        daily = _read_csv_retry(stats_path)
    except (OSError, ValueError):
        return None
    if daily is None or daily.height == 0:
        return None
    capital = float((progress or {}).get("capital_base", DEFAULT_CAPITAL_BASE))
    benchmark = (progress or {}).get("benchmark", "000300.SS")
    empty_trades = pl.DataFrame(
        schema={"side": pl.String, "trade_pnl": pl.Float64, "commission": pl.Float64}
    )
    try:
        m = runtime.compute_metrics(daily, empty_trades, capital, {"benchmark": benchmark})
    except Exception:
        return None
    # 成交笔数/佣金在 running 阶段取自日线累计列
    if "trades_count" in daily.columns:
        m["trade_count"] = int(daily["trades_count"].cast(pl.Float64).sum())
    if "commission" in daily.columns:
        m["total_commission"] = float(daily["commission"].cast(pl.Float64).sum())
    return m


def _run_metrics(d: Path, status: dict) -> dict | None:
    """完成 run 取 summary.json；运行中/中断取实时指标。"""
    if status["status"] == "done":
        s = d / "summary.json"
        if s.exists():
            try:
                return json.loads(s.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
        # summary 尚未落盘（引擎刚结束、CLI 未写完）时退回实时指标
        return _live_metrics(d, status.get("progress"))
    return _live_metrics(d, status.get("progress"))


def _strategy_source(d: Path) -> dict:
    """定位 run 对应的策略源码文件。

    优先读 run 目录内自带的 strategy_source.py 副本（引擎回测时保存），
    缺失再退回 progress.json / summary.json 记录的 strategy 路径。
    返回 {"path", "exists", "source"}；两份都没有时 exists=False。
    """
    # 1) run 自带副本（runtime._save_strategy_source 落盘）
    copy = d / "strategy_source.py"
    if copy.exists() and copy.is_file():
        try:
            return {
                "path": str(copy),
                "exists": True,
                "source": copy.read_text(encoding="utf-8", errors="replace"),
            }
        except OSError:
            pass  # 副本读取失败则退回外部路径
    # 2) 外部策略文件（历史 run 无副本）
    cand: str | None = None
    for pname in ("progress.json", "summary.json"):
        pf = d / pname
        if pf.exists():
            try:
                data = json.loads(pf.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if pname == "progress.json":
                cand = data.get("strategy")
            else:
                cfg = data.get("config") or {}
                cand = cfg.get("strategy")
            if cand:
                break
    if not cand:
        return {"path": None, "exists": False, "source": None}
    p = Path(cand)
    if not p.is_absolute():
        p = Path.cwd() / p
    if not p.exists() or not p.is_file():
        return {"path": str(p), "exists": False, "source": None}
    try:
        src = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"path": str(p), "exists": False, "source": None}
    return {"path": str(p), "exists": True, "source": src}


def _tail_lines(path: Path, n: int, max_bytes: int = TAIL_MAX_BYTES, offset: int = 0) -> list[str]:
    """读取文件尾部的最后 n 行（按 \\n 切分，尊重 UTF-8）。

    offset=0 读最后 n 行；offset>0 表示跳过末尾 offset 行再往前读 n 行
    （用于“加载更早日志”向前翻页），返回行数可能不足 n（已到文件头）；
    offset 已越过文件头时返回**空列表**（再无更早的行可给）。
    """
    if not path.exists() or n <= 0:
        return []
    size = path.stat().st_size
    with path.open("rb") as f:
        if size <= max_bytes:
            f.seek(0)
            data = f.read()
        else:
            f.seek(size - max_bytes)
            data = f.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    if offset > 0:
        # 越界 = 已翻到文件头，此时**没有更早的行**，必须返回空列表。
        # 返回 lines[:n] 会把读窗开头那几行当成「更早的日志」重复下发。
        if offset >= len(lines):
            return []
        return lines[-offset - n : -offset]
    return lines[-n:]


def _count_lines(path: Path) -> int:
    """快速统计文本行数（无需读全量，按块扫描 \\n）。"""
    if not path.exists():
        return 0
    count = 0
    with path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            count += chunk.count(b"\n")
    return count


def _safe_run_dir(root: Path, name: str) -> Path | None:
    """校验 run 名并返回其目录；不合法或越权返回 None。"""
    if not NAME_RE.match(name):
        return None
    d = (root / name).resolve()
    if not d.is_dir() or not d.is_relative_to(root.resolve()):
        return None
    return d


def _dir_timestamp(d: Path) -> datetime | None:
    """从目录名 策略名-YYYYMMDD_HHMMSS 解析开始时间；无法解析返回 None。"""
    m = RUN_RE.match(d.name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(2), "%Y%m%d_%H%M%S")
    except ValueError:
        return None


def _name_from_dir(run_name: str) -> str:
    """从 run 目录名推展示名：去掉末尾的 ``-YYYYMMDD_HHMMSS``。

    run 目录由 CLI 命名为 ``{策略目录名}-{时间戳}``，所以去掉时间戳即得策略名。
    这是**最后**的回退 —— 只在 run 既没有留档的策略配置、``summary.json`` 也没记
    ``strategy_name``（早期或不完整的 run）时才会用到。
    """
    return re.sub(r"-\d{8}_\d{6}$", "", run_name)


def _run_strategy_name(d: Path) -> str | None:
    """从 run 目录留档的策略配置里取展示名。

    策略目录形态（``strategies/x/{strategy.py,strategy_config.json}``）会把
    ``strategy_config.json`` 复制进 run 目录，故展示名由 run 自身即可确定，
    不依赖任何外部配置。
    """
    for fn in ("strategy_config.json", "run_config.json"):
        p = d / fn
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            continue
        nm = data.get("name") or data.get("strategy_name")
        if nm:
            return str(nm)
    return None


def _slim_metrics(m: dict | None) -> dict | None:
    if not m:
        return None
    return {k: m.get(k) for k in _LIST_METRIC_KEYS}


def _scan_one(d: Path, name: str) -> dict:
    """解析单个 run 目录 -> 看板列表项。

    只依赖传入的目录，不读全局状态，故可单独测试与基准。
    """
    # 排序键统一为 epoch 秒（float）：
    # 命中时间戳目录时用目录名里的开始时间，否则退回目录 mtime。
    # 两者必须是同一类型才可比 —— 目录名是 15 字符字符串（YYYYMMDD_HHMMSS），
    # mtime 是 float，直接 str() 混排会按「长度 + 字典序」比，
    # 对混合目录得到的不是时间倒序。
    dt_dir = _dir_timestamp(d)
    sort_key = dt_dir.timestamp() if dt_dir is not None else d.stat().st_mtime
    status = _run_status(d)
    metrics = _run_metrics(d, status)
    progress = status.get("progress")
    started_at = (progress or {}).get("started_at")
    dt = None
    if started_at:
        try:
            dt = datetime.fromisoformat(started_at)
        except ValueError:
            dt = None
    if dt is None:
        dt = dt_dir
    start_iso = dt.isoformat(timespec="seconds") if dt else None
    # 历史 run 无 progress：区间/策略字段从 summary.json.config 回退
    sum_cfg: dict = {}
    sf = d / "summary.json"
    if sf.exists():
        try:
            sum_cfg = (json.loads(sf.read_text(encoding="utf-8")) or {}).get("config") or {}
        except (OSError, ValueError):
            sum_cfg = {}
    # 历史 run 无 progress：结束时间用 summary.json mtime / 目录 mtime 近似
    done_ts = None
    if status["status"] == "done" and (progress or {}).get("status") != "done":
        for cand in (sf, d):
            if cand.exists():
                done_ts = datetime.fromtimestamp(cand.stat().st_mtime)
                break
    src = _strategy_source(d) if status["status"] == "done" else {"path": None, "exists": False}
    return {
        "name": name,
        "sort_key": sort_key,
        "status": status["status"],
        "phase": (progress or {}).get("phase"),
        "strategy": (progress or {}).get("strategy") or sum_cfg.get("strategy"),
        # 展示名优先级：run 自带策略配置的 name > summary 记录 > run 目录名（去时间戳）
        "strategy_name": (
            _run_strategy_name(d) or sum_cfg.get("strategy_name") or _name_from_dir(name)
        ),
        "start_date": (progress or {}).get("start_date") or sum_cfg.get("start_date"),
        "end_date": (progress or {}).get("end_date") or sum_cfg.get("end_date"),
        "capital_base": (progress or {}).get("capital_base") or sum_cfg.get("capital_base"),
        "benchmark": (progress or {}).get("benchmark") or sum_cfg.get("benchmark"),
        "total_days": (progress or {}).get("total_days"),
        "day_done": (progress or {}).get("day_done"),
        "current_date": (progress or {}).get("current_date"),
        "started_at": start_iso,
        "elapsed_sec": (progress or {}).get("elapsed_sec"),
        "metrics": _json_clean(_slim_metrics(metrics)),
        "done_at": done_ts.isoformat(timespec="seconds") if done_ts else None,
        # 源码仅给元信息（路径/是否存在），正文走 /api/run/<name>/source
        "source_path": src["path"],
        "source_exists": src["exists"],
    }


def _fingerprint(d: Path) -> tuple:
    """run 目录的廉价指纹：只看会随回测推进而变化的三个文件。

    用 ``stat`` 而非 ``exists`` —— 后者内部也是一次 stat，读内容则更贵。
    文件不存在记为 0。
    """

    def mt(p: Path) -> int:
        try:
            return p.stat().st_mtime_ns
        except OSError:
            return 0

    return (mt(d / "progress.json"), mt(d / "daily_stats.csv"), mt(d / "summary.json"))


#: 指纹缓存：(根目录, run 名) -> (指纹, 列表项)。看板每 3 秒轮询，而两次轮询
#: 之间绝大多数 run 毫无变化 —— 没有它就得反复读 JSON/CSV 并重算派生量。
#:
#: ⚠️ 键必须带根目录：不同 --root 下的同名 run（测试里很常见，都用 tmp_path）
#: 否则会互相串结果。
_SCAN_CACHE: dict[tuple[str, str], tuple[tuple, dict]] = {}


def scan_runs(root: Path, use_cache: bool = True) -> list[dict]:
    """扫描根目录下全部 run 目录，按开始时间倒序。

    ``use_cache=False`` 可强制全量重算（测试用）。
    """
    runs = []
    # 缓存键必须带根目录：不同 --root 下的同名 run（测试里都用 tmp_path，很常见）
    # 否则会互相串结果。
    root_key = str(Path(root).resolve())
    seen: set[tuple[str, str]] = set()
    for d in root.iterdir():
        if not d.is_dir():
            continue
        name = d.name
        key = (root_key, name)
        seen.add(key)
        fp: tuple | None = _fingerprint(d) if use_cache else None
        if fp is not None:
            hit = _SCAN_CACHE.get(key)
            if hit is not None and hit[0] == fp:
                # 浅拷贝顶层：调用方可能给条目补字段，不该污染缓存
                runs.append(dict(hit[1]))
                continue
        entry = _scan_one(d, name)
        if fp is not None:
            # 存**副本**：下面的 r.pop("sort_key") 会改到列表里的那个 dict，
            # 若缓存同一对象，第二次命中就会因缺 sort_key 而 KeyError。
            _SCAN_CACHE[key] = (fp, dict(entry))
        runs.append(entry)
    # 清掉已消失的 run，避免缓存无限增长
    if use_cache:
        for gone in set(_SCAN_CACHE) - seen:
            _SCAN_CACHE.pop(gone, None)
    # 排序：正常 run 按时间倒序（新的在前）；"数据缺失"（残缺 run）沉底。
    # 稳定排序两次：先按时间倒序，再按 missing 升序分组（False 在前，组内顺序保持）。
    # sort_key 已在 _scan_one 里统一为 epoch 秒（float），此处可直接比较。
    runs.sort(key=lambda r: r["sort_key"], reverse=True)
    runs.sort(key=lambda r: r["status"] == "missing")
    for r in runs:
        r.pop("sort_key", None)
    return runs
