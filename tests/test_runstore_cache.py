"""``runstore.scan_runs`` 的指纹缓存（``_SCAN_CACHE``）测试。

``scan_runs`` 是看板 ``/api/runs`` 的实现，前端**每 3 秒**轮询一次，所以它按
「run 目录 mtime 指纹」缓存解析结果（实测 124 个 run：98ms → 11ms）。这条路径
此前**零测试**，而它既是性能机制也直接决定正确性 —— 缓存的**键**、**指纹覆盖面**、
**失效**、以及与调用方**共享可变对象**，任何一处错了都会让看板显示**过期或串台**
的数据（"B 项目显示了 A 项目的收益"这类现象与"缓存"毫无字面关联，极难定位）。

因此本文件的断言重点不是「结果对不对」—— 缓存完全失效时结果照样对 —— 而是
**「这一次到底有没有命中缓存 / 该失效时有没有失效」**。手段是给
``runstore._scan_one`` 装计数器，逐个断言"这次解析了哪几个 run"。

已知缺陷（**故意不写成断言**，只在交付报告里列出，因为按现状断言会让测试变红）：
``status`` 是**随墙上时钟变化**的字段（``running`` 超过 ``STALE_SEC`` 应转为
``interrupted``），却被缓存在纯 mtime 指纹下。回测进程挂掉后不再写 ``progress.json``，
指纹永不变化 → 看板**永久**显示 ``running``，而同一时刻 ``use_cache=False`` 已经
能算出 ``interrupted``。
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path

import pytest

from ptrade_sim import runstore

pytestmark = pytest.mark.unit


#: daily_stats.csv 列与引擎实际写出的对齐（服务端按这些列现算指标）。
_CSV_HEADER = (
    "date,total_value,cash,positions_value,benchmark_close,"
    "daily_return,cum_return,drawdown,trades_count,commission\n"
)

#: 把两个 root 下的同名 run 伪装成「指纹逐位相同」用的固定 mtime（2025-01-01 UTC）。
#: ``status == "done"`` 的 run 不看 progress.json 的 mtime，故用固定值不会触发陈旧判定。
_FIXED_NS = 1_735_689_600_000_000_000

_FINGERPRINT_FILES = ("progress.json", "daily_stats.csv", "summary.json")


# ============================================================
# 造数据 / 计数 / 隔离
# ============================================================


def _csv_text(last_total_value: float) -> str:
    return (
        _CSV_HEADER
        + "2025-01-02,1000000,1000000,0,4000.0,0.0,0.0,0.0,0,0.0\n"
        + f"2025-01-03,{last_total_value},900000,110000,4010.0,0.01,0.01,-0.005,2,6.1\n"
    )


def _write_progress(
    d: Path,
    *,
    status: str = "done",
    phase: str = "day_5",
    day_done: int = 5,
) -> None:
    (d / "progress.json").write_text(
        json.dumps(
            {
                "phase": phase,
                "status": status,
                "day_done": day_done,
                "total_days": 5,
                "started_at": "2025-01-02T09:30:00",
                "strategy_name": "演示策略",
                "start_date": "2025-01-02",
                "end_date": "2025-01-08",
                "capital_base": 1_000_000.0,
                "benchmark": "000300.SS",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _make_run(
    root: Path,
    name: str,
    *,
    status: str = "done",
    with_progress: bool = True,
    with_summary: bool = True,
    with_csv: bool = True,
    phase: str = "day_5",
    day_done: int = 5,
    total_return: float = 0.01,
    last_total_value: float = 1_010_000.0,
) -> Path:
    """造一个最小可解析的 run 目录（目录名 ``策略-YYYYMMDD_HHMMSS``）。"""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    if with_progress:
        _write_progress(d, status=status, phase=phase, day_done=day_done)
    if with_summary:
        (d / "summary.json").write_text(
            json.dumps({"total_return": total_return, "config": {}}, ensure_ascii=False),
            encoding="utf-8",
        )
    if with_csv:
        (d / "daily_stats.csv").write_text(_csv_text(last_total_value), encoding="utf-8")
    return d


def _pin_fingerprint(run: Path, base_ns: int = _FIXED_NS) -> None:
    """把三个指纹文件的 mtime 钉死，使不同目录的 run 拥有**逐位相同**的指纹。

    不钉死的话"缓存键漏了根目录"这个缺陷会被指纹不匹配掩盖成"侥幸正确"
    （实现照样重解析，结果碰巧对），测试就白写了。
    """
    for i, fn in enumerate(_FINGERPRINT_FILES):
        p = run / fn
        if p.exists():
            os.utime(p, ns=(base_ns + i, base_ns + i))


def _bump(path: Path, delta_ns: int = 10_000_000_000) -> None:
    """把 mtime 往后推 10 秒。

    显式推 mtime 而不是"写两次内容"：文件系统时间戳的更新粒度可能粗到几十毫秒，
    同一 tick 内的两次写会得到同一个 ``st_mtime_ns``，指纹不变得出假阴/假阳。
    """
    st = path.stat()
    os.utime(path, ns=(st.st_mtime_ns + delta_ns, st.st_mtime_ns + delta_ns))


def _spy_scan_one(monkeypatch) -> list[str]:
    """给 ``runstore._scan_one`` 装计数器，返回「本次被解析的 run 名」列表。

    ``scan_runs`` 是按模块全局名调用它的，替换模块属性即可生效。
    这是本文件判断"有没有命中缓存"的唯一手段：只比较结果相等是无效的，
    缓存全失效（每次重解析）时结果同样相等。
    """
    calls: list[str] = []
    real = runstore._scan_one

    def spy(d: Path, name: str) -> dict:
        calls.append(name)
        return real(d, name)

    monkeypatch.setattr(runstore, "_scan_one", spy)
    return calls


def _entry(runs: list[dict], name: str) -> dict:
    hit = [r for r in runs if r["name"] == name]
    assert len(hit) == 1, f"结果里应当恰好有一个 {name}，实际是 {[r['name'] for r in runs]}"
    return hit[0]


def _scan_expecting_hit(root: Path, calls: list[str]) -> list[dict]:
    """再扫一次并要求**命中缓存**。

    前提校验：如果这一步就重新解析了，说明缓存本来就没生效，后面任何"失效"断言
    都是假绿。
    """
    calls.clear()
    runs = runstore.scan_runs(root)
    assert calls == [], f"第二次扫描应当命中缓存（不重新解析），实际解析了 {calls}"
    return runs


@pytest.fixture(autouse=True)
def _isolated_scan_cache():
    """``_SCAN_CACHE`` 是模块级全局：不隔离会让测试之间互相命中/串台。"""
    runstore._SCAN_CACHE.clear()
    yield
    runstore._SCAN_CACHE.clear()


# ============================================================
# 1. 命中缓存
# ============================================================


def test_cache_hit_skips_reparse_and_invalidates_per_run(tmp_path, monkeypatch):
    """命中时不得重新解析；失效粒度是**单个 run**，不是整表。

    防的场景：
    - 缓存根本没生效（键算错、写后即删）→ 看板每 3 秒轮询都重复读 JSON/CSV，
      9 倍加速归零，且只有在 run 多到卡顿时才被发现；
    - 反过来，任何一个 run 的进度变化都清空整张缓存 → 运行中的 run 每 3 秒把
      全部 N 个 run 重解析一遍，同样失去意义。
    """
    root = tmp_path / "results"
    names = ["a-20250101_120000", "b-20250102_120000", "c-20250103_120000"]
    for n in names:
        _make_run(root, n)
    calls = _spy_scan_one(monkeypatch)

    first = runstore.scan_runs(root)
    assert sorted(calls) == names, f"首次扫描必须逐个解析，实际解析了 {sorted(calls)}"

    second = _scan_expecting_hit(root, calls)
    assert second == first, "命中缓存返回的内容必须与首次解析一致"

    # 对照组：证明这个计数器真的能发现"重新解析"（否则上面的空断言可能只是假绿）
    _bump(root / "c-20250103_120000" / "progress.json")
    runstore.scan_runs(root)
    assert calls == ["c-20250103_120000"], (
        f"只改了 c 的进度文件，应当只重解析 c（按 run 粒度失效），实际解析了 {calls}"
    )


# ============================================================
# 2. 指纹变化即失效（三个文件各自都要在指纹里）
# ============================================================


def test_progress_json_change_invalidates_cache(tmp_path, monkeypatch):
    """progress.json 变了必须反映到看板。

    防的场景：回测推进（day_done / phase 变化）后看板进度条**卡在旧值**不动 ——
    用户以为回测卡死，实际是缓存把旧条目一直喂给前端。
    """
    root = tmp_path / "results"
    name = "a-20250101_120000"
    run = _make_run(root, name, phase="day_3", day_done=3)
    calls = _spy_scan_one(monkeypatch)

    first = _entry(runstore.scan_runs(root), name)
    assert (first["phase"], first["day_done"]) == ("day_3", 3)
    _scan_expecting_hit(root, calls)

    _write_progress(run, phase="day_5", day_done=5)
    _bump(run / "progress.json")
    calls.clear()
    again = _entry(runstore.scan_runs(root), name)
    assert calls == [name], "progress.json 变了却没重解析 —— 看板会一直显示旧进度"
    assert (again["phase"], again["day_done"]) == ("day_5", 5)


def test_daily_stats_change_invalidates_cache(tmp_path, monkeypatch):
    """daily_stats.csv 变了必须反映到运行中 run 的指标。

    防的场景：运行中的 run 指标是**现算**的（summary.json 还没落盘），
    只靠 summary/progress 的指纹管不住它 —— 看板的实时收益曲线停在几小时前，
    直到回测结束才"跳"到最终值。
    """
    root = tmp_path / "results"
    name = "run-20250101_120000"
    run = _make_run(root, name, status="running", with_summary=False, last_total_value=1_010_000.0)
    calls = _spy_scan_one(monkeypatch)

    first = _entry(runstore.scan_runs(root), name)
    assert first["status"] == "running", "夹具写出的 progress 是新鲜的 running，不该被判为中断"
    assert first["metrics"]["final_value"] == 1_010_000.0
    _scan_expecting_hit(root, calls)

    (run / "daily_stats.csv").write_text(_csv_text(1_030_000.0), encoding="utf-8")
    _bump(run / "daily_stats.csv")
    calls.clear()
    again = _entry(runstore.scan_runs(root), name)
    assert calls == [name], "daily_stats.csv 变了却没重解析 —— 运行中的实时指标会停在旧值"
    assert again["metrics"]["final_value"] == 1_030_000.0


def test_summary_change_invalidates_cache(tmp_path, monkeypatch):
    """summary.json 变了必须反映到完成 run 的指标。

    防的场景：run 结束（summary.json 落盘）后看板仍显示运行中的估算指标，
    最终收益/夏普等数字**永远差一个版本**。
    """
    root = tmp_path / "results"
    name = "run-20250101_120000"
    run = _make_run(root, name, total_return=0.01)
    calls = _spy_scan_one(monkeypatch)

    assert _entry(runstore.scan_runs(root), name)["metrics"]["total_return"] == 0.01
    _scan_expecting_hit(root, calls)

    (run / "summary.json").write_text(
        json.dumps({"total_return": 0.99, "config": {}}), encoding="utf-8"
    )
    _bump(run / "summary.json")
    calls.clear()
    again = _entry(runstore.scan_runs(root), name)
    assert calls == [name], "summary.json 变了却没重解析 —— 完成指标停在旧版本"
    assert again["metrics"]["total_return"] == 0.99


def test_irrelevant_file_churn_keeps_cache_hit(tmp_path, monkeypatch):
    """不在指纹里的高频写入文件不得让缓存失效。

    防的场景：有人"顺手更保险"把 output.log / trades.csv 也纳入指纹 ——
    引擎每写一行日志都在追写 output.log，于是运行中的 run 每次轮询都失效重解析，
    这个缓存存在的唯一理由（9 倍加速）被抵消掉。
    """
    root = tmp_path / "results"
    name = "a-20250101_120000"
    run = _make_run(root, name)
    calls = _spy_scan_one(monkeypatch)

    runstore.scan_runs(root)
    _scan_expecting_hit(root, calls)

    for fn, text in (("output.log", "line1\nline2\n"), ("trades.csv", "time,security\n")):
        (run / fn).write_text(text, encoding="utf-8")
        _bump(run / fn)

    calls.clear()
    again = runstore.scan_runs(root)
    assert calls == [], (
        f"无关文件（output.log / trades.csv）的变动触发了重解析 {calls} —— "
        "运行中的 run 会永久失去缓存"
    )
    assert _entry(again, name)["status"] == "done"


# ============================================================
# 3. 缓存键必须带根目录
# ============================================================


def test_cache_key_includes_root_so_same_named_runs_do_not_cross(tmp_path, monkeypatch):
    """不同 root 下的**同名** run 不得互相串结果（键必须是 ``(root, name)``）。

    防的场景：两个项目（``--root`` 不同）里都有 ``demo-20250101_120000`` 时，
    B 项目列表显示 A 项目的收益曲线/策略名。测试里尤其常见（都用 ``tmp_path``），
    生产里则是"看板连错项目却看不出来"。

    两个同名 run 的三个指纹文件被钉成**逐位相同** —— 否则指纹本身就能区分它们，
    "键漏了根目录"会表现为正常的缓存未命中，测试无法发现缺陷。
    """
    name = "demo-20250101_120000"
    root_a = tmp_path / "proj_a"
    root_b = tmp_path / "proj_b"
    _make_run(root_a, name, total_return=0.01)
    _make_run(root_b, name, total_return=0.99)
    _pin_fingerprint(root_a / name)
    _pin_fingerprint(root_b / name)
    calls = _spy_scan_one(monkeypatch)

    assert _entry(runstore.scan_runs(root_a), name)["metrics"]["total_return"] == 0.01
    assert calls.count(name) == 1, f"首次扫描应解析一次，实际 {calls}"

    b_runs = runstore.scan_runs(root_b)
    assert _entry(b_runs, name)["metrics"]["total_return"] == 0.99, (
        "B 根目录下的同名 run 取到了 A 的数据 —— 缓存键必须带根目录"
    )
    # **机制级**断言：若缓存键漏了根目录，扫 B 会命中 A 的条目而**根本不解析**，
    # calls 里 name 只会出现 1 次。仅比较返回值是不够的（B 正确重算时结果也对）。
    assert calls.count(name) >= 2, (
        f"扫 B 时没有重新解析同名 run —— 缓存键可能漏了根目录（calls={calls}）"
    )

    # 回到 A：内容仍必须是 A 自己的（缓存命中或重算都应如此）
    assert _entry(runstore.scan_runs(root_a), name)["metrics"]["total_return"] == 0.01


# ============================================================
# 4. 缓存不得与调用方共享可变对象
# ============================================================


def test_cached_entry_is_not_shared_with_callers(tmp_path, monkeypatch):
    """命中缓存返回的条目必须是**独立副本**（浅拷贝顶层）。

    防的场景（历史回归）：缓存存的是同一个 dict 对象 →
    ``scan_runs`` 收尾的 ``pop("sort_key")`` 摘掉的是缓存里的键，
    第二次轮询命中即 KeyError（看板 3 秒后 500）；调用方补的字段也会写回缓存，
    之后所有轮询都显示被污染的数据。
    """
    root = tmp_path / "results"
    name = "a-20250101_120000"
    _make_run(root, name)
    calls = _spy_scan_one(monkeypatch)

    first = runstore.scan_runs(root)
    assert calls == [name], "首次扫描应当解析"
    assert all("sort_key" not in r for r in first), (
        "sort_key 是内部排序键，必须在返回前摘掉（它同时是『缓存对象被共享』的导火索）"
    )

    # 调用方乱改：不得影响下一次轮询
    first[0]["status"] = "被调用方改过"
    first[0]["caller_field"] = 1

    second = runstore.scan_runs(root)
    assert calls == [name], "第二次扫描应当命中缓存（否则本用例证明不了命中不串脏数据）"
    assert second[0]["status"] == "done", "调用方改过的 status 被写回缓存 —— 存的是同一个 dict"
    assert "caller_field" not in second[0], "调用方补的字段被写回缓存"

    # 反向：命中路径返回的对象若与缓存共享，改它同样会污染后续轮询
    second[0]["status"] = "第二次改的"
    second[0]["caller_field2"] = 2

    third = runstore.scan_runs(root)
    assert calls == [name], "第三次也应命中缓存"
    assert third[0]["status"] == "done", "命中路径没返回副本，调用方改动污染了缓存"
    assert "caller_field2" not in third[0]
    assert all("sort_key" not in r for r in third)


# ============================================================
# 5. 消失的 run 要清出缓存
# ============================================================


def test_deleted_run_is_evicted_from_cache(tmp_path, monkeypatch):
    """删掉的 run 必须从缓存里清出。

    防的场景有两个：
    1. 缓存随"历史上出现过的 run 名"无界增长（看板长期运行只涨不落）；
    2. **同名 run 重建**后显示被删掉那个 run 的数据 —— 复制/还原结果目录会原样
       保留 mtime（``rsync -t``、备份还原、容器层），指纹与旧条目逐位相同，
       没有清出就会直接命中旧条目。
    """
    root = tmp_path / "results"
    name = "gone-20250101_120000"
    keep = "keep-20250102_120000"
    _make_run(root, name, total_return=0.01)
    _make_run(root, keep)
    _pin_fingerprint(root / name)
    calls = _spy_scan_one(monkeypatch)

    assert _entry(runstore.scan_runs(root), name)["metrics"]["total_return"] == 0.01
    key = (str(root.resolve()), name)
    assert key in runstore._SCAN_CACHE, "首次扫描后应当留下缓存条目（前提校验）"

    shutil.rmtree(root / name)
    left = runstore.scan_runs(root)
    assert [r["name"] for r in left] == [keep], f"删除后只剩 keep，实际 {[r['name'] for r in left]}"
    assert key not in runstore._SCAN_CACHE, "已删除的 run 仍留在缓存里 —— 缓存无界增长"

    # 同名重建，且指纹（mtime）与旧条目完全相同 → 没有清出就会命中旧条目
    _make_run(root, name, total_return=0.77)
    _pin_fingerprint(root / name)
    calls.clear()
    again = _entry(runstore.scan_runs(root), name)
    # **机制级**断言：指纹逐位相同，唯一能救回来的就是「消失即清出」。
    # 若清出逻辑失效，这里会命中旧条目而完全不解析。
    assert name in calls, f"同名 run 重建后没有重新解析 —— 消失的 run 未被清出缓存（calls={calls}）"
    assert again["metrics"]["total_return"] == 0.77, (
        "同名 run 重建后显示的是被删除的旧 run 的数据 —— 消失的 run 必须清出缓存"
    )


# ============================================================
# 6. use_cache=False 是正确性基准
# ============================================================


def test_use_cache_false_is_full_recompute_baseline(tmp_path, monkeypatch):
    """``use_cache=False`` 必须与缓存版**逐字段一致**，且每次都全量重算。

    防的场景：缓存把过期/串台的数据喂给看板，而"关掉缓存"这条本该用于对照和排障
    的路径也一起错（例如把它实现成"读缓存但忽略指纹"），于是排障时无从判断
    到底是不是缓存的问题。
    """
    root = tmp_path / "results"
    names = {
        "done-20250105_120000",
        "running-20250104_120000",
        "missing-20250106_120000",  # 残缺 run：目录存在但没有任何产物
        "baddate-20250132_999999",  # 目录名时间戳非法 → 退回目录 mtime
    }
    _make_run(root, "done-20250105_120000")
    _make_run(root, "running-20250104_120000", status="running", with_summary=False)
    (root / "missing-20250106_120000").mkdir()
    _make_run(root, "baddate-20250132_999999")
    (root / "notes.txt").write_text("not a run\n", encoding="utf-8")  # 非目录项必须被忽略
    calls = _spy_scan_one(monkeypatch)

    cached = runstore.scan_runs(root)
    assert {r["name"] for r in cached} == names, (
        f"根目录下的非目录项不得进结果，实际 {[r['name'] for r in cached]}"
    )

    hit = _scan_expecting_hit(root, calls)
    assert hit == cached, "两次缓存命中之间的结果必须完全一致"

    calls.clear()
    fresh = runstore.scan_runs(root, use_cache=False)
    assert sorted(calls) == sorted(names), (
        f"use_cache=False 必须全量重算（正确性基准），实际只解析了 {sorted(calls)}"
    )
    assert fresh == cached, "绕过缓存的结果与缓存版不一致 —— 缓存给出了过期/串台的数据"


# ============================================================
# 7. 排序（含混合目录）
# ============================================================


def test_sort_order_is_time_desc_with_missing_runs_sunk(tmp_path):
    """正常 run 按开始时间倒序；``status == "missing"`` 的残缺 run 沉底。

    混合目录是这里的重点：目录名带时间戳的 run 用**目录名里的时间**，
    不带时间戳的退回**目录 mtime** —— 两种排序键必须同尺度可比。
    顺带钉住"残缺 run 即使时间戳最新也要沉底"（否则看板第一屏全是空目录）。

    防的场景：
    - 按目录 mtime 排（而不是目录名里的开始时间）→ 结果目录被复制/还原过之后
      顺序全乱；
    - 残缺 run 混在正常 run 中间 → 用户第一眼看到的是"数据缺失"的空条目；
    - 排序键类型混乱（字符串 vs float 混排）→ 混合目录得不到时间倒序。
    """
    root = tmp_path / "results"
    # 故意按"期望顺序的逆序"创建，避免实现靠 iterdir/创建顺序巧合排对
    _make_run(root, "beta-20250103_120000")
    legacy = _make_run(root, "legacy_no_ts")
    # 从旧备份/压缩包解出来的结果目录：目录 mtime 是 1999 年。
    # epoch 秒只有 9 位，而 2025 年的时间戳是 10 位 —— 正好卡住
    # 「排序键直接 str() 混排」这个坑（源码注释点名过）：字符串序会把 1999 年
    # 的目录排到最前面，而它其实最旧。
    ancient = _make_run(root, "ancient_no_ts")
    (root / "broken-20250106_120000").mkdir()  # 时间戳最新，但产物缺失 → 必须沉底
    _make_run(root, "alpha-20250105_120000")
    # 无时间戳目录的排序键 = 目录 mtime
    ts_legacy = datetime(2025, 1, 4, 12, 0, 0).timestamp()  # 夹在 alpha 与 beta 之间
    os.utime(legacy, (ts_legacy, ts_legacy))
    ts_ancient = datetime(1999, 1, 1, 0, 0, 0).timestamp()
    os.utime(ancient, (ts_ancient, ts_ancient))

    runs = runstore.scan_runs(root)
    assert [r["name"] for r in runs] == [
        "alpha-20250105_120000",
        "legacy_no_ts",
        "beta-20250103_120000",
        "ancient_no_ts",
        "broken-20250106_120000",
    ], f"排序不是『时间倒序 + 残缺沉底』，实际 {[r['name'] for r in runs]}"
    assert runs[-1]["status"] == "missing", "残缺 run 必须沉底且状态为 missing"
