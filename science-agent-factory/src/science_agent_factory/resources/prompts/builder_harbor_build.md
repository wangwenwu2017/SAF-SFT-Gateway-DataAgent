# Builder Harbor 交付阶段

当前阶段是 `phase=build`。轻量和审查工作流都交付完整 Harbor Task；具体 rollout harness 由 Harbor 配置选择。

## 构建内设计

这是任务工程与交付工作，不是开放式研究。先阅读并尽早运行 Seed 已有参考解验证基线；
已有材料能支持方案时直接使用，不要把完整反推隐藏公式、噪声机制或原始生成器作为开工前提。
只探索影响候选方案或正确性的关键不确定性；基线与验收路径明确后立即落盘设计卡、实现任务。
运行时会在探索软预算到期时提醒收敛；提醒不要求重启设计，也不能替代真实自测。
优先完成一个科学能力和难度保留的可验证方案，给实现、Oracle/Nop 自测及元数据校验保留时间。
若证据不足或基线失败，记录明确阻塞和真实结果，不反复运行没有新决策价值的实验。

科学计算可以耗时很长，不能仅凭单次计算时长、轮询次数或软预算到期判断探索无效。
每次昂贵实验应明确待验证的假设、与上次的实质差异、预期观测和据此采取的下一步，
保存中间结果、最终候选输出与日志；已有计算仍在正常推进时等待其结果，不重复启动同一实验。
自测失败先区分环境/验证器执行错误、输出契约错误与数值质量不达标。
若已有候选制品，先用它低成本复现和诊断失败；仅修改验证器时，先重放已保存制品，
确认修复有效且查看全部相关指标，再安排最终 Harbor 自测。诊断重放不能替代最终任务绑定的原生自测证据。
数值质量不足时，根据残差、收敛日志或对照实验调整求解方案，不因时间压力任意缩减搜索或放宽验收阈值。
减少重复计算，不限制有明确假设、可观察进展和决策价值的探索。

在本次调用中先静态阅读 Seed 的说明、解法、轨迹和环境，分析新旧任务的最短解法、核心能力、
五个难度维度、公开输入输出及实质变化。明确验收声明与计划验证证据、至少一种错误解法及其拒绝方式、
低成本捷径、被否决的表面改名方案和实现风险。不要编造实验结果；不确定之处记录为风险。

依据 `.saf/schemas/task-design-card.schema.json` 写出 `outputs/task-metadata/design_card.json`，
符合 `TaskDesignCard`（`saf.task-design-card.v1`）；`task_id`、`seed_id` 原样使用请求中的
`candidate_id`、`seed_id`。随后继续完成环境探索、任务实现和真实自测，不要在设计后结束或等待审批。
实际探索使方案改变时更新设计卡，确保最终卡片与交付任务一致；每次修订也必须交付当前版本的设计卡。
设计卡会随构建元数据归档，在 audited 模式由后续 preflight 一并审查。

## 输入与输出

读取 `inputs/seed/`、可选的 `inputs/previous_task/` 和 `inputs/review.json`，以及请求 JSON
中的 `candidate_id`、`environment_strategy`、`harbor_executable`、可选预热镜像和 Docker daemon。
只在当前 workspace 工作。

必须写出且只写出以下两个目录和交付结果：

- `outputs/harbor-task/`：正式、可移植的 Harbor 任务根，包含 `instruction.md`、
  `task.toml`、`environment/`、`solution/solve.sh` 和 `tests/test.sh`。
  `reuse_seed` 下 `environment/` 已由工厂预置，你只负责其余文件；`synthesize` 下才由你生成 `environment/`。
- `outputs/task-metadata/`：旁路审计证据，包含 `design_card.json`、`metadata.json`、`exploration_report.json`、
  `environment_report.json`、`self_test.json` 及这些报告引用的证据文件。
- `outputs/build_result.json`：符合 `saf.harbor-builder-delivery.v1`，`task_id` 必须等于
  请求中的 `candidate_id`，目录字段使用上述相对路径，`summary` 非空。

## Harbor task.toml 固定契约

使用 Harbor 0.22 的 TaskConfig 和 `schema_version = "1.4"`。不要把 `task_id` 写成顶层
字段；任务身份必须写成：

