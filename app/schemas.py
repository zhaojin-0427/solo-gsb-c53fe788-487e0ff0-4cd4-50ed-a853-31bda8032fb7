"""请求 / 响应数据模型与输入校验。

非法数据统一由 Pydantic 抛出 ``RequestValidationError``，FastAPI 自动返回
HTTP 422（见 :mod:`app.main` 中对错误响应的统一格式处理）。
"""

from __future__ import annotations

from typing import Dict, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SegmentIn(BaseModel):
    """单个层段：一种材料的连续打印区间。"""

    model_config = ConfigDict(extra="forbid")

    material_id: str = Field(..., min_length=1, description="层段所用材料 ID")
    print_seconds: int = Field(..., ge=0, description="该层段打印耗时（秒）")
    volume_mm3: int = Field(..., ge=0, description="该层段耗材体积（mm^3，仅记录用）")


class JobIn(BaseModel):
    """一个打印作业，按顺序给出层段与交付截止时间。"""

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(..., min_length=1, description="作业 ID")
    deadline_seconds: int = Field(..., ge=0, description="截止秒数（相对开工时刻 0）")
    segments: list[SegmentIn] = Field(
        ...,
        min_length=1,
        description="按打印顺序排列的层段（至少 1 段，无固定上限；长作业直接进入 CP-SAT 规划）",
    )


class PlanRequest(BaseModel):
    """POST /plans 的请求体。"""

    model_config = ConfigDict(extra="forbid")

    jobs: list[JobIn] = Field(
        ...,
        min_length=2,
        max_length=8,
        description="2~8 个作业",
    )
    initial_material: str = Field(..., min_length=1, description="开机时喷头内的初始材料")
    # purge_matrix[from][to] = 从材料 from 切换到材料 to 需要排出的 purge 体积
    purge_matrix: Dict[str, Dict[str, int]] = Field(
        ...,
        description="材料切换 purge 体积矩阵（mm^3），键为材料 ID",
    )
    switch_seconds: int = Field(..., ge=0, description="每次材料切换的固定耗时（秒）")
    total_purge_capacity_mm3: int = Field(..., ge=0, description="全流程 purge 总容量（mm^3）")
    time_limit_seconds: Optional[float] = Field(
        default=None,
        ge=0.1,
        le=300.0,
        description="求解器时间上限（秒，可选，默认 15）",
    )

    @model_validator(mode="after")
    def _validate(self) -> "PlanRequest":
        # 作业 ID 唯一
        job_ids = [j.job_id for j in self.jobs]
        if len(set(job_ids)) != len(job_ids):
            raise ValueError("jobs 中 job_id 必须唯一")

        used_materials = {self.initial_material}
        for job in self.jobs:
            for seg in job.segments:
                used_materials.add(seg.material_id)

        # purge 矩阵条目必须为非负整数；同材料切换 purge 必须为 0
        for from_m, row in self.purge_matrix.items():
            if not isinstance(from_m, str) or not from_m:
                raise ValueError("purge_matrix 的键必须为非空字符串")
            for to_m, val in row.items():
                if not isinstance(to_m, str) or not to_m:
                    raise ValueError("purge_matrix 的键必须为非空字符串")
                if val < 0:
                    raise ValueError(
                        f"purge_matrix[{from_m}][{to_m}] 不能为负"
                    )
                if from_m == to_m and val != 0:
                    raise ValueError(
                        f"purge_matrix[{from_m}][{to_m}] 同材料切换必须为 0"
                    )

        # 被引用材料构成的子矩阵必须完整：每个来源材料行都必须存在，
        # 且包含每个目标材料列（允许出现未被引用的额外材料）。
        used = sorted(used_materials)
        for from_m in used:
            row = self.purge_matrix.get(from_m)
            if row is None:
                raise ValueError(f"purge_matrix 缺少来源材料行: {from_m}")
            missing = [to_m for to_m in used if to_m not in row]
            if missing:
                raise ValueError(
                    f"purge_matrix[{from_m}] 缺少目标材料列: " + ", ".join(missing)
                )
        return self


class PlannedSegment(BaseModel):
    """输出计划中的单个层段（按实际执行顺序排列）。"""

    order: int = Field(..., description="全局执行序号，从 1 开始")
    job_id: str
    segment_index: int = Field(..., description="作业内层段序号，从 1 开始")
    material_id: str
    print_seconds: int
    volume_mm3: int
    start_seconds: int
    end_seconds: int


