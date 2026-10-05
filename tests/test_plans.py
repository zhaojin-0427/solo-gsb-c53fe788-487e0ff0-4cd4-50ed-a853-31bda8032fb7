"""端到端测试：通过 FastAPI TestClient 验证 /plans 的关键行为。"""

import json
import os

from fastapi.testclient import TestClient

from app import solver
from app.main import app
from app.schemas import PlanRequest

client = TestClient(app)

HERE = os.path.dirname(__file__)


def post(payload: dict):
    return client.post("/plans", json=payload)


def load_example(name: str) -> dict:
    with open(os.path.join(HERE, "..", "examples", name)) as f:
        return json.load(f)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_example_is_optimal_and_consistent():
    r = post(load_example("request.json"))
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "OPTIMAL"
    assert d["optimal"] is True

    order = [(p["job_id"], p["segment_index"]) for p in d["plan"]]
    # purge 最小（200）、逾期 0，字典序最小顺序：J1.1 J1.2 J2.1 J2.2
    assert order == [("J1", 1), ("J1", 2), ("J2", 1), ("J2", 2)]
    assert d["purge_used_mm3"] == 200
    assert d["purge_remaining_mm3"] == 200
    assert d["total_tardiness_seconds"] == 0
    assert d["makespan_seconds"] == 210
    assert d["objective"]["lexicographic_minimal"] is True

    # 切换记录与计划时间线一致
    sw = d["switches"]
    assert [(s["from_material"], s["to_material"], s["purge_mm3"]) for s in sw] == [
        ("PLA", "ABS", 120),
        ("ABS", "PLA", 80),
    ]
    assert sum(s["purge_mm3"] for s in sw) == d["purge_used_mm3"]

    # 层段紧密排列、切换耗时正确：每个切换后第一个层段的开始时刻
    # = 前一层段结束 + 切换耗时（after_segment_order=0 时相对时刻 0）
    for s in sw:
        before = d["plan"][s["after_segment_order"] - 1] if s["after_segment_order"] else None
        after = d["plan"][s["after_segment_order"]]
        base_end = before["end_seconds"] if before else 0
        assert after["start_seconds"] == base_end + s["switch_seconds"]
        assert s["at_seconds"] == base_end
    # 无切换处层段首尾相接
    switch_after = {s["after_segment_order"] for s in sw}
    for i, p in enumerate(d["plan"][:-1], start=1):
        if i not in switch_after:
            nxt = d["plan"][i]
            assert nxt["start_seconds"] == p["end_seconds"]
    # 每个作业内层段按序出现
    for job_id in ("J1", "J2"):
        segs = [p["segment_index"] for p in d["plan"] if p["job_id"] == job_id]
        assert segs == sorted(segs)


def test_infeasible_capacity():
    r = post(load_example("request_infeasible.json"))
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "INFEASIBLE"
    assert d["optimal"] is False
    assert d["infeasible_reason"]["code"] == "CAPACITY"
    assert d["infeasible_reason"]["minimum_required_purge_mm3"] == 200
    assert d["plan"] == []


def test_capacity_exactly_enough():
    payload = load_example("request_infeasible.json")
    payload["total_purge_capacity_mm3"] = 200
    r = post(payload)
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "OPTIMAL"
    assert d["purge_used_mm3"] == 200
    assert d["purge_remaining_mm3"] == 0


def test_three_job_interleaving():
    payload = {
        "jobs": [
            {"job_id": "J1", "deadline_seconds": 200, "segments": [
                {"material_id": "A", "print_seconds": 30, "volume_mm3": 10},
                {"material_id": "B", "print_seconds": 60, "volume_mm3": 10}]},
            {"job_id": "J2", "deadline_seconds": 100, "segments": [
                {"material_id": "B", "print_seconds": 20, "volume_mm3": 10},
                {"material_id": "C", "print_seconds": 40, "volume_mm3": 10}]},
            {"job_id": "J3", "deadline_seconds": 200, "segments": [
                {"material_id": "A", "print_seconds": 20, "volume_mm3": 10}]},
        ],
        "initial_material": "A",
        "purge_matrix": {
            "A": {"A": 0, "B": 100, "C": 100},
            "B": {"A": 100, "B": 0, "C": 100},
            "C": {"A": 100, "B": 100, "C": 0},
        },
        "switch_seconds": 5,
        "total_purge_capacity_mm3": 1000,
    }
    r = post(payload)
    d = r.json()
    assert r.status_code == 200
    assert d["status"] == "OPTIMAL"
    order = [(p["job_id"], p["segment_index"]) for p in d["plan"]]
    # A,A 相邻免 purge；最优 purge 200（A->B, B->C）
    assert order == [("J1", 1), ("J3", 1), ("J1", 2), ("J2", 1), ("J2", 2)]
    assert d["purge_used_mm3"] == 200
    assert d["total_tardiness_seconds"] == 80
    j2 = next(x for x in d["job_tardiness"] if x["job_id"] == "J2")
    assert j2["tardy_seconds"] == 80