```toml
schema_version = "1.4"

[task]
name = "science-agent-factory/<candidate_id>"
description = "<公开任务的一句话说明>"

[agent]
timeout_sec = 28800

[verifier]
timeout_sec = 1800

[environment]
cpus = 2
memory_mb = 4096
build_timeout_sec = 3600
network_mode = "public"
```

将尖括号替换为真实值；`<candidate_id>` 必须是请求中的完整 ID，不能使用 Seed 名称、
自定义标题或只含任务描述的名称。package name 必须满足安全的 `org/name` 形式：
字母或数字开头，只能含字母、数字、点、下划线、短横线，且不能含 `..`。

正式任务中不要写 `environment.docker_image`、宿主绝对路径、`~`、凭据、`inputs/seed`、
`outputs/bundle` 或其他机器专属 Bind Mount。Harbor 会自动使用任务根下的
`environment/Dockerfile` 或 Compose；不要臆造 `environment.dockerfile` 等不受支持的字段。
若 Verifier 需要从独立容器取得候选制品，按 Harbor 支持的 `artifacts` 声明公开输出路径，
并在 Instruction 中逐字说明路径、文件名、列名和格式。

## 任务语义和可移植环境

Instruction、`solution/solve.sh` 和 `tests/test.sh` 必须描述并执行同一个公开问题。
Solution 只能使用 Instruction 与公开环境能发现的输入；不得读取 Builder workspace、
`inputs/seed`、`solution/`、`tests/`、答案标签或任何隐藏参考文件。测试必须真正检查核心
输出和边界条件，不能只检查退出码、文件存在、import 或 CSV 语法。


Harbor 的执行边界必须明确：`solution/` 是仅供 Oracle agent 上传参考解的目录；普通求解
agent 的运行环境不能假定存在 `/solution` 或 `/app/solution`。`tests/test.sh` 在 agent
完成后直接验证公开输出（通常位于 `/app`），不得调用或复制 `solution/solve.sh`，也不得依赖
任何隐藏参考文件；需要的初始化必须通过 Instruction 规定的公开命令完成。这样 Oracle
与正常 agent 使用同一套 verifier 语义，Nop/空候选才会可靠地产生 0 reward。

当 `environment_strategy=reuse_seed` 时，工厂已在构建开始前把 `inputs/seed/environment/` 逐文件、逐字节
完整复制到 `outputs/harbor-task/environment/`，保留所有数据、源码、软件树、二进制和锁文件。你不得修改、
删除或重新生成该目录，也不要再复制一份。metadata 的 `environment_source` 必须为 `seed_copy`，并记录
Seed environment 的 tree digest（工厂会校验实际副本与 Seed 摘要一致）。
digest 算法是：按相对 POSIX 路径排序，对每个文件依次 hash(路径 UTF-8、NUL、文件字节、NUL)；
不计算宿主绝对路径，不使用压缩包摘要。若 `environment_strategy=synthesize`，则使用自包含环境，
metadata 的 source 为 `synthesized` 且不填写 Seed digest。

字段名必须与对应 Schema 逐字一致，不能使用同义词；写完每个 JSON 后用 `python`/Pydantic 或 JSON Schema 实际校验。

`exploration_report.json` 的顶层键必须是：`schema_version`、`seed_id`、`seed_reproduction`、
`experiments`、`selected_insight`、`preserved_capabilities`、`substantive_changes`、
`rejected_superficial_ideas`、`difficulty_assessment`、`capability_mapping`。
`seed_reproduction` 必须是含 `hypothesis`、`commands`、`outcome`、`observation`、
`evidence_paths`、`design_implication` 的对象，`outcome` 只能是 `confirmed`、`refuted` 或
`inconclusive`。`difficulty_assessment` 必须包含五个完整维度对象（generality、
algorithmic_depth、implementation_scope、edge_case_coverage、environment_tooling），而
`capability_mapping` 必须是含 `schema_version`、`entries`、`summary` 的对象；不能将
这些对象简写为字符串，也不能把 `selected_insight` 改成 `selected_insights`。

`environment_report.json` 的键必须是 `backend`、`detected_inputs`、`start_commands`、
`experiment_commands`、`resources`、`evidence_paths`、`cleaned`、`cleanup_summary`；
`self_test.json` 必须遵守 v2 Schema，包含任务摘要、两个原生 Trial 结果路径、自测命令和证据。`metadata.json` 必须使用 `exploration_report_path`、
`environment_report_path`、`self_test_report_path`，不能写成 `exploration_report`、
`environment_report` 或 `self_test`。
## metadata 报告契约

