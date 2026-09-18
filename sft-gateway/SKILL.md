---
name: sft-gateway
description: 启动 SFT Gateway，为原生 Codex 或 Harbor 配置中转 API，并从捕获中导出可追溯的文本 SFT。适用于接入、采集、导出和常见问题排查。
---

# SFT Gateway 使用说明

把它当成一个**会记录模型交互的中转站**：先启动网关，再把 Codex 使用的 API 地址和 key 换成网关提供的值。任务、工具执行、Harbor 调度和 verifier 仍由原来的系统负责。

```text
Harbor → 原生 Codex → SFT Gateway → 外部中转站 → 模型
                          ↓
                    保存请求与响应
                          ↓ 执行 export
                    可读 JSON / 训练 JSONL
```

**当前行为：自动采集，显式导出。** 普通 `serve` 服务不会在任务结束后自动生成 SFT 文件，也无法仅凭网络请求知道 Harbor 是否验证成功。

本说明对应 0.3.2；已实测 Harbor 0.22.0、Codex 0.153.4、Responses 协议。其他版本接入后应先完成一次短任务检查。

## 一、使用者需要知道什么

如果有人已经为你启动网关，你只需要取得以下信息：

| 信息 | 示例 | 用途 |
|---|---|---|
| 网关 API 地址 | `http://gateway-host:8100/v1` | 配置为 Codex 的 base URL |
| 网关访问 key | 由网关维护者提供 | 配置为 Codex 的 API key |
| 上游支持的模型名 | 本次实测为 `glm-5.3-responses` | 沿用实际可用的模型名，网关不会自动改名 |
| 数据交付方式 | 由维护者导出指定会话 | 取得最终 SFT 和验收记录 |

**不用修改 Codex 源码、不用添加 `run_id`、不用经过网关执行工具。** 工具仍由 harness 执行；工具结果在后续模型请求中出现时，才会被网关观察到。

已有 Harbor 用户可直接跳到第三节。下面第二节由网关维护者执行一次。

## 二、维护者：安装并启动网关

准备 Python 3.11+、项目源码、外部中转站的 base URL 和计费 key。下面的终端命令使用 Bash；先进入项目目录。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .

export SFT_UPSTREAM_URL='https://your-relay.example/v1'
read -r -s -p '外部中转站计费 key: ' SFT_UPSTREAM_API_KEY
echo
export SFT_UPSTREAM_API_KEY

# 生成一次并交给使用者；重启时复用同一个值，避免客户端认证失效。
export SFT_GATEWAY_API_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export SFT_DB="$PWD/data/gateway.sqlite"

sft-gateway serve --host 0.0.0.0 --port 8100
```

命令在前台持续运行，保留这个终端；长期部署可交给你原有的进程管理服务。`0.0.0.0` 是监听配置，**交给使用者的是服务器实际 IP 或域名**，例如 `http://10.0.0.8:8100/v1`。

把生成的 `SFT_GATEWAY_API_KEY` 通过你们现有的凭据管理方式交给使用者。外部计费 key 只留在网关端；这两个 key 用途不同。`.env.example` 是模板，应用不会自动读取 `.env`；重启时需要重新加载保存好的环境配置。

在另一终端检查服务是否启动：

```bash
curl --fail http://127.0.0.1:8100/health
```

返回 `status: ok` 只代表网关存活；上游模型是否可用，需要实际模型请求确认。

按需设置的选项：

| 变量 | 什么时候设置 |
|---|---|
| `SFT_ADMIN_API_KEY` | 需要远程读取捕获或调用管理导出接口时；不设置则管理 API 禁用，离线 CLI 导出仍可用 |
| `SFT_STRIP_CODEX_INTERNAL_METADATA=1` | 中转站拒绝 Codex 内部元数据时启用；本项目的 Harbor 实测启用了它，接收与转发正文分别留存 |
| `SFT_MAX_CONCURRENT_REQUESTS` | 调整每个 worker 的活跃 HTTP 请求上限，默认 32；超额返回 503 |

在受控网络提供服务；需要跨公网访问时使用你们现有的 HTTPS 入口。当前项目使用共享访问 key，不提供按用户区分的数据访问权限。

## 三、Harbor 用户：沿用原来的任务，只替换模型连接

**保留原有 task、agent 参数、并发数、超时、镜像和挂载，只修改 Codex agent 的模型连接。** 已经挂载预热镜像和 Codex runtime 的配置继续保留，不需要为了网关重新构建镜像。

