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
    """一卷实物料卷：唯一 ID、材料与当前余量。"""

    model_config = ConfigDict(extra="forbid")

    spool_id: str = Field(..., min_length=1, description="料卷 ID，请求内全局唯一")
    material_id: str = Field(..., min_length=1, description="该卷材料 ID")
    remaining_mm3: int = Field(..., ge=0, description="当前余量（mm^3，非负）")


class OrderedSegmentRef(BaseModel):
    """固定顺序中的一个层段引用：作业 ID + 作业内层序。"""

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(..., min_length=1, description="所属作业 ID")
    segment_index: int = Field(..., ge=1, description="作业内层段序号，从 1 开始")


class AllocateRequest(PlanRequest):
    """POST /spools/allocate 的请求体：/plans 全部参数 + 料卷库存 + 固定层段顺序。

    层段顺序不由求解器重排，而由调用方以 ``order`` 显式给定：必须**覆盖全部
    层段且每段恰好一次**，跨作业可交错，但同一作业内的层序必须严格递增
    （乱序、漏段、重复、引用未知作业 / 未知层段一律 422）。料卷 ID 必须唯一、
    余量非负；未被任何材料引用的料卷不会被选用，仍在响应中原样列出。
    """

    spools: list[SpoolIn] = Field(..., min_length=1, description="料卷库存（至少 1 卷）")
    order: list[OrderedSegmentRef] = Field(
        ...,
        description=(
            "层段的固定执行顺序：覆盖全部层段且每段恰好一次；跨作业可交错，"
            "同一作业内层序必须严格递增"
        ),
    )

    @model_validator(mode="after")
    def _validate_allocate(self) -> "AllocateRequest":
        # 料卷 ID 唯一
        spool_ids = [s.spool_id for s in self.spools]
        if len(set(spool_ids)) != len(spool_ids):
            raise ValueError("spools 中 spool_id 必须唯一")

        seg_counts = {j.job_id: len(j.segments) for j in self.jobs}
        expected = {
            (j.job_id, k)
            for j in self.jobs
            for k in range(1, seg_counts[j.job_id] + 1)
        }

        seen: set = set()
        last_index: Dict[str, int] = {}
        for pos, ref in enumerate(self.order, start=1):
            if ref.job_id not in seg_counts:
                raise ValueError(f"order 引用了未知作业: {ref.job_id}")
            if ref.segment_index > seg_counts[ref.job_id]:
                raise ValueError(
                    f"order 引用了未知层段: 作业 {ref.job_id} 只有 "
                    f"{seg_counts[ref.job_id]} 段，却给出第 {ref.segment_index} 段"
                )
            key = (ref.job_id, ref.segment_index)
            if key in seen:
                raise ValueError(
                    f"order 中层段重复: 作业 {ref.job_id} 第 {ref.segment_index} 段"
                )
            prev = last_index.get(ref.job_id)
            if prev is not None and ref.segment_index <= prev:
                raise ValueError(
                    f"order 中作业 {ref.job_id} 的层序乱序：第 {ref.segment_index} 段"
                    f"出现在第 {prev} 段之后（作业内必须严格按序）"
                )
            last_index[ref.job_id] = ref.segment_index
            seen.add(key)

        missing = sorted(expected - seen)
        if missing:
            preview = ", ".join(f"{j}.{k}" for j, k in missing[:5])
            more = " …" if len(missing) > 5 else ""
            raise ValueError(
                f"order 未覆盖全部层段：期望 {len(expected)} 段、实际 {len(seen)} 段，"
                f"缺少 {len(missing)} 段（{preview}{more}）"
            )
        return self


class AllocationItem(BaseModel):
    """固定顺序中一段的选卷结果与该段对料卷的扣减。"""

    order: int = Field(..., description="在提交顺序中的序号，从 1 开始")
    job_id: str
    segment_index: int
    material_id: str
    spool_id: str = Field(..., description="该段选用的料卷 ID（每段恰选一卷）")
    material_switched: bool = Field(
        ..., description="相对上一段（首段相对初始材料）是否换料"
    )
    purge_before_mm3: int = Field(
        ..., description="沿顺序按 purge_matrix 计算的换料 purge；同材料为 0"
    )
    print_volume_mm3: int = Field(..., description="该段名义打印体积（mm^3）")
    deducted_mm3: int = Field(
        ..., description="从所选料卷扣除的总量 = purge_before_mm3 + print_volume_mm3"
    )
    spool_changed: bool = Field(
        ...,
        description=(
            "所选料卷 ID 是否与紧邻上一段不同（首段为 false）；"
            "换料处必然换卷，同材料相邻时换卷是优化目标"
        ),
    )


class SpoolDeduction(BaseModel):
    """一次从某料卷的扣减（对应一个层段，按提交顺序排列）。"""

    order: int
    job_id: str
    segment_index: int
    material_id: str
    purge_before_mm3: int
    print_volume_mm3: int
    deducted_mm3: int
    remaining_after_mm3: int = Field(
        ..., description="本次扣减后该卷余量（mm^3）"
    )


class SpoolBalance(BaseModel):
    """单卷的扣减汇总与逐段扣减明细。"""

    spool_id: str
    material_id: str
    initial_remaining_mm3: int = Field(..., description="请求中声明的初始余量")
    deducted_mm3: int = Field(..., description="累计扣减（purge + 打印体积）")
    remaining_mm3: int = Field(..., description="最终余量 = 初始余量 - 累计扣减")
    deductions: list[SpoolDeduction] = Field(
        default_factory=list, description="逐段扣减明细（按提交顺序）"
    )


class AllocateResponse(BaseModel):
    status: str = Field(
        ...,
        description=(
            "ALLOCATED（已完整分配）/ MATERIAL_SHORTAGE（库存不足）/ "
            "PURGE_CAPACITY_EXCEEDED（固定顺序的 purge 超出总容量）"
        ),
    )
    optimal: bool = Field(
        ...,
        description=(
            "是否已证明分配最优（料卷变化次数最小且逐段料卷 ID 字典序决胜均证明）；"
            "缺料 / 超容时为 false"
        ),
    )
    allocation: list[AllocationItem] = Field(
        default_factory=list,
        description="逐段选卷与扣减（按提交顺序）；缺料 / 超容时为空，不产生任何分配",
    )
    spools: list[SpoolBalance] = Field(
        default_factory=list,
        description="各卷扣减汇总与余量；缺料 / 超容时累计扣减均为 0",
    )
    spool_changes: int = Field(
        0, description="相邻层段所选料卷 ID 不同的次数（优化目标）"
    )
    purge_used_mm3: int = Field(
        0, description="沿固定顺序累计的换料 purge（mm^3）"
    )
    purge_remaining_mm3: int = Field(
        0, description="purge 总容量 - 已用 purge"
    )
    reason: Optional[dict] = Field(
        None,
        description=(
            "非 ALLOCATED 时的原因：MATERIAL_SHORTAGE 带各材料缺料明细，"
            "PURGE_CAPACITY_EXCEEDED 带所需 purge 与总容量"
        ),
    )
    solver_message: str = ""
