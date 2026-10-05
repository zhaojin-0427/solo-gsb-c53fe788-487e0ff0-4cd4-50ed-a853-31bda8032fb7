"""端到端测试：通过 FastAPI TestClient 验证 POST /executions/audit。

核验只按实测时间线回放（保留空闲与实际耗时，不重新排产）：
* 合法完成 / 进行中前缀（未完工不计逾期）；
* 重叠、层序错误、换料留时不足、purge 不足、同材料段前 purge 非零、
  超容均标 VIOLATED，且即使全部层段已提交也不能标完成；
* 未知或重复层段、起止倒置返回 422；
* 跨作业交错允许，但各作业层序须合法。
"""

import os

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

HERE = os.path.dirname(__file__)


def audit_post(payload: dict):
    return client.post("/executions/audit", json=payload)


def base_plan() -> dict:
    """与 examples/request.json 相同的基准（switch=10，容量 400）。"""
    return {
        "jobs": [
            {"job_id": "J1", "deadline_seconds": 120, "segments": [
                {"material_id": "PLA", "print_seconds": 60, "volume_mm3": 100},
                {"material_id": "ABS", "print_seconds": 30, "volume_mm3": 40}]},
            {"job_id": "J2", "deadline_seconds": 220, "segments": [
                {"material_id": "ABS", "print_seconds": 80, "volume_mm3": 120},
                {"material_id": "PLA", "print_seconds": 20, "volume_mm3": 30}]},
        ],
        "initial_material": "PLA",
        "purge_matrix": {
            "PLA": {"PLA": 0, "ABS": 120},
            "ABS": {"PLA": 80, "ABS": 0},
        },
        "switch_seconds": 10,
        "total_purge_capacity_mm3": 400,
    }


def ex(job_id, k, start, end, purge_before):
    return {
        "job_id": job_id,
        "segment_index": k,
        "start_seconds": start,
        "end_seconds": end,
        "purge_before_mm3": purge_before,
    }


# ------------------------------- COMPLETED -------------------------------

def test_audit_completed_all_ok():
    """按 /plans 紧密计划的实测时间线回放：完成、purge 200、零逾期。"""
    payload = base_plan()
    payload["executions"] = [
        ex("J1", 1, 0, 60, 0),       # PLA 与初始材料相同：无换料
        ex("J1", 2, 70, 100, 120),   # PLA→ABS，留 10s
        ex("J2", 1, 100, 180, 0),    # 同材料 ABS 相邻：段前 purge 为零
        ex("J2", 2, 190, 210, 80),   # ABS→PLA，留 10s
    ]
    r = audit_post(payload)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "COMPLETED"
    assert d["violations"] == []
    assert d["segments_total"] == 4
    assert d["segments_executed"] == 4
    assert d["purge_used_mm3"] == 200
    assert d["purge_remaining_mm3"] == 200
    assert d["total_tardiness_seconds"] == 0
    assert d["makespan_seconds"] == 210
    assert {(j["job_id"], j["tardy_seconds"]) for j in d["job_tardiness"]} == {
        ("J1", 0), ("J2", 0)
    }
    # 回放明细：实测耗时与空闲保留，不重算
    rp = d["replay"]
    assert [x["print_seconds"] for x in rp] == [60, 30, 80, 20]
    assert [x["idle_seconds_before"] for x in rp] == [0, 10, 0, 10]
    assert [x["material_switched"] for x in rp] == [False, True, False, True]
    assert [x["purge_required_mm3"] for x in rp] == [0, 120, 0, 80]


