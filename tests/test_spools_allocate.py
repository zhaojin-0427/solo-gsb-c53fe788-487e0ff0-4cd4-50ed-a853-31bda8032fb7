"""端到端测试：POST /spools/allocate 的固定顺序料卷分配。"""

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


def _seg(material: str, volume: int, seconds: int = 10) -> dict:
    return {"material_id": material, "print_seconds": seconds, "volume_mm3": volume}


def _base(
    jobs,
    spools,
    order,
    *,
    initial="A",
    purge=None,
    switch_seconds=0,
    capacity=10**6,
    time_limit=None,
) -> dict:
    materials = {initial}
    for job in jobs:
        for seg in job["segments"]:
            materials.add(seg["material_id"])
    if purge is None:
        mats = sorted(materials)
        # 默认所有切换 purge 均为 0：聚焦选卷逻辑；需要 purge 的用例显式传入
        purge = {a: {b: 0 for b in mats} for a in mats}
    payload = {
        "jobs": jobs,
        "initial_material": initial,
        "purge_matrix": purge,
        "switch_seconds": switch_seconds,
        "total_purge_capacity_mm3": capacity,
        "spools": spools,
        "order": order,
    }
    if time_limit is not None:
        payload["time_limit_seconds"] = time_limit
    return payload


def _job(jid, *segs, deadline=10**9):
    return {"job_id": jid, "deadline_seconds": deadline, "segments": list(segs)}


def _ref(jid, k):
    return {"job_id": jid, "segment_index": k}


def _spool(sid, material, remaining):
    return {"spool_id": sid, "material_id": material, "remaining_mm3": remaining}


# ------------------------------- 示例与基本扣减 -------------------------------


def test_example_allocated_and_response_consistent():
    payload = load_example("request_spools_allocate.json")
    r = post(payload)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "ALLOCATED"
    assert d["optimal"] is True

    alloc = d["allocation"]
    assert len(alloc) == 4
    # 逐段选卷（沿顺序 J1.1 J1.2 J2.1 J2.2）
    assert [(a["order"], a["spool_id"], a["deducted_mm3"]) for a in alloc] == [
        (1, "PLA-A", 100),   # 打印 100，首段与初始材料相同，无 purge
        (2, "ABS-A", 160),   # PLA→ABS purge 120 + 打印 40
        (3, "ABS-B", 120),   # 同材料相邻无 purge；ABS-A 仅剩 0 必须换卷
        (4, "PLA-B", 110),   # ABS→PLA purge 80 + 打印 30
    ]
    # 换料处必然换卷；同材料相邻的 J1.2→J2.1 因 ABS-A 余量不足也换卷
    assert [a["spool_changed"] for a in alloc] == [False, True, True, True]
    assert d["spool_changes"] == 3
    assert d["purge_used_mm3"] == 200
    assert d["purge_remaining_mm3"] == 200

    # 每段扣减 = purge + 打印体积，且从目标材料料卷扣
    for a in alloc:
        assert a["deducted_mm3"] == a["purge_before_mm3"] + a["print_volume_mm3"]

    # 各卷余量与逐次扣减后余量自洽
    for sp in d["spools"]:
        assert sp["remaining_mm3"] == (
            sp["initial_remaining_mm3"] - sp["deducted_mm3"]
        )
        assert sum(x["deducted_mm3"] for x in sp["deductions"]) == sp["deducted_mm3"]
        running = sp["initial_remaining_mm3"]
        for x in sp["deductions"]:
            running -= x["deducted_mm3"]
            assert x["remaining_after_mm3"] == running
        assert sp["remaining_mm3"] >= 0

    # 料卷按 ID 排序；未使用卷也会原样列出（本示例全部用尽）
    assert [sp["spool_id"] for sp in d["spools"]] == [
        "ABS-A", "ABS-B", "PLA-A", "PLA-B",
    ]
    # PLA-A 扣 100、ABS-A 扣 160、PLA-B 扣 110 均用尽；ABS-B 扣 120 余 80
    assert {sp["spool_id"]: sp["remaining_mm3"] for sp in d["spools"]} == {
        "ABS-A": 0,
        "ABS-B": 80,
        "PLA-A": 0,
        "PLA-B": 0,
    }


