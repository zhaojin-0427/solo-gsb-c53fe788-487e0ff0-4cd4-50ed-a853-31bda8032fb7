"""POST /spools/allocate 的料卷分配：固定执行顺序下的逐段选卷与扣减。

语义
----

请求在 :class:`app.schemas.PlanRequest` 的全部规划参数之外，给出**固定执行
顺序**（覆盖全部层段、作业内按层序的 ``(job_id, segment_index)`` 序列）与
可用**料卷**（唯一 ID、材料、非负余量）。分配不重新排产、不涉及时间：

* 经 :func:`app.solver.build_instance` 取各层段材料与名义体积；
* 沿固定顺序从 ``initial_material`` 逐段计算段前 purge：段材料与上一段
  （首段相对初始材料）不同时，按 ``purge_matrix[from][to]`` 计 purge；
* 每段的**打印体积与段前 purge 均从该段所选（目标材料）料卷**的余量中
  扣除；同一料卷卸下后余量保留，之后可再次装回复用；
* 顺序本身的 purge 总量超过 ``total_purge_capacity_mm3`` 时直接返回
  ``PURGE_CAPACITY_EXCEEDED``，不进行分配。

在所有可行分配中，先**最小化相邻层段料卷 ID 变化次数**，再按**逐段料卷
ID 字典序**决胜。两级目标用 OR-Tools CP-SAT 精确求解：阶段 1 最小化变化
次数；阶段 2 在变化次数锁定的前提下逐位固定字典序最小的料卷，每一步都
经可行性证明。因此"同材料两段依次需 4、6，余量为 4、6 的两卷"这类需要
全局搭配的实例也能正确分配（4→4、6→6），不会贪心错配后误判缺料。

库存不足（不存在可行分配，已证无解）返回 ``INSUFFICIENT_STOCK`` 且不返回
分配；已找到分配但限时内未证明全部目标最优时返回 ``FEASIBLE`` 并在
``objective`` 中如实标记；求解器无法判定是否有解时返回 ``UNKNOWN``。
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional, Tuple

from ortools.sat.python import cp_model

from .schemas import SpoolAllocateRequest
from .solver import build_instance

OPTIMAL = "OPTIMAL"
FEASIBLE = "FEASIBLE"
INSUFFICIENT_STOCK = "INSUFFICIENT_STOCK"
PURGE_CAPACITY_EXCEEDED = "PURGE_CAPACITY_EXCEEDED"
UNKNOWN = "UNKNOWN"


def _build_model(
    n: int,
    cand: List[List[int]],
    demands: List[int],
    spools: list,
    *,
    changes_eq: Optional[int] = None,
    fixed: Optional[Dict[Tuple[int, int], int]] = None,
):
    """选卷模型：每段恰选一卷同材料料卷，每卷累计扣减不超过其余量。

    ``cand[i]`` 为第 i 段（0 基）可用的料卷下标（材料匹配）；``demands[i]``
    为该段从所选料卷扣除的总量（打印体积 + 段前 purge）。相邻段使用不同
    料卷记一次变化；相邻段材料不同（候选无交集）时变化不可避免，直接置 1。
    """
    m = cp_model.CpModel()
    x: Dict[Tuple[int, int], cp_model.IntVar] = {}
    for i in range(n):
        for s in cand[i]:
            x[(i, s)] = m.NewBoolVar(f"x_{i}_{s}")
    for i in range(n):
        m.AddExactlyOne([x[(i, s)] for s in cand[i]])

    load: List[list] = [[] for _ in spools]
    for i in range(n):
        if demands[i] == 0:
            continue
        for s in cand[i]:
            load[s].append(demands[i] * x[(i, s)])
    for s, sp in enumerate(spools):
        if load[s]:
            m.Add(sum(load[s]) <= sp.remaining_mm3)

    change_vars = []
    for i in range(1, n):
        c = m.NewBoolVar(f"c_{i}")
        common = set(cand[i - 1]) & set(cand[i])
        if not common:
            # 相邻段材料不同，候选料卷无交集，必然换卷
            m.Add(c == 1)
        else:
            for s in common:
                m.Add(c >= x[(i, s)] - x[(i - 1, s)])
        change_vars.append(c)
    changes = m.NewIntVar(0, max(n - 1, 0), "spool_changes")
    if change_vars:
        m.Add(changes == sum(change_vars))
    else:
        m.Add(changes == 0)

    if changes_eq is not None:
        m.Add(changes == changes_eq)
    if fixed:
        for (i, s), val in fixed.items():
            m.Add(x[(i, s)] == val)
    return m, x, changes


def _extract_assignment(
    solver: cp_model.CpSolver,
    x: Dict[Tuple[int, int], cp_model.IntVar],
    n: int,
    cand: List[List[int]],
) -> List[int]:
    assignment = [-1] * n
    for i in range(n):
        for s in cand[i]:
            if solver.Value(x[(i, s)]) == 1:
                assignment[i] = s
                break
    return assignment


def _count_changes(assignment: List[int]) -> int:
    return sum(
        1 for i in range(1, len(assignment)) if assignment[i] != assignment[i - 1]
    )


def _lexicographic_fix(
    n: int,
    cand: List[List[int]],
    demands: List[int],
    spools: list,
    changes_star: int,
    assignment: List[int],
    per_check_limit: float,
    total_cap: float,
) -> Tuple[List[int], bool]:
    """逐位固定字典序最小的料卷序列（变化次数锁定为最优值）。

    每一位都以"最小化该位料卷名次（按 spool_id 升序）"为目标求解，并把
    最优选择固定后继续；每步都经 CP-SAT 证最优，全部位完成即字典序最小。
    任一步超时 / 未证最优则保守返回当前可行分配，调用方不得声称字典序最优。
    """
    fixed: Dict[Tuple[int, int], int] = {}
    current = list(assignment)
    deadline = time.monotonic() + total_cap

    for i in range(n):
        remaining = deadline - time.monotonic()
        if remaining <= 0.02:
            return current, False
        ranked = sorted(cand[i], key=lambda s: spools[s].spool_id)
        m, x, _ = _build_model(
            n, cand, demands, spools, changes_eq=changes_star, fixed=fixed
        )
        m.Minimize(sum(rank * x[(i, s)] for rank, s in enumerate(ranked)))
        # 用当前可行分配做提示，加速逐位判定
        for (ii, s), var in x.items():
            m.AddHint(var, 1 if current[ii] == s else 0)
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = min(per_check_limit, remaining)
        solver.parameters.num_workers = 8
        solver.parameters.log_search_progress = False
        code = solver.Solve(m)
        if code == cp_model.OPTIMAL:
            chosen = next(s for s in cand[i] if solver.Value(x[(i, s)]) == 1)
            fixed[(i, chosen)] = 1
            current = _extract_assignment(solver, x, n, cand)
        elif code == cp_model.FEASIBLE:
            # 该位最小名次未证：采用此可行分配（已固定的前缀仍最优），
            # 但不再声称整体字典序最优
            return _extract_assignment(solver, x, n, cand), False
        else:
            return current, False
    return current, True


def _failure(status: str, total_purge: int, capacity: int, message: str) -> dict:
    return {
        "status": status,
        "optimal": False,
        "objective": None,
        "allocations": [],
        "spools": [],
        "spool_changes": None,
        "purge_used_mm3": total_purge,
        "purge_remaining_mm3": capacity - total_purge,
        "solver_message": message,
    }


def allocate(req: SpoolAllocateRequest) -> dict:
    """分配入口：请求已通过 Pydantic 校验，此处构建基准并求解选卷。"""
    inst = build_instance(req)

    # (job_id, segment_index) -> 全局层段号
    idx_of: Dict[tuple, int] = {}
    for jd in inst.jobs:
        for seg in jd.segments:
            idx_of[(jd.job_id, seg["seg_index"])] = seg["global_idx"]
    gis = [idx_of[(it.job_id, it.segment_index)] for it in req.sequence]
    n = len(gis)

    # 沿固定顺序从初始材料逐段计算材料、名义体积与段前 purge
    mats: List[str] = []
    vols: List[int] = []
    purges: List[int] = []
    cur = inst.initial_material
    for gi in gis:
        mat = inst.material_of[gi]
        purge = 0 if mat == cur else int(inst.purge_matrix[cur][mat])
        mats.append(mat)
        vols.append(inst.volume_of[gi])
        purges.append(purge)
        cur = mat
    total_purge = sum(purges)
    # 每段从所选料卷扣除的总量：打印体积 + 段前 purge（purge 记目标材料）
    demands = [v + p for v, p in zip(vols, purges)]

    # 顺序本身的 purge 超容：与库存无关，直接返回超容状态
    if total_purge > inst.purge_capacity:
        return _failure(
            PURGE_CAPACITY_EXCEEDED,
            total_purge,
            inst.purge_capacity,
            f"固定顺序的 purge 总需求 {total_purge} mm^3 超过总容量 "
            f"{inst.purge_capacity} mm^3，未进行料卷分配",
        )

    spools = list(req.spools)
    cand: List[List[int]] = [
        [s for s, sp in enumerate(spools) if sp.material_id == mats[i]]
        for i in range(n)
    ]
    # 快速判定：某段无同材料料卷，或某材料总需求超过总余量 → 必缺料
    for i in range(n):
        if not cand[i]:
            return _failure(
                INSUFFICIENT_STOCK,
                total_purge,
                inst.purge_capacity,
                f"第 {i + 1} 段（材料 {mats[i]}）没有可用的同材料料卷",
            )
    need: Dict[str, int] = {}
    have: Dict[str, int] = {}
    for i in range(n):
        need[mats[i]] = need.get(mats[i], 0) + demands[i]
    for sp in spools:
        have[sp.material_id] = have.get(sp.material_id, 0) + sp.remaining_mm3
    for mat, total_need in need.items():
        if total_need > have.get(mat, 0):
            return _failure(
                INSUFFICIENT_STOCK,
                total_purge,
                inst.purge_capacity,
                f"材料 {mat} 库存不足：需求 {total_need} mm^3，"
                f"可用 {have.get(mat, 0)} mm^3",
            )

    budget = float(req.time_limit_seconds or 15.0)
    stage1_limit = max(1.0, 0.5 * budget)
    per_check = max(0.2, min(1.0, 0.1 * budget))
    lex_cap = max(1.0, 0.45 * budget)

    # ---- 阶段 1：最小化相邻层段料卷变化次数 ----
    m, x, changes_var = _build_model(n, cand, demands, spools)
    m.Minimize(changes_var)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = stage1_limit
    solver.parameters.num_workers = 8
    solver.parameters.log_search_progress = False
    code = solver.Solve(m)

    if code == cp_model.INFEASIBLE:
        return _failure(
            INSUFFICIENT_STOCK,
            total_purge,
            inst.purge_capacity,
            "库存不足：不存在满足全部层段扣减的料卷分配（已证无解）",
        )
    if code not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return _failure(
            UNKNOWN,
            total_purge,
            inst.purge_capacity,
            f"限时内无法判定是否存在可行分配（{solver.StatusName(code)}）",
        )

    assignment = _extract_assignment(solver, x, n, cand)
    changes_star = _count_changes(assignment)
    changes_proven = code == cp_model.OPTIMAL

    # ---- 阶段 2：变化次数锁定，逐位固定字典序最小的料卷 ----
    lex_proven = False
    if changes_proven:
        assignment, lex_proven = _lexicographic_fix(
            n, cand, demands, spools, changes_star, assignment, per_check, lex_cap
        )
        changes_star = _count_changes(assignment)

    return _build_response(
        inst,
        req,
        gis,
        mats,
        vols,
        purges,
        demands,
        assignment,
        spools,
        changes_star,
        changes_proven=changes_proven,
        lex_proven=lex_proven,
        total_purge=total_purge,
    )


def _build_response(
    inst,
    req: SpoolAllocateRequest,
    gis: List[int],
    mats: List[str],
    vols: List[int],
    purges: List[int],
    demands: List[int],
    assignment: List[int],
    spools: list,
    changes: int,
    *,
    changes_proven: bool,
    lex_proven: bool,
    total_purge: int,
) -> dict:
    """按分配结果生成逐段选卷、扣减与各卷余量明细。"""
    balances = [sp.remaining_mm3 for sp in spools]
    allocations = []
    for pos, gi in enumerate(gis):
        s = assignment[pos]
        balances[s] -= demands[pos]
        allocations.append(
            {
                "order": pos + 1,
                "job_id": inst.job_of[gi],
                "segment_index": inst.seg_index_of[gi],
                "material_id": mats[pos],
                "volume_mm3": vols[pos],
                "purge_before_mm3": purges[pos],
                "spool_id": spools[s].spool_id,
                "deduct_mm3": demands[pos],
                "spool_remaining_after_mm3": balances[s],
            }
        )
    spool_usage = [
        {
            "spool_id": sp.spool_id,
            "material_id": sp.material_id,
            "initial_remaining_mm3": sp.remaining_mm3,
            "used_mm3": sp.remaining_mm3 - balances[k],
            "remaining_mm3": balances[k],
        }
        for k, sp in enumerate(spools)
    ]

    optimal = changes_proven and lex_proven
    if optimal:
        status = OPTIMAL
        message = (
            f"已找到完整分配（相邻料卷变化 {changes} 次），"
            "变化次数与逐段料卷字典序均已证最优"
        )
    elif changes_proven:
        status = FEASIBLE
        message = (
            f"已找到完整分配（相邻料卷变化 {changes} 次，已证最优）；"
            "逐段料卷字典序决胜在限时内未完全证明，不声称字典序最小"
        )
    else:
        status = FEASIBLE
        message = (
            f"已找到完整分配（相邻料卷变化 {changes} 次），"
            "但限时内未证明变化次数最优"
        )
    return {
        "status": status,
        "optimal": optimal,
        "objective": {
            "spool_changes": changes,
            "spool_changes_optimal": changes_proven,
            "lexicographic_minimal": lex_proven,
        },
        "allocations": allocations,
        "spools": spool_usage,
        "spool_changes": changes,
        "purge_used_mm3": total_purge,
        "purge_remaining_mm3": inst.purge_capacity - total_purge,
        "solver_message": message,
    }