def test_audit_preserves_idle_and_actual_durations():
    """空闲与实测耗时原样保留，不按紧密排产重算；逾期按实测完工时刻。"""
    payload = base_plan()
    payload["executions"] = [
        ex("J1", 1, 0, 55, 0),          # 实际只打 55s（计划 60s）
        ex("J1", 2, 70, 105, 120),      # 空闲 15s（>switch 10s，允许）
        ex("J2", 1, 105, 190, 0),       # 同材料紧跟
        ex("J2", 2, 220, 245, 80),      # 空闲 30s
    ]
    d = audit_post(payload).json()
    assert d["status"] == "COMPLETED"
    rp = d["replay"]
    # 实测耗时（非 print_seconds）被采用
    assert [x["print_seconds"] for x in rp] == [55, 35, 85, 25]
    assert [x["idle_seconds_before"] for x in rp] == [0, 15, 0, 30]
    # 起止时刻原样回显，未被重排
    assert [(x["start_seconds"], x["end_seconds"]) for x in rp] == [
        (0, 55), (70, 105), (105, 190), (220, 245)
    ]
    assert d["makespan_seconds"] == 245
    # J2 实测完工 245 > 截止 220：逾期 25；J1 完工 105 ≤ 120
    assert d["total_tardiness_seconds"] == 25
    j2 = next(j for j in d["job_tardiness"] if j["job_id"] == "J2")
    assert j2["completion_seconds"] == 245 and j2["tardy_seconds"] == 25


# ------------------------------ IN_PROGRESS ------------------------------

def test_audit_in_progress_prefix_no_tardiness_for_unfinished():
    """只提交前缀：进行中；未完工的作业即使已过截止也不计逾期。"""
    payload = base_plan()
    payload["jobs"][1]["deadline_seconds"] = 50  # J2 显然已逾期，但未完工
    payload["executions"] = [
        ex("J1", 1, 0, 60, 0),
        ex("J2", 1, 100, 180, 120),  # PLA→ABS，留 40s
    ]
    d = audit_post(payload).json()
    assert d["status"] == "IN_PROGRESS"
    assert d["violations"] == []
    assert d["segments_executed"] == 2
    assert d["purge_used_mm3"] == 120
    assert d["purge_remaining_mm3"] == 280
    # J1 也未完工（缺第 2 段）：没有任何作业进入逾期明细
    assert d["job_tardiness"] == []
    assert d["total_tardiness_seconds"] == 0
    assert d["makespan_seconds"] == 180


def test_audit_completed_job_tardiness_counted_in_prefix():
    """前缀中已全部完工的作业计逾期，未完工的不计。"""
    payload = base_plan()
    payload["executions"] = [
        ex("J1", 1, 0, 60, 0),
        ex("J1", 2, 70, 150, 120),   # J1 完工 150 > 截止 120 → 逾期 30
        ex("J2", 1, 150, 230, 0),    # ABS 同材料；J2 未完工
    ]
    d = audit_post(payload).json()
    assert d["status"] == "IN_PROGRESS"
    assert [(j["job_id"], j["tardy_seconds"]) for j in d["job_tardiness"]] == [
        ("J1", 30)
    ]
    assert d["total_tardiness_seconds"] == 30


def test_audit_empty_executions_is_in_progress():
    payload = base_plan()
    payload["executions"] = []
    d = audit_post(payload).json()
    assert d["status"] == "IN_PROGRESS"
    assert d["segments_executed"] == 0
    assert d["purge_used_mm3"] == 0
    assert d["purge_remaining_mm3"] == 400
    assert d["makespan_seconds"] == 0
    assert d["job_tardiness"] == [] and d["violations"] == []


# --------------------------- 交错 / 首段换料 ---------------------------

def test_audit_cross_job_interleaving_is_legal():
    """跨作业交错合法，只要各作业层序正确、换料约束满足。"""
    payload = base_plan()
    payload["executions"] = [
        ex("J1", 1, 0, 60, 0),       # PLA
        ex("J2", 1, 70, 150, 120),   # PLA→ABS
        ex("J1", 2, 150, 180, 0),    # ABS 同材料，交错回 J1
        ex("J2", 2, 190, 210, 80),   # ABS→PLA
    ]
    d = audit_post(payload).json()
    assert d["status"] == "COMPLETED", d["violations"]
    assert [x["job_id"] for x in d["replay"]] == ["J1", "J2", "J1", "J2"]


