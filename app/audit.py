"""POST /executions/audit 的执行核验：按实测时间线逐段回放。

与 :mod:`app.solver` 的紧密重排不同，核验**保留空闲与实际耗时**——不重新
排产，只把提交的已执行层段按给定顺序逐条对照基准（:func:`build_instance`）
检查：

* 单喷头串行：每个层段的实测开始不得早于上一已执行层段的实测结束，
  否则记 ``OVERLAP``；
* 作业内层段严格按序：第 k 段执行时第 k-1 段必须已执行，否则记
  ``LAYER_ORDER_INVALID``（跨作业交错本身允许）；
* 换料（与上一已执行层段材料不同；首段相对初始材料）前必须留足
  ``switch_seconds`` 秒，否则记 ``SWITCH_TIME_INSUFFICIENT``；
* 段前实测 purge 不得低于矩阵需求，否则记 ``PURGE_SHORTFALL``；同材料
  相邻段前 purge 必须为零，否则记 ``SAME_MATERIAL_PURGE_NONZERO``；
  容量始终按**实测值**扣减，累计超出总容量记 ``PURGE_CAPACITY_EXCEEDED``。

未知作业 / 未知层序 / 重复层段 / 起止倒置属于请求格式错误，在
:class:`app.schemas.AuditRequest` 与 :class:`app.schemas.ExecutedSegment`
中拦截，返回 HTTP 422；上述六条规则的违反是执行违规，返回 200 且
``status = VIOLATED``，即使全部层段已提交也不能标完成。完工作业逾期只
统计全部层段均已执行的作业，未完工前缀不计。
"""

from __future__ import annotations

from typing import Dict, List, Optional

from .schemas import AuditRequest
from .solver import build_instance

# 违规代码常量
OVERLAP = "OVERLAP"
LAYER_ORDER_INVALID = "LAYER_ORDER_INVALID"
SWITCH_TIME_INSUFFICIENT = "SWITCH_TIME_INSUFFICIENT"
PURGE_SHORTFALL = "PURGE_SHORTFALL"
SAME_MATERIAL_PURGE_NONZERO = "SAME_MATERIAL_PURGE_NONZERO"
PURGE_CAPACITY_EXCEEDED = "PURGE_CAPACITY_EXCEEDED"


