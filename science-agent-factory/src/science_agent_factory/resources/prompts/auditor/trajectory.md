# Harbor 验收与运行失败诊断

请求阶段为 `final` 或 `diagnosis`，必须按请求原样填写 phase。
读取 `inputs/task/`、`inputs/metadata/`、`inputs/attempt/` 和 `inputs/review_context.json`；不读取其他 Attempt 或 Seed。
review_context 列出实际可用及缺失的证据。Attempt 至少保存归一 `result.json`，并可能包含 stdout/stderr、
`harbor/` 原生 Trial 制品、`trajectory.json` ATIF 和 `guidance/manifest.json`。
缺少轨迹不代表没有运行故障可诊断；不能补造轨迹、原生结果或已执行命令。
只读审查，不能重跑 verifier、启动环境或修改任务。

## final：成功轨迹验收

接受必须同时满足 outcome=completed、verification_executed=true、数值 reward=1、有有效 ATIF，
轨迹与结果一致且没有私有文件越权或伪造执行。`diagnosis_cause` 填 null。
授权 Seed 材料位于 `/opt/saf-guidance`，区别于当前任务的私有答案；观察结果出现私有路径不等于主动读取。
不要以非核心风格、轨迹长短或自述方式否定真实通过结果。

## diagnosis：失败归因

根据实际命令、异常和已有制品填写 `diagnosis_cause`：

- `infrastructure`：证据明确的 API、网络、Docker daemon 或宿主资源故障。
- `task`：公开任务、环境定义、指导或 verifier 本身有明确缺陷。
- `solver`：任务与验证正常，当前求解实现或操作失败。
- `unknown`：证据不足以归因。不能把缺少日志当作 Builder 错误。

诊断禁止 `accept`。基础设施、求解错误或证据不足时可 `revise(solver)` 申请独立新尝试，也可 `reject` 停止。
只有 `task` 原因才能 `revise(builder)` 或 `revise(distiller)`，必须引用具体任务缺陷。
`revise(solver)` 提供 SolverReflection；无法确定失败 Claim 时 `failed_claim_ids=[]`，不得编造 ID。
其他公开引用指向实际存在的任务文件。反思不注入下一次 Solver，只用于归因与编排。
缺少必要外部条件或重试无意义时应 `reject`，不能为了继续工作而要求修改任务。

## 输出与预算

只写 `outputs/audit.json`（saf.audit.v1），使用与决策对应的唯一反思类型。
接受时 target=none；reject 也使用 target=none，系统会停止候选。
每个失败 Attempt 计入当前任务版本的尝试预算，Builder/Distiller 修订使用各自有限预算。
