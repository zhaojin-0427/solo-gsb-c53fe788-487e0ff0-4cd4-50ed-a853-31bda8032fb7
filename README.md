# 多材料 3D 打印换料规划 API

基于 **Python + FastAPI + Google OR-Tools（CP-SAT）** 的排产服务。给定 2～8 个打印作业
（每个作业由若干**按顺序执行的层段**组成）、初始材料、材料切换 purge 体积矩阵、
单次切换耗时与总 purge 容量，计算层段交错执行计划：

- 作业之间**只能在层段边界交错**，层段不可拆分，作业内层段严格按序；
- 每次材料切换按 `purge_matrix[from][to]` 消耗 purge 容量，并占用 `switch_seconds` 秒
  （首层段材料与初始材料不同时，也要先切换再打印）；同材料相邻无需切换；
- 作业逾期 = `max(0, 该作业最后层段完成时刻 - 截止秒数)`；
- purge 总消耗超过 `total_purge_capacity_mm3` 即**无解**。

优化目标按严格优先级依次为：

1. **总 purge 体积最小**；
2. 在 purge 最优的前提下，**总逾期秒数最小**；
3. 前两者相同时，按执行序列的 `(作业 ID, 层序)` 取**字典序最小**计划
   （作业 ID 字符串升序，再按作业内层段序号升序，逐位比较）。

返回切换记录、purge 余量、逐作业逾期明细与求解状态；未在限时内证明最优时
**不会**标记为最优。

服务另提供 **`POST /executions/audit`（执行核验）**：提交与 `/plans` 相同的
规划参数，以及按**实际执行顺序**排列的已完成层段（作业 ID、层序、实测起止秒、
段前实测 purge）。核验经同样的 `PlanRequest` 校验、用 `build_instance` 构建
基准后**逐段按实测时间线回放**——保留空闲与实际耗时，不按紧密排产重算——返回
进行中 / 完成 / 违规状态、已用与剩余 purge、完工作业逾期与违规位置。

---

## 一、Docker Compose 一键启动

前置要求：已安装 Docker 与 Docker Compose 插件。

```bash
docker compose up --build -d
```

服务监听在宿主机 `http://localhost:8000`。查看日志 / 停止：

```bash
docker compose logs -f
docker compose down
```

健康检查：

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

交互式 API 文档（浏览器打开）：

- Swagger UI：http://localhost:8000/docs
- ReDoc：http://localhost:8000/redoc

## 二、本地直接运行（可选）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行测试：

```bash
pip install pytest
pytest -q
```

## 三、调用接口

### `POST /plans`

请求字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `jobs` | 数组（2～8 项） | 作业列表 |
| `jobs[].job_id` | 非空字符串 | 作业 ID，全局唯一 |
| `jobs[].deadline_seconds` | 非负整数 | 截止秒数（相对开工时刻 0） |
| `jobs[].segments` | 非空数组（至少 1 段，无固定上限） | 按打印顺序排列的层段；单作业 25 段及以上的长作业同样进入 CP-SAT 规划 |
| `segments[].material_id` | 非空字符串 | 层段材料 ID |
| `segments[].print_seconds` | 非负整数 | 层段打印秒数 |
| `segments[].volume_mm3` | 非负整数 | 层段耗材体积 mm³（记录用） |
| `initial_material` | 非空字符串 | 喷头初始材料 |
| `purge_matrix` | 对象 | `{from: {to: purge体积}}`，非负整数；对角必须为 0；所有被引用材料（含初始材料）构成的子矩阵行列必须完整 |
| `switch_seconds` | 非负整数 | 每次材料切换的固定耗时（秒） |
| `total_purge_capacity_mm3` | 非负整数 | 全流程 purge 总容量（mm³） |
| `time_limit_seconds` | 数值，0.1～300，可选 | 求解器时间上限，默认 15 秒 |

请求示例（文件见 `examples/request.json`）：

```json
{
  "jobs": [
    {
      "job_id": "J1",
      "deadline_seconds": 120,
      "segments": [
        {"material_id": "PLA", "print_seconds": 60, "volume_mm3": 100},
        {"material_id": "ABS", "print_seconds": 30, "volume_mm3": 40}
      ]
    },
    {
      "job_id": "J2",
      "deadline_seconds": 220,
      "segments": [
        {"material_id": "ABS", "print_seconds": 80, "volume_mm3": 120},
        {"material_id": "PLA", "print_seconds": 20, "volume_mm3": 30}
      ]
    }
  ],
  "initial_material": "PLA",
  "purge_matrix": {
    "PLA": {"PLA": 0, "ABS": 120},
    "ABS": {"PLA": 80, "ABS": 0}
  },
  "switch_seconds": 10,
  "total_purge_capacity_mm3": 400,
  "time_limit_seconds": 15
}
```