def test_audit_first_segment_switch_from_initial_material():
    """首段材料与初始材料不同：同样须留 switch_seconds 且 purge 不低于矩阵。"""
    payload = base_plan()
    # 留时不足
    payload["executions"] = [ex("J2", 1, 5, 85, 120)]  # 仅 5s < 10s
    d = audit_post(payload).json()
    assert d["status"] == "VIOLATED"
    codes = [v["code"] for v in d["violations"]]
    assert codes == ["SWITCH_TIME_INSUFFICIENT"]
    v = d["violations"][0]
    assert v["detail"]["from_material"] == "PLA"
    assert v["detail"]["to_material"] == "ABS"

    # purge 不足（留时足够）
    payload["executions"] = [ex("J2", 1, 10, 90, 100)]
    d = audit_post(payload).json()
    assert [v["code"] for v in d["violations"]] == ["PURGE_SHORTFALL"]

    # 两者都满足 → 进行中且无违规
    payload["executions"] = [ex("J2", 1, 10, 90, 120)]
    d = audit_post(payload).json()
    assert d["status"] == "IN_PROGRESS" and d["violations"] == []
    assert d["purge_used_mm3"] == 120


# ------------------------------- 违规 -------------------------------

def test_audit_overlap_is_violation_and_not_completed():
    payload = base_plan()
    payload["executions"] = [
        ex("J1", 1, 0, 60, 0),
        ex("J1", 2, 70, 100, 120),
        ex("J2", 1, 100, 180, 0),
        ex("J2", 2, 170, 190, 80),   # 170 < 上一段结束 180：重叠
    ]
    d = audit_post(payload).json()
    assert d["status"] == "VIOLATED"  # 全部层段已提交也不能标完成
    assert d["segments_executed"] == 4
    v = d["violations"]
    assert len(v) == 1
    assert v[0]["code"] == "OVERLAP"
    assert v[0]["order"] == 4
    assert v[0]["detail"] == {
        "start_seconds": 170,
        "previous_end_seconds": 180,
    }
    # 重叠段的空闲为负值
    assert d["replay"][3]["idle_seconds_before"] == -10


def test_audit_layer_order_invalid():
    # 换料留时与 purge 都满足，唯独层序非法：先打 J1 第 2 段（ABS）
    payload = base_plan()
    payload["executions"] = [
        ex("J1", 2, 10, 40, 120),      # PLA→ABS 留足 10s、purge 120，但第 1 段未执行
        ex("J1", 1, 50, 110, 80),      # ABS→PLA 留足 10s、purge 80
    ]
    d = audit_post(payload).json()
    assert d["status"] == "VIOLATED"
    assert len(d["violations"]) == 1
    v = d["violations"][0]
    assert v["code"] == "LAYER_ORDER_INVALID"
    assert v["order"] == 1 and v["job_id"] == "J1" and v["segment_index"] == 2
    assert v["detail"] == {"missing_predecessor": 1}


def test_audit_switch_time_insufficient():
    """换料前空闲存在但不足 switch_seconds：违规（留 0 也算不足）。"""
    payload = base_plan()
    payload["executions"] = [
        ex("J1", 1, 0, 60, 0),
        ex("J1", 2, 66, 96, 120),     # 空闲 6 < 10
    ]
    d = audit_post(payload).json()
    assert d["status"] == "VIOLATED"
    v = d["violations"]
    assert [x["code"] for x in v] == ["SWITCH_TIME_INSUFFICIENT"]
    assert v[0]["detail"]["gap_seconds"] == 6
    assert v[0]["detail"]["required_switch_seconds"] == 10


def test_audit_purge_shortfall_but_actual_charged():
    """段前 purge 低于矩阵需求：违规；容量仍按实测值扣减。"""
    payload = base_plan()
    payload["executions"] = [
        ex("J1", 1, 0, 60, 0),
        ex("J1", 2, 70, 100, 100),   # 需求 120，实测 100
    ]
    d = audit_post(payload).json()
    assert d["status"] == "VIOLATED"
    v = d["violations"]
    assert [x["code"] for x in v] == ["PURGE_SHORTFALL"]
    assert v[0]["detail"]["purge_before_mm3"] == 100
    assert v[0]["detail"]["purge_required_mm3"] == 120
    assert d["purge_used_mm3"] == 100          # 按实测扣
    assert d["purge_remaining_mm3"] == 300