class SwitchRecord(BaseModel):
    """一次材料切换（含从初始材料开始的首次切换）。"""

    order: int = Field(..., description="切换序号，从 1 开始")
    from_material: Optional[str] = Field(
        ..., description="切换前材料；首次切换来自初始材料，仍显式给出"
    )
    to_material: str
    purge_mm3: int
    switch_seconds: int
    after_segment_order: int = Field(
        ...,
        description="该切换发生在第几号层段之后；0 表示在首个层段之前",
    )
    at_seconds: int = Field(..., description="切换开始时刻（秒）")


class JobTardiness(BaseModel):
    job_id: str
    completion_seconds: int
    deadline_seconds: int
    tardy_seconds: int


class PlanResponse(BaseModel):
    status: str = Field(
        ...,
        description="OPTIMAL / FEASIBLE / INFEASIBLE / UNKNOWN",
    )
    optimal: bool = Field(..., description="是否已证明最优（含字典序决胜已证最优）")
    objective: Optional[dict] = Field(
        None,
        description="目标值：总 purge、总逾期，以及决胜阶段是否已证字典序最小"
    )
    plan: list[PlannedSegment] = Field(default_factory=list)
    switches: list[SwitchRecord] = Field(default_factory=list)
    job_tardiness: list[JobTardiness] = Field(default_factory=list)
    purge_used_mm3: int = 0
    purge_remaining_mm3: Optional[int] = None
    total_tardiness_seconds: int = 0
    makespan_seconds: int = 0
    infeasible_reason: Optional[dict] = Field(
        None,
        description="无解原因：CAPACITY（容量）或 PRECEDENCE（前置冲突）"
    )
    solver_message: str = ""


# ======================== POST /executions/audit ========================


class ExecutedSegment(BaseModel):
    """一个已实际执行的层段：实测起止秒与段前实测 purge。"""

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(..., min_length=1, description="所属作业 ID")
    segment_index: int = Field(..., ge=1, description="作业内层段序号，从 1 开始")
    start_seconds: int = Field(..., ge=0, description="实测开始时刻（秒，相对开工时刻 0）")
    end_seconds: int = Field(..., ge=0, description="实测结束时刻（秒）")
    purge_before_mm3: int = Field(
        ..., ge=0, description="该段开始前实际排出的 purge 体积（mm^3）"
    )

    @model_validator(mode="after")
    def _check_interval(self) -> "ExecutedSegment":
        if self.end_seconds < self.start_seconds:
            raise ValueError("end_seconds 不能早于 start_seconds（起止倒置）")
        return self


class AuditRequest(PlanRequest):
    """POST /executions/audit 的请求体：/plans 全部参数 + 实测执行层段序列。

    基类 :class:`PlanRequest` 的校验（作业、材料、purge 矩阵等）原样生效；
    本类额外校验已执行层段：未知作业 / 未知层序、重复层段一律 422
    （起止倒置在 :class:`ExecutedSegment` 内拦截）。
    """

    executions: list[ExecutedSegment] = Field(
        ...,
        description=(
            "按实际执行顺序排列的已完成层段；允许只提交前缀（作业未完工）。"
            "跨作业可交错，同一作业的层序合法性在回放时检查（违规而非 422）。"
        ),
    )

    @model_validator(mode="after")
    def _validate_executions(self) -> "AuditRequest":
        seg_counts = {j.job_id: len(j.segments) for j in self.jobs}
        seen: set = set()
        for ex in self.executions:
            if ex.job_id not in seg_counts:
                raise ValueError(f"executions 引用了未知作业: {ex.job_id}")
            if ex.segment_index > seg_counts[ex.job_id]:
                raise ValueError(
                    f"executions 引用了未知层段: 作业 {ex.job_id} 只有 "
                    f"{seg_counts[ex.job_id]} 段，却给出第 {ex.segment_index} 段"
                )
            key = (ex.job_id, ex.segment_index)
            if key in seen:
                raise ValueError(
                    f"executions 中层段重复: 作业 {ex.job_id} 第 {ex.segment_index} 段"
                )
            seen.add(key)
        return self