调用：

```bash
curl -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d @examples/request.json
```

**响应（与服务实际输出一致）**：

```json
{
  "status": "OPTIMAL",
  "optimal": true,
  "objective": {
    "total_purge_mm3": 200,
    "total_tardiness_seconds": 0,
    "purge_optimal": true,
    "tardiness_optimal": true,
    "lexicographic_minimal": true
  },
  "plan": [
    {"order": 1, "job_id": "J1", "segment_index": 1, "material_id": "PLA",
     "print_seconds": 60, "volume_mm3": 100, "start_seconds": 0, "end_seconds": 60},
    {"order": 2, "job_id": "J1", "segment_index": 2, "material_id": "ABS",
     "print_seconds": 30, "volume_mm3": 40, "start_seconds": 70, "end_seconds": 100},
    {"order": 3, "job_id": "J2", "segment_index": 1, "material_id": "ABS",
     "print_seconds": 80, "volume_mm3": 120, "start_seconds": 100, "end_seconds": 180},
    {"order": 4, "job_id": "J2", "segment_index": 2, "material_id": "PLA",
     "print_seconds": 20, "volume_mm3": 30, "start_seconds": 190, "end_seconds": 210}
  ],
  "switches": [
    {"order": 1, "from_material": "PLA", "to_material": "ABS",
     "purge_mm3": 120, "switch_seconds": 10,
     "after_segment_order": 1, "at_seconds": 60},
    {"order": 2, "from_material": "ABS", "to_material": "PLA",
     "purge_mm3": 80, "switch_seconds": 10,
     "after_segment_order": 3, "at_seconds": 180}
  ],
  "job_tardiness": [
    {"job_id": "J1", "completion_seconds": 100, "deadline_seconds": 120, "tardy_seconds": 0},
    {"job_id": "J2", "completion_seconds": 210, "deadline_seconds": 220, "tardy_seconds": 0}
  ],
  "purge_used_mm3": 200,
  "purge_remaining_mm3": 200,
  "total_tardiness_seconds": 0,
  "makespan_seconds": 210,
  "infeasible_reason": null,
  "solver_message": "已证明三级目标（purge、逾期、字典序）均最优"
}
```

结果解读：最优排产把两个 ABS 层段相邻（J1.2 后紧跟 J2.1，免一次切换），
全流程仅 PLA→ABS（120 mm³）与 ABS→PLA（80 mm³）两次切换，共 200 mm³，
两个作业均按期完成，purge 余量 200 mm³。

### 无解响应（容量超限）

将 `examples/request_infeasible.json` 中容量改为 150 mm³：

```bash
curl -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d @examples/request_infeasible.json
```

```json
{
  "status": "INFEASIBLE",
  "optimal": false,
  "objective": null,
  "plan": [],
  "switches": [],
  "job_tardiness": [],
  "purge_used_mm3": 0,
  "purge_remaining_mm3": null,
  "total_tardiness_seconds": 0,
  "makespan_seconds": 0,
  "infeasible_reason": {
    "code": "CAPACITY",
    "message": "任何可行排产至少需要 purge 200 mm^3，超过总容量 150 mm^3",
    "minimum_required_purge_mm3": 200,
    "capacity_mm3": 150
  },
  "solver_message": "模型无解，归因: CAPACITY"
}
```

无解归因 `infeasible_reason.code`：

- `CAPACITY`：层段先后本身可排，但最小 purge 需求仍超过容量（响应给出
  `minimum_required_purge_mm3`）；
- `PRECEDENCE`：层段前置约束冲突（本服务层序由输入保证，正常数据下不会出现，
  仅作为防御性归因）；
- `UNKNOWN`：限时内无法完成归因诊断。

### 长作业（单作业 25 段及以上）

层段数量没有固定上限：单个作业包含 25 个或更多按序层段时是**合法请求**，
不会被拒绝（HTTP 422 仅针对真正非法的输入），而是直接进入 OR-Tools
CP-SAT 规划。示例 `examples/request_long_job.json` 中 `LONG` 作业含 25 个
按序层段（PLA/ABS/TPU 循环）：

```bash
docker compose up --build -d

curl -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d @examples/request_long_job.json
```