def audit(req: AuditRequest) -> dict:
    """核验入口：PlanRequest 已通过 Pydantic 校验，此处构建基准并逐段回放。"""
    inst = build_instance(req)

    # (job_id, segment_index) -> 全局层段号
    idx_of: Dict[tuple, int] = {}
    seg_count: Dict[str, int] = {}
    for jd in inst.jobs:
        seg_count[jd.job_id] = len(jd.segments)
        for seg in jd.segments:
            idx_of[(jd.job_id, seg["seg_index"])] = seg["global_idx"]

    violations: List[dict] = []
    replay: List[dict] = []

    executed: Dict[str, set] = {jd.job_id: set() for jd in inst.jobs}
    # 作业完工时刻（仅在全部层段集齐的那一刻记录）
    completion: Dict[str, int] = {}
    prev_gi: Optional[int] = None
    prev_end = 0
    cur_material = inst.initial_material
    purge_used = 0
    makespan = 0

    for pos, ex in enumerate(req.executions, start=1):
        gi = idx_of[(ex.job_id, ex.segment_index)]
        mat = inst.material_of[gi]
        switched = mat != cur_material
        gap = ex.start_seconds - prev_end

        # ---- 规则 1：单喷头串行，不得与上一已执行层段重叠 ----
        if ex.start_seconds < prev_end:
            if prev_gi is None:
                prev_ref = "开工时刻 0"
            else:
                prev_ref = (
                    f"作业 {inst.job_of[prev_gi]} 第 "
                    f"{inst.seg_index_of[prev_gi]} 段结束时刻 {prev_end}"
                )
            violations.append(
                {
                    "order": pos,
                    "job_id": ex.job_id,
                    "segment_index": ex.segment_index,
                    "code": OVERLAP,
                    "message": (
                        f"层段开始时刻 {ex.start_seconds} 早于{prev_ref}，"
                        "已执行层段时间重叠"
                    ),
                    "detail": {
                        "start_seconds": ex.start_seconds,
                        "previous_end_seconds": prev_end,
                    },
                }
            )

        # ---- 规则 2：作业内层段必须按序执行 ----
        k = ex.segment_index
        if k > 1 and (k - 1) not in executed[ex.job_id]:
            violations.append(
                {
                    "order": pos,
                    "job_id": ex.job_id,
                    "segment_index": k,
                    "code": LAYER_ORDER_INVALID,
                    "message": (
                        f"作业 {ex.job_id} 第 {k} 段在其第 {k - 1} 段之前执行，"
                        "作业内层序非法（跨作业可交错，但各作业层序须合法）"
                    ),
                    "detail": {"missing_predecessor": k - 1},
                }
            )

        # ---- 规则 3/4：换料留时、段前 purge 不低于矩阵需求 ----
        required_purge = 0
        if switched:
            required_purge = int(inst.purge_matrix[cur_material][mat])
            # 重叠已单独记 OVERLAP（gap 为负），不再重复记留时不足
            if gap >= 0 and gap < inst.switch_seconds:
                violations.append(
                    {
                        "order": pos,
                        "job_id": ex.job_id,
                        "segment_index": k,
                        "code": SWITCH_TIME_INSUFFICIENT,
                        "message": (
                            f"{cur_material}→{mat} 换料前须留足 "
                            f"switch_seconds={inst.switch_seconds} 秒，"
                            f"实际仅有 {gap} 秒空闲"
                        ),
                        "detail": {
                            "from_material": cur_material,
                            "to_material": mat,
                            "gap_seconds": gap,
                            "required_switch_seconds": inst.switch_seconds,
                            "previous_end_seconds": prev_end,
                            "start_seconds": ex.start_seconds,
                        },
                    }
                )
            if ex.purge_before_mm3 < required_purge:
                violations.append(
                    {
                        "order": pos,
                        "job_id": ex.job_id,
                        "segment_index": k,
                        "code": PURGE_SHORTFALL,
                        "message": (
                            f"{cur_material}→{mat} 换料段前实测 purge "
                            f"{ex.purge_before_mm3} mm^3 低于矩阵需求 "
                            f"{required_purge} mm^3"
                        ),
                        "detail": {
                            "from_material": cur_material,
                            "to_material": mat,
                            "purge_before_mm3": ex.purge_before_mm3,
                            "purge_required_mm3": required_purge,
                        },
                    }
                )
        elif ex.purge_before_mm3 != 0:
            # 同材料相邻：无需换料，段前 purge 必须为零
            violations.append(
                {
                    "order": pos,
                    "job_id": ex.job_id,
                    "segment_index": k,
                    "code": SAME_MATERIAL_PURGE_NONZERO,
                    "message": (
                        f"与上一已执行层段同为材料 {mat}，段前 purge 必须为 0，"
                        f"实测 {ex.purge_before_mm3} mm^3"
                    ),
                    "detail": {
                        "material_id": mat,
                        "purge_before_mm3": ex.purge_before_mm3,
                    },
                }
            )

        # 容量一律按实测段前 purge 扣减（即使不满足矩阵需求 / 同材料误排）
        purge_used += ex.purge_before_mm3
        if purge_used > inst.purge_capacity:
            violations.append(
                {
                    "order": pos,
                    "job_id": ex.job_id,
                    "segment_index": k,
                    "code": PURGE_CAPACITY_EXCEEDED,
                    "message": (
                        f"扣减段前实测 purge 后累计消耗 {purge_used} mm^3，"
                        f"超过总容量 {inst.purge_capacity} mm^3"
                    ),
                    "detail": {
                        "purge_used_mm3": purge_used,
                        "capacity_mm3": inst.purge_capacity,
                    },
                }
            )

        replay.append(
            {
                "order": pos,
                "job_id": ex.job_id,
                "segment_index": k,
                "material_id": mat,
                "start_seconds": ex.start_seconds,
                "end_seconds": ex.end_seconds,
                "print_seconds": ex.end_seconds - ex.start_seconds,
                "idle_seconds_before": gap,
                "material_switched": switched,
                "purge_required_mm3": required_purge,
                "purge_before_mm3": ex.purge_before_mm3,
            }
        )

        executed[ex.job_id].add(k)
        if (
            ex.job_id not in completion
            and len(executed[ex.job_id]) == seg_count[ex.job_id]
        ):
            completion[ex.job_id] = ex.end_seconds

        prev_gi = gi
        prev_end = ex.end_seconds
        cur_material = mat
        makespan = ex.end_seconds

    # 完工作业逾期：仅统计全部层段均已执行的作业，未完工前缀不计
    job_tardiness: List[dict] = []
    total_tardiness = 0
    for jd in inst.jobs:
        if jd.job_id not in completion:
            continue
        comp = completion[jd.job_id]
        tardy = max(0, comp - jd.deadline)
        total_tardiness += tardy
        job_tardiness.append(
            {
                "job_id": jd.job_id,
                "completion_seconds": comp,
                "deadline_seconds": jd.deadline,
                "tardy_seconds": tardy,
            }
        )

    executed_count = len(req.executions)
    if violations:
        status = "VIOLATED"
    elif executed_count == inst.n:
        status = "COMPLETED"
    else:
        status = "IN_PROGRESS"

    return {
        "status": status,
        "segments_total": inst.n,
        "segments_executed": executed_count,
        "purge_used_mm3": purge_used,
        "purge_remaining_mm3": inst.purge_capacity - purge_used,
        "total_tardiness_seconds": total_tardiness,
        "makespan_seconds": makespan,
        "job_tardiness": job_tardiness,
        "replay": replay,
        "violations": violations,
    }
