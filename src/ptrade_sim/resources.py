"""系统资源探查与回测准入决策。

零新依赖：内存/CPU 用标准库探测（``ctypes``/``os``/``/proc``）；
装了 ``psutil`` 则用其更精确的 CPU 占用，否则回退为「本进程视角」的估算。

用途：回测启动前判断当前机器是否吃得下，吃不下就排队而不是硬上
（硬上的后果是内存耗尽/OOM 或 CPU 争抢导致整体更慢）。
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# ============================================================
# 内存探测（标准库）
# ============================================================


def _mem_windows() -> tuple[int, int]:
    """返回 (total_bytes, available_bytes)。"""

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    st = MEMORYSTATUSEX()
    st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
        raise OSError("GlobalMemoryStatusEx 失败")
    return int(st.ullTotalPhys), int(st.ullAvailPhys)


def _mem_linux() -> tuple[int, int]:
    total = avail = 0
    with Path("/proc/meminfo").open(encoding="utf-8") as f:
        for line in f:
            if line.startswith("MemTotal:"):
                total = int(line.split()[1]) * 1024
            elif line.startswith("MemAvailable:"):
                avail = int(line.split()[1]) * 1024
            if total and avail:
                break
    if not total:
        raise OSError("无法解析 /proc/meminfo")
    return total, avail or total


def _mem_macos() -> tuple[int, int]:
    total = int(os.popen("sysctl -n hw.memsize").read().strip() or 0)
    if not total:
        raise OSError("sysctl hw.memsize 失败")
    # macOS 无直接 available；用 vm_stat 的 free+inactive 近似
    try:
        out = os.popen("vm_stat").read()
        page = 4096
        free = inactive = 0
        for line in out.splitlines():
            if "page size of" in line:
                page = int(line.split("page size of")[1].split()[0])
            elif line.startswith("Pages free:"):
                free = int(line.split(":")[1].strip().rstrip("."))
            elif line.startswith("Pages inactive:"):
                inactive = int(line.split(":")[1].strip().rstrip("."))
        avail = (free + inactive) * page
        return total, avail or total // 2
    except Exception:
        return total, total // 2


def probe_memory() -> tuple[int, int]:
    """(total_bytes, available_bytes)；探测失败返回 (0, 0)。"""
    try:
        if sys.platform.startswith("win"):
            return _mem_windows()
        if sys.platform == "darwin":
            return _mem_macos()
        return _mem_linux()
    except Exception:
        pass
    try:  # 有 psutil 就用它兜底
        import psutil

        vm = psutil.virtual_memory()
        return int(vm.total), int(vm.available)
    except Exception:
        return 0, 0


# ============================================================
# 资源快照
# ============================================================


@dataclass
class ResourceSnapshot:
    """某一时刻的机器资源视图。"""

    cpu_count: int
    mem_total: int
    mem_available: int
    load_avg: float | None = None  # 仅 POSIX；Windows 为 None
    psutil_cpu_pct: float | None = None  # 装了 psutil 才有
    disk_free: int = 0
    taken_at: float = field(default_factory=time.time)

    @property
    def mem_used_pct(self) -> float:
        if not self.mem_total:
            return 0.0
        return 100.0 * (1 - self.mem_available / self.mem_total)

    def describe(self) -> str:
        mb = 1024 * 1024
        s = (
            f"CPU {self.cpu_count} 核"
            f"{f'（系统占用 {self.psutil_cpu_pct:.0f}%）' if self.psutil_cpu_pct is not None else ''}"
            f" | 内存可用 {self.mem_available / mb:,.0f} / {self.mem_total / mb:,.0f} MB"
            f"（已用 {self.mem_used_pct:.0f}%）"
        )
        if self.disk_free:
            s += f" | 磁盘可用 {self.disk_free / 1024**3:.0f} GB"
        return s


def probe(disk_path: str | Path | None = None) -> ResourceSnapshot:
    """采集当前资源快照。"""
    import shutil

    total, avail = probe_memory()
    cpu = os.cpu_count() or 1
    load = None
    try:
        if hasattr(os, "getloadavg"):
            load = float(os.getloadavg()[0])
    except Exception:
        load = None
    cpu_pct = None
    try:
        import psutil

        cpu_pct = float(psutil.cpu_percent(interval=0.1))
    except Exception:
        cpu_pct = None
    disk = 0
    if disk_path:
        try:
            disk = shutil.disk_usage(str(disk_path)).free
        except Exception:
            disk = 0
    return ResourceSnapshot(
        cpu_count=cpu,
        mem_total=total,
        mem_available=avail,
        load_avg=load,
        psutil_cpu_pct=cpu_pct,
        disk_free=disk,
    )


# ============================================================
# 回测资源估算
# ============================================================


@dataclass
class BacktestCost:
    """一次回测的资源需求估算。"""

    days: int
    threads: int
    est_mem_bytes: int
    est_cpu_slots: int
    preload_mode: str = "rolling"
    minute_budget_bytes: int = 0
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        mb = 1024 * 1024
        return (
            f"{self.days} 个交易日 / {self.threads} 线程"
            f" | 预计内存 {self.est_mem_bytes / mb:,.0f} MB"
            f" | 占用 CPU 槽 {self.est_cpu_slots}"
            f" | preload={self.preload_mode}"
        )


#: 单日全市场分钟数据的内存占用估算（实测约 40MB/天，见设计文档 §5.2）
BYTES_PER_MINUTE_DAY = 40 * 1024 * 1024
#: 固定开销（基础表 + 日线/估值缓存 + Python 运行时）
BASE_OVERHEAD_BYTES = 700 * 1024 * 1024


def estimate_cost(
    days: int,
    threads: int = 8,
    preload_mode: str = "rolling",
    rolling_window: int = 10,
    minute_budget_bytes: int = 0,
) -> BacktestCost:
    """估算一次回测的内存与 CPU 占用。

    - ``preload_mode='all'``：需容纳区间内**全部**交易日的分钟数据（危险，长区间会爆）。
    - ``rolling``（流式，默认）：只需 ``rolling_window`` 天常驻 + 求解期开销。
    """
    notes = []
    if preload_mode == "all":
        cached_days = max(1, days)
        notes.append(f"preload=all 将常驻 {cached_days} 天分钟数据")
    else:
        cached_days = max(1, min(days, max(1, rolling_window)))
    minute_mem = cached_days * BYTES_PER_MINUTE_DAY
    if minute_budget_bytes:
        minute_mem = min(minute_mem, minute_budget_bytes)

    # 策略侧：持仓/中间 DataFrame/指标缓存，按天数温和增长
    strategy_mem = 200 * 1024 * 1024 + days * 4 * 1024 * 1024
    est = BASE_OVERHEAD_BYTES + minute_mem + strategy_mem

    # CPU 槽：线程数是主要占用；至少占 1 槽。
    # ⚠️ 必须**封顶到 cpu_count-1**：否则当用户把 threads 设得比核数还大时，
    # 单任务需求就超过总容量，准入判定 `used + need > cap` 恒成立 → 永远排不上队。
    cpu = os.cpu_count() or 1
    slots = max(1, min(int(threads), max(1, cpu - 1)))
    return BacktestCost(
        days=days,
        threads=int(threads),
        est_mem_bytes=int(est),
        est_cpu_slots=int(slots),
        preload_mode=preload_mode,
        minute_budget_bytes=int(minute_mem),
        notes=notes,
    )


# ============================================================
# 准入决策
# ============================================================


@dataclass
class Admission:
    """准入结论。"""

    ok: bool
    reason: str = ""
    detail: str = ""
    wait_hint_sec: float = 0.0


def decide(
    cost: BacktestCost,
    snap: ResourceSnapshot,
    used_slots: int = 0,
    used_count: int = 0,
    max_parallel: int | None = None,
    mem_reserve_ratio: float = 0.15,
    max_slots: int | None = None,
) -> Admission:
    """判断当前能否开跑；不能则给出排队原因。

    同时受**两个独立维度**约束（任一不满足即排队）：

    1. **并发数**：``used_count + 1 <= max_parallel``
       （``max_parallel`` 默认 = ``cpu_count // 2``，至少 1；也可由 config 指定）
    2. **CPU 槽**：``used_slots + cost.est_cpu_slots <= max_slots``
       （``max_slots`` 默认 = ``cpu_count - 1``，留一核给系统）
    3. **内存**：可用内存扣除预留（默认 15%）后仍能容纳 ``est_mem_bytes``
    4. **磁盘**：结果目录所在盘可用 > 2GB

    分开计数很重要：并发上限是「几个回测」，CPU 槽是「总共几个线程」。
    若把二者混为一个数，单任务线程数一超过上限就会永久排队。
    """
    mb = 1024 * 1024
    total_slots = max_slots if max_slots is not None else max(1, snap.cpu_count - 1)
    par = max_parallel if max_parallel else max(1, snap.cpu_count // 2)

    # 1) 并发数
    if used_count + 1 > par:
        return Admission(
            False,
            "parallel",
            f"并发已达上限：正在运行 {used_count} 个，上限 {par} 个",
            wait_hint_sec=20.0,
        )

    # 2) CPU 槽
    if used_slots + cost.est_cpu_slots > total_slots:
        return Admission(
            False,
            "cpu",
            f"CPU 槽不足：已占 {used_slots} + 本次 {cost.est_cpu_slots} > 上限 {total_slots}"
            f"（{snap.cpu_count} 核留 1 核给系统）",
            wait_hint_sec=20.0,
        )

    # 3) 内存
    if snap.mem_total:
        reserve = int(snap.mem_total * mem_reserve_ratio)
        usable = snap.mem_available - reserve
        if usable < cost.est_mem_bytes:
            return Admission(
                False,
                "memory",
                f"可用内存 {usable / mb:,.0f} MB（已扣 {mem_reserve_ratio:.0%} 预留）"
                f" < 本次预计需求 {cost.est_mem_bytes / mb:,.0f} MB",
                wait_hint_sec=30.0,
            )

    # 4) 磁盘
    if snap.disk_free and snap.disk_free < 2 * 1024**3:
        return Admission(
            False,
            "disk",
            f"磁盘可用仅 {snap.disk_free / 1024**3:.1f} GB（< 2 GB）",
            wait_hint_sec=60.0,
        )

    return Admission(True, "ok", "资源充足")


def suggest_threads(snap: ResourceSnapshot, prefer: int = 8) -> int:
    """按当前 CPU 核数建议线程数（留 1 核给系统）。"""
    return max(1, min(int(prefer), max(1, snap.cpu_count - 1)))