响应与普通请求结构完全一致：`plan` 覆盖全部层段且每个作业内的
`segment_index` 严格按 1…N 排列，`switches` / `job_tardiness` /
`purge_used_mm3` / `purge_remaining_mm3` 等明细同样完整返回；三级目标
全部由 CP-SAT 证明时才标记 `"status": "OPTIMAL"`。

当模型较大或 `time_limit_seconds` 到期时：

- CP-SAT 已给出可行解但未完成最优性证明 → `FEASIBLE`，`objective` 中
  `purge_optimal` / `tardiness_optimal` / `lexicographic_minimal` 分别
  如实反映各级目标**是否确已证明**（可能 purge 已证最优而字典序未证明）；
- CP-SAT 连可行解都未找到 → 服务用免求解的**构造性排列**兜底：除整作业
  串行外，还把每个作业切成同材料连续段（run）并**按材料交错**，让不同
  作业的同材料 run 相邻执行（两个均按 A→B→A 用料的作业，串行至少换料
  4 次，按材料交错只需 2 次）。候选严格遵守作业内层段顺序，purge 经
  精确核算，仅取不超容者并按（purge、逾期、字典序）择优，仍返回完整、
  按序的计划与全部明细，状态为 `FEASIBLE`，且三个最优性标记均为 `false`；
- 构造性候选也不满足 purge 容量（或确属容量无解）→ `UNKNOWN` /
  `INFEASIBLE`，此时 `plan` 为空，不会伪造计划。

可用极短时限观察该行为（大模型通常来不及证明最优）：

```bash
curl -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d @examples/request_long_job.json \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status"], d["objective"])'
# OPTIMAL {'total_purge_mm3': 2320, ..., 'lexicographic_minimal': True}
```

### 按材料交错的长作业（A→B→A 用料）

`examples/request_interleave.json` 是需要跨作业交错才能满足容量的典型场景：
两个 25 段长作业均按 A→B→A 用料（9 段 A、8 段 B、8 段 A，允许同材料连续
多段），初始材料 A，跨材料 purge 均为 1 mm³，总容量仅 2 mm³：

- **整作业串行**（J1 打完再打 J2）至少换料 4 次（每个作业 A→B→A 各 2 次），
  purge 共 4 mm³，超出容量；
- **按材料交错**——两作业的 A 段合流、B 段合流、再回到 A 段——只需
  A→B、B→A 共 2 次换料，恰好不超容。

启动服务并调用：

```bash
docker compose up --build -d

curl -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d @examples/request_interleave.json
```

实际响应（`plan` 共 50 段，此处中间从略，其余字段与实际输出一致）：

```json
{
  "status": "FEASIBLE",
  "optimal": false,
  "objective": {
    "total_purge_mm3": 2,
    "total_tardiness_seconds": 0,
    "purge_optimal": true,
    "tardiness_optimal": true,
    "lexicographic_minimal": false
  },
  "plan": [
    {"order": 1, "job_id": "J1", "segment_index": 1, "material_id": "A",
     "print_seconds": 10, "volume_mm3": 5, "start_seconds": 0, "end_seconds": 10},
    {"order": 2, "job_id": "J1", "segment_index": 2, "material_id": "A",
     "print_seconds": 10, "volume_mm3": 5, "start_seconds": 10, "end_seconds": 20},
    ...
    {"order": 49, "job_id": "J2", "segment_index": 24, "material_id": "A",
     "print_seconds": 10, "volume_mm3": 5, "start_seconds": 484, "end_seconds": 494},
    {"order": 50, "job_id": "J2", "segment_index": 25, "material_id": "A",
     "print_seconds": 10, "volume_mm3": 5, "start_seconds": 494, "end_seconds": 504}
  ],
  "switches": [
    {"order": 1, "from_material": "A", "to_material": "B", "purge_mm3": 1,
     "switch_seconds": 2, "after_segment_order": 18, "at_seconds": 180},
    {"order": 2, "from_material": "B", "to_material": "A", "purge_mm3": 1,
     "switch_seconds": 2, "after_segment_order": 34, "at_seconds": 342}
  ],
  "job_tardiness": [
    {"job_id": "J1", "completion_seconds": 424, "deadline_seconds": 100000, "tardy_seconds": 0},
    {"job_id": "J2", "completion_seconds": 504, "deadline_seconds": 100000, "tardy_seconds": 0}
  ],
  "purge_used_mm3": 2,
  "purge_remaining_mm3": 0,
  "total_tardiness_seconds": 0,
  "makespan_seconds": 504,
  "infeasible_reason": null,
  "solver_message": "purge 与逾期均已证最优；字典序决胜在限时内未完全证明，返回当前最优目标值下的可行计划（不声称字典序最小）"
}
```

