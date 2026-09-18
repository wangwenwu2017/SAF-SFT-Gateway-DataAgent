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
| `docs/` | 根级文档索引与整合说明 | `docs/README.md` |
| `Makefile` | 单一项目入口：安装、测试、启动、检查 | `make help` |

两个子项目**各自维护自己的依赖与虚拟环境**（SAF 用 `uv`，网关用 `venv` + `pip`），互不绑定版本。

## 快速开始

前置：Python 3.11+、Docker（建议独立 rootless daemon）、独立安装的 Harbor、原生 Codex runtime 二进制、
一个 OpenAI **Responses** 兼容的上游模型端点。

```bash
# 1) 安装两个子项目
make install

# 2) 准备配置（模板，不含真实密钥）
cp science-agent-factory/.env.example science-agent-factory/.env
cp sft-gateway/.env.example sft-gateway/.env
cp science-agent-factory/configs/example.yaml science-agent-factory/configs/local.yaml

# 3) 启动网关（计费 key 只留在网关端）
make gateway-serve            # 等价：sft-gateway serve --host 0.0.0.0 --port 8100

# 4) 让 Harbor 的 Codex agent 指向网关（地址必须是容器可达的宿主 IP）
#    { "OPENAI_BASE_URL": "http://<gateway-host>:8100/v1", "OPENAI_API_KEY": "<gateway key>" }

# 5) 运行 SAF（详细配置见 science-agent-factory/README.md）
cd science-agent-factory && uv run saf --config configs/local.yaml run examples/seed_bundle
```

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