先阅读 workspace 中 `.saf/schemas/` 下与以下文件对应的 JSON Schema，再写报告；不能省略报告或
用只有一两个字段的占位 JSON：

- `metadata.json`：`saf.harbor-task-metadata.v1`，引用三个报告的相对路径；身份为
  `candidate_id` 和请求中的 Seed ID；reuse_seed 时包含 64 位小写环境 digest。
- `design_card.json`：`saf.task-design-card.v1`，记录本次构建的最终设计；固定放在 metadata 根目录，
  不向 `metadata.json` 添加额外字段，不放入公开 Harbor Task。
- `exploration_report.json`：`saf.exploration.v1`，包含 Seed reproduction、实际实验、
  选定洞察、保留能力、实质变化、被拒绝的表面改名方案、难度评估和 capability mapping。
- `environment_report.json`：`saf.environment-run.v1`，记录真实执行的命令、检测到的输入、
  后端、资源、证据路径和清理结果；没有执行的命令不得写成成功。
- `self_test.json`：`saf.harbor-self-test.v2`，必须记录至少两条真实命令、证据路径、
  `task_loadable=true`、`oracle_reward=1.0`、`negative_reward=0.0` 和 `cleaned=true`。

每个报告引用的 evidence path 必须是 task-metadata 内存在的相对文件。所有 JSON 必须先按对应
Schema 检查，再写出 build_result。不要把 metadata 或 evidence 放入正式 Harbor task 根。

## 最低真实自测

使用请求中的 `harbor_executable`。临时副本应整体复制 `outputs/harbor-task`（`reuse_seed` 下环境已在其中，
不要另行从 `inputs/seed` 拼接环境）。如果提供了预热镜像和 daemon，只把它们用于该临时副本：
在副本的 task.toml 中临时注入对应 `docker_image` 和永不拉取的策略，使用正确 daemon 运行
Harbor Oracle 和 Nop/空候选，保存 stdout、stderr、退出码和 reward 证据；完成后删除临时资源，
恢复正式可移植 task.toml，重新确认最终目录没有 docker_image。若没有预热镜像，使用正常构建方式。
不能声称未执行的 Oracle/Nop 成功。自测命令必须实际调用请求中的 Harbor CLI（例如
`harbor run --path <temporary-task> --agent oracle` 与 `--agent nop`），并在临时副本中注入
预热镜像；不能用 `docker run -v <task-root>:/app ... tests/test.sh` 代替 Harbor，因为这种
挂载会把 `solution/` 伪装成 `/app/solution`，掩盖真实 verifier 路径错误。

自测前后都检查：Harbor 可以加载正式结构；Oracle 的 reward 必须为 1，Nop 或空候选的 reward
必须为 0；`tests/test.sh` 在成功、失败和空候选时都创建非空的
`/logs/verifier/reward.txt` 或 `/logs/verifier/rewards.json`，只有完整满足公开要求时写 1，
其他情况写 0。遇到真实失败时修复对应文件并重跑受影响检查，不能放宽测试、删除场景或伪造证据。

完成条件是所有必需文件、Schema、环境来源、真实自测和 build_result 校验均通过；最终回复只概述
已完成内容，详细事实以 workspace 文件和日志为准。

## 自测证据与最终任务绑定

`self_test.json` 还必须包含：

- `task_sha256`：最终正式可移植任务的指纹。可直接调用
  `science_agent_factory.verification.harbor_task.harbor_task_fingerprint(Path("outputs/harbor-task"))`。
- `oracle_result_path`、`negative_result_path`：相对 metadata 目录的原生 Harbor Trial `result.json`，
  不是 Job 汇总。文件必须包含 `trial_name`、`finished_at`，无 `exception_info`；
  `agent_info.name` 分别为 `oracle`、`nop`，`verifier_result.rewards.reward` 分别为数值 1、0。

复制真实结果文件到 metadata。任何任务语义文件修改后必须重跑自测并重算指纹。
临时副本只允许改变预热镜像引用；正式目录始终保留完整环境、Solution 和 Tests。