结果解读：计划把两个作业的同材料连续段合并相邻——第 1～18 段为两作业的
A 段、第 19～34 段为 B 段、第 35～50 段为 A 段——仅 2 次换料
（A→B、B→A 各 1 mm³），purge 余量 0，两作业均按期完成。purge 与逾期
已证最优；50 个位置的字典序决胜未在限时内全部证明，故 `status` 为
`FEASIBLE` 而非 `OPTIMAL`（若证明完成则返回 `OPTIMAL`，计划内容不变）。

用极短时限（如把请求中 `"time_limit_seconds"` 改为 `0.5`）调用同一请求时，
CP-SAT 可能来不及给出任何解，服务改用免求解的构造性候选兜底：整作业串行
候选 purge 为 4、全部超容被弃，按材料交错候选 purge 为 2 被选中，仍返回
与上面相同的完整计划，`status` 为 `FEASIBLE` 且三个 `*_optimal` 标记均为
`false`——不会误报 `UNKNOWN` 和空计划。

### 求解状态语义

| `status` | `optimal` | 含义 |
| --- | --- | --- |
| `OPTIMAL` | `true` | 三级目标（含字典序决胜）均已由 CP-SAT 证明最优 |
| `FEASIBLE` | `false` | 已得到可行计划并给出完整明细，但**未证明全部目标最优**；`objective` 中三个 `*_optimal` 标记分别如实表示 purge / 逾期 / 字典序是否确已证明（含 CP-SAT 超时后由构造性排列兜底的情形，此时三者均为 `false`） |
| `INFEASIBLE` | `false` | 无解，见 `infeasible_reason` |
| `UNKNOWN` | `false` | 限时内连可行计划都无法给出（构造性候选也不满足容量） |

### `POST /executions/audit` 执行核验

提交 `/plans` 的全部请求参数，外加一个 `executions` 数组——按**实际执行顺序**
排列的已完成层段（允许只提交一个未完工前缀；跨作业可以交错）。服务先经与
`/plans` 完全相同的 `PlanRequest` 校验，再用 `build_instance` 构建基准，然后
**按实测时间线逐段回放**：

- **保留空闲与实际耗时，不按紧密排产重算**：段的开始/结束时刻、段间空闲一律
  采用提交的实测值；逾期也按实测完工时刻计算；
- **换料前须留足 `switch_seconds`**：某段材料与上一已执行层段不同（首段相对
  `initial_material`）时，段前空闲不得少于切换耗时；
- **段前 purge 不低于矩阵需求**：实测值低于 `purge_matrix[from][to]` 即违规；
  容量始终**按实测值**扣减（即使实测值不达标也照扣）；
- **同材料段前 purge 为零**：与上一已执行层段材料相同时不换料，段前 purge
  必须为 0；
- **跨作业可交错，但各自层序须合法**：作业 J 第 k 段执行时，其第 k-1 段必须
  已执行；
- **单喷头串行**：层段实测开始不得早于上一已执行层段的实测结束（零耗时层段
  首尾相接合法）。

请求字段（`executions[]` 部分，其余字段与 `/plans` 完全一致）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `executions` | 数组（可为空） | 按实际执行顺序排列的已完成层段 |
| `executions[].job_id` | 非空字符串 | 作业 ID，必须在 `jobs` 中存在 |
| `executions[].segment_index` | 正整数 | 作业内层段序号，必须在该作业层段范围内 |
| `executions[].start_seconds` | 非负整数 | 实测开始时刻（秒，相对开工时刻 0） |
| `executions[].end_seconds` | 非负整数 | 实测结束时刻（秒），不得早于开始时刻 |
| `executions[].purge_before_mm3` | 非负整数 | 段前实测 purge（mm³），按此值扣容量 |

**返回状态**（响应字段 `status`）：

- `IN_PROGRESS`：无违规且尚未提交全部层段（`executions` 为空也是进行中）；
- `COMPLETED`：无违规且全部层段均已提交；
- `VIOLATED`：回放中发现任何违规——**即使全部层段都已提交也不能标完成**。

完工作业逾期只统计**全部层段均已执行**的作业；未完工前缀不计逾期。
响应主要字段：

