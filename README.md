# SAF-SFT-Gateway

一个单体仓库（monorepo），把两个功能独立、但构成完整链路的组件放在同一个项目下：

- **Science Agent Factory（SAF）**：从科学任务 Seed 合成完整 Harbor Task，用 Builder / Distiller /
  Auditor 三个角色审查，再交给 Harbor 执行 rollout，并归档、审核轨迹。
- **SFT Gateway**：OpenAI 兼容的透明采集代理。Harbor rollout 的 Solver 指向它，它转发请求到上游中转站，
  同时把请求/响应记入 SQLite，重建多轮轨迹，导出可训练的文本 SFT 样本。

两个组件可以各自独立安装、独立运行；组合起来就是一条 **任务合成 → 执行 → 采集 → SFT 导出** 的链路。

## 数据流

```text
Seed  ──►  SAF Builder / Distiller / Auditor  ──►  Harbor Task
                                                      │
                                             Harbor rollout（原生 Codex Solver）
                                                      │  OPENAI_BASE_URL 指向网关
                                                      ▼
                                             SFT Gateway（转发 + SQLite 捕获）
                                                      │
                                                      ▼
                                     轨迹重建 / 资格检查 / 文本 SFT 导出
```

## 目录结构

| 路径 | 说明 | 文档入口 |
| --- | --- | --- |
| `science-agent-factory/` | SAF 包，hatchling 构建，入口命令 `saf` | `science-agent-factory/README.md`、`docs/` |
| `sft-gateway/` | 网关包，setuptools 构建，入口命令 `sft-gateway` | `sft-gateway/README.md`、`ARCHITECTURE.md`、`SKILL.md` |
| `docs/` | 根级文档索引与交付说明 | `docs/README.md`、`docs/handoff.md` |
| `Makefile` | 单一项目入口：安装、测试、启动、检查 | `make help` |

两个子项目**各自维护自己的依赖与虚拟环境**（SAF 用 `uv`，网关用 `venv` + `pip`），互不绑定版本。

## 使用教程

### 0. 前置条件

| 需求 | 说明 |
| --- | --- |
| Python 3.11+、`uv` | SAF 的依赖管理（`uv.lock` 全部来自公共 PyPI） |
| Docker daemon | SAF 预热环境与 Harbor 容器执行；建议独立 rootless daemon |
| Harbor CLI（0.22） | 独立安装，不在本仓库内 |
| 原生 Codex runtime 目录 | `codex-cli 0.153.4`，运行时 bind-mount 进容器，不在仓库内 |
| Responses 兼容中转站 | 一个 BASE_URL + API key，SAF 三角色与网关上游共用 |

### 1. 克隆并安装

```bash
git clone git@github.com:wangwenwu2017/SAF-SFT-Gateway.git
cd SAF-SFT-Gateway
make install        # = install-saf（uv sync）+ install-gateway（venv + pip）
```

### 2. 准备凭据

在仓库**外**写一个 env 文件（例如 `~/saf.env`），不要提交：

```bash
# SAF 内部三角色（Builder/Distiller/Auditor）直连中转站
SAF_CODEX_BASE_URL='https://your-relay.example/v1'
SAF_CODEX_API_KEY='sk-your-key'

# 可选：SAF 专用 rootless Docker daemon
SAF_DOCKER_HOST='unix:///run/user/1000/saf-docker.sock'
```

### 3. 单独启动网关（手工采集时用）

```bash
source ~/saf.env
export SFT_UPSTREAM_URL="$SAF_CODEX_BASE_URL"      # 网关的上游 = 同一中转站
export SFT_UPSTREAM_API_KEY="$SAF_CODEX_API_KEY"   # 计费 key 只留在网关侧
export SFT_GATEWAY_API_KEY='an-independent-gateway-key'
export SFT_DB="$PWD/sft-gateway/data/gateway.sqlite"

make gateway-serve        # 前台 0.0.0.0:8100；另开终端验证 ↓
make gateway-health       # {"status":"ok",...}
```

### 4. 配置并运行 SAF

```bash
cd science-agent-factory
cp configs/example.yaml configs/local.yaml   # 按机器修改：storage_root、harbor.executable、
                                             # harbor.config_file、docker host、seed cache root
uv run saf --config configs/local.yaml run examples/seed_bundle
uv run saf --config configs/local.yaml status <job-id>
uv run saf --config configs/local.yaml export <job-id> --format tool-sft
```