def test_purge_priority_beats_tardiness():
    """第一级 purge 优先：即使增加逾期，也必须选 purge 更小的计划。"""
    payload = {
        "jobs": [
            {"job_id": "J1", "deadline_seconds": 50, "segments": [
                {"material_id": "A", "print_seconds": 10, "volume_mm3": 1},
                {"material_id": "B", "print_seconds": 10, "volume_mm3": 1}]},
            {"job_id": "J2", "deadline_seconds": 50, "segments": [
                {"material_id": "A", "print_seconds": 10, "volume_mm3": 1},
                {"material_id": "B", "print_seconds": 10, "volume_mm3": 1}]},
        ],
        "initial_material": "A",
        "purge_matrix": {"A": {"A": 0, "B": 1000}, "B": {"A": 1, "B": 0}},
        "switch_seconds": 0,
        "total_purge_capacity_mm3": 100000,
    }
    d = post(payload).json()
    # A A B B：purge=1000（一次 A->B）；交替 ABAB 需 1000+1+1000+1=2002
    order = [(p["job_id"], p["segment_index"]) for p in d["plan"]]
    assert order == [("J1", 1), ("J2", 1), ("J1", 2), ("J2", 2)]
    assert d["purge_used_mm3"] == 1000
    assert d["objective"]["total_purge_mm3"] == 1000


def test_initial_material_purge_charged():
    payload = {
        "jobs": [
            {"job_id": "J1", "deadline_seconds": 100, "segments": [
                {"material_id": "B", "print_seconds": 10, "volume_mm3": 1}]},
            {"job_id": "J2", "deadline_seconds": 100, "segments": [
                {"material_id": "B", "print_seconds": 10, "volume_mm3": 1}]},
        ],
        "initial_material": "A",
        "purge_matrix": {"A": {"A": 0, "B": 50}, "B": {"A": 1, "B": 0}},
        "switch_seconds": 3,
        "total_purge_capacity_mm3": 1000,
    }
    d = post(payload).json()
    assert d["status"] == "OPTIMAL"
    assert d["purge_used_mm3"] == 50  # 仅首次 A->B
    first = d["plan"][0]
    assert first["start_seconds"] == 3  # 首段前也要切换耗时


def test_lexicographic_tiebreak_prefects_smaller_job_id():
    # 两段同材料、两作业完全对称：字典序应选 ("A",1) 开头
    payload = {
        "jobs": [
            {"job_id": "B", "deadline_seconds": 100, "segments": [
                {"material_id": "X", "print_seconds": 5, "volume_mm3": 1}]},
            {"job_id": "A", "deadline_seconds": 100, "segments": [
                {"material_id": "X", "print_seconds": 5, "volume_mm3": 1}]},
        ],
        "initial_material": "X",
        "purge_matrix": {"X": {"X": 0}},
        "switch_seconds": 0,
        "total_purge_capacity_mm3": 0,
    }
    d = post(payload).json()
    assert [p["job_id"] for p in d["plan"]] == ["A", "B"]
    assert d["objective"]["lexicographic_minimal"] is True


# ----------------------------- 长作业（≥25 段） -----------------------------