| 字段 | 说明 |
| --- | --- |
| `segments_total` / `segments_executed` | 基准层段总数 / 已提交执行的层段数 |
| `purge_used_mm3` | 逐段按实测 `purge_before_mm3` 累计的已用 purge |
| `purge_remaining_mm3` | 总容量 − 已用；超容时为负值 |
| `total_tardiness_seconds` / `job_tardiness[]` | 仅完工作业的逾期合计与明细 |
| `makespan_seconds` | 最后一个已执行层段的实测结束时刻（无执行时为 0） |
| `replay[]` | 逐段回放明细：实测起止、实测耗时、段前空闲、是否换料、矩阵需求 purge、实测 purge |
| `violations[]` | 违规位置（`order` / `job_id` / `segment_index`）、代码与定位信息 |

违规代码 `violations[].code`：

| 代码 | 触发条件 |
| --- | --- |
| `OVERLAP` | 实测开始早于上一已执行层段结束（层段重叠） |
| `LAYER_ORDER_INVALID` | 该作业第 k 段执行时第 k-1 段尚未执行 |
| `SWITCH_TIME_INSUFFICIENT` | 换料前空闲少于 `switch_seconds` |
| `PURGE_SHORTFALL` | 段前实测 purge 低于矩阵需求 |
| `SAME_MATERIAL_PURGE_NONZERO` | 同材料相邻（无需换料）但段前 purge 非零 |
| `PURGE_CAPACITY_EXCEEDED` | 按实测值累计扣减后 purge 超出总容量 |

#### 核验请求示例：合规完成

请求体见 `examples/request_audit_completed.json`（与 `/plans` 最优计划相同的
执行顺序，保留每次换料的 10 秒留时）：

```bash
curl -X POST http://localhost:8000/executions/audit \
  -H 'Content-Type: application/json' \
  -d @examples/request_audit_completed.json
```

**实际响应**：

```json
{
  "status": "COMPLETED",
  "segments_total": 4,
  "segments_executed": 4,
  "purge_used_mm3": 200,
  "purge_remaining_mm3": 200,
  "total_tardiness_seconds": 0,
  "makespan_seconds": 210,
  "job_tardiness": [
    {"job_id": "J1", "completion_seconds": 100, "deadline_seconds": 120, "tardy_seconds": 0},
    {"job_id": "J2", "completion_seconds": 210, "deadline_seconds": 220, "tardy_seconds": 0}
  ],
  "replay": [
    {"order": 1, "job_id": "J1", "segment_index": 1, "material_id": "PLA",
     "start_seconds": 0, "end_seconds": 60, "print_seconds": 60,
     "idle_seconds_before": 0, "material_switched": false,
     "purge_required_mm3": 0, "purge_before_mm3": 0},
    {"order": 2, "job_id": "J1", "segment_index": 2, "material_id": "ABS",
     "start_seconds": 70, "end_seconds": 100, "print_seconds": 30,
     "idle_seconds_before": 10, "material_switched": true,
     "purge_required_mm3": 120, "purge_before_mm3": 120},
    {"order": 3, "job_id": "J2", "segment_index": 1, "material_id": "ABS",
     "start_seconds": 100, "end_seconds": 180, "print_seconds": 80,
     "idle_seconds_before": 0, "material_switched": false,
     "purge_required_mm3": 0, "purge_before_mm3": 0},
    {"order": 4, "job_id": "J2", "segment_index": 2, "material_id": "PLA",
     "start_seconds": 190, "end_seconds": 210, "print_seconds": 20,
     "idle_seconds_before": 10, "material_switched": true,
     "purge_required_mm3": 80, "purge_before_mm3": 80}
  ],
  "violations": []
}
```

`replay` 中两次换料的段前空闲恰为 `switch_seconds=10`，同材料相邻的 J2.1
段前 purge 为 0；J1.2 与 J2.1 都是 ABS，跨作业相邻没有额外消耗。

#### 核验请求示例：进行中前缀（未完工不计逾期）

只提交前两段（J1 全部完成，J2 未开始），并把 J1 截止时刻提前到 90 秒：

```bash
curl -X POST http://localhost:8000/executions/audit \
  -H 'Content-Type: application/json' \
  -d '{
    "jobs": [
      {"job_id": "J1", "deadline_seconds": 90, "segments": [
        {"material_id": "PLA", "print_seconds": 60, "volume_mm3": 100},
        {"material_id": "ABS", "print_seconds": 30, "volume_mm3": 40}]},
      {"job_id": "J2", "deadline_seconds": 220, "segments": [
        {"material_id": "ABS", "print_seconds": 80, "volume_mm3": 120},
        {"material_id": "PLA", "print_seconds": 20, "volume_mm3": 30}]}
    ],
    "initial_material": "PLA",
    "purge_matrix": {"PLA": {"PLA": 0, "ABS": 120}, "ABS": {"PLA": 80, "ABS": 0}},
    "switch_seconds": 10,
    "total_purge_capacity_mm3": 400,
    "executions": [
      {"job_id": "J1", "segment_index": 1, "start_seconds": 0,  "end_seconds": 60,  "purge_before_mm3": 0},
      {"job_id": "J1", "segment_index": 2, "start_seconds": 75, "end_seconds": 105, "purge_before_mm3": 120}
    ]
  }'
```