def test_purge_charged_to_target_material_spool():
    """换料 purge 从换入（目标）材料的料卷扣除，而非换出材料。"""
    payload = _base(
        [_job("J1", _seg("A", 5), _seg("B", 7)), _job("J2", _seg("A", 1))],
        [_spool("A1", "A", 100), _spool("B1", "B", 100)],
        [_ref("J1", 1), _ref("J1", 2), _ref("J2", 1)],
        initial="A",
        purge={"A": {"A": 0, "B": 30}, "B": {"A": 20, "B": 0}},
        capacity=100,
    )
    d = post(payload).json()
    assert d["status"] == "ALLOCATED"
    by_order = {a["order"]: a for a in d["allocation"]}
    # J1.2：A→B purge 30 + 打印 7 = 37，从 B1 扣
    assert by_order[2]["spool_id"] == "B1"
    assert by_order[2]["purge_before_mm3"] == 30
    assert by_order[2]["deducted_mm3"] == 37
    # J2.1：B→A purge 20 + 打印 1 = 21，从 A1 扣（复用 A1，A1 先扣过 5）
    assert by_order[3]["spool_id"] == "A1"
    assert by_order[3]["deducted_mm3"] == 21
    bal = {sp["spool_id"]: sp for sp in d["spools"]}
    assert bal["A1"]["deducted_mm3"] == 26
    assert bal["A1"]["remaining_mm3"] == 74
    assert bal["B1"]["deducted_mm3"] == 37
    assert d["purge_used_mm3"] == 50
    assert d["purge_remaining_mm3"] == 50


def test_initial_material_purge_in_allocate():
    """首段材料与初始材料不同：首次 purge 沿顺序照常计算并扣目标材料卷。"""
    payload = _base(
        [_job("J1", _seg("B", 5)), _job("J2", _seg("A", 1))],
        [_spool("A1", "A", 100), _spool("B1", "B", 100)],
        [_ref("J1", 1), _ref("J2", 1)],
        initial="A",
        purge={"A": {"A": 0, "B": 40}, "B": {"A": 10, "B": 0}},
    )
    d = post(payload).json()
    assert d["allocation"][0]["material_switched"] is True
    assert d["allocation"][0]["purge_before_mm3"] == 40
    assert d["allocation"][0]["deducted_mm3"] == 45
    assert d["purge_used_mm3"] == 50


# --------------------- 卸下料卷可复用 / 变化次数最小 ---------------------


def test_unloaded_spool_reused_without_extra_change():
    """A 段 → B 段 → A 段：先卸下的 A1 可再装回复用，只产生换料必然换卷。"""
    payload = _base(
        [_job("J1", _seg("A", 10), _seg("B", 10), _seg("A", 10)),
         _job("J2", _seg("A", 5))],
        [_spool("A1", "A", 100), _spool("A2", "A", 100), _spool("B1", "B", 100)],
        [_ref("J1", 1), _ref("J1", 2), _ref("J1", 3), _ref("J2", 1)],
    )
    d = post(payload).json()
    assert d["status"] == "ALLOCATED"
    picks = [a["spool_id"] for a in d["allocation"]]
    # 顺序 A B A A：两处材料边界必然换卷；两处 A 边界（2→3 跨边界、3→4 同材料）
    # 复用 A1 使同材料相邻的第 3→4 段不额外换卷
    assert picks == ["A1", "B1", "A1", "A1"]
    # 总变化次数 2（A→B、B→A）；同材料相邻的 3→4 沿用同卷
    assert d["spool_changes"] == 2
    # 仅看同材料（无换料）的层段：首段与第 4 段不换卷；第 3 段是 B→A 换料
    no_switch = {a["order"]: a["spool_changed"]
                 for a in d["allocation"] if not a["material_switched"]}
    assert no_switch == {1: False, 4: False}
    assert d["optimal"] is True


