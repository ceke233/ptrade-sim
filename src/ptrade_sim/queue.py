"""资源感知的回测队列：资源不足时排队，释放后自动开跑。

设计目标
--------
多个回测（可能来自不同终端/脚本/并行调参）同时提交时，**不硬上**——
硬上的后果是内存耗尽（OOM 被杀）或 CPU 争抢（每个都变慢）。
本模块用**跨进程注册表**协调：每台机器上同时运行的回测受 CPU 槽与内存双重约束。

工作机制
--------
1. 提交回测前采集资源快照（:mod:`ptrade_sim.resources`）并估算本次开销；
2. 在 ``<queue_dir>/running.json`` 注册表中登记「我要跑，占 N 槽 + M 内存」；
3. 注册表显示资源不够 → **排队等待**，定期重查（其他回测结束会释放槽位）；
4. 拿不到就等，直到超过 ``max_wait_sec``（默认 1 小时）则放弃并给出提示；
5. 进程退出（含异常/Ctrl-C）时用 ``atexit`` 注销，避免僵尸占用槽位。

跨进程安全
----------
用 ``O_CREAT|O_EXCL`` 原子创建 ``.lock`` 文件实现轻量互斥（避免依赖 fcntl/msvcrt），
临界区内完成"读-改-写"注册表。锁带超时与陈旧锁回收，防止持锁进程崩溃后死锁。

驱动清理
--------
注册表项记录 pid；读取时若 pid 已不存在（``os.kill(pid,0)`` 失败），
判定为陈旧项并剔除 —— 这是"自动管理策略运行"的关键：崩溃的回测不会永久占位。
"""

from __future__ import annotations

import atexit
import json
import os
import socket
import sys
import time
import uuid
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from ptrade_sim import resources
from ptrade_sim.exceptions import QueueTimeoutError

# ============================================================
# 进程存活判断
# ============================================================


def pid_alive(pid: int) -> bool:
    """判断 pid 是否仍在运行（跨平台）。"""
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        if sys.platform.startswith("win"):
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
                return bool(ok) and code.value == STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(h)
        os.kill(pid, 0)
        return True
    except Exception:
        return False


# ============================================================
# 跨进程锁（O_EXCL）
# ============================================================


@contextmanager
def _file_lock(lock_path: Path, timeout: float = 30.0, stale_sec: float = 60.0):
    """轻量文件锁：原子创建 + 陈旧回收。"""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    fd = None
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            break
        except FileExistsError as exc:
            # 陈旧锁回收（持锁进程已死或超时）
            try:
                if time.time() - lock_path.stat().st_mtime > stale_sec:
                    lock_path.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.time() > deadline:
                raise QueueTimeoutError(f"获取队列锁超时：{lock_path}") from exc
            time.sleep(0.05)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
        lock_path.unlink(missing_ok=True)


# ============================================================
# 注册表
# ============================================================


@dataclass
class QueueEntry:
    task_id: str
    pid: int
    host: str
    strategy: str
    slots: int
    mem_bytes: int
    submitted_at: float
    started_at: float = 0.0
    state: str = "queued"  # queued | running
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "pid": self.pid,
            "host": self.host,
            "strategy": self.strategy,
            "slots": self.slots,
            "mem_bytes": self.mem_bytes,
            "submitted_at": self.submitted_at,
            "started_at": self.started_at,
            "state": self.state,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, d: dict) -> QueueEntry:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})


