# 交付说明：SAF + SFT Gateway 源码包

生成日期：2026-09-18
包名：`saf-sft-gateway-20260918`

## 一、包含内容

| 目录 | 说明 |
| --- | --- |
| `science-agent-factory/` | SAF（Science Agent Factory）v0.6.0 当前工作树，含 state v4、reuse_seed 环境预置、prompt、模型环境变量的改动 |
| `sft-gateway/` | SFT Gateway 0.3.2 源码 |

## 二、已排除（请勿从别处补回）

- 真实密钥：`.env`、`configs/server.yaml`、`configs/local.yaml`
- 运行产物：`var/`、`dist/`、`release-manifest.json`
- 虚拟环境与缓存：`.venv/`、`.harbor-venv/`、`__pycache__/`、`.pytest_cache/`、`.ruff_cache/`
- Git 历史：`.git/`
- SFT Gateway 案例数据与数据库：`artifacts/`、`*.sqlite`

## 三、前置条件

- Python **3.11+**
- Docker（建议独立 rootless daemon）
- Harbor（独立安装；当前对接 Harbor 0.22 / Task 1.4）
- 原生 Codex runtime 二进制（版本需与 Harbor 配置一致，本仓库实测 0.147.0 / 0.153.4）
- 一个 OpenAI **Responses** 兼容的外部中转站 / 模型端点

## 四、SAF 快速开始

```bash
cd science-agent-factory
uv sync --extra dev
cp configs/example.yaml configs/local.yaml
# 编辑 configs/local.yaml：storage_root、harbor.executable、harbor.config_file、codex.*
export SAF_CODEX_BASE_URL='https://your-relay.example/v1'
export SAF_CODEX_API_KEY='your-builder-key'
saf --config configs/local.yaml run examples/seed_bundle
saf --config configs/local.yaml status <job-id>
saf --config configs/local.yaml export <job-id> --format harbor
```

注意：`allow_host_full_access: true` 只应在专用 / 一次性 worker 上开启；这是受信任 Worker 边界，不是 OS 沙箱。

## 五、SFT Gateway 快速开始

```bash
cd sft-gateway
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -e '.[test]'
export SFT_UPSTREAM_URL='https://your-relay.example/v1'
export SFT_UPSTREAM_API_KEY='your-billing-key'      # 只留在网关端
export SFT_GATEWAY_API_KEY='an-independent-key'     # 给 harness/容器
export SFT_DB="$PWD/data/gateway.sqlite"
sft-gateway serve --host 0.0.0.0 --port 8100
curl --fail http://127.0.0.1:8100/health
```

让 Harbor 的 Codex agent 指向网关（放入该 agent 的 `env`，地址必须是容器可达的宿主 IP）：

```json
{
  "OPENAI_BASE_URL": "http://<gateway-host>:8100/v1",
  "OPENAI_API_KEY": "<gateway key>"
}
```

导出：

```bash
sft-gateway export <run-or-session-id> --db "$SFT_DB" --output dataset --include-incomplete
```

## 六、接入时必须按你的环境修改的项

本包是**源码**，不含本机路径与镜像：

1. `harbor.executable` / `harbor.config_file` → 你的 Harbor 与原生 JobConfig。
2. `codex.base_url` / `codex.api_key_env` → 你的外部 API。
3. `seed_environment_cache.build_docker_host` / `rollout_docker_host` → 你的 Docker daemon。
4. 若使用预建 Seed 环境镜像，需要自备对应镜像与登记表；本包不含任何移植镜像。
5. Harbor agent 的 `model_name` 必须是你的中转站支持的模型名；**网关不改写模型名**，只透传。
6. 挂载 Codex runtime 的 `environment.mounts[].source` 必须是收件方的真实路径。

## 七、文档索引

- 根级：`README.md`、`docs/README.md`
- SAF：`science-agent-factory/README.md`、`docs/architecture.md`、`docs/task-building-workflows.md`、
  `docs/saf-and-sft-gateway-guide.md`（整合指南：架构、IO、Docker 挂载、模型指定）
- Gateway：`sft-gateway/README.md`、`ARCHITECTURE.md`、`SKILL.md`、`INSTALL.md`、`DELIVERY.md`

## 八、安全提示

- 本包**不含任何真实密钥**，请在收件方通过独立安全渠道提供。
- `.env.example` 仅是模板；应用不会自动读取 `.env`。
- `SFT_UPSTREAM_API_KEY`（计费 key）只应留在网关端；容器只拿 gateway key。
- 捕获数据可能含任务提示、模型响应和工具输出，按授权范围处理。