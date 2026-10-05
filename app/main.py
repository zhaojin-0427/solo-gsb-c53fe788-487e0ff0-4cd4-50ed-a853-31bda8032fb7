"""FastAPI 应用入口：POST /plans、POST /executions/audit、POST /spools/allocate。

* 非法请求体由 Pydantic 校验，自动返回 HTTP 422。
* 可行 / 无解 / 超时都在 200 响应中以 status 字段区分；
  无解时 infeasible_reason 指出 CAPACITY 或 PRECEDENCE。
* /executions/audit 不重新排产，只按实测时间线逐段回放核验，返回
  IN_PROGRESS / COMPLETED / VIOLATED 及已用与剩余 purge、完工作业逾期、
  违规位置；未知或重复层段、起止倒置同样返回 422。
* /spools/allocate 在调用方给定的固定层段顺序上做料卷分配：逐段扣减
  （换料 purge + 打印体积均由该段目标材料料卷承担），最小化相邻层段
  料卷 ID 变化次数并按逐段料卷 ID 字典序决胜；缺料、顺序 purge 超容
  在 200 响应中以 status 区分，漏段 / 乱序 / 料卷非法返回 422。
"""

from __future__ import annotations

from fastapi import FastAPI

from .allocation import allocate
from .audit import audit
from .schemas import (
    AllocateRequest,
    AllocateResponse,
    AuditRequest,
    AuditResponse,
    PlanRequest,
    PlanResponse,
)
from .solver import solve

app = FastAPI(
    title="多材料 3D 打印换料规划 API",
    version="1.2.0",
    description=(
        "在 purge 容量与作业截止时间约束下，规划多材料打印作业的层段交错顺序。"
        "目标优先级：总 purge 体积最小 → 总逾期最小 → 计划字典序最小。"
        "POST /executions/audit 按实测起止时间与段前 purge 逐段回放，核验"
        "执行是否满足串行、层序、换料留时与容量约束（保留空闲与实际耗时，"
        "不按紧密排产重算）。"
        "POST /spools/allocate 在固定层段顺序上为每段选择一卷料并逐卷扣减"
        "（换料 purge 与打印体积均从目标材料料卷扣除，卸下料卷可复用），"
        "最小化相邻层段料卷 ID 变化次数，再按逐段料卷 ID 字典序决胜。"
    ),
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/plans", response_model=PlanResponse)
def create_plan(req: PlanRequest) -> dict:
    return solve(req)


@app.post("/executions/audit", response_model=AuditResponse)
def audit_executions(req: AuditRequest) -> dict:
    return audit(req)


@app.post("/spools/allocate", response_model=AllocateResponse)
def allocate_spools(req: AllocateRequest) -> dict:
    return allocate(req)
