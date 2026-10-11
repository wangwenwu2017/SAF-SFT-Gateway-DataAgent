# SFT Gateway

面向黑盒 harness 的 OpenAI 兼容采集网关。Codex 使用网关 URL 和 key 发起原生请求，网关转发至计费中转站、保存请求与响应，并按可证明的前缀/父响应关系重建多轮轨迹，导出 `sample.json` 所示的文本 SFT 格式。无需 token ID、logprob 或 RL 数据。

当前版本：**0.3.2**。适用于受控部署和文本轨迹采集；仅在观测证据足够、上下文稳定且工具闭合时导出正常训练样本。任务完成、Harbor reward 与 SFT 资格分别判断。

- [开箱使用说明：部署、原生 Harbor 接入与 SFT 导出](SKILL.md)
- [从源码安装](INSTALL.md)
- [架构、模块输入输出和算法](ARCHITECTURE.md)
- [修复及验收记录](REVIEW_AND_PLAN.md)
- [提交说明与案例索引](DELIVERY.md)

## 安装与启动

Python 3.11+：

```bash
python -m pip install -e '.[test]'
export SFT_UPSTREAM_URL='https://your-relay.example/v1'
export SFT_UPSTREAM_API_KEY='your-billing-key'
export SFT_GATEWAY_API_KEY='your-independent-gateway-key'
export SFT_ADMIN_API_KEY='your-independent-admin-key'
export SFT_DB='data/gateway.sqlite'
sft-gateway serve --host 127.0.0.1 --port 8100
```

`.env.example` 是配置模板；应用本身不自动加载 `.env`。不要将实际 key 写入项目或交付包。

计费 key 只用于请求上游；gateway key 用于模型入口认证；admin key 用于读取捕获/导出。固定计费 key 模式缺少 gateway key 会拒绝启动。未设置 admin key 时管理 HTTP API 返回 403，离线 CLI 导出仍可使用。没有固定上游 key 时为透传认证模式，要求客户端提供上游认证；建议使用固定 key 与独立 gateway key 模式。

配置原生 Codex 的模型提供方 URL 为 `http://127.0.0.1:8100/v1`，认证为 gateway key，协议为 Responses。若该 Codex provider 支持环境变量，也可使用：

```bash
export OPENAI_BASE_URL='http://127.0.0.1:8100/v1'
export OPENAI_API_KEY='your-independent-gateway-key'
codex
```

网关不要求 Codex 添加 `run_id`。优先使用原生 thread/session 请求头隔离；没有身份线索也没有 `previous_response_id` 时，不按共享计费 key 强行合并。需要显式外部任务分组时，可以只改变 URL 为 `/runs/<run_id>/v1`，不改变请求正文。

## 导出

```bash
sft-gateway export <run-or-session-id> \
  --db data/gateway.sqlite --output dataset --include-incomplete
```

| 文件 | 用途 |
|---|---|
| `sft_samples.jsonl` | 正常 SFT，一行一个样本 |
| `sft_samples.json` / `sft_samples.pretty.json` | 同一正常集的缩进 JSON，便于查看 |
| `sample.json` / `samples.json` | 正常样本为一个/多个时的便利文件，零条时不存在 |
| `sft_samples.incomplete.*` | 开启调试参数后，仅包含被拒绝的投影，不能作为正常训练集 |
| `sample.incomplete.json` | 恰好一个被拒绝样本时的便利文件 |
| `trajectories.native.jsonl` | 父链对应的原始采集记录 |
| `trajectories.messages.jsonl` / `samples.messages.jsonl` | 轨迹/逐轮审计投影，不作为严格 SFT 入口 |
| `raw_records.jsonl` | 全部捕获，bytes 字段使用 base64，可逆恢复 |
| `provenance.json` | 拒绝理由、请求边界、每条消息的来源、有效上下文和任务状态 |
| `export_report.json` | 当前快照统计；轨迹汇总可能重复计算分支共享历史 |
| `manifest.json` | 最后发布的文件清单和 SHA-256；消费前校验，失败应重新读取 |

正常样本固定为：

```json
{
  "task": "example-task",
  "tools": "[]",
  "messages": [
    {"role": "user", "content": "问题"},
    {"role": "assistant", "content": "回答"}
  ]
}
```

工具消息使用 `tool_call` / `tool_response`。异步或并行调用时，JSON content 中保留 `call_id`，结果按实际观测顺序出现。可读 reasoning 放在 assistant 的 `<think>` 中；优先正文，其次可读摘要，摘要不能冒充完整推理。密文不解密；developer 按示例投影为 system。原始角色、字段和密文仍可在审计记录中查看。

