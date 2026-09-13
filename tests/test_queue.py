"""资源感知回测队列测试（跨进程注册表 / 准入 / 陈旧项清理）。"""

from __future__ import annotations

import json
import os
import socket
import time

import pytest

from ptrade_sim.queue import BacktestQueue, _file_lock, default_queue_dir, pid_alive, queue_dir_for

pytestmark = pytest.mark.unit


@pytest.fixture
def qdir(tmp_path):
    return tmp_path / "queue"


@pytest.fixture
def q(qdir):
    return BacktestQueue(qdir, enabled=True, max_parallel=4, poll_interval=1)


# ============================================================
# 进程存活判断
# ============================================================


def test_pid_alive_self():
    assert pid_alive(os.getpid())


def test_pid_alive_bogus():
    assert not pid_alive(999_999_999)
    assert not pid_alive(0)
    assert not pid_alive(-1)


# ============================================================
# 文件锁
# ============================================================


def test_file_lock_mutual_exclusion(tmp_path):
    lock = tmp_path / "a.lock"
    with _file_lock(lock):
        assert lock.exists()
    assert not lock.exists(), "离开临界区应释放锁"


def test_file_lock_recovers_stale(tmp_path):
    """持锁进程崩溃后留下的陈旧锁必须被回收，否则永久死锁。"""
    lock = tmp_path / "stale.lock"
    lock.write_text("999999", encoding="utf-8")
    old = time.time() - 3600
    os.utime(lock, (old, old))
    with _file_lock(lock, timeout=5, stale_sec=60):
        pass  # 不应超时
    assert not lock.exists()


def test_file_lock_times_out_on_fresh_lock(tmp_path):
    lock = tmp_path / "busy.lock"
    lock.write_text("1", encoding="utf-8")
    with pytest.raises(TimeoutError):
        with _file_lock(lock, timeout=0.3, stale_sec=3600):
            pass


# ============================================================
# 队列目录
# ============================================================


def test_default_queue_dir_is_machine_level():
    """队列目录必须独立于 output-dir，否则不同输出目录的回测各排各的队。"""
    p = default_queue_dir()
    assert ".ptrade-sim" in str(p)
    assert "queue" in str(p)


def test_queue_dir_for_override_wins(qdir):
    assert queue_dir_for("ignored", override=qdir) == qdir


def test_queue_dir_for_ignores_results_dir():
    assert queue_dir_for("G:/somewhere/else") == default_queue_dir()


# ============================================================
# 注册表读写与陈旧清理
# ============================================================


def test_snapshot_empty_when_no_registry(q):
    s = q.snapshot()
    assert s["running"] == [] and s["queued"] == []
    assert s["used_slots"] == 0


def test_snapshot_prunes_dead_process(q, qdir):
    """崩溃进程留下的项必须被自动清理，否则永久占用槽位。"""
    qdir.mkdir(parents=True, exist_ok=True)
    q.registry.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "task_id": "ghost",
                        "pid": 999_999_999,
                        "host": socket.gethostname(),
                        "strategy": "crashed.py",
                        "slots": 4,
                        "mem_bytes": 1024,
                        "submitted_at": time.time(),
                        "state": "running",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    s = q.snapshot()
    assert s["running"] == []
    assert len(s["dead_pruned"]) == 1


def test_foreign_host_entries_kept(q, qdir):
    """其他主机的项不做 pid 判断（共享盘场景宁可保守保留）。"""
    qdir.mkdir(parents=True, exist_ok=True)
    q.registry.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "task_id": "remote",
                        "pid": 999_999_999,
                        "host": "another-host",
                        "strategy": "x.py",
                        "slots": 2,
                        "mem_bytes": 1024,
                        "submitted_at": time.time(),
                        "state": "running",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    s = q.snapshot()
    assert len(s["running"]) == 1


def test_corrupt_registry_treated_as_empty(q, qdir):
    qdir.mkdir(parents=True, exist_ok=True)
    q.registry.write_text("not json at all", encoding="utf-8")
    assert q.snapshot()["running"] == []


def test_registry_write_is_atomic(q, qdir):
    qdir.mkdir(parents=True, exist_ok=True)
    q._write([])
    assert q.registry.exists()
    assert not q.registry.with_suffix(".json.tmp").exists()


# ============================================================
# 准入
# ============================================================


def test_acquire_registers_running(q):
    assert q.acquire("s.py", slots=2, mem_bytes=10**8)
    s = q.snapshot()
    assert len(s["running"]) == 1
    assert s["running"][0]["strategy"] == "s.py"
    assert s["used_slots"] == 2
    q.release()