class BacktestQueue:
    """跨进程的回测准入协调器。

    ``queue_dir`` 由 :func:`queue_dir_for` 决定：显式 override > 机器级默认
    （``~/.ptrade-sim/queue``）。**刻意不随结果目录走** —— 争抢的是机器资源，
    若队列目录随 ``--output-dir`` 变化，不同输出目录的回测就各有各的队列，
    完全无法协调。
    """

    def __init__(
        self,
        queue_dir: str | Path,
        enabled: bool = True,
        max_parallel: int | None = None,
        cpu_slots_limit: int | None = None,
        poll_interval: float = 10.0,
        max_wait_sec: float | None = 3600.0,
        mem_reserve_ratio: float = 0.15,
    ):
        self.dir = Path(queue_dir)
        self.enabled = bool(enabled)
        self.poll_interval = max(1.0, float(poll_interval))
        # 排队超时语义（三态，**不要用 0 表示无限**——那会与「完全不等待」撞车，
        # 早期版本因此让 --no-wait 变成永久挂起）：
        #   None  → 无限等待
        #   0     → 完全不等待（试一次，拿不到就放弃）
        #   >0    → 最多等这么多秒
        self.max_wait_sec = None if max_wait_sec is None else max(0.0, float(max_wait_sec))
        self.mem_reserve_ratio = float(mem_reserve_ratio)
        cpu = os.cpu_count() or 1
        # 并发数（几个回测）与 CPU 槽（总共几个线程）是两个独立维度，
        # 混为一谈会导致单任务线程数超上限后永久排队。
        self.max_parallel = int(max_parallel) if max_parallel else max(1, cpu // 2)
        self.cpu_slots_limit = int(cpu_slots_limit) if cpu_slots_limit else max(1, cpu - 1)
        self.registry = self.dir / "running.json"
        self._lock = self.dir / "queue.lock"
        self._task_id: str | None = None
        self._registered = False
        atexit.register(self.release)

    # ---------- 注册表读写 ----------
    def _read(self) -> list[QueueEntry]:
        if not self.registry.exists():
            return []
        try:
            data = json.loads(self.registry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        items = data.get("entries", []) if isinstance(data, dict) else []
        out: list[QueueEntry] = []
        for d in items:
            try:
                out.append(QueueEntry.from_dict(d))
            except Exception:
                continue
        return out

    def _write(self, entries: list[QueueEntry]) -> None:
        self.registry.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": time.time(),
            "host": socket.gethostname(),
            "entries": [e.as_dict() for e in entries],
        }
        tmp = self.registry.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.registry)

    def _alive(self, entries: list[QueueEntry]) -> tuple[list[QueueEntry], list[QueueEntry]]:
        """分离存活项与陈旧项（pid 已死）。"""
        alive, dead = [], []
        host = socket.gethostname()
        for e in entries:
            # 其他主机的项不做 pid 判断（共享盘场景保守保留）
            if e.host != host or pid_alive(e.pid):
                alive.append(e)
            else:
                dead.append(e)
        return alive, dead

    def snapshot(self, prune: bool = True) -> dict[str, Any]:
        """当前队列/运行态（读操作，必要时清理陈旧项）。"""
        with _file_lock(self._lock):
            entries = self._read()
            alive, dead = self._alive(entries)
            if dead and prune:
                self._write(alive)
        running = [e for e in alive if e.state == "running"]
        queued = [e for e in alive if e.state == "queued"]
        return {
            "running": [e.as_dict() for e in running],
            "queued": [e.as_dict() for e in queued],
            "dead_pruned": [e.as_dict() for e in dead],
            "used_slots": sum(e.slots for e in running),
            "used_count": len(running),
            "used_mem_bytes": sum(e.mem_bytes for e in running),
            "max_parallel": self.max_parallel,
            "cpu_slots_limit": self.cpu_slots_limit,
        }

    # ---------- 准入 ----------
    def acquire(
        self,
        strategy: str,
        slots: int,
        mem_bytes: int,
        probe_path: str | None = None,
        on_wait: Any = None,
    ) -> bool:
        """申请运行许可；返回 True 表示已获批并登记为 running。

        ``on_wait(waited_sec, reason, detail, position)`` 每次轮询时回调，
        用于向用户输出"排队中"的进度。
        """
        self._task_id = uuid.uuid4().hex[:12]
        if not self.enabled:
            logger.debug("队列已禁用（queue.enabled=false），直接开跑")
            return True

        t0 = time.time()
        host = socket.gethostname()
        warned_reasons: set[str] = set()

        while True:
            snap = resources.probe(probe_path)
            with _file_lock(self._lock):
                entries = self._read()
                alive, _dead = self._alive(entries)
                running = [e for e in alive if e.state == "running"]
                used_slots = sum(e.slots for e in running)
                used_count = len(running)
                # 自己的占位（重试时先移除旧的同 task_id 项）
                alive = [e for e in alive if e.task_id != self._task_id]
                queued = [e for e in alive if e.state == "queued"]

                cost = resources.BacktestCost(
                    days=0,
                    threads=slots,
                    est_mem_bytes=mem_bytes,
                    est_cpu_slots=slots,
                )
                adm = resources.decide(
                    cost,
                    snap,
                    used_slots=used_slots,
                    used_count=used_count,
                    max_parallel=self.max_parallel,
                    mem_reserve_ratio=self.mem_reserve_ratio,
                    max_slots=self.cpu_slots_limit,
                )
                if adm.ok:
                    me = QueueEntry(
                        task_id=self._task_id,
                        pid=os.getpid(),
                        host=host,
                        strategy=strategy,
                        slots=slots,
                        mem_bytes=mem_bytes,
                        submitted_at=t0,
                        started_at=time.time(),
                        state="running",
                    )
                    self._write([*alive, me])
                    self._registered = True
                    waited = time.time() - t0
                    if waited > 1:
                        logger.info(
                            f"排队 {waited:.0f}s 后获得资源，开始回测"
                            f"（占 {slots} 槽 / {mem_bytes / 1048576:,.0f} MB）"
                        )
                    return True
                # 未获批：登记为 queued（让其他人看到我在等）
                me = QueueEntry(
                    task_id=self._task_id,
                    pid=os.getpid(),
                    host=host,
                    strategy=strategy,
                    slots=slots,
                    mem_bytes=mem_bytes,
                    submitted_at=t0,
                    state="queued",
                    note=f"{adm.reason}:{adm.detail}",
                )
                self._write([*alive, me])  # 陈旧项（dead）已在上面被剔除
                position = len(queued) + 1

            waited = time.time() - t0
            if self.max_wait_sec is not None and waited > self.max_wait_sec:
                logger.warning(
                    f"排队超过 {self.max_wait_sec:.0f}s 仍未获得资源，放弃等待"
                    f"（原因：{adm.detail}）。可调大 queue.max_wait_sec 或减小 threads。"
                )
                self.release()
                return False

            if adm.reason not in warned_reasons:
                warned_reasons.add(adm.reason)
                logger.warning(f"资源不足，进入等待队列：{adm.detail}")
            if on_wait:
                with suppress(Exception):
                    on_wait(waited, adm.reason, adm.detail, position)
            time.sleep(min(self.poll_interval, max(1.0, adm.wait_hint_sec or 5.0)))

    # ---------- 注销 ----------
    def release(self) -> None:
        """注销自己（幂等；由 atexit 兜底调用）。"""
        if not self._registered or not self._task_id:
            return
        try:
            with _file_lock(self._lock, timeout=5.0):
                entries = self._read()
                self._write([e for e in entries if e.task_id != self._task_id])
        except Exception as exc:
            logger.debug(f"队列注销失败（忽略）：{exc}")
        finally:
            self._registered = False

    # ---------- 展示 ----------
    def describe(self) -> str:
        s = self.snapshot()
        lines = [
            f"队列目录：{self.dir}",
            f"并发上限：{s['max_parallel']} 个回测"
            f"（当前 {s['used_count']} 个） | "
            f"CPU 槽上限 {s['cpu_slots_limit']}"
            f"（当前 {s['used_slots']} 槽 / {s['used_mem_bytes'] / 1048576:,.0f} MB）",
        ]
        for e in s["running"]:
            lines.append(
                f"  [运行] {e['strategy']:<28} pid={e['pid']} "
                f"{e['slots']} 槽 {e['mem_bytes'] / 1048576:,.0f} MB"
            )
        for i, e in enumerate(s["queued"], 1):
            lines.append(f"  [排队{i}] {e['strategy']:<28} pid={e['pid']} {e['note']}")
        if not s["running"] and not s["queued"]:
            lines.append("  （空闲）")
        return "\n".join(lines)


def default_queue_dir() -> Path:
    """机器级队列目录（默认）。

    刻意**不放在结果目录下** —— 争抢的是机器资源，若队列目录随 ``--output-dir``
    变化，不同输出目录的回测就各有各的队列，完全无法协调。
    """
    return Path.home() / ".ptrade-sim" / "queue"


def queue_dir_for(
    results_dir: str | Path | None = None, override: str | Path | None = None
) -> Path:
    """队列目录：显式 override > 机器级默认；``results_dir`` 仅作兼容保留。"""
    if override:
        return Path(override)
    return default_queue_dir()