def test_reuse_same_spool_across_two_separated_runs():
    """回归：同一材料被别的材料隔开成两个 run 时，两个 run 都复用同一卷，
    跨 run 选卷不应被错误计入额外换卷（仅保留材料边界的必然换卷）。

    顺序 A B B A B B：B 的两个 run（位置 2-3、5-6）都应选 B1；
    全局换卷发生在 A→B（1→2）、B→A（3→4）、A→B（4→5）三处材料边界，
    没有因选卷产生额外变化。
    """
    payload = _base(
        [_job("J1", _seg("A", 1), _seg("A", 6), _seg("B", 0)),
         _job("J2", _seg("B", 1), _seg("B", 2), _seg("B", 0))],
        [_spool("A0", "A", 5), _spool("A3", "A", 11),
         _spool("B1", "B", 9), _spool("B2", "B", 12)],
        # 全局顺序 A B B A B B：
        # J1.1(A), J2.1(B), J2.2(B), J1.2(A), J2.3(B), J1.3(B)
        [_ref("J1", 1), _ref("J2", 1), _ref("J2", 2),
         _ref("J1", 2), _ref("J2", 3), _ref("J1", 3)],
        initial="A",
        purge={"A": {"A": 0, "B": 0}, "B": {"A": 0, "B": 0}},
    )
    d = post(payload).json()
    assert d["status"] == "ALLOCATED"
    assert [(a["order"], a["job_id"], a["segment_index"], a["material_id"], a["spool_id"])
            for a in d["allocation"]] == [
        (1, "J1", 1, "A", "A0"),
        (2, "J2", 1, "B", "B1"),
        (3, "J2", 2, "B", "B1"),
        (4, "J1", 2, "A", "A3"),
        (5, "J2", 3, "B", "B1"),
        (6, "J1", 3, "B", "B1"),
    ]
    # B1 累计负载 1+2+0+0 = 3 ≤ 9：卸下再装回复用成立
    b1 = next(sp for sp in d["spools"] if sp["spool_id"] == "B1")
    assert b1["deducted_mm3"] == 3
    assert b1["remaining_mm3"] == 6
    # 三处材料边界必然换卷；B 的两个 run 内部沿用 B1、跨 run 复用 B1，
    # 不产生材料边界之外的额外换卷
    assert d["spool_changes"] == 3
    assert d["optimal"] is True


def test_minimize_changes_prefers_one_spool_for_run():
    """同材料连续三段，一卷足以容纳时必须只用一卷（变化次数 0）。"""
    payload = _base(
        [_job("J1", _seg("A", 5), _seg("A", 5), _seg("A", 5)),
         _job("J2", _seg("B", 1))],
        [_spool("A1", "A", 100), _spool("A2", "A", 100), _spool("B1", "B", 10)],
        [_ref("J1", 1), _ref("J1", 2), _ref("J1", 3), _ref("J2", 1)],
    )
    d = post(payload).json()
    assert [a["spool_id"] for a in d["allocation"][:3]] == ["A1"] * 3
    # A 内部 0 次变化；仅 A→B 边界必然换卷 1 次
    assert d["spool_changes"] == 1


def test_four_six_spools_must_be_allocatable():
    """回归：同材料连续两段依次需 4、6，余量恰为 4、6 的两卷必须能分配。

    第一段用余量 4 的卷（扣 4）、第二段用余量 6 的卷（扣 6），不得误判缺料。
    """
    payload = _base(
        [_job("J1", _seg("A", 4), _seg("A", 6)), _job("J2", _seg("B", 1))],
        [_spool("S4", "A", 4), _spool("S6", "A", 6), _spool("SB", "B", 10)],
        [_ref("J1", 1), _ref("J1", 2), _ref("J2", 1)],
        initial="A",
        purge={"A": {"A": 0, "B": 0}, "B": {"A": 0, "B": 0}},
        capacity=0,
    )
    d = post(payload)
    assert d.status_code == 200, d.text
    body = d.json()
    assert body["status"] == "ALLOCATED"
    picks = [(a["order"], a["spool_id"], a["deducted_mm3"]) for a in body["allocation"]]
    assert picks == [(1, "S4", 4), (2, "S6", 6), (3, "SB", 1)]
    # 两段 A 之间被迫换卷 1 次（容量所限），A→B 边界必然换卷 1 次
    assert body["spool_changes"] == 2
    assert body["optimal"] is True
    assert {sp["spool_id"]: sp["remaining_mm3"] for sp in body["spools"]} == {
        "S4": 0, "S6": 0, "SB": 9,
    }


