"""随 wheel 发布的安全默认配置与正式 Prompt 资源包。

模块用途：让已安装的 CLI 可通过 ``importlib.resources`` 定位默认 YAML 和四个 Agent 的 Prompt，
避免依赖仓库当前工作目录。
主要接口：标准 ``importlib.resources.files('science_agent_factory.resources')`` 资源访问接口。
输入：资源名称 ``default.yaml`` 或 ``prompts`` 下的相对路径。
输出：安装包内可读的配置和 Markdown Prompt。
副作用与边界：模块导入无 I/O；资源只读，不保存用户配置、密钥、Job 状态或运行轨迹。
"""