def _assert_plan_consistent(d: dict, expected_counts: dict) -> None:
    """计划完整、作业内层段按序、切换记录与时间线一致。"""
    assert len(d["plan"]) == sum(expected_counts.values())
    # order 连续覆盖 1..N
    assert [p["order"] for p in d["plan"]] == list(range(1, len(d["plan"]) + 1))
    # 每个作业内层段 1..k 按序出现
    for job_id, count in expected_counts.items():
        idxs = [p["segment_index"] for p in d["plan"] if p["job_id"] == job_id]
        assert idxs == list(range(1, count + 1))
    # 切换记录与时间线一致
    sw = d["switches"]
    assert sum(s["purge_mm3"] for s in sw) == d["purge_used_mm3"]
    for s in sw:
        before = d["plan"][s["after_segment_order"] - 1] if s["after_segment_order"] else None
        after = d["plan"][s["after_segment_order"]]
        base_end = before["end_seconds"] if before else 0
        assert after["start_seconds"] == base_end + s["switch_seconds"]
        assert s["at_seconds"] == base_end
        if before is None:
            # 首次切换来自请求中的初始材料
            assert s["from_material"] is not None
    # 无切换处首尾相接
    switch_after = {s["after_segment_order"] for s in sw}
    for i, p in enumerate(d["plan"][:-1], start=1):
        if i not in switch_after:
            assert d["plan"][i]["start_seconds"] == p["end_seconds"]
    # 逾期明细完整
    assert {j["job_id"] for j in d["job_tardiness"]} == set(expected_counts)
    assert sum(j["tardy_seconds"] for j in d["job_tardiness"]) == d["total_tardiness_seconds"]


def test_long_job_example_25_segments_accepted():
    """25 段（及更多）的合法作业不再被 24 段上限拒绝，进入 CP-SAT 规划。"""
    payload = load_example("request_long_job.json")
    r = post(payload)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "OPTIMAL"
    assert d["optimal"] is True
    assert d["objective"]["lexicographic_minimal"] is True
    _assert_plan_consistent(d, {"LONG": 25, "AUX": 2})


def test_twenty_four_segments_still_ok():
    """边界：旧上限 24 段行为不变。"""
    payload = load_example("request_long_job.json")
    payload["jobs"][0]["segments"] = payload["jobs"][0]["segments"][:24]
    d = post(payload).json()
    _assert_plan_consistent(d, {"LONG": 24, "AUX": 2})
    assert d["status"] in ("OPTIMAL", "FEASIBLE")


def test_large_model_timeout_still_returns_full_plan():
    """大模型 + 极短 time_limit：超时也必须返回完整、按序的可行计划，
    且只在目标确已证明时才标 OPTIMAL。"""
    mats = ["PLA", "ABS", "TPU", "PETG"]
    purge_matrix = {a: {b: (0 if a == b else 100) for b in mats} for a in mats}
    jobs = []
    for j in range(8):
        jobs.append({
            "job_id": f"J{j:02d}",
            "deadline_seconds": 10 ** 9,
            "segments": [
                {"material_id": mats[(j + k) % 4], "print_seconds": 10, "volume_mm3": 5}
                for k in range(30)
            ],
        })
    payload = {
        "jobs": jobs,
        "initial_material": "PLA",
        "purge_matrix": purge_matrix,
        "switch_seconds": 2,
        "total_purge_capacity_mm3": 10 ** 7,
        "time_limit_seconds": 0.5,
    }
    d = post(payload).json()
    assert d["status"] in ("FEASIBLE", "OPTIMAL")
    assert d["plan"], "超时也必须返回完整计划"
    _assert_plan_consistent(d, {f"J{j:02d}": 30 for j in range(8)})
    # 目标未证明时绝不声称最优
    obj = d["objective"]
    assert d["optimal"] == (
        obj["purge_optimal"] and obj["tardiness_optimal"] and obj["lexicographic_minimal"]
    )
    if d["status"] == "FEASIBLE":
        assert d["optimal"] is False


def test_long_job_capacity_infeasible_unchanged():
    """长作业下容量无解行为不变（构造性候选也超容时如实 UNKNOWN/INFEASIBLE，
    绝不伪造计划）。"""
    payload = load_example("request_long_job.json")
    payload["total_purge_capacity_mm3"] = 1
    payload["time_limit_seconds"] = 5
    d = post(payload).json()
    assert d["status"] in ("INFEASIBLE", "UNKNOWN")
    assert d["optimal"] is False
    assert d["plan"] == []


# --------------------- 长作业按材料交错（A→B→A 用料） ---------------------

def _aba_payload(time_limit: float = 15) -> dict:
    """两个 25 段作业，均按 A→B→A 用料（9A + 8B + 8A），容量仅够 2 次换料。"""
    segs = [
        {"material_id": m, "print_seconds": 10, "volume_mm3": 5}
        for m in ["A"] * 9 + ["B"] * 8 + ["A"] * 8
    ]
    return {
        "jobs": [
            {"job_id": "J1", "deadline_seconds": 100000, "segments": segs},
            {"job_id": "J2", "deadline_seconds": 100000, "segments": [dict(s) for s in segs]},
        ],
        "initial_material": "A",
        "purge_matrix": {"A": {"A": 0, "B": 1}, "B": {"A": 1, "B": 0}},
        "switch_seconds": 2,
        "total_purge_capacity_mm3": 2,
        "time_limit_seconds": time_limit,
    }