class ReplayedSegment(BaseModel):
    """回放序列中的单个已执行层段（时间全部采用实测值，不做紧密重排）。"""

    order: int = Field(..., description="在实测执行序列中的序号，从 1 开始")
    job_id: str
    segment_index: int
    material_id: str
    start_seconds: int = Field(..., description="实测开始时刻")
    end_seconds: int = Field(..., description="实测结束时刻")
    print_seconds: int = Field(..., description="实测打印耗时 = end - start")
    idle_seconds_before: int = Field(
        ...,
        description=(
            "该段开始距上一已执行层段结束的空闲秒数（首段相对时刻 0）；"
            "重叠时为负值"
        ),
    )
    material_switched: bool = Field(
        ..., description="相对上一已执行层段（首段相对初始材料）是否发生换料"
    )
    purge_required_mm3: int = Field(
        ..., description="按材料矩阵该换料所需的 purge；同材料为 0"
    )
    purge_before_mm3: int = Field(..., description="段前实测 purge（按此值扣容量）")


class ExecutionViolation(BaseModel):
    """回放中发现的一处违规（违规位置由 order/job_id/segment_index 定位）。"""

    order: int = Field(..., description="违规层段在实测执行序列中的序号")
    job_id: str
    segment_index: int
    code: str = Field(
        ...,
        description=(
            "OVERLAP / LAYER_ORDER_INVALID / SWITCH_TIME_INSUFFICIENT / "
            "PURGE_SHORTFALL / SAME_MATERIAL_PURGE_NONZERO / PURGE_CAPACITY_EXCEEDED"
        ),
    )
    message: str
    detail: Optional[dict] = Field(
        None, description="违规相关的实测值 / 要求值等定位信息"
    )


class AuditResponse(BaseModel):
    status: str = Field(
        ...,
        description="IN_PROGRESS（进行中）/ COMPLETED（全部完成且无违规）/ VIOLATED（存在违规）",
    )
    segments_total: int = Field(..., description="请求中全部层段数")
    segments_executed: int = Field(..., description="已执行层段数")
    purge_used_mm3: int = Field(..., description="已用 purge（逐段按实测值累计）")
    purge_remaining_mm3: int = Field(
        ..., description="剩余 purge = 总容量 - 已用；超容时为负值"
    )
    total_tardiness_seconds: int = Field(
        ..., description="已完工（全部层段均已执行）作业的逾期合计；未完工前缀不计"
    )
    makespan_seconds: int = Field(
        ..., description="最后一个已执行层段的实测结束时刻（无执行时为 0）"
    )
    job_tardiness: list[JobTardiness] = Field(
        default_factory=list, description="完工作业逾期明细（未完工的作业不计入）"
    )
    replay: list[ReplayedSegment] = Field(default_factory=list)
    violations: list[ExecutionViolation] = Field(default_factory=list)


# ======================== POST /spools/allocate ========================


class SpoolIn(BaseModel):
    """一个可用料卷：唯一 ID、材料与当前余量（卸下后余量保留，可复用）。"""

    model_config = ConfigDict(extra="forbid")

    spool_id: str = Field(..., min_length=1, description="料卷 ID，全局唯一")
    material_id: str = Field(..., min_length=1, description="料卷材料 ID")
    remaining_mm3: int = Field(..., ge=0, description="料卷当前余量（mm^3）")


class SequenceItem(BaseModel):
    """固定执行顺序中的一个层段引用。"""

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(..., min_length=1, description="作业 ID")
    segment_index: int = Field(..., ge=1, description="作业内层段序号，从 1 开始")