**实际响应**（`replay` 与其余字段从略）：

```json
{
  "status": "IN_PROGRESS",
  "segments_total": 4,
  "segments_executed": 2,
  "purge_used_mm3": 120,
  "purge_remaining_mm3": 280,
  "total_tardiness_seconds": 15,
  "makespan_seconds": 105,
  "job_tardiness": [
    {"job_id": "J1", "completion_seconds": 105, "deadline_seconds": 90, "tardy_seconds": 15}
  ],
  "replay": [ "..." ],
  "violations": []
}
```

J1 两段已全部执行故计入逾期（完工 105 − 截止 90 = 15 秒）；段前 15 秒空闲
（>10 秒切换耗时）原样保留；J2 未完工，不出现在逾期明细中。

#### 核验请求示例：违规（重叠 / 层序错误 / 超容不能标完成）

请求体见 `examples/request_audit_violation.json`：四段全部提交，但 J2 的第 2
段排在第 1 段之前，且最后一段换料只留了 6 秒、段前 purge 只有 40 mm³
（需求 120）。

```bash
curl -X POST http://localhost:8000/executions/audit \
  -H 'Content-Type: application/json' \
  -d @examples/request_audit_violation.json
```

**实际响应**（`replay` 完整，违规处全部列出）：

```json
{
  "status": "VIOLATED",
  "segments_total": 4,
  "segments_executed": 4,
  "purge_used_mm3": 240,
  "purge_remaining_mm3": 160,
  "total_tardiness_seconds": 0,
  "makespan_seconds": 210,
  "job_tardiness": [
    {"job_id": "J1", "completion_seconds": 100, "deadline_seconds": 120, "tardy_seconds": 0},
    {"job_id": "J2", "completion_seconds": 210, "deadline_seconds": 220, "tardy_seconds": 0}
  ],
  "replay": [
    {"order": 1, "job_id": "J1", "segment_index": 1, "material_id": "PLA",
     "start_seconds": 0, "end_seconds": 60, "print_seconds": 60,
     "idle_seconds_before": 0, "material_switched": false,
     "purge_required_mm3": 0, "purge_before_mm3": 0},
    {"order": 2, "job_id": "J1", "segment_index": 2, "material_id": "ABS",
     "start_seconds": 70, "end_seconds": 100, "print_seconds": 30,
     "idle_seconds_before": 10, "material_switched": true,
     "purge_required_mm3": 120, "purge_before_mm3": 120},
    {"order": 3, "job_id": "J2", "segment_index": 2, "material_id": "PLA",
     "start_seconds": 110, "end_seconds": 130, "print_seconds": 20,
     "idle_seconds_before": 10, "material_switched": true,
     "purge_required_mm3": 80, "purge_before_mm3": 80},
    {"order": 4, "job_id": "J2", "segment_index": 1, "material_id": "ABS",
     "start_seconds": 136, "end_seconds": 210, "print_seconds": 74,
     "idle_seconds_before": 6, "material_switched": true,
     "purge_required_mm3": 120, "purge_before_mm3": 40}
  ],
  "violations": [
    {"order": 3, "job_id": "J2", "segment_index": 2,
     "code": "LAYER_ORDER_INVALID",
     "message": "作业 J2 第 2 段在其第 1 段之前执行，作业内层序非法（跨作业可交错，但各作业层序须合法）",
     "detail": {"missing_predecessor": 1}},
    {"order": 4, "job_id": "J2", "segment_index": 1,
     "code": "SWITCH_TIME_INSUFFICIENT",
     "message": "PLA→ABS 换料前须留足 switch_seconds=10 秒，实际仅有 6 秒空闲",
     "detail": {"from_material": "PLA", "to_material": "ABS", "gap_seconds": 6,
                "required_switch_seconds": 10,
                "previous_end_seconds": 130, "start_seconds": 136}},
    {"order": 4, "job_id": "J2", "segment_index": 1,
     "code": "PURGE_SHORTFALL",
     "message": "PLA→ABS 换料段前实测 purge 40 mm^3 低于矩阵需求 120 mm^3",
     "detail": {"from_material": "PLA", "to_material": "ABS",
                "purge_before_mm3": 40, "purge_required_mm3": 120}}
  ]
}
```

