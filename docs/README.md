# 文档索引

本仓库是 `science-agent-factory/`（SAF）与 `sft-gateway/`（SFT Gateway）的单体项目，
两者功能独立，组合成“任务合成 → 执行 → 采集 → SFT 导出”的完整链路。

## 根级

- `README.md`：项目定位、数据流、目录结构、快速开始。
- `AGENTS.md`：仓库约定与 GitHub 工作流。
- `docs/handoff.md`：交付/移植说明（含前置条件、配置项清单、安全提示）。

## Science Agent Factory

- `science-agent-factory/README.md`：配置、启动、两种工作流、批次与缓存、认证与制品。
- `science-agent-factory/docs/architecture.md`：模块与架构。
- `science-agent-factory/docs/task-building-workflows.md`：任务构建流程。
- `science-agent-factory/docs/saf-and-sft-gateway-guide.md`：**整合指南**，含两组件关系、模块 IO、
  Docker 挂载、模型指定方式与常见坑。
- `science-agent-factory/AGENTS.md`：该子项目的代码文件头与实现规范。

## SFT Gateway

- `sft-gateway/README.md`：安装与服务、环境变量、导出命令。
- `sft-gateway/ARCHITECTURE.md`：数据流、模块 IO、因果图算法、资格与投影契约。
- `sft-gateway/REVIEW_AND_PLAN.md`：修复与验收记录、明确限制。
- `sft-gateway/SKILL.md`：使用技能说明。
- `sft-gateway/INSTALL.md`、`sft-gateway/DELIVERY.md`：安装与交付记录。