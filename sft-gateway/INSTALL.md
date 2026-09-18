# 从源码安装 SFT Gateway

这份说明适合把项目源码或 `sft-gateway-0.3.2-source.tar.gz` 交给另一台机器的使用者。安装完成后，得到的是 `sft-gateway` 命令；它负责启动网关和导出捕获，不会替代 Harbor、Codex 或任务镜像。

## 运行条件

- Linux 或其他支持 Python 3.11+、SQLite 和网络监听的系统
- Python 3.11 或更高版本
- 能访问外部中转站的网络
- 外部中转站的 URL 和计费 key
- 如果使用 Harbor：Docker/Harbor 由使用者另行安装，容器还必须能访问网关地址

项目依赖会从 Python 包索引安装：FastAPI、HTTPX、AnyIO 和 Uvicorn。源码包本身不包含这些第三方依赖，也不包含任何真实 key、模型 runtime 或 Docker 镜像。

## 方式一：从源码目录安装

```bash
git clone <your-repository-url> sft-gateway
cd sft-gateway

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .
```

开发或需要运行测试时安装测试依赖：

```bash
python -m pip install '.[test]'
```

`pip install .` 会把命令安装到当前虚拟环境；`pip install -e .` 是可编辑安装，适合修改源码后立即生效的开发环境。生产运行建议使用普通安装，并把数据库放到项目目录之外的持久化路径。

## 方式二：从源码压缩包安装

把 `sft-gateway-0.3.2-source.tar.gz` 复制到目标机器后：

```bash
tar -xzf sft-gateway-0.3.2-source.tar.gz
cd sft-gateway

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .
```

如果同时收到 `SHA256SUMS`，先校验压缩包：

```bash
sha256sum -c SHA256SUMS
```

源码包适合部署网关；含 `artifacts` 和测试捕获的 submission 包适合内部审查，不建议将其中的任务数据直接部署到生产服务器。

## 安装确认

```bash
python -c 'import sft_gateway; print(sft_gateway.__version__)'
sft-gateway --help
```

如果当前版本的包没有暴露 `__version__`，以 `sft-gateway --help` 能正常返回和 `python -m pip show sft-gateway` 中的版本为准。源码包还可以运行：

```bash
python -m pytest -q
bash -n scripts/run_harbor_via_gateway.sh scripts/codex-runtime-launcher.sh
```

测试使用临时数据库和模拟上游，不会调用真实模型。

## 配置并启动

```bash
export SFT_UPSTREAM_URL='https://your-relay.example/v1'
export SFT_UPSTREAM_API_KEY='外部中转站计费 key'
export SFT_GATEWAY_API_KEY='给 Codex/Harbor 使用的独立 key'
export SFT_DB='/var/lib/sft-gateway/gateway.sqlite'

mkdir -p "$(dirname "$SFT_DB")"
sft-gateway serve --host 0.0.0.0 --port 8100
```

更完整的环境变量、认证和 Harbor 配置说明见 [SKILL.md](SKILL.md)。应用不会自动读取 `.env`；可以由你们自己的进程管理器在启动前加载环境变量，但不要把真实 key 写入源码或提交到仓库。

启动后检查：

```bash
curl --fail http://127.0.0.1:8100/health
```

交给 Harbor/Codex 的地址应是宿主机可达的 IP 或 DNS，例如 `http://10.0.0.8:8100/v1`，不要把 `127.0.0.1` 或 `0.0.0.0` 直接填进 Docker 容器配置。

## 接入 Harbor 后的行为

其他 Harbor 调用方式保持不变：原有 task、镜像、Codex 版本、工具和 verifier 都继续使用。只需在实际生效的 Codex agent 环境中设置：

```json
{
  "OPENAI_BASE_URL": "http://gateway-host:8100/v1",
  "OPENAI_API_KEY": "给 Codex/Harbor 使用的独立 key"
}
```

不需要修改 Codex 源码，也不需要手工添加 `run_id`。网关会尽量使用原生 thread/session 元数据分组；任务结束后，网关维护者按会话执行 `sft-gateway export` 生成 `sft_samples.jsonl`。这一步不会自动发生。

若使用自定义 Codex provider、`auth.json` 或 Harbor 的其他配置层，请确认它们没有覆盖这两个连接参数。详细接入示例和 Docker 可达性说明见 [SKILL.md](SKILL.md#三harbor-用户沿用原来的任务只替换模型连接)。
