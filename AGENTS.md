# SAF-SFT-Gateway 仓库约定

本文件适用于仓库根目录及其所有子目录。子目录内还有各自的约定文件时，就近者优先：
`science-agent-factory/AGENTS.md` 定义该子项目的代码文件头与实现规范。

## 项目结构

- `science-agent-factory/`：SAF 包（hatchling，`src/science_agent_factory`，命令 `saf`）。
- `sft-gateway/`：网关包（setuptools，`sft_gateway`，命令 `sft-gateway`）。
- 两个子项目功能独立、依赖各自管理，不互相导入；跨组件协作只通过 Harbor 配置与 HTTP 接口。
- 根 `Makefile` 是单一项目入口；根 `README.md`、`docs/` 是统一说明。

## Git 与 GitHub 工作流

- `origin` 指向 GitHub 仓库 `wangwenwu2017/SAF-SFT-Gateway`，默认分支 `main`。
- 提交身份：`user.name=wenwu`，`user.email=wangwenwu22@mails.ucas.ac.cn`。
- 直接向 `main` 提交即可；改动大或需评审时另开分支并发起 Pull Request，合并回 `main`。
- 禁止强推、变基或删除已推送的共享历史；回退一律使用 `git revert` 生成可审计的新提交。
- 推送前必须确认工作区只包含预期变更，并运行 `make check`。

## 密钥与忽略项

以下内容**不得**加入 Git：`.env`、`configs/server.yaml`、`var/`、`configs/local.yaml`、
`data/`、`*.sqlite*`、虚拟环境（`.venv/`、`.harbor-venv/`）、构建产物（`dist/`、`*.egg-info/`）。
根 `.gitignore` 已覆盖，提交前仍应逐项确认。`*.env.example` 只放占位符，不放真实值。

## 测试与检查

```bash
make check     # lint + 两个子项目测试
make test      # 仅测试
make lint      # 仅静态检查
```

SAF 子树的完整门禁：`uv run pytest -q tests/test_module_documentation.py && uv run ruff check src tests && uv run pytest -q`。