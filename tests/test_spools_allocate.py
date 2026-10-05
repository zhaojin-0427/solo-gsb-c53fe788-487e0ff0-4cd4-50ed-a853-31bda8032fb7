"""端到端测试：通过 FastAPI TestClient 验证 POST /spools/allocate。

固定执行顺序下的料卷分配：
* 沿顺序从初始材料计算 purge，每段打印体积与换料 purge 均从所选
  （目标材料）料卷扣除，卸下料卷可复用；
* 先最小化相邻层段料卷 ID 变化次数，再按逐段料卷 ID 字典序决胜
  （同材料两段依次需 4、6，余量为 4、6 的料卷必须能分配）；
* 库存不足返回 INSUFFICIENT_STOCK、顺序超总 purge 容量返回
  PURGE_CAPACITY_EXCEEDED，均无分配；
* 漏段、乱序、重复层段或非法料卷返回 422；
* 与 /plans、/executions/audit 共存互不影响。
"""

import json
import os

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

HERE = os.path.dirname(__file__)


def post(payload: dict):
    return client.post("/spools/allocate", json=payload)


def load_example(name: str) -> dict:
    with open(os.path.join(HERE, "..", "examples", name)) as f:
        return json.load(f)


def base_request() -> dict:
    """与 examples/request_spools.json 相同的基准（选卷与余量示例）。"""
    return load_example("request_spools.json")


def one_material_payload(volumes, spools, sequence=None):
    """单材料、无 purge 的极简请求：两段（分属两作业）依次需 volumes[0]、volumes[1]。"""
    assert len(volumes) == 2
    payload = {
        "jobs": [
            {"job_id": "J1", "deadline_seconds": 100, "segments": [
                {"material_id": "M", "print_seconds": 10, "volume_mm3": volumes[0]}]},
            {"job_id": "J2", "deadline_seconds": 100, "segments": [
                {"material_id": "M", "print_seconds": 10, "volume_mm3": volumes[1]}]},
        ],
        "initial_material": "M",
        "purge_matrix": {"M": {"M": 0}},
        "switch_seconds": 0,
        "total_purge_capacity_mm3": 0,
        "sequence": sequence or [
            {"job_id": "J1", "segment_index": 1},
            {"job_id": "J2", "segment_index": 1},
        ],
        "spools": [
            {"spool_id": sid, "material_id": "M", "remaining_mm3": rem}
            for sid, rem in spools
        ],
    }
    return payload


def assert_allocation_consistent(d: dict, req: dict) -> None:
    """分配结构性一致：逐段材料匹配、扣减 = 体积 + purge、余量单调且非负、
    各卷用量汇总与逐段扣减一致、变化次数与逐段选卷一致。"""
    allocs = d["allocations"]
    assert [a["order"] for a in allocs] == list(range(1, len(allocs) + 1))
    spool_mat = {s["spool_id"]: s["material_id"] for s in req["spools"]}
    balances = {s["spool_id"]: s["remaining_mm3"] for s in req["spools"]}
    for a, item in zip(allocs, req["sequence"]):
        assert (a["job_id"], a["segment_index"]) == (
            item["job_id"], item["segment_index"])
        assert spool_mat[a["spool_id"]] == a["material_id"]
        assert a["deduct_mm3"] == a["volume_mm3"] + a["purge_before_mm3"]
        balances[a["spool_id"]] -= a["deduct_mm3"]
        assert a["spool_remaining_after_mm3"] == balances[a["spool_id"]] >= 0
    changes = sum(
        1 for i in range(1, len(allocs))
        if allocs[i]["spool_id"] != allocs[i - 1]["spool_id"]
    )
    assert d["spool_changes"] == changes
    assert d["objective"]["spool_changes"] == changes
    usage = {s["spool_id"]: s for s in d["spools"]}
    assert set(usage) == set(balances)
    for sid, s in usage.items():
        assert s["remaining_mm3"] == balances[sid]
        assert s["used_mm3"] == s["initial_remaining_mm3"] - balances[sid]
    assert sum(a["purge_before_mm3"] for a in allocs) == d["purge_used_mm3"]
    assert d["purge_remaining_mm3"] == (
        req["total_purge_capacity_mm3"] - d["purge_used_mm3"]
    )


# ------------------------------- 基本分配 -------------------------------