def _assert_interleaved_aba_plan(d: dict) -> None:
    """两作业 A→B→A 交错后：A 段合流 → B 段合流 → A 段合流，恰好 2 次换料。"""
    _assert_plan_consistent(d, {"J1": 25, "J2": 25})
    # 任何可行计划至少换料 2 次（每个作业的 B 段夹在 A 段之间），容量恰为 2
    assert d["purge_used_mm3"] == 2
    assert d["purge_remaining_mm3"] == 0
    assert [(s["from_material"], s["to_material"], s["purge_mm3"]) for s in d["switches"]] == [
        ("A", "B", 1),
        ("B", "A", 1),
    ]
    mats = [p["material_id"] for p in d["plan"]]
    assert mats == ["A"] * 18 + ["B"] * 16 + ["A"] * 16
    assert d["total_tardiness_seconds"] == 0


def test_interleave_example_file():
    """示例文件：两个 25 段 A→B→A 作业按材料交错，2 次换料在容量 2 之内。"""
    r = post(load_example("request_interleave.json"))
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] in ("FEASIBLE", "OPTIMAL")
    _assert_interleaved_aba_plan(d)


def test_long_jobs_ab_runs_short_limit_not_unknown():
    """回归：短限时下 CP-SAT 可能未得解，兜底不得只试串行而误报 UNKNOWN。

    整作业串行至少换料 4 次（purge 4，超容 2），按材料交错只需 2 次；
    必须返回按材料交错的完整计划（FEASIBLE 或已证 OPTIMAL），而非空计划。
    """
    r = post(_aba_payload(time_limit=0.5))
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] in ("FEASIBLE", "OPTIMAL"), d["solver_message"]
    assert d["plan"], "长作业 + 短限时不得再误报空计划"
    _assert_interleaved_aba_plan(d)
    if d["status"] == "FEASIBLE":
        assert d["optimal"] is False


def test_heuristic_candidates_include_material_interleaving():
    """构造性候选本身（不经 CP-SAT）就必须包含按材料交错的低 purge 排列。"""
    req = PlanRequest(**_aba_payload())
    inst = solver.build_instance(req)
    purges = [
        solver._materialize(inst, order)["purge_used_mm3"]
        for order in solver._heuristic_orders(inst)
    ]
    assert 4 in purges  # 整作业串行候选仍保留（换料 4 次，超容）
    assert min(purges) == 2  # 新增按材料交错候选把换料压到 2 次
    heur = solver._feasible_heuristic(inst)
    assert heur is not None
    _, timeline = heur
    assert timeline["purge_used_mm3"] == 2  # 兜底选中不超容的交错计划


# ----------------------------- 422 校验 -----------------------------

def test_422_cases():
    base = load_example("request.json")
    bad = {
        "one job": dict(base, jobs=base["jobs"][:1]),
        "nine jobs": dict(base, jobs=base["jobs"] * 5),
        "dup id": dict(base, jobs=[
            dict(base["jobs"][0], job_id="X"),
            dict(base["jobs"][1], job_id="X")]),
        "unknown initial": dict(base, initial_material="PETG"),
        "negative cell": dict(base, purge_matrix={
            "PLA": {"PLA": 0, "ABS": -1}, "ABS": {"PLA": 80, "ABS": 0}}),
        "nonzero diagonal": dict(base, purge_matrix={
            "PLA": {"PLA": 5, "ABS": 120}, "ABS": {"PLA": 80, "ABS": 0}}),
        "missing column": dict(base, purge_matrix={
            "PLA": {"PLA": 0}, "ABS": {"PLA": 80, "ABS": 0}}),
        "negative switch": dict(base, switch_seconds=-1),
        "empty segments": dict(base, jobs=[
            dict(base["jobs"][0], segments=[]), base["jobs"][1]]),
        "extra field": dict(base, foo=1),
        "negative print": dict(base, jobs=[
            dict(base["jobs"][0], segments=[
                {"material_id": "PLA", "print_seconds": -1, "volume_mm3": 1}]),
            base["jobs"][1]]),
        "missing body": None,
    }
    for name, payload in bad.items():
        r = post(payload) if payload is not None else client.post("/plans")
        assert r.status_code == 422, name
