# 0.6.0

- 完整 Harbor Task 成为唯一交付格式，所有 rollout 使用原生 Harbor 配置。
- 删除 SDK Solver、Terminus 专用适配、旧格式兼容及重复批次调度。
- 固定 Seed 槽位，统一失败轨迹归档与审核导出；历史数据不自动删除。

# Changelog

## 0.5.0

- 新增默认 `lightweight_terminus` 工作流：Builder 保持完整构建逻辑，外部编排跳过 Auditor、Distiller、
  Harness 和 verifier，直接用 Harbor Terminus-2 采集 ATIF。
- 原 `audited` 四 Agent 闭环保留为配置可选模式；Job 会固化工作流模式以保证恢复一致。
- Tool-SFT 与批次导出新增 `unguided`、`not_run` 和 `unverified` 标签，支持 ATIF 推理/工具事件转换。
- Terminus 只获得当前 instruction 与结构化 Docker context；Seed、策略卡、Reference 和 Verifier 不投放。
- Terminus 的 LiteLLM provider 与网关模型名分离，默认使用无服务端粘性依赖的 Chat
  Completions；回环记录网关会自动绕过宿主 HTTP 代理，请求预算同时覆盖两种 API。
- Harbor 内部 Trial 结果成为 outcome 的权威来源；即使 CLI 以 0 退出，内部异常仍会标为失败并
  保留已产生的 ATIF。
- 新增不含密钥的 Dockerfile、共享服务器安全配置和白名单部署打包器；发布包携带相对路径清单与
  SHA-256，拒绝符号链接、已知密钥、本机绝对路径和不完整的成功样本。
- 新增服务器主导的 Git 双端维护：角色化 publish/pull/check 守卫、完整 pre-push 门禁、启动前 Commit
  一致性检查，以及发布清单和 Docker OCI 标签中的 Git revision。

## 0.4.0

- 将四 Agent 工作流提升为唯一正式架构，统一使用 `saf` 命令和 `science_agent_factory` 包路径。
- 删除旧 SQLite 编排器、旧 Agent 集合和专用 QEMU 执行栈。
- 将正式 Prompt 与安全默认配置打包进 wheel。
- 统一 `saf.*` Schema 命名，不再读取历史 `saf.simple.*` Job/Batch 状态。
- 导出接口收敛为完整 Bundle 与分层 Tool-SFT。
- Solver 指导收敛为默认 `strategy` 与隔离的 `strategy_seed`，删除盲解和逐级暴露旧模式。
- Distiller 固定从当前任务 Reference 生成策略卡；`seed_guided` 数据不会进入默认导出。
- 外部 YAML 改为递归覆盖内置默认配置，Batch 每个槽位只创建一个 Job。
- 合并 benchmark 导入器公共文件操作，并将示例更新为带真实 Solution 的 `saf.seed.v2`。
- 清理历史运行产物、日期化 smoke 配置和旧架构测试。
- 将精选任务与 Bundle 升级为任务根相对路径的 v4，并安全兼容 Job 内的旧 v3 索引。
- 为 Solver Attempt 增加原子归档检查点，避免状态提交前崩溃造成固定 Attempt 目录永久阻塞。
- Seed 导入改用相对路径和可回滚受管目录替换，Batch 拒绝越出 Seed 根的清单路径。
- 环境、Verifier 和 Task Bundle 的 v1/v2 版本必须显式声明。
- 资源守卫覆盖带 Run 前缀的 Docker 镜像、数据卷和网络，Gateway 同步脱敏查询参数。