def test_example_file_allocated_optimally():
    """README 示例：逐段选卷、扣减与各卷余量全部精确匹配。"""
    req = base_request()
    r = post(req)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "OPTIMAL"
    assert d["optimal"] is True
    assert d["objective"] == {
        "spool_changes": 3,
        "spool_changes_optimal": True,
        "lexicographic_minimal": True,
    }
    # 沿顺序的段前 purge：J1.2 前 PLA→ABS 120，J2.2 前 ABS→PLA 80
    assert [a["purge_before_mm3"] for a in d["allocations"]] == [0, 120, 0, 80]
    # 逐段选卷与扣减（体积 + purge 均从目标材料料卷扣除）
    assert [(a["spool_id"], a["deduct_mm3"], a["spool_remaining_after_mm3"])
            for a in d["allocations"]] == [
        ("SPLA-1", 100, 50),
        ("SABS-1", 160, 40),   # 40 打印 + 120 purge
        ("SABS-2", 120, 0),
        ("SPLA-2", 110, 0),    # 30 打印 + 80 purge
    ]
    assert d["spool_changes"] == 3
    assert d["purge_used_mm3"] == 200
    assert d["purge_remaining_mm3"] == 200
    usage = {s["spool_id"]: s for s in d["spools"]}
    assert usage["SPLA-1"]["remaining_mm3"] == 50
    assert usage["SPLA-2"]["remaining_mm3"] == 0
    assert usage["SABS-1"]["remaining_mm3"] == 40
    assert usage["SABS-2"]["remaining_mm3"] == 0
    assert_allocation_consistent(d, req)


def test_same_material_4_then_6_with_spools_4_and_6():
    """任务书用例：同材料两段依次需 4、6，余量为 4、6 的料卷须能分配。"""
    d = post(one_material_payload([4, 6], [("S1", 4), ("S2", 6)])).json()
    assert d["status"] == "OPTIMAL"
    assert [a["spool_id"] for a in d["allocations"]] == ["S1", "S2"]
    assert [a["spool_remaining_after_mm3"] for a in d["allocations"]] == [0, 0]
    assert d["spool_changes"] == 1


def test_same_material_4_then_6_with_spools_6_and_4():
    """镜像陷阱：余量 6、4 时首段不能贪大卷（否则次段无解），须 4→次大卷。"""
    d = post(one_material_payload([4, 6], [("S1", 6), ("S2", 4)])).json()
    assert d["status"] == "OPTIMAL"
    # S1 先分会给首段将导致次段无卷可用；唯一可行分配是 S2→首段、S1→次段
    assert [a["spool_id"] for a in d["allocations"]] == ["S2", "S1"]
    assert d["spool_changes"] == 1


def test_min_changes_beats_lexicographic():
    """第一级目标优先：宁可全程用字典序更大的卷，也不多换一次卷。"""
    # S2 一卷可覆盖 40+60=100（0 次变化）；S1+S2 虽字典序更小但需 1 次变化
    d = post(one_material_payload([40, 60], [("S1", 60), ("S2", 100)])).json()
    assert d["status"] == "OPTIMAL"
    assert [a["spool_id"] for a in d["allocations"]] == ["S2", "S2"]
    assert d["spool_changes"] == 0


def test_lexicographic_tiebreak_prefers_smaller_spool_id():
    """变化次数相同时，逐段选字典序最小的料卷。"""
    d = post(one_material_payload([40, 60], [("SPB", 500), ("SPA", 500)])).json()
    assert d["status"] == "OPTIMAL"
    assert [a["spool_id"] for a in d["allocations"]] == ["SPA", "SPA"]
    assert d["spool_changes"] == 0
    assert d["objective"]["lexicographic_minimal"] is True


def test_run_split_minimizes_changes_and_reuses_balance():
    """同材料连续段拆分：变化次数最小，且卷余量跨段累计。"""
    payload = one_material_payload([50, 50], [("S1", 100), ("S2", 60), ("S3", 10)])
    # 调整为四段（50, 50, 50, 10）：J1 两段 50，J2 一段 50 一段 10
    payload["jobs"][0]["segments"].append(
        {"material_id": "M", "print_seconds": 10, "volume_mm3": 50})
    payload["jobs"][1]["segments"].append(
        {"material_id": "M", "print_seconds": 10, "volume_mm3": 10})
    payload["sequence"] = [
        {"job_id": "J1", "segment_index": 1},
        {"job_id": "J1", "segment_index": 2},
        {"job_id": "J2", "segment_index": 1},
        {"job_id": "J2", "segment_index": 2},
    ]
    d = post(payload).json()
    assert d["status"] == "OPTIMAL"
    # S1 覆盖前两段（50+50=100），S2 覆盖后两段（50+10=60）：仅 1 次变化
    assert [a["spool_id"] for a in d["allocations"]] == ["S1", "S1", "S2", "S2"]
    assert d["spool_changes"] == 1
    usage = {s["spool_id"]: s for s in d["spools"]}
    assert usage["S1"]["remaining_mm3"] == 0
    assert usage["S2"]["remaining_mm3"] == 0
    assert usage["S3"]["remaining_mm3"] == 10  # 未使用


