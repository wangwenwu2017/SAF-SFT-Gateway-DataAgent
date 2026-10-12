# Science Agent Factory

从科学任务 Seed 生成完整 Harbor Task，再由 Harbor 选择 harness 并执行 rollout。
Builder、Distiller、Auditor 使用 Codex SDK；系统不再实现 SDK Solver 或 Terminus 专用适配。

## 配置与启动

Python 3.11+；Harbor 独立安装在自己的环境中。当前对接本机 Harbor 0.22 的原生 JobConfig、Task 1.4 和 ATIF。

```bash
uv sync --extra dev
cp configs/example.yaml configs/local.yaml
```

编辑 `configs/local.yaml`：指定存储目录、Codex 配置，以及允许执行的 `allow_host_full_access: true`。
`harbor.config_file` 相对 SAF 配置文件解析，指向原生 Harbor YAML：

```yaml
harbor:
  executable: harbor
  config_file: ./harbor.example.yaml
pipeline:
  workflow_mode: audited
```

原生文件中的 `agents` 必须包含一个 agent，可用 `name` 或 `import_path`；模型、认证、环境变量、
`kwargs`、环境和 verifier 等由 Harbor 解释。`configs/harbor.example.yaml` 默认使用 Codex harness，可替换为其他 harness。
内部 SDK 的 `codex.*` 与外部 Harbor 的认证互相独立；模型 HTTP 交互记录由外部网关承担，SAF 不启动内置代理。
SAF 不改写模型名、不转换 Claude 轨迹、不挂载 Codex 订阅目录、不预装具体 harness。

```bash
saf --config configs/local.yaml run examples/seed_bundle
saf --config configs/local.yaml status <job-id>
saf --config configs/local.yaml resume <job-id>
saf --config configs/local.yaml export <job-id> --format harbor
saf --config configs/local.yaml export <job-id> --format tool-sft
saf --config configs/local.yaml export <job-id> --format tool-sft --selection all
```

示例 Seed 演示输入格式；真实生成会调用模型和环境。服务器实际运行前按 AGENTS.md 执行 Git 一致性检查。

## 两种工作流

- `lightweight`：Builder 设计、构建、自测，直接交给 Harbor。可禁用 verifier；未验证记录明确标记，不能视作审查通过样本。
- `audited`（默认）：增加 Reference 蒸馏、任务预审和轨迹审查。设计卡在 Builder 构建调用内生成，随元数据交付，由任务预审一并检查。Harbor 必须执行 verifier；仅 completed、数值 reward=1 且 Auditor 接受的当前任务版本进入默认训练导出。

两种模式均要求 Builder 交付完整 `instruction.md`、`task.toml`、`environment/`、`solution/solve.sh`、
`tests/test.sh`。旁路 metadata 保存任务指纹、真实 Oracle=1/Nop=0 Trial 结果与探索证据。
Auditor 只读审查证据，不另起一套验证器。

## 批次与缓存

```bash
saf --config configs/local.yaml batch-run <seed-root>
saf --config configs/local.yaml batch-resume-generation <batch-id>
saf --config configs/local.yaml batch-resume <batch-id>
saf --config configs/local.yaml batch-export <batch-id>
```

Seed 根需含 `saf.seed-import.v1` 清单；`scripts/prepare_tb*_seeds.py` 提供导入入口。
每个 included Seed 固定一个 Job 和 `batch.tasks_per_seed` 个候选。生成和采集分别限流，失败保留原槽位，
不替换 Seed 凑成功数。`trajectory_concurrency: 0` 可只生成，随后提高并发并恢复。

可选 `seed_environment_cache` 只预热 Seed 科学环境，使用独立 `v2/` 缓存命名空间。
`environment-prewarm` 预热，`batch-supervise` 将预热与生产衔接，`batch-rollout` 只采集已有任务。
缓存不包含 harness；正式导出的任务始终自包含，临时运行副本才引用预热镜像。

## SDK 认证与外部 API