虽然四段全部提交且没有逾期，`status` 仍是 `VIOLATED` 而非 `COMPLETED`；
最后一段实测耗时 74 秒（基准中该段 `print_seconds=80`）与段前 6 秒空闲都
如实保留，不重算。purge 容量始终按实测值扣减（0+120+80+40=240），若
实测 purge 累计超出 `total_purge_capacity_mm3`，越界的那一段会带
`PURGE_CAPACITY_EXCEEDED` 违规，且 `purge_remaining_mm3` 如实给出负值。

#### 核验请求的 422

以下情况在进入回放前即返回 **422**（与 `/plans` 相同的 Pydantic 错误体）：

- `executions[]` 引用了**未知作业**或**未知层段**（层序超出该作业段数）；
- 同一 `(job_id, segment_index)` **重复**出现；
- `end_seconds < start_seconds`（**起止倒置**）、时间或 purge 为负；
- 缺少 `executions` 字段、出现未声明的额外字段；
- `/plans` 参数本身非法（作业数、purge 矩阵完整性等，规则同前）。

注意：**层序错误**（第 k 段早于第 k-1 段执行）与**时间重叠**是执行违规
（200 + `VIOLATED`），因为它们只能在按实际顺序回放时发现；只有“引用了
不存在的层段/作业”“同一段报了两次”这类请求格式问题才是 422。

```bash
curl -i -X POST http://localhost:8000/executions/audit \
  -H 'Content-Type: application/json' \
  -d '{
    "jobs": [
      {"job_id": "J1", "deadline_seconds": 100, "segments": [
        {"material_id": "PLA", "print_seconds": 10, "volume_mm3": 1}]},
      {"job_id": "J2", "deadline_seconds": 100, "segments": [
        {"material_id": "PLA", "print_seconds": 10, "volume_mm3": 1}]}
    ],
    "initial_material": "PLA",
    "purge_matrix": {"PLA": {"PLA": 0}},
    "switch_seconds": 0,
    "total_purge_capacity_mm3": 0,
    "executions": [
      {"job_id": "J1", "segment_index": 9,
       "start_seconds": 0, "end_seconds": 10, "purge_before_mm3": 0}
    ]
  }'
# HTTP/1.1 422 Unprocessable Entity
# ... "msg": "executions 引用了未知层段: 作业 J1 只有 1 段，却给出第 9 段" ...
```

### 非法数据（HTTP 422）

以下情况返回 **422 Unprocessable Entity**（FastAPI/Pydantic 标准错误体，含字段定位）：

- 作业数不在 2～8；`job_id` 重复或为空；
- 层段列表为空；打印秒数 / 体积 / 截止秒数 / 切换耗时 / 容量为负；
- purge 矩阵单元格为负、对角非 0；
- 初始材料或某层段材料在矩阵中缺行或缺列；
- 出现未声明的额外字段、类型错误等。