def test_spool_reused_after_unload():
    """卸下料卷可复用：A→B→A 顺序中同一 A 卷服务首尾两段，余量累计。"""
    payload = {
        "jobs": [
            {"job_id": "J1", "deadline_seconds": 100, "segments": [
                {"material_id": "A", "print_seconds": 10, "volume_mm3": 10},
                {"material_id": "B", "print_seconds": 10, "volume_mm3": 10}]},
            {"job_id": "J2", "deadline_seconds": 100, "segments": [
                {"material_id": "A", "print_seconds": 10, "volume_mm3": 10}]},
        ],
        "initial_material": "A",
        "purge_matrix": {"A": {"A": 0, "B": 5}, "B": {"A": 5, "B": 0}},
        "switch_seconds": 1,
        "total_purge_capacity_mm3": 100,
        "sequence": [
            {"job_id": "J1", "segment_index": 1},
            {"job_id": "J1", "segment_index": 2},
            {"job_id": "J2", "segment_index": 1},
        ],
        "spools": [
            {"spool_id": "SA", "material_id": "A", "remaining_mm3": 100},
            {"spool_id": "SB", "material_id": "B", "remaining_mm3": 100},
        ],
    }
    d = post(payload).json()
    assert d["status"] == "OPTIMAL"
    assert [a["spool_id"] for a in d["allocations"]] == ["SA", "SB", "SA"]
    # SA 首尾两段共扣 10 + (10+5 purge) = 25；SB 扣 10+5 purge = 15
    assert [a["deduct_mm3"] for a in d["allocations"]] == [10, 15, 15]
    usage = {s["spool_id"]: s for s in d["spools"]}
    assert usage["SA"]["remaining_mm3"] == 75
    assert usage["SB"]["remaining_mm3"] == 85
    assert d["spool_changes"] == 2
    assert d["purge_used_mm3"] == 10


def test_purge_charged_to_target_material_spool():
    """换料 purge 从目标材料料卷扣除（首段相对初始材料同理）。"""
    payload = {
        "jobs": [
            {"job_id": "J1", "deadline_seconds": 100, "segments": [
                {"material_id": "PLA", "print_seconds": 10, "volume_mm3": 0}]},
            {"job_id": "J2", "deadline_seconds": 100, "segments": [
                {"material_id": "ABS", "print_seconds": 10, "volume_mm3": 0}]},
        ],
        "initial_material": "PLA",
        "purge_matrix": {"PLA": {"PLA": 0, "ABS": 120}, "ABS": {"PLA": 80, "ABS": 0}},
        "switch_seconds": 10,
        "total_purge_capacity_mm3": 400,
        "sequence": [
            {"job_id": "J1", "segment_index": 1},
            {"job_id": "J2", "segment_index": 1},
        ],
        "spools": [
            {"spool_id": "SP", "material_id": "PLA", "remaining_mm3": 50},
            {"spool_id": "SA", "material_id": "ABS", "remaining_mm3": 200},
        ],
    }
    d = post(payload).json()
    assert d["status"] == "OPTIMAL"
    # 首段与初始材料同料：无 purge；第二段 PLA→ABS 的 120 全记在 ABS 卷上
    assert [(a["spool_id"], a["purge_before_mm3"], a["deduct_mm3"])
            for a in d["allocations"]] == [("SP", 0, 0), ("SA", 120, 120)]
    usage = {s["spool_id"]: s for s in d["spools"]}
    assert usage["SP"]["remaining_mm3"] == 50
    assert usage["SA"]["remaining_mm3"] == 80