Builder、Auditor、Distiller 支持 `codex.auth_mode: api_key` 或 `chatgpt_subscription`。
API 模式通过 `codex.base_url` / `SAF_CODEX_BASE_URL` 直连外部 Responses API；密钥由 `api_key_env` 指定。
订阅模式使用 `codex.account_home` 的现有 ChatGPT 登录，忽略自定义 API 地址，不经过外部 API gateway。
因此订阅生成角色不会在该 gateway 留下 HTTP 记录；本地仍保存 SDK 执行事件以便恢复和诊断。
SDK 有限重试配置为 `turn_retries`、`retry_backoff_s`，没有本地请求计数代理。

Solver 独立使用 Harbor 原生配置；示例的 `SAF_HARBOR_BASE_URL`、`SAF_HARBOR_API_KEY` 指向外部 API，
与内部三角色的订阅或 API 认证互不绑定。

## 制品与恢复

Job 冻结原生配置与任务指纹；Harbor 保留配置源目录作为工作目录，原生相对资源按该目录解释。
SAF 只覆盖单次调度字段（任务、输出目录、一次 trial、无 Harbor 内部重试），并添加授权指导。
原生文件中其他字段透传；外部引用文件和环境变量仍由部署方维护。

每次 Attempt 保存配置、日志、原始 Harbor 制品、归一结果、ATIF、指导清单和恢复 checkpoint。
超时、错误、取消与验证失败明确区分，有效失败 ATIF 全部归档。`--selection all` 导出诊断记录；
默认 audited 导出过滤未接受记录。仅导出 ATIF 实际提供的可见推理，不从网关补造推理。

这是不兼容清理：旧配置、旧 Job/Batch 状态、SAF Bundle 和旧导出格式不再读取；历史运行目录不会自动删除。
请使用新的存储目录。详见 [架构](docs/architecture.md) 和 [构建流程](docs/task-building-workflows.md)。

## 开发检查

```bash
uv run ruff check src tests
uv run pytest -q tests/test_module_documentation.py
uv run pytest -q
```

Harbor 验证失败、超时、启动错误或没有有效 ATIF 时，audited 流程先让 Auditor 读取日志和已有制品诊断。
诊断不能接受训练样本；只能请求有预算的新尝试、依据具体证据修订任务，或拒绝候选。
Auditor 自身失败会保持待审状态，恢复时只重审，不重跑已有 Solver Attempt。取消后需显式恢复才继续。
本轮移除了 `codex.gateway_capture`；旧 `gateway_retries` / `gateway_backoff_s` 也不再接受。

## Data Designer 并行任务合成（可选）

安装 `uv sync --extra designer --extra dev`，然后运行：

```bash
saf-designer --requests requests.jsonl --out var/designer-batch --concurrency 2 --env-file .env
```

`requests.jsonl` 每行一个独立输入；路径相对 JSONL 所在目录解析：

```json
{"request_id":"seed-a","seed":"seeds/a","config":"configs/local.yaml"}
{"request_id":"seed-b","seed":"seeds/b","config":"configs/local.yaml"}
```

每行调用原生 `generate_tasks()`，执行 Builder、Distiller 和 preflight Auditor，停止在独立 Solver 前；配置必须为 `audited` 且允许原生执行。`--concurrency` 控制并行输入数，行内候选并发仍由 SAF 配置决定，两者相乘影响资源使用。

结果保存到 `OUT/results.json`；每项原生状态和任务位于 `OUT/items/<request_id>/jobs/generation/`，单项结果为 `OUT/items/<request_id>/result.json`；Data Designer 数据集位于 `OUT/designer/`。结果中的 `task_paths` 只包含预审接受的候选。所有候选接受才记为 `completed`，部分接受记为 `partial`；单项异常会记为 `failed`，不阻止其他输入执行。存在未完成项时命令返回非零退出码。

输出目录必须是新目录，不自动重跑或覆盖旧批次。已有 job 的恢复沿用原生命令，配置中的 `storage_root` 需指向 `OUT/items/<request_id>`；批量调度入口不自动恢复。它不会自动提交 DataAgent 归档或启动 Harbor rollout。