需要 Harbor 的 Solver 经网关采集时，把网关地址放进该 agent 的 JobConfig `env`
（地址必须是容器可达的宿主 IP）：

```json
{
  "OPENAI_BASE_URL": "http://<gateway-host>:8100/v1",
  "OPENAI_API_KEY": "<gateway key>"
}
```

### 5. 一键“经网关跑 Harbor 并导出”

```bash
source ~/saf.env
export SFT_ENV_FILE="$HOME/saf.env"                        # 必填：SAF_CODEX_* 所在 env 文件
export SFT_CODEX_RUNTIME=/path/to/codex-runtime-0.1534     # 必填：本机 Codex runtime 目录
export SFT_DOCKER_HOST="$SAF_DOCKER_HOST"                  # 必填：含预建镜像的 Docker daemon

sft-gateway/scripts/run_harbor_via_gateway.sh /path/to/prepared-harbor-task
```

要求任务 `task.toml` 的 `environment.docker_image` 已指向该 daemon 中存在的预建镜像。
脚本会自动起网关、校验 runtime 版本、调 `harbor run`，结束时自动导出：

- 捕获库：`sft-gateway/data/harbor/<RUN_ID>.sqlite`
- SFT 导出：`sft-gateway/artifacts/harbor-<RUN_ID>/`（含 manifest 与拒绝集）

### 环境变量速查

| 变量 | 谁在用 | 说明 |
| --- | --- | --- |
| `SAF_CODEX_BASE_URL` / `SAF_CODEX_API_KEY` | SAF 三角色、网关上游 | 中转站地址与计费 key |
| `SFT_GATEWAY_API_KEY` / `SFT_ADMIN_API_KEY` | 网关 | 发给容器/管理端的独立 key，与上游 key 分离 |
| `SFT_DB` | 网关 | 捕获 SQLite 路径 |
| `SFT_ENV_FILE` | `run_harbor_via_gateway.sh` | **必填**，SAF_CODEX_* 所在 env 文件 |
| `SFT_CODEX_RUNTIME` | `run_harbor_via_gateway.sh` | **必填**，codex-cli 0.153.4 目录 |
| `SFT_DOCKER_HOST` 或 `SAF_DOCKER_HOST` | `run_harbor_via_gateway.sh` | **必填**，预建镜像所在 daemon |
| `SFT_MODEL` / `SFT_CODEX_CONFIG` / `SFT_GATEWAY_PORT` / `SFT_RUN_ID` | `run_harbor_via_gateway.sh` | 可选覆盖 |

### 常见报错

| 报错 | 处理 |
| --- | --- |
| `SFT_ENV_FILE is required` | 按第 5 步 export，文件里要有 SAF_CODEX_BASE_URL/API_KEY |
| `expected codex-cli 0.153.4` | `SFT_CODEX_RUNTIME` 指向的 runtime 版本不符 |
| `SFT_PREWARMED_IMAGE must match ...` | 任务副本的 docker_image 与所选镜像不一致 |
| `gateway host must be reachable from Docker` | 设 `SFT_GATEWAY_HOST` 为宿主真实 IP |

## 认证分工

- SAF 内部三角色（Builder / Distiller / Auditor）通过 Codex SDK 直连外部 Responses API。
- Harbor rollout 的 Solver（容器内原生 Codex）只拿 gateway key。
- 计费上游 key（`SFT_UPSTREAM_API_KEY`）只留在网关端，永不进容器、不进仓库。

## 文档

- 整合架构、模块 IO、Docker 挂载、模型指定：`science-agent-factory/docs/saf-and-sft-gateway-guide.md`
- 网关算法契约与边界：`sft-gateway/ARCHITECTURE.md`、`sft-gateway/REVIEW_AND_PLAN.md`
- 交付/移植说明：`docs/handoff.md`
- 根级索引：`docs/README.md`

## 安全

真实密钥、运行产物、虚拟环境、Docker 镜像与捕获数据库均不进入本仓库（见 `.gitignore`）。
`*.env.example` 仅为模板；应用不会自动读取 `.env`。

## 开发

```bash
make test          # 运行两个子项目的测试
make lint          # 两个子项目的静态检查
make check         # lint + test
```

约定见根 `AGENTS.md`；SAF 子树另有 `science-agent-factory/AGENTS.md`。