def test_lexicographic_tiebreak_smallest_spool_id_sequence():
    """变化次数相同时，逐段料卷 ID 元组字典序最小：S1,S1,S2 优于 S2,S2,S1。"""
    payload = _base(
        [_job("J1", _seg("A", 5), _seg("A", 5), _seg("A", 5)),
         _job("J2", _seg("B", 1))],
        # 每卷最多容纳两段：最优必须换 1 次（同材料内部），决胜选最小 ID 序列
        [_spool("S2", "A", 10), _spool("S1", "A", 10), _spool("B1", "B", 10)],
        [_ref("J1", 1), _ref("J1", 2), _ref("J1", 3), _ref("J2", 1)],
    )
    d = post(payload).json()
    assert d["status"] == "ALLOCATED"
    assert [a["spool_id"] for a in d["allocation"][:3]] == ["S1", "S1", "S2"]
    assert d["spool_changes"] == 2  # A 内 1 次 + A→B 边界 1 次
    assert d["optimal"] is True


def test_lex_tiebreak_respects_change_primacy():
    """字典序不得牺牲变化次数：若最小 ID 卷容量会多换一次，则让位给更大 ID 卷。

    三段各需 5：small=6（只能装一段），big=100（可装三段，0 次内部换卷）。
    虽然 small 字典序更小，但首段选 small 会多产生 1 次变化，故选 big 开头。
    """
    payload = _base(
        [_job("J1", _seg("A", 5), _seg("A", 5), _seg("A", 5)),
         _job("J2", _seg("B", 1))],
        [_spool("small", "A", 6), _spool("big", "A", 100), _spool("B1", "B", 10)],
        [_ref("J1", 1), _ref("J1", 2), _ref("J1", 3), _ref("J2", 1)],
    )
    d = post(payload).json()
    assert [a["spool_id"] for a in d["allocation"][:3]] == ["big"] * 3
    assert d["spool_changes"] == 1  # 仅 A→B 边界
    assert d["optimal"] is True


def test_cross_job_interleave_order_honored():
    """跨作业交错的固定顺序被严格遵守，不重排；作业内层序仍须合法（422 另测）。"""
    payload = _base(
        [_job("J1", _seg("A", 10), _seg("B", 10)),
         _job("J2", _seg("A", 20), _seg("B", 20))],
        [_spool("A1", "A", 100), _spool("B1", "B", 100)],
        [_ref("J1", 1), _ref("J2", 1), _ref("J1", 2), _ref("J2", 2)],
    )
    d = post(payload).json()
    assert d["status"] == "ALLOCATED"
    assert [(a["job_id"], a["segment_index"]) for a in d["allocation"]] == [
        ("J1", 1), ("J2", 1), ("J1", 2), ("J2", 2),
    ]
    # A A 相邻沿用 A1，B B 相邻沿用 B1：仅 1 次换卷（A→B）
    assert [a["spool_id"] for a in d["allocation"]] == ["A1", "A1", "B1", "B1"]
    assert d["spool_changes"] == 1


# ------------------------------- 缺料 / 超容 -------------------------------


def test_material_shortage_returns_status_and_no_allocation():
    payload = _base(
        [_job("J1", _seg("A", 10), _seg("B", 10)), _job("J2", _seg("A", 10))],
        [_spool("A1", "A", 5), _spool("B1", "B", 100)],
        [_ref("J1", 1), _ref("J1", 2), _ref("J2", 1)],
    )
    d = post(payload).json()
    assert d["status"] == "MATERIAL_SHORTAGE"
    assert d["optimal"] is False
    assert d["allocation"] == []
    assert d["spool_changes"] == 0
    assert d["reason"]["code"] == "MATERIAL_SHORTAGE"
    deficit = d["reason"]["deficits"][0]
    assert deficit["material_id"] == "A"
    assert deficit["required_mm3"] == 20
    assert deficit["available_mm3"] == 5
    assert deficit["shortage_mm3"] == 15
    # 无分配：各卷累计扣减为 0、余量等于初始
    assert all(sp["deducted_mm3"] == 0 for sp in d["spools"])
    assert all(sp["deductions"] == [] for sp in d["spools"])
    assert {sp["spool_id"]: sp["remaining_mm3"] for sp in d["spools"]} == {
        "A1": 5, "B1": 100,
    }