`POST /executions/audit` 继承上述全部规则，此外 `executions[]` 引用未知
作业 / 未知层段、层段重复、`end_seconds < start_seconds`（起止倒置）、
时间或 purge 为负、缺少 `executions` 字段同样返回 422（详见上文
[核验请求的 422](#核验请求的-422)）。

示例：

```bash
curl -i -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d '{"jobs":[],"initial_material":"PLA",
       "purge_matrix":{"PLA":{"PLA":0}},
       "switch_seconds":10,"total_purge_capacity_mm3":100}'
# HTTP/1.1 422 Unprocessable Entity
```

---

## 四、项目结构

```
.
├── app/
│   ├── __init__.py
│   ├── main.py        # FastAPI 应用：GET /health、POST /plans、POST /executions/audit
│   ├── schemas.py     # 请求/响应模型与 422 输入校验
│   ├── solver.py      # CP-SAT 建模、分阶段求解与字典序决胜
│   └── audit.py       # 执行核验：基准构建后按实测时间线逐段回放
├── examples/
│   ├── request.json                     # 可行示例
│   ├── request_long_job.json            # 长作业示例（单作业 25 段）
│   ├── request_interleave.json          # 按材料交错示例（两个 25 段 A→B→A 作业，容量 2）
│   ├── request_infeasible.json          # 容量超限示例
│   ├── request_audit_completed.json     # 核验示例：合规完成
│   └── request_audit_violation.json     # 核验示例：层序错误 + 换料留时/purge 违规
├── tests/
│   ├── test_plans.py            # 16 个端到端测试（最优性、无解、交错、优先级、长作业、超时兜底、422 等）
│   └── test_executions_audit.py # 19 个端到端测试（完成/进行中、保留空闲、重叠、层序、换料留时、purge 与超容、422 等）
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md
```

## 五、建模与求解说明

- 每个层段为不可拆分节点，另设虚拟起/止节点；节点间有向弧表示“紧随其后执行”，
  弧上携带 purge 体积与切换耗时；`AddCircuit` 强制选出一条覆盖全部层段的
  Hamilton 回路（即合法排列）。
- 作业内层段用先后约束限制；弧被选中时强制层段开始时刻 = 上一层段结束 +
  切换耗时，因此计划天然紧密无空转。
- 求解分三阶段：① CP-SAT 最小化总 purge（容量为硬约束）；② 固定 purge 最优值
  后最小化总逾期；③ **逐位贪心 + 可行性证明**：对执行序列的每个位置，按
  `(作业 ID, 层序)` 升序逐一尝试固定候选层段并用 CP-SAT 验证可行性，第一个可行者
  即为该位字典序最优选择；每一步都经过可行性证明，全部位置完成即字典序最小。
  任何一步在限时内无法判定，则返回当前最优目标值下的可行计划并保守地标为
  `FEASIBLE`，绝不声称未证明的最优性。
- 无解时临时移除容量上限再求最小 purge：仍无解归因为 `PRECEDENCE`，
  最小 purge 超出容量则归因为 `CAPACITY` 并给出最小需求值。
- 层段数没有固定上限，长作业直接建模；大模型在 `time_limit_seconds` 内
  未求得可行解时，用免求解的构造性排列兜底：整作业串行（输入顺序、反向、
  截止时间升序、作业 ID 升序）之外，还把每个作业切成同材料连续段（run）
  并按材料交错——"同材料连续打印优先"与"截止时间优先"两种贪心在少换料与
  少逾期之间取舍。全部候选严格遵守作业内层序、精确核算 purge 后仅取不超
  容者，并按（purge、逾期、字典序）择优，仍返回完整、按序的计划与全部
  明细，状态如实标为 `FEASIBLE`，仅当兜底排列也超容时才返回 `UNKNOWN`。
  三级目标的最优性标记互相独立，只有 CP-SAT 对相应目标（以及字典序决胜
  的每一步可行性）确已证明时才置为 `true`。

## 六、执行核验（/executions/audit）回放规则

核验**不调用求解器、不重新排产**：请求先经与 `/plans` 相同的 `PlanRequest`
校验，再由 `build_instance` 构建基准，随后把 `executions` 按给定顺序逐条
回放，段的开始/结束时刻完全采用实测值，因此空闲与实际耗时（含与基准
`print_seconds` 不一致的实测耗时）都原样保留。每段检查：

1. **串行不重叠**：`start_seconds` 不得小于上一已执行层段的 `end_seconds`
   （首段不得早于 0，由字段约束保证）；零耗时层段首尾相接合法。
2. **作业内层序**：第 k 段执行时第 k-1 段必须已在更早的序列位置出现；
   跨作业交错本身不限制。
3. **换料留时**：材料相对上一已执行层段（首段相对 `initial_material`）
   不同时，段前空闲 `start - 上一段 end` 必须 `≥ switch_seconds`；
   同材料不换料、无留时要求。
4. **段前 purge**：换料时实测 `purge_before_mm3` 不得低于
   `purge_matrix[from][to]`；同材料时必须为 0。容量一律按**实测值**
   逐段累计扣减，累计超过总容量即在越界段记违规（余量如实给负值）。

每条违规都带有序列位置 `order` 与 `(job_id, segment_index)`，同一层段可
同时触发多条（如留时不足与 purge 不足）；重叠时该段不再重复记留时不足。
作业在其全部层段集齐的那一刻按当时最后一段的实测结束时刻记完工，逾期
`max(0, 完工时刻 - deadline)` 只对完工的作业累计——未完工前缀不计。
存在任何违规即 `VIOLATED`（哪怕层段已全部提交）；无违规时按是否全部
提交区分 `COMPLETED` / `IN_PROGRESS`。未知或重复层段、起止倒置等请求
格式问题在 Pydantic 层拦截为 HTTP 422，不进入回放。