在现有 Harbor 配置中，找到 `name` 为 `codex` 的 agent，合并以下两个环境变量；其他字段不要删除：

```json
{
  "OPENAI_BASE_URL": "http://gateway-host:8100/v1",
  "OPENAI_API_KEY": "网关维护者给你的访问 key"
}
```

这两个字段放在 **`agents` 中对应 Codex 项的 `env` 内**。例如，下面仅展示配置中的 `agents` 部分，不能用它覆盖你完整的任务配置：

```json
{
  "agents": [
    {
      "name": "codex",
      "model_name": "glm-5.3-responses",
      "kwargs": {
        "version": "0.153.4",
        "reasoning_effort": "high"
      },
      "env": {
        "OPENAI_BASE_URL": "http://gateway-host:8100/v1",
        "OPENAI_API_KEY": "网关维护者给你的访问 key"
      }
    }
  ]
}
```

这里的模型和版本是已验证示例；接入已有任务时保留你的模型、版本、`kwargs.config`、其他环境变量以及镜像挂载。模型必须由你的外部中转站支持。配置内包含访问 key，不要提交到源码仓库。

然后执行原来的命令，例如：

```bash
harbor run --config /path/to/your-harbor-config.json --yes
```

需要确认的两件事：

- **地址从 agent 容器内可达。** Docker 内的 `127.0.0.1` 指向容器自己。使用容器可访问的宿主机 IP、服务名或网关域名；`0.0.0.0` 不能作为客户端地址。`host.docker.internal` 是否可用取决于原有 Docker 配置。
- **修改的是实际生效的提供方。** 不要假设宿主机 `export OPENAI_BASE_URL` 一定会传进 Harbor 容器。本机 Harbor 的 Codex agent 已验证支持上述 `agents[].env` 配置；如果你使用自定义 provider 或额外的 `auth.json`，需检查它是否仍覆盖目标地址或认证。

直接运行 Codex、没有 Harbor 时，同样修改其当前生效的 provider base URL 和 API key。对于使用默认 OpenAI 提供方且接受这两个环境变量的配置，可以设置 `OPENAI_BASE_URL`、`OPENAI_API_KEY` 后按原命令启动；自定义 provider 应修改对应的提供方配置。

### 是否必须使用项目里的 Harbor 脚本？

不需要。**共享常驻网关＋原来的 Harbor 命令**是上面的接入方式。