def test_material_with_no_spool_is_shortage():
    """某层段材料在库存中完全没有料卷：缺料（available 为 0），不触发 500。"""
    payload = _base(
        [_job("J1", _seg("A", 10), _seg("C", 5)), _job("J2", _seg("A", 1))],
        [_spool("A1", "A", 100)],
        [_ref("J1", 1), _ref("J1", 2), _ref("J2", 1)],
    )
    r = post(payload)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "MATERIAL_SHORTAGE"
    assert d["allocation"] == []
    deficit = next(x for x in d["reason"]["deficits"] if x["material_id"] == "C")
    assert deficit["available_mm3"] == 0
    assert deficit["required_mm3"] == 5
    assert deficit["shortage_mm3"] == 5


def test_purge_capacity_exceeded_returns_status_and_no_allocation():
    payload = _base(
        [_job("J1", _seg("A", 10), _seg("B", 10)), _job("J2", _seg("B", 5))],
        [_spool("A1", "A", 1000), _spool("B1", "B", 1000)],
        [_ref("J1", 1), _ref("J1", 2), _ref("J2", 1)],
        purge={"A": {"A": 0, "B": 30}, "B": {"A": 30, "B": 0}},
        capacity=10,
    )
    d = post(payload).json()
    assert d["status"] == "PURGE_CAPACITY_EXCEEDED"
    assert d["optimal"] is False
    assert d["allocation"] == []
    assert d["reason"]["code"] == "PURGE_CAPACITY_EXCEEDED"
    assert d["reason"]["purge_required_mm3"] == 30
    assert d["reason"]["capacity_mm3"] == 10
    assert d["purge_used_mm3"] == 30
    assert d["purge_remaining_mm3"] == -20
    assert all(sp["deducted_mm3"] == 0 for sp in d["spools"])


def test_over_capacity_takes_precedence_over_shortage():
    """顺序 purge 已超容时直接报超容，即使库存本身也不足。"""
    payload = _base(
        [_job("J1", _seg("A", 10), _seg("B", 10)), _job("J2", _seg("B", 5))],
        [_spool("A1", "A", 1), _spool("B1", "B", 1)],
        [_ref("J1", 1), _ref("J1", 2), _ref("J2", 1)],
        purge={"A": {"A": 0, "B": 30}, "B": {"A": 30, "B": 0}},
        capacity=10,
    )
    d = post(payload).json()
    assert d["status"] == "PURGE_CAPACITY_EXCEEDED"


def test_fragmentation_reported_as_shortage():
    """总量够但单卷容量碎片化、逐段整卷扣减无法装下：缺料且无分配。

    连续三段 A 各需 10，库存 A1=9、A2=21（总量 30 恰好够）。首段只能选 A2
    （A1 装不下任何一段），扣 10 后 A2 剩 11 再装一段，第三段时 A1=9 仍装
    不下、A2 仅余 1，没有任何可行选卷组合。
    """
    payload = _base(
        [_job("J1", _seg("A", 10), _seg("A", 10), _seg("A", 10)),
         _job("J2", _seg("B", 1))],
        [_spool("A1", "A", 9), _spool("A2", "A", 21), _spool("B1", "B", 10)],
        [_ref("J1", 1), _ref("J1", 2), _ref("J1", 3), _ref("J2", 1)],
    )
    d = post(payload).json()
    assert d["status"] == "MATERIAL_SHORTAGE"
    assert d["allocation"] == []
    assert d["reason"]["deficits"][0]["material_id"] == "A"
    assert all(sp["deducted_mm3"] == 0 for sp in d["spools"])


# --------------------------------- 422 ---------------------------------


