"""FastAPI 应用入口：POST /plans、POST /spools/allocate、POST /executions/audit。

* 非法请求体由 Pydantic 校验，自动返回 HTTP 422。
* /plans：可行 / 无解 / 超时都在 200 响应中以 status 字段区分；
  无解时 infeasible_reason 指出 CAPACITY 或 PRECEDENCE。
* /spools/allocate：提交 PlanRequest 与覆盖全部层段、作业内按序的固定
  执行顺序及料卷（唯一 ID、材料、非负余量），沿顺序计算 purge 并把每段
  打印体积与换料 purge 从所选（目标材料）料卷扣除；先最小化相邻层段料卷
  变化次数，再按逐段料卷 ID 字典序决胜。库存不足返回 INSUFFICIENT_STOCK、
  顺序 purge 超容返回 PURGE_CAPACITY_EXCEEDED（均无分配）；漏段、乱序、
  重复或非法料卷返回 422。
* /executions/audit 不重新排产，只按实测时间线逐段回放核验，返回
  IN_PROGRESS / COMPLETED / VIOLATED 及已用与剩余 purge、完工作业逾期、
  违规位置；未知或重复层段、起止倒置同样返回 422。
"""

from __future__ import annotations

from fastapi import FastAPI

from .audit import audit
from .schemas import (
    AuditRequest,
    AuditResponse,
    PlanRequest,
    PlanResponse,
    SpoolAllocateRequest,
    SpoolAllocateResponse,
)
from .solver import solve
from .spools import allocate

app = FastAPI(
    title="多材料 3D 打印换料规划 API",
    version="1.2.0",
    description=(
        "在 purge 容量与作业截止时间约束下，规划多材料打印作业的层段交错顺序。"
        "目标优先级：总 purge 体积最小 → 总逾期最小 → 计划字典序最小。"
        "POST /spools/allocate 在给定固定执行顺序与料卷库存时逐段选卷："
        "最小化相邻层段料卷变化次数，再按逐段料卷 ID 字典序决胜，"
        "返回逐段扣减与各卷余量。POST /executions/audit 按实测起止时间与"
        "段前 purge 逐段回放，核验执行是否满足串行、层序、换料留时与容量"
        "约束（保留空闲与实际耗时，不按紧密排产重算）。"
    ),
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/plans", response_model=PlanResponse)
def create_plan(req: PlanRequest) -> dict:
    return solve(req)


@app.post("/spools/allocate", response_model=SpoolAllocateResponse)
def allocate_spools(req: SpoolAllocateRequest) -> dict:
    return allocate(req)


@app.post("/executions/audit", response_model=AuditResponse)
def audit_executions(req: AuditRequest) -> dict:
    return audit(req)