def test_audit_same_material_purge_must_be_zero():
    payload = base_plan()
    payload["executions"] = [
        ex("J1", 1, 0, 60, 0),
        ex("J1", 2, 70, 100, 120),
        ex("J2", 1, 100, 180, 30),  # ABS 同材料相邻，段前 purge 必须为 0
    ]
    d = audit_post(payload).json()
    assert d["status"] == "VIOLATED"
    v = d["violations"]
    assert [x["code"] for x in v] == ["SAME_MATERIAL_PURGE_NONZERO"]
    assert v[0]["detail"]["material_id"] == "ABS"
    assert d["purge_used_mm3"] == 150  # 120 + 实测 30 仍扣容量
    assert d["purge_remaining_mm3"] == 250


def test_audit_capacity_exceeded():
    payload = base_plan()
    payload["total_purge_capacity_mm3"] = 150
    payload["executions"] = [
        ex("J1", 1, 0, 60, 0),
        ex("J1", 2, 70, 100, 120),   # 累计 120 ≤ 150
        ex("J2", 1, 100, 180, 0),
        ex("J2", 2, 190, 210, 80),   # 累计 200 > 150
    ]
    d = audit_post(payload).json()
    assert d["status"] == "VIOLATED"
    v = [x for x in d["violations"] if x["code"] == "PURGE_CAPACITY_EXCEEDED"]
    assert len(v) == 1 and v[0]["order"] == 4
    assert v[0]["detail"] == {"purge_used_mm3": 200, "capacity_mm3": 150}
    assert d["purge_used_mm3"] == 200
    assert d["purge_remaining_mm3"] == -50   # 超容时余量为负，如实返回


def test_audit_multiple_violations_collected():
    """同一层段可同时触发多条违规；重叠时不重复记留时不足。"""
    payload = base_plan()
    payload["total_purge_capacity_mm3"] = 400
    payload["executions"] = [
        ex("J1", 1, 0, 60, 0),
        ex("J1", 2, 65, 95, 50),      # 留时不足(6<10) + purge 不足(50<120)
    ]
    d = audit_post(payload).json()
    assert sorted(v["code"] for v in d["violations"]) == [
        "PURGE_SHORTFALL", "SWITCH_TIME_INSUFFICIENT"
    ]
    assert all(v["order"] == 2 for v in d["violations"])

    # 重叠 + purge 不足：只记 OVERLAP 与 PURGE_SHORTFALL，留时不重复记
    payload["executions"] = [
        ex("J1", 1, 0, 60, 0),
        ex("J1", 2, 50, 80, 50),
    ]
    d = audit_post(payload).json()
    assert sorted(v["code"] for v in d["violations"]) == [
        "OVERLAP", "PURGE_SHORTFALL"
    ]


def test_audit_zero_duration_segments_allowed():
    """零耗时层段首尾相接（start==end==上一段结束）不算重叠。"""
    payload = base_plan()
    payload["jobs"][0]["segments"][0]["print_seconds"] = 0
    payload["executions"] = [
        ex("J1", 1, 0, 0, 0),
        ex("J1", 2, 10, 40, 120),
    ]
    d = audit_post(payload).json()
    # J2 未执行；J1 已完工；无违规
    assert d["status"] == "IN_PROGRESS"
    assert d["violations"] == []
    assert d["replay"][0]["print_seconds"] == 0