def test_422_cases():
    base = load_example("request_spools_allocate.json")

    def with_order(order):
        p = json.loads(json.dumps(base))
        p["order"] = order
        return p

    cases = {
        "missing segment": with_order(base["order"][:3]),
        "duplicate segment": with_order(
            [_ref("J1", 1)] + base["order"]
        ),
        "job internal order shuffled": with_order(
            [_ref("J1", 2), _ref("J1", 1), _ref("J2", 1), _ref("J2", 2)]
        ),
        "unknown job": with_order(
            [_ref("NOPE", 1), _ref("J1", 2), _ref("J2", 1), _ref("J2", 2)]
        ),
        "unknown segment": with_order(
            [_ref("J1", 9), _ref("J1", 2), _ref("J2", 1), _ref("J2", 2)]
        ),
        "segment index zero": with_order(
            [_ref("J1", 0), _ref("J1", 2), _ref("J2", 1), _ref("J2", 2)]
        ),
    }
    for name, payload in cases.items():
        r = post(payload)
        assert r.status_code == 422, name

    bad_spools = {
        "duplicate spool id": (
            lambda p: (p["spools"][1].__setitem__("spool_id", "PLA-A"), p)[1]
        ),
        "negative remaining": (
            lambda p: (p["spools"][0].__setitem__("remaining_mm3", -1), p)[1]
        ),
        "blank spool id": (
            lambda p: (p["spools"][0].__setitem__("spool_id", ""), p)[1]
        ),
        "extra spool field": (
            lambda p: (p["spools"][0].__setitem__("bogus", 1), p)[1]
        ),
    }
    for name, mutate in bad_spools.items():
        p = json.loads(json.dumps(base))
        mutate(p)
        r = post(p)
        assert r.status_code == 422, name

    for dele in ("spools", "order"):
        p = json.loads(json.dumps(base))
        del p[dele]
        assert post(p).status_code == 422, f"missing {dele}"

    p = json.loads(json.dumps(base))
    p["spools"] = []
    assert post(p).status_code == 422, "empty spools"

    # 继承 /plans 的校验（作业数、purge 矩阵完整性等）
    p = json.loads(json.dumps(base))
    p["jobs"] = p["jobs"][:1]
    assert post(p).status_code == 422, "one job inherited"
    p = json.loads(json.dumps(base))
    p["initial_material"] = "PETG"
    assert post(p).status_code == 422, "unknown material inherited"


# ----------------------------- 与既有接口兼容 -----------------------------


def test_compatible_with_plans_and_audit():
    """同样的规划参数：/plans、/executions/audit 行为不受新接口影响。"""
    plan_payload = {
        k: v
        for k, v in load_example("request_spools_allocate.json").items()
        if k in ("jobs", "initial_material", "purge_matrix",
                 "switch_seconds", "total_purge_capacity_mm3")
    }
    r = client.post("/plans", json=plan_payload)
    assert r.status_code == 200
    assert r.json()["status"] == "OPTIMAL"

    audit_payload = load_example("request_audit_completed.json")
    r = client.post("/executions/audit", json=audit_payload)
    assert r.status_code == 200
    assert r.json()["status"] == "COMPLETED"


def test_allocate_order_need_not_match_plans_optimal_order():
    """/spools/allocate 接受任意作业内层序合法的顺序（含非 /plans 最优顺序）。

    用足量库存，跨作业交错顺序 PLA,ABS,ABS,PLA 的 purge 为 120+0+80=200。
    """
    payload = _base(
        [_job("J1", _seg("PLA", 100, 60), _seg("ABS", 40, 30), deadline=120),
         _job("J2", _seg("ABS", 120, 80), _seg("PLA", 30, 20), deadline=220)],
        [_spool("ABS-A", "ABS", 1000), _spool("PLA-A", "PLA", 1000)],
        [_ref("J1", 1), _ref("J2", 1), _ref("J1", 2), _ref("J2", 2)],
        initial="PLA",
        purge={"PLA": {"PLA": 0, "ABS": 120}, "ABS": {"PLA": 80, "ABS": 0}},
        capacity=400,
    )
    r = post(payload)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "ALLOCATED"
    assert [(a["job_id"], a["segment_index"]) for a in d["allocation"]] == [
        ("J1", 1), ("J2", 1), ("J1", 2), ("J2", 2),
    ]
    assert d["purge_used_mm3"] == 200
    # 两段 ABS 相邻（顺序 2、3）、两段 PLA 被隔开：仅 PLA→ABS→PLA 两次换卷
    assert [a["material_switched"] for a in d["allocation"]] == [
        False, True, False, True
    ]