def test_interleaved_sequence_purge_follows_given_order():
    """跨作业交错：purge 严格沿给定顺序计算，而非按作业重排。"""
    payload = {
        "jobs": [
            {"job_id": "J1", "deadline_seconds": 100, "segments": [
                {"material_id": "PLA", "print_seconds": 10, "volume_mm3": 10},
                {"material_id": "PLA", "print_seconds": 10, "volume_mm3": 10}]},
            {"job_id": "J2", "deadline_seconds": 100, "segments": [
                {"material_id": "ABS", "print_seconds": 10, "volume_mm3": 10},
                {"material_id": "ABS", "print_seconds": 10, "volume_mm3": 10}]},
        ],
        "initial_material": "PLA",
        "purge_matrix": {"PLA": {"PLA": 0, "ABS": 120}, "ABS": {"PLA": 80, "ABS": 0}},
        "switch_seconds": 10,
        "total_purge_capacity_mm3": 400,
        "sequence": [
            {"job_id": "J1", "segment_index": 1},
            {"job_id": "J2", "segment_index": 1},
            {"job_id": "J1", "segment_index": 2},
            {"job_id": "J2", "segment_index": 2},
        ],
        "spools": [
            {"spool_id": "SP", "material_id": "PLA", "remaining_mm3": 500},
            {"spool_id": "SA", "material_id": "ABS", "remaining_mm3": 500},
        ],
    }
    d = post(payload).json()
    assert d["status"] == "OPTIMAL"
    # 材料序 PLA,ABS,PLA,ABS → purge 120, 80, 120
    assert [a["purge_before_mm3"] for a in d["allocations"]] == [0, 120, 80, 120]
    assert d["purge_used_mm3"] == 320
    assert d["purge_remaining_mm3"] == 80
    usage = {s["spool_id"]: s for s in d["spools"]}
    assert usage["SP"]["remaining_mm3"] == 500 - 10 - (10 + 80)
    assert usage["SA"]["remaining_mm3"] == 500 - (10 + 120) - (10 + 120)


def test_zero_volume_segments_and_zero_remaining_spool():
    """零体积段可从零余量卷分配（不消耗），字典序选最小 ID。"""
    d = post(one_material_payload([0, 0], [("S2", 0), ("S1", 0)])).json()
    assert d["status"] == "OPTIMAL"
    assert [a["spool_id"] for a in d["allocations"]] == ["S1", "S1"]
    assert d["spool_changes"] == 0
    usage = {s["spool_id"]: s for s in d["spools"]}
    assert usage["S1"]["remaining_mm3"] == 0
    assert usage["S2"]["remaining_mm3"] == 0


# ------------------------------- 缺料 / 超容 -------------------------------

def test_insufficient_stock_total():
    """某材料总余量小于总需求：INSUFFICIENT_STOCK 且无分配。"""
    req = base_request()
    req["spools"] = [
        {"spool_id": "SPLA-1", "material_id": "PLA", "remaining_mm3": 500},
        {"spool_id": "SABS-1", "material_id": "ABS", "remaining_mm3": 100},
    ]
    r = post(req)
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "INSUFFICIENT_STOCK"
    assert d["optimal"] is False
    assert d["objective"] is None
    assert d["allocations"] == []
    assert d["spools"] == []
    assert d["spool_changes"] is None
    # ABS 需求 160+120=280，仅 100 可用
    assert "ABS" in d["solver_message"]


def test_insufficient_stock_fragmentation():
    """总量够但无法搭配（11+1 装不下 6+6）：求解器证无解，同样缺料。"""
    d = post(one_material_payload([6, 6], [("S1", 11), ("S2", 1)])).json()
    assert d["status"] == "INSUFFICIENT_STOCK"
    assert d["allocations"] == []


def test_insufficient_stock_no_spool_of_material():
    """某段材料完全无卷可用：缺料。"""
    req = base_request()
    req["spools"] = [s for s in req["spools"] if s["material_id"] == "PLA"]
    d = post(req).json()
    assert d["status"] == "INSUFFICIENT_STOCK"
    assert d["allocations"] == []


def test_insufficient_stock_empty_spool_list():
    req = base_request()
    req["spools"] = []
    d = post(req).json()
    assert d["status"] == "INSUFFICIENT_STOCK"
    assert d["allocations"] == []


def test_purge_capacity_exceeded():
    """固定顺序的 purge 总量超容：PURGE_CAPACITY_EXCEEDED 且无分配。"""
    req = base_request()
    req["total_purge_capacity_mm3"] = 150  # 顺序 purge 200
    r = post(req)
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "PURGE_CAPACITY_EXCEEDED"
    assert d["optimal"] is False
    assert d["allocations"] == []
    assert d["spools"] == []
    assert d["purge_used_mm3"] == 200
    assert d["purge_remaining_mm3"] == -50


def test_capacity_checked_before_stock():
    """超容与缺料同时存在时，优先返回超容（顺序本身不可执行）。"""
    req = base_request()
    req["total_purge_capacity_mm3"] = 150
    req["spools"] = []  # 同时缺料
    d = post(req).json()
    assert d["status"] == "PURGE_CAPACITY_EXCEEDED"