class SpoolAllocateRequest(PlanRequest):
    """POST /spools/allocate 的请求体：PlanRequest 全部参数 + 固定顺序 + 料卷。

    基类 :class:`PlanRequest` 的校验（作业、材料、purge 矩阵等）原样生效；
    本类额外校验：``sequence`` 必须恰好覆盖全部层段、无重复、不引用未知
    层段，且每个作业的层段按层序出现（漏段 / 重复 / 未知 / 乱序均 422）；
    ``spools`` 的 ``spool_id`` 必须唯一（空 ID、空材料、负余量由字段
    约束拦截）。
    """

    sequence: list[SequenceItem] = Field(
        ...,
        description="固定执行顺序：覆盖全部层段且作业内按层序排列（跨作业可交错）",
    )
    spools: list[SpoolIn] = Field(
        ...,
        description="可用料卷：唯一 spool_id、材料 ID、非负余量",
    )

    @model_validator(mode="after")
    def _validate_sequence_and_spools(self) -> "SpoolAllocateRequest":
        seg_counts = {j.job_id: len(j.segments) for j in self.jobs}
        seen: set = set()
        per_job: Dict[str, list] = {}
        for it in self.sequence:
            if it.job_id not in seg_counts:
                raise ValueError(f"sequence 引用了未知作业: {it.job_id}")
            if it.segment_index > seg_counts[it.job_id]:
                raise ValueError(
                    f"sequence 引用了未知层段: 作业 {it.job_id} 只有 "
                    f"{seg_counts[it.job_id]} 段，却给出第 {it.segment_index} 段"
                )
            key = (it.job_id, it.segment_index)
            if key in seen:
                raise ValueError(
                    f"sequence 中层段重复: 作业 {it.job_id} 第 {it.segment_index} 段"
                )
            seen.add(key)
            per_job.setdefault(it.job_id, []).append(it.segment_index)
        missing = [
            (j.job_id, k)
            for j in self.jobs
            for k in range(1, len(j.segments) + 1)
            if (j.job_id, k) not in seen
        ]
        if missing:
            job_id, seg_idx = missing[0]
            raise ValueError(
                f"sequence 未覆盖全部层段: 缺少作业 {job_id} 第 {seg_idx} 段"
                f"（共缺 {len(missing)} 段）"
            )
        # 覆盖全集后，作业内层段按下标升序 ⟺ 严格按 1..k 层序出现
        for job_id, idxs in per_job.items():
            if idxs != sorted(idxs):
                raise ValueError(
                    f"sequence 中作业 {job_id} 的层段未按层序排列（乱序）"
                )
        spool_ids = [s.spool_id for s in self.spools]
        if len(set(spool_ids)) != len(spool_ids):
            raise ValueError("spools 中 spool_id 必须唯一")
        return self


class AllocatedSegment(BaseModel):
    """逐段分配结果：选卷、扣减与扣后余量（按固定顺序排列）。"""

    order: int = Field(..., description="在固定执行顺序中的序号，从 1 开始")
    job_id: str
    segment_index: int = Field(..., description="作业内层段序号，从 1 开始")
    material_id: str
    volume_mm3: int = Field(..., description="该段名义打印体积（mm^3）")
    purge_before_mm3: int = Field(
        ...,
        description="段前 purge：与上一段材料不同（首段相对初始材料）时按矩阵计，否则为 0",
    )
    spool_id: str = Field(..., description="该段选用的料卷 ID")
    deduct_mm3: int = Field(
        ..., description="从该卷扣除的总量（mm^3）= 打印体积 + 段前 purge"
    )
    spool_remaining_after_mm3: int = Field(..., description="本段扣除后该卷的余量（mm^3）")


class SpoolUsage(BaseModel):
    """单卷用量汇总。"""

    spool_id: str
    material_id: str
    initial_remaining_mm3: int = Field(..., description="请求中给出的初始余量（mm^3）")
    used_mm3: int = Field(..., description="全流程累计扣减（mm^3）")
    remaining_mm3: int = Field(..., description="分配结束后的余量（mm^3）")


class SpoolAllocateResponse(BaseModel):
    status: str = Field(
        ...,
        description=(
            "OPTIMAL / FEASIBLE / INSUFFICIENT_STOCK / "
            "PURGE_CAPACITY_EXCEEDED / UNKNOWN"
        ),
    )
    optimal: bool = Field(
        ..., description="相邻料卷变化次数与逐段料卷字典序是否均已证最优"
    )
    objective: Optional[dict] = Field(
        None,
        description="目标值：相邻料卷变化次数，以及变化次数 / 字典序是否已证最优",
    )
    allocations: list[AllocatedSegment] = Field(default_factory=list)
    spools: list[SpoolUsage] = Field(default_factory=list)
    spool_changes: Optional[int] = Field(
        None, description="相邻层段料卷 ID 变化次数（无分配时为 null）"
    )
    purge_used_mm3: int = 0
    purge_remaining_mm3: Optional[int] = Field(
        None, description="总容量 - 顺序 purge 总量；超容时为负值"
    )
    solver_message: str = ""