def test_audit_violated_still_reports_completed_job_tardiness():
    """VIOLATED 不影响逾期核算：已完工的作业逾期仍须报告。"""
    payload = base_plan()
    payload["executions"] = [
        ex("J1", 1, 0, 60, 0),
        ex("J2", 1, 70, 150, 120),
        ex("J1", 2, 150, 200, 0),     # 同材料 ABS，段前 purge 0：合法
        ex("J2", 2, 205, 225, 80),    # 空闲 5 < 10：换料留时违规
    ]
    d = audit_post(payload).json()
    assert d["status"] == "VIOLATED"
    assert [v["code"] for v in d["violations"]] == ["SWITCH_TIME_INSUFFICIENT"]
    # 两段作业均已集齐 → 完工；J1 完工 200 逾期 80，J2 完工 225 逾期 5
    assert {(j["job_id"], j["tardy_seconds"]) for j in d["job_tardiness"]} == {
        ("J1", 80), ("J2", 5)
    }
    assert d["total_tardiness_seconds"] == 85
    assert d["makespan_seconds"] == 225


def test_audit_switch_seconds_zero_needs_no_gap():
    """switch_seconds=0：紧邻（空闲 0）换料合法。"""
    payload = {
        "jobs": [
            {"job_id": "J1", "deadline_seconds": 100, "segments": [
                {"material_id": "A", "print_seconds": 10, "volume_mm3": 1},
                {"material_id": "B", "print_seconds": 10, "volume_mm3": 1}]},
            {"job_id": "J2", "deadline_seconds": 100, "segments": [
                {"material_id": "A", "print_seconds": 10, "volume_mm3": 1}]},
        ],
        "initial_material": "A",
        "purge_matrix": {"A": {"A": 0, "B": 7}, "B": {"A": 3, "B": 0}},
        "switch_seconds": 0,
        "total_purge_capacity_mm3": 100,
        "executions": [
            ex("J1", 1, 0, 10, 0),
            ex("J1", 2, 10, 20, 7),   # A→B 紧邻，purge 达标
            ex("J2", 1, 20, 30, 3),   # B→A 紧邻
        ],
    }
    d = audit_post(payload).json()
    # 三段即全部层段：紧邻换料合法，全部完成
    assert d["status"] == "COMPLETED"
    assert d["violations"] == []
    assert d["purge_used_mm3"] == 10


def test_audit_extra_purge_for_switch_is_allowed_and_charged():
    """换料时段前 purge 高于矩阵需求合法（不低于即可），按实测值扣容量。"""
    payload = base_plan()
    payload["executions"] = [
        ex("J1", 1, 0, 60, 0),
        ex("J1", 2, 70, 100, 150),   # 需求 120，实测 150：允许
    ]
    d = audit_post(payload).json()
    assert d["status"] == "IN_PROGRESS"
    assert d["violations"] == []
    assert d["purge_used_mm3"] == 150
    assert d["purge_remaining_mm3"] == 250


# ------------------------------- 422 -------------------------------

def test_audit_422_cases():
    good_execs = [ex("J1", 1, 0, 60, 0)]

    cases = {
        "unknown job": dict(base_plan(), executions=[
            ex("ZZ", 1, 0, 10, 0)]),
        "unknown segment": dict(base_plan(), executions=[
            ex("J1", 9, 0, 10, 0)]),
        "duplicate segment": dict(base_plan(), executions=[
            ex("J1", 1, 0, 10, 0), ex("J1", 1, 10, 20, 0)]),
        "inverted interval": dict(base_plan(), executions=[
            ex("J1", 1, 60, 10, 0)]),
        "negative start": dict(base_plan(), executions=[
            {**ex("J1", 1, 0, 10, 0), "start_seconds": -1}]),
        "negative purge": dict(base_plan(), executions=[
            {**ex("J1", 1, 0, 10, 0), "purge_before_mm3": -1}]),
        "segment index zero": dict(base_plan(), executions=[
            {**ex("J1", 1, 0, 10, 0), "segment_index": 0}]),
        "missing executions": base_plan(),
        "extra field": dict(base_plan(), executions=good_execs, foo=1),
        "bad plan param (one job)": dict(base_plan(),
                                         jobs=base_plan()["jobs"][:1],
                                         executions=good_execs),
        "bad purge matrix": dict(base_plan(), executions=good_execs,
                                 purge_matrix={"PLA": {"PLA": 0}}),
    }
    for name, payload in cases.items():
        r = audit_post(payload)
        assert r.status_code == 422, name
