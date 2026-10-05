"""多材料 3D 打印换料规划求解器。

建模（OR-Tools CP-SAT）
----------------------

* 每个层段是一个不可拆分的加工节点；另设虚拟起点 START 与终点 END。
* 节点间的有向弧 ``u -> v`` 表示层段 v 紧跟在 u 之后执行，弧上携带
  purge 体积与切换耗时；用 ``AddCircuit`` 强制选出一条
  ``START -> (全部层段的某个排列) -> END -> START`` 的 Hamilton 回路。
* 同一作业内的层段用先后约束保证顺序；作业之间只能在层段边界交错。
* 弧被选中时强制 ``start[v] = end[u] + 切换耗时``，故排产是紧密无空转的。
* 作业逾期 ``max(0, 最后层段完成时刻 - 截止秒数)``。

三级目标（严格优先级）：
1. 总 purge 体积最小；
2. 总逾期秒数最小（purge 固定为最优值）；
3. 计划按 ``(作业 ID, 层序)`` 的字典序最小——采用逐位贪心固定前缀 +
   可行性检查的方式，每一步都经过 CP-SAT 可行性证明，因此完成全部位置
   后即"证明字典序最小"；任何一步在限时内无法判定则保守地不声称最优。

长作业与超时兜底
----------------

层段数量没有固定上限，合法的长作业（如单作业 25 段以上）直接进入
CP-SAT 规划。若大模型在 ``time_limit_seconds`` 内连一个可行解都找不到，
求解器会用免求解的构造性排列兜底：除整作业串行外，还会把每个作业切成
同材料连续段（run）并按材料交错，让不同作业的同材料 run 相邻执行——
整作业串行在同材料可跨作业合并时换料次数虚高（例如两作业均按
A→B→A 用料时，串行至少换料 4 次，按材料交错只需 2 次），只试串行
会把本可满足 purge 容量的请求误报为 UNKNOWN。候选均严格遵守作业内
层序，purge 经精确核算，仅取不超容者并按 (purge, 逾期, 字典序) 择优，
仍返回完整的计划 / 切换记录 / purge / 逾期明细，状态如实标记为
``FEASIBLE``；构造性候选也超容时才返回 ``UNKNOWN``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ortools.sat.python import cp_model

from .schemas import PlanRequest

START = 0


@dataclass
class JobData:
    job_id: str
    deadline: int
    segments: List[dict]  # [{global_idx, material_id, print_seconds, volume_mm3, seg_index}]
    last_idx: int


@dataclass
class Instance:
    jobs: List[JobData]
    n: int
    initial_material: str
    purge_matrix: Dict[str, Dict[str, int]]
    switch_seconds: int
    purge_capacity: int
    # 以全局层段号（1..n）为索引的属性表
    job_of: Dict[int, str] = field(default_factory=dict)
    seg_index_of: Dict[int, int] = field(default_factory=dict)
    material_of: Dict[int, str] = field(default_factory=dict)
    duration_of: Dict[int, int] = field(default_factory=dict)
    volume_of: Dict[int, int] = field(default_factory=dict)
    predecessor_of: Dict[int, Optional[int]] = field(default_factory=dict)
    rank_of: Dict[int, Tuple[str, int]] = field(default_factory=dict)
    horizon: int = 0


def build_instance(req: PlanRequest) -> Instance:
    jobs: List[JobData] = []
    idx = 0
    total_print = 0
    job_of: Dict[int, str] = {}
    seg_index_of: Dict[int, int] = {}
    material_of: Dict[int, str] = {}
    duration_of: Dict[int, int] = {}
    volume_of: Dict[int, int] = {}
    predecessor_of: Dict[int, Optional[int]] = {}
    rank_of: Dict[int, Tuple[str, int]] = {}

    for job in req.jobs:
        segs = []
        prev_idx: Optional[int] = None
        for k, seg in enumerate(job.segments, start=1):
            idx += 1
            segs.append(
                {
                    "global_idx": idx,
                    "material_id": seg.material_id,
                    "print_seconds": seg.print_seconds,
                    "volume_mm3": seg.volume_mm3,
                    "seg_index": k,
                }
            )
            job_of[idx] = job.job_id
            seg_index_of[idx] = k
            material_of[idx] = seg.material_id
            duration_of[idx] = seg.print_seconds
            volume_of[idx] = seg.volume_mm3
            predecessor_of[idx] = prev_idx
            rank_of[idx] = (job.job_id, k)
            prev_idx = idx
            total_print += seg.print_seconds
        jobs.append(
            JobData(
                job_id=job.job_id,
                deadline=job.deadline_seconds,
                segments=segs,
                last_idx=prev_idx,  # type: ignore[arg-type]
            )
        )

    n = idx
    # 任意相邻层段最多切换一次，再加首段前可能的一次切换
    horizon = total_print + (n + 1) * req.switch_seconds + 1
    return Instance(
        jobs=jobs,
        n=n,
        initial_material=req.initial_material,
        purge_matrix=req.purge_matrix,
        switch_seconds=req.switch_seconds,
        purge_capacity=req.total_purge_capacity_mm3,
        job_of=job_of,
        seg_index_of=seg_index_of,
        material_of=material_of,
        duration_of=duration_of,
        volume_of=volume_of,
        predecessor_of=predecessor_of,
        rank_of=rank_of,
        horizon=horizon,
    )


@dataclass
class ModelArtifacts:
    model: cp_model.CpModel
    starts: Dict[int, cp_model.IntVar]
    ends: Dict[int, object]
    arc_lit: Dict[Tuple[int, int], cp_model.IntVar]
    tardy: Dict[str, cp_model.IntVar]
    purge_var: cp_model.IntVar
    tardiness_var: cp_model.IntVar
    completion: Dict[str, cp_model.IntVar]


def _arc_costs(inst: Instance, u: int, v: int, end_node: int) -> Tuple[int, int]:
    """返回弧 (u,v) 的 (切换秒数, purge 体积)。

    START -> v：从初始材料换到 v 的材料；
    u -> END / END -> START：收尾弧，无切换无 purge；
    u -> v（均为层段）：按两层层段材料查矩阵（同材料矩阵对角为 0）。
    """
    if v == START or v == end_node or u == end_node:
        return 0, 0
    to_mat = inst.material_of[v]
    if u == START:
        from_mat = inst.initial_material
    else:
        from_mat = inst.material_of[u]
    if from_mat == to_mat:
        return 0, 0
    return inst.switch_seconds, int(inst.purge_matrix[from_mat][to_mat])


def build_model(
    inst: Instance,
    *,
    purge_eq: Optional[int] = None,
    tardiness_eq: Optional[int] = None,
    force_arcs: Optional[Dict[Tuple[int, int], bool]] = None,
) -> ModelArtifacts:
    m = cp_model.CpModel()
    n = inst.n
    end_node = n + 1

    starts = {i: m.NewIntVar(0, inst.horizon, f"s_{i}") for i in range(1, n + 1)}
    ends = {
        i: m.NewIntVar(0, inst.horizon, f"e_{i}") for i in range(1, n + 1)
    }
    for i in range(1, n + 1):
        if inst.duration_of[i] > 0:
            m.Add(ends[i] == starts[i] + inst.duration_of[i])
        else:
            m.Add(ends[i] == starts[i])

    # 作业内层段先后顺序
    for i in range(1, n + 1):
        pred = inst.predecessor_of[i]
        if pred is not None:
            m.Add(starts[i] >= ends[pred])

    arc_lit: Dict[Tuple[int, int], cp_model.IntVar] = {}
    arcs: List[Tuple[int, int, cp_model.IntVar]] = []
    purge_terms: List[cp_model.IntVar] = []

    def add_arc(u: int, v: int) -> cp_model.IntVar:
        lit = m.NewBoolVar(f"arc_{u}_{v}")
        setup_sec, purge = _arc_costs(inst, u, v, end_node)
        arcs.append((u, v, lit))
        arc_lit[(u, v)] = lit
        if purge > 0:
            cost = m.NewIntVar(0, purge, f"p_{u}_{v}")
            m.Add(cost == purge).OnlyEnforceIf(lit)
            m.Add(cost == 0).OnlyEnforceIf(lit.Not())
            purge_terms.append(cost)
        # 弧进入真实层段：v 恰在 u（或起点 0 时刻）之后经过切换耗时开始
        if v != START and v != end_node:
            base = 0 if u == START else ends[u]
            m.Add(starts[v] == base + setup_sec).OnlyEnforceIf(lit)
        return lit

    # 起点 -> 首层段；末层段 -> 终点；回路闭合
    for v in range(1, n + 1):
        add_arc(START, v)
        add_arc(v, end_node)
    add_arc(end_node, START)
    # 层段之间任意先后（作业内顺序由上面的约束保证合法）
    for u in range(1, n + 1):
        for v in range(1, n + 1):
            if u != v:
                add_arc(u, v)

    m.AddCircuit(arcs)

    # 固定前缀（字典序决胜阶段使用）
    if force_arcs:
        for (u, v), val in force_arcs.items():
            m.Add(arc_lit[(u, v)] == 1 if val else arc_lit[(u, v)] == 0)

    purge_var = m.NewIntVar(0, max(inst.purge_capacity * 2 + 1, 1), "purge")
    if purge_terms:
        m.Add(purge_var == sum(purge_terms))
    else:
        m.Add(purge_var == 0)

    # purge 容量：硬约束，超限即无解
    if purge_eq is not None:
        m.Add(purge_var == purge_eq)
    else:
        m.Add(purge_var <= inst.purge_capacity)

    completion: Dict[str, cp_model.IntVar] = {}
    tardy: Dict[str, cp_model.IntVar] = {}
    tardy_terms = []
    for job in inst.jobs:
        comp = m.NewIntVar(0, inst.horizon, f"comp_{job.job_id}")
        m.Add(comp == ends[job.last_idx])
        completion[job.job_id] = comp
        t = m.NewIntVar(0, inst.horizon, f"tardy_{job.job_id}")
        m.AddMaxEquality(t, [0, comp - job.deadline])
        tardy[job.job_id] = t
        tardy_terms.append(t)

    tardiness_var = m.NewIntVar(0, inst.horizon * max(n, 1), "tardiness")
    m.Add(tardiness_var == sum(tardy_terms))
    if tardiness_eq is not None:
        m.Add(tardiness_var == tardiness_eq)

    return ModelArtifacts(
        model=m,
        starts=starts,
        ends=ends,
        arc_lit=arc_lit,
        tardy=tardy,
        purge_var=purge_var,
        tardiness_var=tardiness_var,
        completion=completion,
    )


@dataclass
class SolveOutcome:
    status: str  # OPTIMAL / FEASIBLE / INFEASIBLE / UNKNOWN
    message: str
    order: Optional[List[int]] = None
    purge: Optional[int] = None
    tardiness: Optional[int] = None


def _solve(
    artifacts: ModelArtifacts,
    objective: cp_model.IntVar,
    time_limit: float,
    *,
    hint_order: Optional[List[int]] = None,
    inst: Optional[Instance] = None,
) -> SolveOutcome:
    artifacts.model.Minimize(objective)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_workers = 8
    solver.parameters.log_search_progress = False

    # 用已知可行序列给求解器提示，显著加速字典序阶段的可行性判定
    if hint_order is not None and inst is not None:
        art = artifacts
        t = 0
        end_node = inst.n + 1
        prev = START
        for v in hint_order:
            setup_sec, _ = _arc_costs(inst, prev, v, end_node)
            art.model.AddHint(art.arc_lit[(prev, v)], 1)
            art.model.AddHint(art.starts[v], t + setup_sec)
            art.model.AddHint(art.ends[v], t + setup_sec + inst.duration_of[v])
            t += setup_sec + inst.duration_of[v]
            prev = v
        art.model.AddHint(art.arc_lit[(prev, end_node)], 1)
        art.model.AddHint(art.arc_lit[(end_node, START)], 1)

    status_code = solver.Solve(artifacts.model)

    if status_code == cp_model.OPTIMAL:
        status = "OPTIMAL"
    elif status_code == cp_model.FEASIBLE:
        status = "FEASIBLE"
    elif status_code == cp_model.INFEASIBLE:
        return SolveOutcome(status="INFEASIBLE", message=solver.StatusName(status_code))
    else:
        return SolveOutcome(status="UNKNOWN", message=solver.StatusName(status_code))

    # 沿被选中的弧从 START 开始还原执行顺序
    order: List[int] = []
    succ = _successor_map(solver, artifacts.arc_lit)
    node = succ[START]
    end_node = inst.n + 1  # type: ignore[union-attr]
    while node != end_node:
        order.append(node)
        node = succ[node]

    return SolveOutcome(
        status=status,
        message=solver.StatusName(status_code),
        order=order,
        purge=int(solver.Value(artifacts.purge_var)),
        tardiness=int(sum(solver.Value(t) for t in artifacts.tardy.values())),
    )


def _successor_map(solver: cp_model.CpSolver, arc_lit: dict) -> Dict[int, int]:
    succ: Dict[int, int] = {}
    for (u, v), lit in arc_lit.items():
        if solver.Value(lit) == 1:
            succ[u] = v
    return succ


def _feasibility_check(
    inst: Instance,
    force_arcs: Dict[Tuple[int, int], bool],
    purge_star: int,
    tard_star: int,
    time_limit: float,
    hint_order: Optional[List[int]],
) -> Tuple[str, Optional[List[int]]]:
    """在强制弧前缀 + 两级目标值锁定下判定可行性，返回 (状态, 可行时的序列)。"""
    art = build_model(
        inst,
        purge_eq=purge_star,
        tardiness_eq=tard_star,
        force_arcs=force_arcs,
    )
    # 常量目标：纯可行性判定
    art.model.Minimize(art.purge_var)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_workers = 8
    solver.parameters.log_search_progress = False
    if hint_order is not None:
        t = 0
        prev = START
        end_node = inst.n + 1
        for v in hint_order:
            setup_sec, _ = _arc_costs(inst, prev, v, end_node)
            art.model.AddHint(art.arc_lit[(prev, v)], 1)
            art.model.AddHint(art.starts[v], t + setup_sec)
            art.model.AddHint(art.ends[v], t + setup_sec + inst.duration_of[v])
            t += setup_sec + inst.duration_of[v]
            prev = v
        art.model.AddHint(art.arc_lit[(prev, end_node)], 1)
        art.model.AddHint(art.arc_lit[(end_node, START)], 1)

    code = solver.Solve(art.model)
    if code in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        order: List[int] = []
        succ = _successor_map(solver, art.arc_lit)
        node = succ[START]
        while node != inst.n + 1:
            order.append(node)
            node = succ[node]
        return "FEASIBLE", order
    if code == cp_model.INFEASIBLE:
        return "INFEASIBLE", None
    return "UNKNOWN", None


def _lexicographic_fix(
    inst: Instance,
    best_order: List[int],
    purge_star: int,
    tard_star: int,
    per_check_limit: float,
    total_cap: float,
) -> Tuple[List[int], bool]:
    """逐位贪心选出字典序最小的最优序列。

    返回 (序列, 是否全程被证明)。任一可行性判定在限时内 UNKNOWN，
    则保守终止并返回当前已知最优序列，调用方不得据此声称字典序最优。
    """
    import time

    n = inst.n
    forced: Dict[Tuple[int, int], bool] = {}
    placed: set[int] = set()
    result: List[int] = []
    prev = START
    hint = list(best_order)
    deadline_wall = time.monotonic() + total_cap

    for _ in range(n):
        if deadline_wall - time.monotonic() <= 0.02:
            # 总预算已耗尽：不再构建新模型，保守地不声称字典序最优
            return best_order, False
        candidates = [
            v
            for v in range(1, n + 1)
            if v not in placed
            and (inst.predecessor_of[v] is None or inst.predecessor_of[v] in placed)
        ]
        candidates.sort(key=lambda v: inst.rank_of[v])

        chosen: Optional[int] = None
        for v in candidates:
            remaining = deadline_wall - time.monotonic()
            if remaining <= 0.02:
                return best_order, False
            trial = dict(forced)
            trial[(prev, v)] = True
            limit = min(per_check_limit, remaining)
            status, feas_order = _feasibility_check(
                inst, trial, purge_star, tard_star, limit, hint
            )
            if status == "FEASIBLE" and feas_order is not None:
                chosen = v
                forced = trial
                hint = feas_order
                break
            if status == "UNKNOWN":
                return best_order, False
            # INFEASIBLE：该候选与前缀不相容，试下一个
        if chosen is None:
            # 理论上不会发生（当前前缀本身可行），保守处理
            return best_order, False
        result.append(chosen)
        placed.add(chosen)
        prev = chosen

    return result, True


def diagnose_infeasible(inst: Instance, time_limit: float) -> Tuple[str, dict]:
    """去掉 purge 容量上限重新最小化 purge，区分容量超限与前置冲突。"""
    # 构造无容量约束模型：临时放大容量
    saved = inst.purge_capacity
    inst.purge_capacity = 10**12
    art = build_model(inst)
    inst.purge_capacity = saved
    outcome = _solve(art, art.purge_var, time_limit, inst=inst)
    if outcome.status == "INFEASIBLE":
        return "PRECEDENCE", {
            "code": "PRECEDENCE",
            "message": "层段先后约束无法满足（作业内层序冲突）",
        }
    if outcome.status in ("OPTIMAL", "FEASIBLE") and outcome.purge is not None:
        if outcome.purge > saved:
            return "CAPACITY", {
                "code": "CAPACITY",
                "message": (
                    f"任何可行排产至少需要 purge {outcome.purge} mm^3，"
                    f"超过总容量 {saved} mm^3"
                ),
                "minimum_required_purge_mm3": outcome.purge,
                "capacity_mm3": saved,
            }
    return (
        "UNKNOWN",
        {
            "code": "UNKNOWN",
            "message": "求解器在限时内无法给出无解的确切归因",
        },
    )


def _job_runs(inst: Instance) -> List[List[List[int]]]:
    """把每个作业切成同材料连续段（run）；run 内层段与 run 之间均保持原顺序。"""
    runs_per_job: List[List[List[int]]] = []
    for jd in inst.jobs:
        runs: List[List[int]] = []
        for seg in jd.segments:
            gi = seg["global_idx"]
            if runs and inst.material_of[runs[-1][-1]] == seg["material_id"]:
                runs[-1].append(gi)
            else:
                runs.append([gi])
        runs_per_job.append(runs)
    return runs_per_job


def _interleaved_orders(inst: Instance) -> List[List[int]]:
    """run 级按材料交错的候选排列（严格遵守作业内层段顺序）。

    整作业串行在同材料可跨作业合并时会付出大量本可避免的换料：两个
    均按 A→B→A 用料的作业串行执行至少换料 4 次，而把同材料 run 合并
    相邻（A 段合流 → B 段合流 → A 段合流）只需 2 次。这里用两种贪心
    策略在"同材料尽量连续打印（省 purge）"与"先赶截止时间（省逾期）"
    之间取舍，生成不同倾向的候选；最终由 :func:`_feasible_heuristic`
    按 (purge, 逾期, 字典序) 统一核算择优。
    """
    jobs = inst.jobs
    runs_per_job = _job_runs(inst)

    def build(mode: str) -> List[int]:
        pos = [0] * len(jobs)  # 每个作业下一个待排 run 的下标
        cur = inst.initial_material
        order: List[int] = []
        remaining = sum(len(runs) for runs in runs_per_job)
        while remaining:
            avail = [j for j in range(len(jobs)) if pos[j] < len(runs_per_job[j])]

            def mat(j: int) -> str:
                return inst.material_of[runs_per_job[j][pos[j]][0]]

            same = [j for j in avail if mat(j) == cur]
            if mode == "edd":
                # 截止优先：先赶截止最早作业的下一个 run，不惜因此换料
                j = min(avail, key=lambda j: (jobs[j].deadline, jobs[j].job_id))
            elif same:
                # 同材料连续打印优先；多个同材料 run 间按 (截止, 作业 ID) 取舍
                j = min(same, key=lambda j: (jobs[j].deadline, jobs[j].job_id))
            else:
                # 不得不换料：purge 最小者优先，再按 (截止, 作业 ID) 取舍
                j = min(
                    avail,
                    key=lambda j: (
                        int(inst.purge_matrix[cur][mat(j)]),
                        jobs[j].deadline,
                        jobs[j].job_id,
                    ),
                )
            run = runs_per_job[j][pos[j]]
            order.extend(run)
            cur = mat(j)
            pos[j] += 1
            remaining -= 1
        return order

    orders: List[List[int]] = []
    seen: set = set()
    for mode in ("purge", "edd"):
        order = build(mode)
        key = tuple(order)
        if key not in seen:
            seen.add(key)
            orders.append(order)
    return orders


def _heuristic_orders(inst: Instance) -> List[List[int]]:
    """构造性候选排列（均严格遵守作业内层段顺序，因此必定合法）。

    CP-SAT 在限时内可能连一个可行解都找不到（大模型 / 时限极短）。
    用这些免求解的排列作为兜底，保证 API 仍能返回完整计划：

    * 整作业串行：输入作业顺序、反向输入顺序、截止时刻升序（EDD）、
      作业 ID 升序；
    * run 级按材料交错（见 :func:`_interleaved_orders`）：同材料连续段
      跨作业合并相邻，避免串行候选在同材料可合并时换料次数虚高
      （两作业均为 A→B→A 时串行至少 4 次换料、交错只需 2 次）。
    """
    job_orders = [
        [jd for jd in inst.jobs],
        list(reversed(inst.jobs)),
        sorted(inst.jobs, key=lambda jd: (jd.deadline, jd.job_id)),
        sorted(inst.jobs, key=lambda jd: jd.job_id),
    ]
    orders: List[List[int]] = []
    seen: set = set()
    for jds in job_orders:
        order = [seg["global_idx"] for jd in jds for seg in jd.segments]
        key = tuple(order)
        if key not in seen:
            seen.add(key)
            orders.append(order)
    for order in _interleaved_orders(inst):
        key = tuple(order)
        if key not in seen:
            seen.add(key)
            orders.append(order)
    return orders


def _feasible_heuristic(
    inst: Instance,
) -> Optional[Tuple[List[int], dict]]:
    """在所有构造性候选中挑出满足 purge 容量且 (purge, 逾期, 字典序) 最优者。

    紧密排产下任一遵守层序的排列都是可执行计划；purge 是否超容可经
    :func:`_materialize` 精确核算，不需要求解器参与。找不到满足容量的
    候选时返回 ``None``（可能真的容量无解，也可能只是启发式未找到）。
    """
    best: Optional[Tuple[Tuple[int, int, Tuple], List[int], dict]] = None
    for order in _heuristic_orders(inst):
        tl = _materialize(inst, order)
        if tl["purge_used_mm3"] > inst.purge_capacity:
            continue
        rank_key = tuple(
            inst.rank_of[i] for i in order
        )
        score = (
            tl["purge_used_mm3"],
            tl["total_tardiness_seconds"],
            rank_key,
        )
        if best is None or score < best[0]:
            best = (score, order, tl)
    if best is None:
        return None
    return best[1], best[2]


def solve(req: PlanRequest) -> dict:
    """求解入口，返回可直接用于构造 PlanResponse 的结果字典。

    任何情况下只要能给出可执行计划，就返回完整的计划 / 切换 / purge /
    逾期明细；只有确实拿不到计划时才返回 ``UNKNOWN``。三级目标的最优性
    分别按 CP-SAT 实际返回的状态如实标记，字典序决胜未完成证明时
    不声称 ``OPTIMAL``。
    """
    inst = build_instance(req)
    time_limit = float(req.time_limit_seconds or 15.0)
    t_phase = max(1.0, min(10.0, 0.4 * time_limit))
    per_check = max(0.2, min(1.5, 0.1 * time_limit))
    greedy_cap = max(2.0, 0.35 * time_limit)

    # 构造性可行计划：作为 CP-SAT 提示加速首解，并在超时时兜底
    heur = _feasible_heuristic(inst)
    heur_order = heur[0] if heur is not None else None

    # ---- 阶段 1：最小化 purge ----
    art1 = build_model(inst)
    r1 = _solve(art1, art1.purge_var, t_phase, inst=inst, hint_order=heur_order)

    if r1.status == "INFEASIBLE":
        code, detail = diagnose_infeasible(inst, t_phase)
        return {
            "status": "INFEASIBLE",
            "optimal": False,
            "infeasible_reason": detail,
            "solver_message": f"模型无解，归因: {code}",
        }

    purge_proven = r1.status == "OPTIMAL"
    if r1.order is not None and r1.purge is not None:
        best_order = r1.order
        purge_star = r1.purge
        tard_star = None
    elif heur is not None:
        # 求解器超时且未给出可行解：用构造性计划兜底，目标均未证明
        best_order, heur_tl = heur
        purge_star = heur_tl["purge_used_mm3"]
        tard_star = heur_tl["total_tardiness_seconds"]
        return _build_response(
            inst,
            best_order,
            purge_proven=False,
            tard_proven=False,
            lex_proven=False,
            status="FEASIBLE",
            solver_message=(
                f"限时内 CP-SAT 未找到可行解（{r1.message}）；"
                "返回构造性可行计划（作业内层段严格按序），未证明任何目标最优"
            ),
        )
    else:
        return {
            "status": "UNKNOWN",
            "optimal": False,
            "infeasible_reason": None,
            "solver_message": (
                f"限时内未找到可行解（{r1.message}），"
                "且构造性候选均不满足 purge 容量"
            ),
        }

    # ---- 阶段 2：purge 固定，最小化逾期 ----
    art2 = build_model(inst, purge_eq=purge_star)
    r2 = _solve(art2, art2.tardiness_var, t_phase, inst=inst, hint_order=best_order)
    tard_proven = False
    if r2.order is not None and r2.tardiness is not None:
        if r2.status != "INFEASIBLE":
            best_order = r2.order
            tard_star = r2.tardiness
            tard_proven = purge_proven and r2.status == "OPTIMAL"
    if tard_star is None:
        # 不应发生：阶段 1 的解本身可行；用阶段 1 解重算逾期，保守标记
        tl1 = _materialize(inst, best_order)
        tard_star = tl1["total_tardiness_seconds"]
        tard_proven = False

    # ---- 阶段 3：字典序最小决胜（仅前两级均已证最优时才值得做）----
    lex_proven = False
    if purge_proven and tard_proven:
        best_order, lex_proven = _lexicographic_fix(
            inst, best_order, purge_star, tard_star, per_check, greedy_cap
        )

    proven = purge_proven and tard_proven and lex_proven
    if proven:
        status = "OPTIMAL"
        message = "已证明三级目标（purge、逾期、字典序）均最优"
    elif purge_proven and tard_proven:
        status = "FEASIBLE"
        message = (
            "purge 与逾期均已证最优；字典序决胜在限时内未完全证明，"
            "返回当前最优目标值下的可行计划（不声称字典序最小）"
        )
    elif purge_proven:
        status = "FEASIBLE"
        message = "purge 已证最优；逾期优化在限时内未证明，字典序决胜未执行"
    else:
        status = "FEASIBLE"
        message = "限时内得到可行解并给出了目标值，但未证明最优"
    return _build_response(
        inst,
        best_order,
        purge_proven=purge_proven,
        tard_proven=tard_proven,
        lex_proven=lex_proven,
        status=status,
        solver_message=message,
    )


def _build_response(
    inst: Instance,
    order: List[int],
    *,
    purge_proven: bool,
    tard_proven: bool,
    lex_proven: bool,
    status: str,
    solver_message: str,
) -> dict:
    timeline = _materialize(inst, order)
    timeline["status"] = status
    timeline["optimal"] = purge_proven and tard_proven and lex_proven
    timeline["objective"] = {
        "total_purge_mm3": timeline["purge_used_mm3"],
        "total_tardiness_seconds": timeline["total_tardiness_seconds"],
        "purge_optimal": purge_proven,
        "tardiness_optimal": tard_proven,
        "lexicographic_minimal": lex_proven,
    }
    timeline["infeasible_reason"] = None
    timeline["solver_message"] = solver_message
    return timeline


def _materialize(inst: Instance, order: List[int]) -> dict:
    """按执行顺序紧密重排，生成输出计划、切换记录与逾期明细。"""
    t = 0
    cur = inst.initial_material
    plan = []
    switches = []
    purge_used = 0
    completion: Dict[str, int] = {}

    for pos, gi in enumerate(order, start=1):
        mat = inst.material_of[gi]
        if mat != cur:
            purge = int(inst.purge_matrix[cur][mat])
            switches.append(
                {
                    "order": len(switches) + 1,
                    "from_material": cur,
                    "to_material": mat,
                    "purge_mm3": purge,
                    "switch_seconds": inst.switch_seconds,
                    "after_segment_order": pos - 1,
                    "at_seconds": t,
                }
            )
            purge_used += purge
            t += inst.switch_seconds
            cur = mat
        start = t
        t += inst.duration_of[gi]
        plan.append(
            {
                "order": pos,
                "job_id": inst.job_of[gi],
                "segment_index": inst.seg_index_of[gi],
                "material_id": mat,
                "print_seconds": inst.duration_of[gi],
                "volume_mm3": inst.volume_of[gi],
                "start_seconds": start,
                "end_seconds": t,
            }
        )
        # 若该层段是某作业最后一段，记录完成时刻
        for jd in inst.jobs:
            if jd.last_idx == gi:
                completion[jd.job_id] = t

    job_tardiness = []
    total_tardiness = 0
    for job in inst.jobs:
        comp = completion.get(job.job_id, 0)
        tardy = max(0, comp - job.deadline)
        total_tardiness += tardy
        job_tardiness.append(
            {
                "job_id": job.job_id,
                "completion_seconds": comp,
                "deadline_seconds": job.deadline,
                "tardy_seconds": tardy,
            }
        )

    return {
        "plan": plan,
        "switches": switches,
        "job_tardiness": job_tardiness,
        "purge_used_mm3": purge_used,
        "purge_remaining_mm3": inst.purge_capacity - purge_used,
        "total_tardiness_seconds": total_tardiness,
        "makespan_seconds": t,
    }