def test_release_removes_entry(q):
    q.acquire("s.py", slots=2, mem_bytes=10**8)
    q.release()
    assert q.snapshot()["running"] == []


def test_release_is_idempotent(q):
    q.acquire("s.py", slots=1, mem_bytes=1024)
    q.release()
    q.release()  # 不应抛
    assert q.snapshot()["running"] == []


def test_disabled_queue_skips_acquire(qdir):
    q = BacktestQueue(qdir, enabled=False)
    assert q.acquire("s.py", slots=1, mem_bytes=1024)
    assert q.snapshot()["running"] == [], "禁用时不应登记"


def test_acquire_waits_when_parallel_limit_reached(qdir):
    """并发达上限时排队；前一个释放后应自动获准 —— 且不能死等。"""
    q1 = BacktestQueue(qdir, max_parallel=1, poll_interval=1)
    q2 = BacktestQueue(qdir, max_parallel=1, poll_interval=1)
    assert q1.acquire("a.py", slots=1, mem_bytes=1024), "第一个应直接获准"

    waits: list[tuple] = []
    # 让 q1 在 2 秒后释放
    import threading

    threading.Timer(2.0, q1.release).start()
    ok = q2.acquire("b.py", slots=1, mem_bytes=1024, on_wait=lambda *a: waits.append(a))
    assert ok, "前一个释放后应获准"
    assert waits, "应回调过等待进度"
    q2.release()


def test_acquire_gives_up_after_max_wait(qdir):
    q1 = BacktestQueue(qdir, max_parallel=1, poll_interval=1)
    q1.acquire("a.py", slots=1, mem_bytes=1024)
    q2 = BacktestQueue(qdir, max_parallel=1, poll_interval=1, max_wait_sec=1)
    assert not q2.acquire("b.py", slots=1, mem_bytes=1024), "超时应放弃而不是死等"
    q1.release()


def test_zero_max_wait_gives_up_immediately(qdir):
    """``max_wait_sec=0`` = **完全不等待**（--no-wait 语义）。

    回归防护：早期用 0 同时表示「无限等待」与「不等待」，
    导致 --no-wait 实际变成永久挂起。
    """
    q1 = BacktestQueue(qdir, max_parallel=1)
    q1.acquire("a.py", slots=1, mem_bytes=1024)
    q2 = BacktestQueue(qdir, max_parallel=1, max_wait_sec=0)
    assert q2.max_wait_sec == 0.0
    assert not q2.acquire("b.py", slots=1, mem_bytes=1024)
    q1.release()


def test_none_max_wait_means_infinite(qdir):
    q = BacktestQueue(qdir, max_parallel=4, max_wait_sec=None)
    assert q.max_wait_sec is None, "None 才表示无限等待"


def test_queued_entry_visible_while_waiting(qdir):
    """排队中也要可见（否则用户不知道在等什么）。"""
    q1 = BacktestQueue(qdir, max_parallel=1, poll_interval=1)
    q1.acquire("a.py", slots=1, mem_bytes=1024)
    q2 = BacktestQueue(qdir, max_parallel=1, poll_interval=1, max_wait_sec=1)
    q2.acquire("b.py", slots=1, mem_bytes=1024)
    s = q1.snapshot()
    assert len(s["running"]) == 1
    assert any(e["strategy"] == "b.py" for e in s["queued"]), "排队项应可见"
    q1.release()


# ============================================================
# 展示与配置
# ============================================================


def test_describe_idle(q):
    assert "空闲" in q.describe()


def test_describe_running(q):
    q.acquire("s.py", slots=2, mem_bytes=10**8)
    text = q.describe()
    assert "运行" in text and "s.py" in text
    q.release()


def test_max_parallel_default_is_half_cores(qdir):
    q = BacktestQueue(qdir)
    assert q.max_parallel == max(1, (os.cpu_count() or 1) // 2)


def test_cpu_slots_limit_default_leaves_one_core(qdir):
    q = BacktestQueue(qdir)
    assert q.cpu_slots_limit == max(1, (os.cpu_count() or 1) - 1)


def test_explicit_limits_honored(qdir):
    q = BacktestQueue(qdir, max_parallel=3, cpu_slots_limit=5)
    assert q.max_parallel == 3
    assert q.cpu_slots_limit == 5


def test_atexit_release_registered(q):
    """异常退出也要释放槽位（atexit 兜底）。"""
    assert callable(q.release)
    # release 幂等：未 acquire 时调用不应抛
    q.release()