def test_capacity_exactly_enough_still_allocates():
    req = base_request()
    req["total_purge_capacity_mm3"] = 200  # 恰等于顺序 purge
    d = post(req).json()
    assert d["status"] == "OPTIMAL"
    assert d["purge_remaining_mm3"] == 0
    assert_allocation_consistent(d, req)


# ------------------------------- 422 校验 -------------------------------

def test_422_sequence_problems():
    req = base_request()
    seq = req["sequence"]
    bad = {
        # 漏段：缺少 J2.2
        "missing segment": dict(req, sequence=seq[:3]),
        # 乱序：J2 的第 2 段排在第 1 段之前
        "out of order": dict(req, sequence=[seq[0], seq[1], seq[3], seq[2]]),
        # 重复层段
        "duplicate": dict(req, sequence=seq + [seq[0]]),
        # 未知作业
        "unknown job": dict(req, sequence=seq[:3] + [
            {"job_id": "J9", "segment_index": 1}]),
        # 未知层段（层序超出该作业段数）
        "unknown segment": dict(req, sequence=seq[:3] + [
            {"job_id": "J2", "segment_index": 3}]),
        # 层序为 0
        "zero segment index": dict(req, sequence=seq[:3] + [
            {"job_id": "J2", "segment_index": 0}]),
        # 缺少 sequence 字段
        "missing sequence": {k: v for k, v in req.items() if k != "sequence"},
        # sequence 项带额外字段
        "extra field in item": dict(req, sequence=seq[:3] + [
            {"job_id": "J2", "segment_index": 2, "foo": 1}]),
    }
    for name, payload in bad.items():
        r = post(payload)
        assert r.status_code == 422, f"{name}: {r.status_code} {r.text}"


def test_422_spool_problems():
    req = base_request()
    sp = req["spools"]
    bad = {
        # 料卷 ID 重复
        "dup spool id": dict(req, spools=sp + [dict(sp[0])]),
        # 空料卷 ID
        "empty spool id": dict(req, spools=sp + [
            {"spool_id": "", "material_id": "PLA", "remaining_mm3": 1}]),
        # 空材料 ID
        "empty material": dict(req, spools=sp + [
            {"spool_id": "SX", "material_id": "", "remaining_mm3": 1}]),
        # 负余量
        "negative remaining": dict(req, spools=sp + [
            {"spool_id": "SX", "material_id": "PLA", "remaining_mm3": -1}]),
        # 料卷带额外字段
        "extra field in spool": dict(req, spools=sp + [
            {"spool_id": "SX", "material_id": "PLA", "remaining_mm3": 1, "x": 1}]),
        # 缺少 spools 字段
        "missing spools": {k: v for k, v in req.items() if k != "spools"},
        # 顶层额外字段
        "extra top-level field": dict(req, foo=1),
    }
    for name, payload in bad.items():
        r = post(payload)
        assert r.status_code == 422, f"{name}: {r.status_code} {r.text}"


def test_422_plan_request_errors_still_apply():
    """PlanRequest 的既有校验对 /spools/allocate 同样生效。"""
    req = base_request()
    bad = {
        "one job": dict(req, jobs=req["jobs"][:1]),
        "dup job id": dict(req, jobs=[
            dict(req["jobs"][0], job_id="X"), dict(req["jobs"][1], job_id="X")]),
        "negative purge cell": dict(req, purge_matrix={
            "PLA": {"PLA": 0, "ABS": -1}, "ABS": {"PLA": 80, "ABS": 0}}),
        "missing matrix row": dict(req, purge_matrix={
            "ABS": {"PLA": 80, "ABS": 0}}),
        "missing body": None,
    }
    for name, payload in bad.items():
        r = post(payload) if payload is not None else client.post("/spools/allocate")
        assert r.status_code == 422, name


# --------------------------- 与既有接口的兼容性 ---------------------------

def test_plans_and_audit_unaffected():
    """新增接口不影响 /plans 与 /executions/audit 的既有行为。"""
    r = client.post("/plans", json=load_example("request.json"))
    assert r.status_code == 200
    assert r.json()["status"] == "OPTIMAL"
    r = client.post("/executions/audit", json=load_example("request_audit_completed.json"))
    assert r.status_code == 200
    assert r.json()["status"] == "COMPLETED"
    assert client.get("/health").json() == {"status": "ok"}