`scripts/run_harbor_via_gateway.sh` 是可选的一次性便捷脚本：它会另起一个网关、挂载 runtime、运行 Harbor，再执行导出，使用 `/runs/<run_id>/v1` 显式分组。它不是连接既有共享网关的客户端，且包含本机路径默认值；不要在已经接好共享网关后再额外套一层。其参数见 [README](README.md#harbor-与预热镜像)。

## 四、维护者：找到会话并导出 SFT

原生 Codex 的 thread/session 元数据会被用于分组。使用者不必添加 `run_id`，但网关也不会自动知道某个会话对应 Harbor 的哪个任务名。

在网关机器上，用与服务相同的数据库路径列出会话。下面的查询只读，不会在路径写错时创建一个空库：

```bash
export SFT_DB='/absolute/path/to/data/gateway.sqlite'
python - <<'PY'
import os, sqlite3
from pathlib import Path

uri = Path(os.environ['SFT_DB']).resolve().as_uri() + '?mode=ro'
db = sqlite3.connect(uri, uri=True)
try:
    print('session_id | requests | in_flight | first_request | last_request')
    for row in db.execute('''
        SELECT run_id, COUNT(*), SUM(status = 'in_flight'),
               MIN(started_at), MAX(started_at)
        FROM requests GROUP BY run_id ORDER BY MAX(started_at) DESC
    '''):
        print(' | '.join(map(str, row)))
finally:
    db.close()
PY
```

结合 Codex 会话 ID、任务运行时间和 Harbor 日志确认归属。`in_flight=0` 只是当前没有进行中的模型请求，**不是任务结束的证明**。确认 Harbor 已返回后，选择该会话导出：

```bash
SESSION_ID='上一步查到的会话ID'
sft-gateway export "$SESSION_ID" \
  --db "$SFT_DB" \
  --output "datasets/$SESSION_ID" \
  --task '你的任务名称'
```

`--task` 只设置训练样本的任务名称，不参与轨迹分组。每个会话使用独立输出目录，避免不同任务覆盖。任务进行中也可导出一致快照，但后续新请求不会自动追加到已导出的文件，需要再次导出。

如果出现 `unassigned`，说明这些请求没有可用会话身份；不要把其中所有请求都当成同一个任务。缺少身份且没有父响应链接时，网关不会根据共享 API key 猜测归属。确实需要外部指定分组时，可选择 `/runs/唯一任务ID/v1`，同一个 ID 不要分给无关任务。

## 五、拿哪些文件交给训练与审查

| 文件 | 用法 |
|---|---|
| `sft_samples.jsonl` | 正常训练集，一行一个样本 |
| `sft_samples.json` / `sft_samples.pretty.json` | 同一数据的缩进版本，适合阅读和汇报 |
| `sample.json` | 恰好一条正常样本时生成的便利文件 |
| `export_report.json` | 看 `sft_trajectories`、`eligible`、`rejected` 与工具完整性统计；`samples` 是请求轮数，不是最终 SFT 条数 |
| `provenance.json` | 每条消息来源、父链与拒绝理由 |
| `manifest.json` | 导出文件的 SHA-256；消费前核对，避免读取到正在替换中的文件集合 |

格式与根目录 [sample.json](sample.json) 一致：`task` 是字符串、`tools` 是 JSON 字符串、`messages` 是消息列表。工具角色为 `tool_call` / `tool_response`；训练侧应按这一格式加载。developer 投影为 system，可读 reasoning 放入 assistant 的 `<think>`；不会凭空生成或解密推理文本。

同一任务的多个请求，只有在上下文连续、生成来源可证明且工具结果闭合时，才可能组成一条完整 SFT。存在分支时可能得到多条，证据不足时可能为零。异步工具结果按观测顺序保留，用 `call_id` 关联，不移动到调用旁边。

若正常集为空，先看 `provenance.json`。需要查看被拒绝轨迹时，重新导出并加 `--include-incomplete`；它们进入单独的 `*.incomplete.*` 文件，**不能混入正常训练集**。

Harbor 的 verifier/reward 仍查看原来的 Harbor 结果。SFT 合格不等于 reward=1，gateway 不替代任务验证。

## 六、常见问题

| 现象 | 先检查 |
|---|---|
| 容器连不上网关 | URL 是否从容器可达、端口是否开放、服务是否仍在运行 |
| 401 | 客户端使用的是 gateway key；网关端配置的是正确计费 key；结合响应与网关日志区分本地认证失败和上游拒绝 |
| 模型不存在、上游 4xx/5xx | 模型名和外部中转站能力；网关不会自动换模型或重试计费请求 |
| 请求仍去了原中转站 | 实际生效的 Codex provider、Harbor agent env 或原有认证配置没有切换 |
| 看不到 SFT 文件 | 普通服务只自动采集；是否已对正确 DB、正确 session 执行 export |
| 有采集但正常 SFT 为零 | 检查未返回工具、断链、上下文变化、压缩/多模态或未观测历史等拒绝理由 |
| 管理接口 403 | 未设置 `SFT_ADMIN_API_KEY`；可由维护者继续使用本地 CLI 导出 |
| 503 或长任务慢 | 活跃请求上限、上游响应速度、磁盘写入；SQLite 支持并发采集，但不是无限吞吐 |

停止网关后，已提交的 SQLite 捕获仍可离线导出；未收到的响应和工具结果无法补回。普通服务和任务没有自动结束握手，不要因为一次模型请求结束就停止网关。

缓存相关字段会继续转发；兼容变换开启时原始与转发正文均留存。0.3.2 实测观察到了上游缓存命中，但命中率取决于中转站、模型与请求内容，不是网关承诺。

## 给执行此说明的代理

先辨别用户是“接入已有网关”还是“部署新网关”，已有服务不要重复启动。保留用户原来的模型、任务、镜像、挂载、并发和超时；仅改实际需要的连接配置。没有任务 ID 证据时不要按时间或共享 key 强行拼接。交付时给出 API 地址、采集会话、SFT 路径及实际验证结果，不输出 key；将 SFT 资格与 Harbor reward 分开报告。

更详细的实现边界见 [ARCHITECTURE.md](ARCHITECTURE.md)，本次实测证据见 [DELIVERY.md](DELIVERY.md)。