`provenance.json` 中 `eligible=true` 仅表示符合当前文本投影契约，不表示任务成功。工具未返回、无法证明父链、配置变化、上下文压缩、未知关键 item、多模态内容或缺少监督来源都会排除正常 SFT。

Responses 流的原始终态快照与 SSE 事件均保留。导出时重放 SSE：完整的
`response.output_item.done` 序列作为客户端回放视图，`response.completed`
提供完成状态。两者的 item ID、顺序、正文和工具参数必须一致；同一 reasoning
item 的非空密文允许不同，选取实际 done 事件中的密文，并在跨请求关联时严格核对。
历史比较只规范化助手协议项的 `metadata.turn_id`、空 reasoning `content`、
空文本 `logprobs` 等传输差异，保留业务 metadata、非空字段、phase 和工具标识。
缺项、重复、终态后追加 item 或可见内容冲突会排除该记录。
`provenance.json` 的 `turns[].response_source` 标注 `responses_sse_replay` 或
`stored_response`。旧数据库可通过原有 `export` 命令离线重导，无需再次调用模型。

## Harbor 与预热镜像

```bash
SFT_ENV_FILE=/path/to/trusted.env \
SFT_PREWARMED_TASK=/path/to/prepared-task \
SFT_CODEX_RUNTIME=/path/to/codex-runtime-0.1534 \
SFT_MODEL=glm-5.3-responses \
bash scripts/run_harbor_via_gateway.sh /path/to/task
```

可信 env 文件需要 `SAF_CODEX_BASE_URL`、`SAF_CODEX_API_KEY` 和 `SFT_DOCKER_HOST`（或 `SAF_DOCKER_HOST`）。脚本会 source 该文件，应使用自己维护的配置。准备好的任务 `task.toml` 必须指定预热 `environment.docker_image`；`SFT_PREWARMED_IMAGE` 如设置，必须与该实际镜像一致，否则直接拒绝启动。脚本不下载或构建预热镜像。

脚本挂载 Codex 0.153.4 runtime 和 launcher，在选定镜像中检查实际版本，再启动 Harbor。仅向容器传 gateway key。Harbor 配置权限受 `umask 077` 限制且已忽略提交；admin key 不传容器。reasoning effort 为 high，模型由 `SFT_MODEL` 选择。

网关不增加任务总时限。Harbor 的 task/命令参数仍可能设置超时；停止后导出已捕获数据，不补造缺失结果。强制杀机不保证 shell 清理步骤执行。

## 资源与缓存

| 环境变量 | 默认值 |
|---|---:|
| `SFT_REQUEST_TIMEOUT` | 900 秒，上游 HTTP 操作超时 |
| `SFT_MAX_REQUEST_BYTES` | 64 MiB |
| `SFT_MAX_RESPONSE_BYTES` | 64 MiB，解压后非流式响应 |
| `SFT_MAX_FRAME_BYTES` | 8 MiB |
| `SFT_MAX_STREAM_BYTES` | 256 MiB |
| `SFT_MAX_CONCURRENT_REQUESTS` | 每 worker 32 个 HTTP 请求，含活跃流 |

超额请求拒绝，不进行自动计费重试。流写入在线程中逐条等待提交，提供背压；SQLite 短事务仍会短暂串行化写操作。并发数与大小限制共同决定内存预算，较大的默认上限不等于实际内存占用承诺。

默认逐字节转发请求正文，保留缓存相关字段与响应诊断头。`SFT_STRIP_CODEX_INTERNAL_METADATA=1` 是兼容部分中转站的显式正文变换，接收及转发 bytes 均保存；Harbor 脚本默认启用它。网关本身不保证中转站缓存命中。

## 测试与边界

```bash
pytest -q
bash -n scripts/run_harbor_via_gateway.sh
```

测试使用模拟上游和临时数据库，不调用真实计费模型。导入 CLI/应用不建库；导出以只读连接读取一致快照。写模式升级旧 schema 前创建 `.pre-v3.bak` 备份，不伪造旧请求的原始 bytes。

这是共享凭据的受控网关，不是多租户授权服务。没有会话身份、发生服务端不可见状态变化，或密文上下文不能比较时，不能保证每个任务都恢复为一条完整样本。保持原始证据和拒绝理由，比强行拼接更重要。

上游 HTTP 客户端由应用生命周期管理，实例内请求复用连接；SSE 在同一接收块内批量提交事件，事务完成后才转发。正常关闭会释放连接池和 SQLite；强制进程退出仍只能保存已经接收并持久化的证据。
