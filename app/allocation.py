"""POST /spools/allocate 的料卷分配。

固定顺序回放
------------

层段顺序由调用方以 ``(作业 ID, 层序)`` 序列显式给定（校验见
:class:`app.schemas.AllocateRequest`：覆盖全部层段且每段恰好一次、作业内
严格按序），本模块**不重排**，只经现有 :func:`build_instance` 取出每段的
材料与名义打印体积，并沿顺序从初始材料计算 purge：

* 第 1 段材料与初始材料不同时，purge 取 ``purge_matrix[初始][材料]``；
* 第 k 段材料与第 k-1 段不同时，purge 取 ``purge_matrix[前材料][本材料]``；
  同材料相邻为 0。

每段的**换料 purge 与打印体积都从该段（目标材料）所选料卷扣除**，即该段
只选一卷、扣 ``purge + 打印体积``；料卷一经"卸下"就重新进入库存池，可被
后续任意不相邻的层段复用（只受累计扣减不超过其余量约束）。

三种终态
--------

* 沿固定顺序的 purge 之和超过 ``total_purge_capacity_mm3`` →
  ``PURGE_CAPACITY_EXCEEDED``，不产生任何分配；
* purge 不超容但某材料全部料卷余量之和不足以覆盖该材料的累计需求
  （purge + 打印体积）→ ``MATERIAL_SHORTAGE``，同样不产生任何分配；
  总量足够但单卷容量碎片化导致无法完整分配时也按缺料处理并给出缺料明细；
* 否则给出覆盖全部层段的完整分配（``ALLOCATED``）。

优化目标（严格优先级）
----------------------

换料处料卷必然变化、不计优化；只有同材料相邻段的选卷有选择余地。按材料
拆分后各材料相互独立，分别用 CP-SAT 求解：

1. **相邻层段料卷 ID 变化次数最小**（即尽量让同材料相邻段沿用同一卷，
   卸下后复用别的卷不会额外产生约束，只需累计扣减不超该卷余量）；
2. 变化次数相同时，逐段所选料卷 ID 的**字典序**最小——按顺序位置逐位
   固定 + CP-SAT 可行性证明，未在限时内完成证明则保守地标 ``optimal=false``。

特例（回归保证）：同材料连续两段分别需 4、6 时，余量恰为 4 与 6 的两卷
必须能分配——第一段用余量 4 的卷（扣 4）、第二段用余量 6 的卷（扣 6），
这是唯一可行分配，目标层面也不会被错误判成缺料。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from ortools.sat.python import cp_model

from .schemas import AllocateRequest
from .solver import build_instance

ALLOCATED = "ALLOCATED"
MATERIAL_SHORTAGE = "MATERIAL_SHORTAGE"
PURGE_CAPACITY_EXCEEDED = "PURGE_CAPACITY_EXCEEDED"


@dataclass
class FixedSegment:
    """固定顺序中的一个层段及其沿顺序计算出的 purge / 需求。"""

    order: int
    global_idx: int
    job_id: str
    segment_index: int
    material_id: str
    print_volume: int
    purge_before: int  # 换料 purge（同材料相邻 / 与初始材料相同为 0）
    material_switched: bool

    @property
    def demand(self) -> int:
        """该段从目标材料料卷扣除的总量。"""
        return self.print_volume + self.purge_before


def _fixed_segments(req: AllocateRequest) -> List[FixedSegment]:
    """经 build_instance 取材料与名义体积，沿固定顺序计算逐段 purge。"""
    inst = build_instance(req)
    idx_of: Dict[Tuple[str, int], int] = {}
    for jd in inst.jobs:
        for seg in jd.segments:
            idx_of[(jd.job_id, seg["seg_index"])] = seg["global_idx"]

    fixed: List[FixedSegment] = []
    cur_material = inst.initial_material
    for pos, order_ref in enumerate(req.order, start=1):
        gi = idx_of[(order_ref.job_id, order_ref.segment_index)]
        mat = inst.material_of[gi]
        switched = mat != cur_material
        purge = int(inst.purge_matrix[cur_material][mat]) if switched else 0
        fixed.append(
            FixedSegment(
                order=pos,
                global_idx=gi,
                job_id=order_ref.job_id,
                segment_index=order_ref.segment_index,
                material_id=mat,
                print_volume=inst.volume_of[gi],
                purge_before=purge,
                material_switched=switched,
            )
        )
        cur_material = mat
    return fixed


@dataclass
class _MaterialModel:
    model: cp_model.CpModel
    assign: Dict[Tuple[int, str], cp_model.IntVar]
    load: Dict[str, cp_model.IntVar]
    changes_var: cp_model.IntVar
    items: List[FixedSegment]
    spool_ids: List[str]
    capacities: Dict[str, int]
    # 该材料子序列中第 t 个层段与第 t-1 个层段在全局顺序中是否紧邻
    adjacent: List[bool]
    demands: List[int]


def _build_material_model(
    items: List[FixedSegment],
    spool_ids: List[str],
    capacities: Dict[str, int],
    adjacent: List[bool],
    *,
    force_assign: Optional[Dict[int, str]] = None,
    changes_eq: Optional[int] = None,
) -> _MaterialModel:
    """构建单材料的料卷分配 CP 模型。

    ``items`` 是该材料层段按全局顺序抽出的子序列；``adjacent[t]`` 表示
    子序列第 t 个层段与第 t-1 个在**全局顺序中是否紧邻**（``t=0`` 恒为
    ``False``）。布尔变量 ``x[t][s]`` 表示第 t 个层段选用卷 s（每段恰一
    卷）；``load[s] = Σ demand_t * x[t][s] ≤ 该卷余量``。

    目标变量统计的换卷次数：

    * ``adjacent[t] = True``（同材料且全局相邻）：沿用同卷则不换卷，换卷
      是可优化的选择；
    * ``adjacent[t] = False``（中间隔着别的材料）：全局意义上料卷必然变化，
      但卸下的卷可复用——这里不约束选卷、也不计入目标，跨 run 可自由同卷
      或异卷，只受累计负载约束。

    ``force_assign`` 用于字典序决胜阶段固定前缀选卷；``changes_eq`` 用于
    在已证最优的变化次数下做可行性判定。
    """
    m = cp_model.CpModel()
    q = len(items)
    demands = [it.demand for it in items]

    assign: Dict[Tuple[int, str], cp_model.IntVar] = {}
    for t in range(q):
        chosen = []
        for s in spool_ids:
            lit = m.NewBoolVar(f"x_{t}_{s}")
            assign[(t, s)] = lit
            chosen.append(lit)
        m.AddExactlyOne(chosen)

    load: Dict[str, cp_model.IntVar] = {}
    for s in spool_ids:
        terms = [assign[(t, s)] * demands[t] for t in range(q)]
        var = m.NewIntVar(0, capacities[s], f"load_{s}")
        m.Add(var == sum(terms))
        load[s] = var

    change_terms = []
    for t in range(1, q):
        if not adjacent[t]:
            # 跨材料边界：卷必然变化，不进入可优化目标
            continue
        # both_s = 1 当且仅当 t-1 与 t 两段同选卷 s；每段恰选一卷，
        # 故 Σ both_s 恰为 0/1，为 1 即两段沿用同一卷
        both = []
        for s in spool_ids:
            b = m.NewBoolVar(f"both_{t}_{s}")
            m.AddBoolAnd([assign[(t - 1, s)], assign[(t, s)]]).OnlyEnforceIf(b)
            m.AddBoolOr(
                [assign[(t - 1, s)].Not(), assign[(t, s)].Not()]
            ).OnlyEnforceIf(b.Not())
            both.append(b)
        same = m.NewBoolVar(f"same_{t}")
        m.Add(same == sum(both))
        ch = m.NewBoolVar(f"ch_{t}")
        m.Add(ch == 1 - same)
        change_terms.append(ch)
    changes_var = m.NewIntVar(0, max(q - 1, 0), "changes")
    m.Add(changes_var == sum(change_terms))
    if changes_eq is not None:
        m.Add(changes_var == changes_eq)

    if force_assign:
        for t, s in force_assign.items():
            m.Add(assign[(t, s)] == 1)

    return _MaterialModel(
        model=m,
        assign=assign,
        load=load,
        changes_var=changes_var,
        items=items,
        spool_ids=spool_ids,
        capacities=capacities,
        adjacent=adjacent,
        demands=demands,
    )


def _assignment_of(
    solver: cp_model.CpSolver, art: _MaterialModel
) -> List[str]:
    result = []
    for t in range(len(art.items)):
        for s in art.spool_ids:
            if solver.Value(art.assign[(t, s)]) == 1:
                result.append(s)
                break
        else:  # pragma: no cover - 可行解必有赋值
            raise RuntimeError("CP-SAT 未给出选卷赋值")
    return result


def _greedy_assignment(
    items: List[FixedSegment],
    spool_ids: List[str],
    capacities: Dict[str, int],
    adjacent: List[bool],
) -> Optional[List[str]]:
    """免求解构造兜底：按 spool_id 升序，优先沿用同材料相邻的当前卷，
    否则取第一卷够量者；跨材料边界后没有可沿用的卷。

    卸下料卷即回到库存池：后续不相邻段仍可复用任意一卷，余量按累计扣减。
    找不到完整分配时返回 ``None``（总量不足或碎片化导致）。
    """
    used: Dict[str, int] = {s: 0 for s in spool_ids}
    result: List[str] = []
    for t, it in enumerate(items):
        candidates = list(spool_ids)
        if adjacent[t] and result:
            current = result[-1]
            candidates = [current] + [s for s in spool_ids if s != current]
        for s in candidates:
            if used[s] + it.demand <= capacities[s]:
                used[s] += it.demand
                result.append(s)
                break
        else:
            return None
    return result


def _changes_of(assignment: List[str], adjacent: List[bool]) -> int:
    return sum(
        1
        for t in range(1, len(assignment))
        if adjacent[t] and assignment[t] != assignment[t - 1]
    )


def _solve_material(
    items: List[FixedSegment],
    spool_ids: List[str],
    capacities: Dict[str, int],
    adjacent: List[bool],
    time_limit: float,
) -> Tuple[Optional[List[str]], bool, int, str]:
    """单材料两阶段求解，返回 (逐段选卷, 是否全程证明最优, 变化次数, 消息)。

    无可行解时选卷为 ``None``（调用方据此报缺料）。
    """
    total_demand = sum(it.demand for it in items)
    art = _build_material_model(items, spool_ids, capacities, adjacent)
    art.model.Minimize(art.changes_var)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_workers = 8
    solver.parameters.log_search_progress = False
    code = solver.Solve(art.model)

    if code == cp_model.INFEASIBLE:
        return None, False, 0, "料卷余量无法完整分配该材料的层段需求"
    if code not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        greedy = _greedy_assignment(items, spool_ids, capacities, adjacent)
        if greedy is None:
            return None, False, 0, f"限时内无法判定可行性（{solver.StatusName(code)}）"
        return (
            greedy,
            False,
            _changes_of(greedy, adjacent),
            f"限时内 CP-SAT 未给出可行解（{solver.StatusName(code)}）；"
            "返回构造性选卷，未证明变化次数与字典序最优",
        )

    assignment = _assignment_of(solver, art)
    changes_star = int(solver.Value(art.changes_var))
    changes_proven = code == cp_model.OPTIMAL

    # 阶段 2：变化次数锁定，逐位贪心固定料卷 ID（字典序最小）
    if total_demand == 0:
        # 全部段需求为 0：任何卷都可承载（含余量 0 的卷），字典序最小即
        # 全程使用 ID 最小的卷（跨材料边界在该材料组内也不存在容量问题）
        lex_proven = True
        lex_assignment = [spool_ids[0]] * len(items)
    elif not changes_proven:
        lex_proven = False
        lex_assignment = assignment
    else:
        lex_assignment, lex_proven = _lexicographic_fix(
            items, spool_ids, capacities, adjacent, assignment, changes_star, time_limit
        )

    proven = changes_proven and lex_proven
    if proven:
        message = "已证明料卷变化次数最小且逐段料卷 ID 字典序最小"
    elif changes_proven:
        message = "料卷变化次数已证最小；字典序决胜在限时内未完全证明"
    else:
        message = "限时内得到可行选卷，未证明变化次数与字典序最优"
    return (
        lex_assignment,
        proven,
        _changes_of(lex_assignment, adjacent),
        message,
    )


def _lexicographic_fix(
    items: List[FixedSegment],
    spool_ids: List[str],
    capacities: Dict[str, int],
    adjacent: List[bool],
    best: List[str],
    changes_star: int,
    total_time: float,
) -> Tuple[List[str], bool]:
    """逐位固定料卷 ID 最小者（每步一次 CP-SAT 可行性证明）。

    每个位置按 spool_id 升序尝试；首个在"前缀固定 + 变化次数锁定"下仍可行
    的卷即字典序最优。任一步在限时内 UNKNOWN 则返回当前解且不声称最优。
    """
    import time

    q = len(items)
    forced: Dict[int, str] = {}
    result: List[str] = []
    deadline = time.monotonic() + total_time

    for t in range(q):
        remaining = deadline - time.monotonic()
        if remaining <= 0.02:
            return best, False
        chosen: Optional[str] = None
        for s in spool_ids:
            trial = dict(forced)
            trial[t] = s
            art = _build_material_model(
                items, spool_ids, capacities, adjacent,
                force_assign=trial, changes_eq=changes_star,
            )
            art.model.Minimize(art.changes_var)
            solver = cp_model.CpSolver()
            solver.parameters.max_time_in_seconds = min(max(remaining, 0.02), 1.5)
            solver.parameters.num_workers = 8
            solver.parameters.log_search_progress = False
            # 用当前已知最优赋值给求解器提示
            for u, sp in enumerate(best):
                art.model.AddHint(art.assign[(u, sp)], 1)
            code = solver.Solve(art.model)
            if code in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                chosen = s
                best = _assignment_of(solver, art)
                break
            if code == cp_model.UNKNOWN:
                return best, False
            # INFEASIBLE：该卷与前缀不兼容，试下一卷
        if chosen is None:  # pragma: no cover - 前缀本身可行，不会发生
            return best, False
        forced[t] = chosen
        result.append(chosen)
    return result, True


def allocate(req: AllocateRequest) -> dict:
    """分配入口：返回可直接构造 AllocateResponse 的结果字典。"""
    fixed = _fixed_segments(req)
    capacity = req.total_purge_capacity_mm3
    purge_used = sum(seg.purge_before for seg in fixed)

    # ---- 终态 1：固定顺序的 purge 超出总容量，不产生任何分配 ----
    if purge_used > capacity:
        return {
            "status": PURGE_CAPACITY_EXCEEDED,
            "optimal": False,
            "allocation": [],
            "spools": _idle_spools(req),
            "spool_changes": 0,
            "purge_used_mm3": purge_used,
            "purge_remaining_mm3": capacity - purge_used,
            "reason": {
                "code": PURGE_CAPACITY_EXCEEDED,
                "message": (
                    f"固定顺序共需换料 purge {purge_used} mm^3，"
                    f"超过总容量 {capacity} mm^3；顺序已固定无法重排"
                ),
                "purge_required_mm3": purge_used,
                "capacity_mm3": capacity,
            },
            "solver_message": "固定顺序 purge 超容，未进行料卷分配",
        }

    # 库存按材料分组（spool_id 已在 schema 层保证唯一），按 ID 升序
    spools_by_material: Dict[str, List[str]] = {}
    capacity_of: Dict[str, int] = {}
    for sp in sorted(req.spools, key=lambda s: s.spool_id):
        spools_by_material.setdefault(sp.material_id, []).append(sp.spool_id)
        capacity_of[sp.spool_id] = sp.remaining_mm3

    items_by_material: Dict[str, List[FixedSegment]] = {}
    for seg in fixed:
        items_by_material.setdefault(seg.material_id, []).append(seg)

    # ---- 终态 2：库存总量不足，直接缺料且不产生任何分配 ----
    deficits = []
    for mat, items in items_by_material.items():
        need = sum(it.demand for it in items)
        avail = sum(capacity_of[s] for s in spools_by_material.get(mat, []))
        if avail < need:
            deficits.append(
                {
                    "material_id": mat,
                    "required_mm3": need,
                    "available_mm3": avail,
                    "shortage_mm3": need - avail,
                }
            )
    if deficits:
        deficits.sort(key=lambda d: d["material_id"])
        return {
            "status": MATERIAL_SHORTAGE,
            "optimal": False,
            "allocation": [],
            "spools": _idle_spools(req),
            "spool_changes": 0,
            "purge_used_mm3": purge_used,
            "purge_remaining_mm3": capacity - purge_used,
            "reason": {
                "code": MATERIAL_SHORTAGE,
                "message": (
                    "材料 "
                    + "、".join(
                        f"{d['material_id']} 缺 {d['shortage_mm3']} mm^3"
                        for d in deficits
                    )
                    + "：同材料全部料卷余量之和不足以覆盖该材料的"
                    "打印体积与换料 purge 总需求；未产生任何分配"
                ),
                "deficits": deficits,
            },
            "solver_message": "库存不足，未进行料卷分配",
        }

    # ---- 逐材料独立求解（目标也只在同材料相邻段之间产生）----
    # 各材料共享一个总时限（默认取 time_limit_seconds），避免材料数 × 单材料时限
    import time

    time_limit = float(req.time_limit_seconds or 15.0)
    wall_deadline = time.monotonic() + time_limit
    assignment_by_order: Dict[int, str] = {}
    all_proven = True
    messages: List[str] = []
    for mat, items in sorted(items_by_material.items()):
        spool_ids = spools_by_material.get(mat, [])
        caps = {s: capacity_of[s] for s in spool_ids}
        # items 已按全局顺序排列：adjacent[t] 表示子序列第 t 段与第 t-1 段
        # 在全局顺序中是否紧邻（t=0 恒 False）
        adjacent = [False] + [
            items[t].order == items[t - 1].order + 1 for t in range(1, len(items))
        ]
        remaining = max(wall_deadline - time.monotonic(), 0.05)
        chosen, proven, _changes, message = _solve_material(
            items, spool_ids, caps, adjacent, min(time_limit, remaining)
        )
        if chosen is None:
            # 总量够但碎片化（无单卷组合可装下）：如实报缺料，不给部分分配
            need = sum(it.demand for it in items)
            return {
                "status": MATERIAL_SHORTAGE,
                "optimal": False,
                "allocation": [],
                "spools": _idle_spools(req),
                "spool_changes": 0,
                "purge_used_mm3": purge_used,
                "purge_remaining_mm3": capacity - purge_used,
                "reason": {
                    "code": MATERIAL_SHORTAGE,
                    "message": (
                        f"材料 {mat} 余量总量 {need} mm^3 虽够，但单卷容量碎片化，"
                        "没有任何料卷组合能在逐段整卷扣减下完整分配；"
                        "未产生任何分配"
                    ),
                    "deficits": [
                        {
                            "material_id": mat,
                            "required_mm3": need,
                            "available_mm3": need,
                            "shortage_mm3": 0,
                        }
                    ],
                },
                "solver_message": message,
            }
        all_proven = all_proven and proven
        if message:
            messages.append(f"{mat}: {message}")
        for it, s in zip(items, chosen):
            assignment_by_order[it.order] = s

    return _build_allocation(
        req, fixed, assignment_by_order,
        purge_used, capacity, all_proven, messages,
    )


def _idle_spools(req: AllocateRequest) -> List[dict]:
    """缺料 / 超容时各卷原样列出：累计扣减为 0、无逐段明细。"""
    return [
        {
            "spool_id": sp.spool_id,
            "material_id": sp.material_id,
            "initial_remaining_mm3": sp.remaining_mm3,
            "deducted_mm3": 0,
            "remaining_mm3": sp.remaining_mm3,
            "deductions": [],
        }
        for sp in sorted(req.spools, key=lambda s: s.spool_id)
    ]


def _build_allocation(
    req: AllocateRequest,
    fixed: List[FixedSegment],
    assignment_by_order: Dict[int, str],
    purge_used: int,
    capacity: int,
    optimal: bool,
    messages: List[str],
) -> dict:
    """按逐段选卷实际扣减，组装逐段分配与各卷余量明细。"""
    initial_of = {sp.spool_id: sp.remaining_mm3 for sp in req.spools}
    used_of: Dict[str, int] = {sp.spool_id: 0 for sp in req.spools}
    deductions_of: Dict[str, List[dict]] = {
        sp.spool_id: [] for sp in req.spools
    }

    allocation: List[dict] = []
    spool_changes = 0
    prev_spool: Optional[str] = None
    for seg in fixed:
        s = assignment_by_order[seg.order]
        deducted = seg.demand
        used_of[s] += deducted
        spool_changed = prev_spool is not None and s != prev_spool
        if spool_changed:
            spool_changes += 1
        allocation.append(
            {
                "order": seg.order,
                "job_id": seg.job_id,
                "segment_index": seg.segment_index,
                "material_id": seg.material_id,
                "spool_id": s,
                "material_switched": seg.material_switched,
                "purge_before_mm3": seg.purge_before,
                "print_volume_mm3": seg.print_volume,
                "deducted_mm3": deducted,
                "spool_changed": spool_changed,
            }
        )
        deductions_of[s].append(
            {
                "order": seg.order,
                "job_id": seg.job_id,
                "segment_index": seg.segment_index,
                "material_id": seg.material_id,
                "purge_before_mm3": seg.purge_before,
                "print_volume_mm3": seg.print_volume,
                "deducted_mm3": deducted,
                "remaining_after_mm3": initial_of[s] - used_of[s],
            }
        )
        prev_spool = s

    spools = []
    for sp in sorted(req.spools, key=lambda s: s.spool_id):
        used = used_of[sp.spool_id]
        spools.append(
            {
                "spool_id": sp.spool_id,
                "material_id": sp.material_id,
                "initial_remaining_mm3": sp.remaining_mm3,
                "deducted_mm3": used,
                "remaining_mm3": sp.remaining_mm3 - used,
                "deductions": deductions_of[sp.spool_id],
            }
        )

    if optimal:
        message = "；".join(messages) if messages else (
            "已证明料卷变化次数最小且逐段料卷 ID 字典序最小"
        )
    else:
        message = "；".join(messages)
    return {
        "status": ALLOCATED,
        "optimal": optimal,
        "allocation": allocation,
        "spools": spools,
        "spool_changes": spool_changes,
        "purge_used_mm3": purge_used,
        "purge_remaining_mm3": capacity - purge_used,
        "reason": None,
        "solver_message": message,
    }
