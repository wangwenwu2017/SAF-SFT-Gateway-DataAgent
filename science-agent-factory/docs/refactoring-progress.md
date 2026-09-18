# Harbor 原生执行重构

已移除 Terminus 专用 runner、SDK Solver、RuntimePlugin、SAF Bundle、旧状态兼容和两套批次调度。
当前执行职责、状态边界及配置方式见 [架构](architecture.md)。
历史运行数据未清理。新状态使用 saf.job.v4 / saf.batch.v5，缓存位于独立 v2 命名空间。
验证采用假 SDK / 假 Harbor 的离线集成、进程超时取消测试，以及原生 Harbor 配置解析检查；
未通过本次重构启动真实模型或 Docker 生产任务。

## 第二轮边界复查

- Harbor 与预热共用进程组终止逻辑，主进程退出后仍清理存活后代；镜像 inspect 支持超时和取消。
- 保留超时/取消根因并追加制品诊断，校验 Trial 身份和 agent/model 字段类型。
- 摘要拒绝缺失路径、符号链接根及错误目录类型，避免产生误导缓存键。
- 只生成模式下部分失败保持 failed/blocked；成功恢复清除旧错误。
- 当前离线回归为 136 项，包含实际子进程组清理测试；未运行真实模型或 Docker 任务。

## 外部 Gateway 与失败诊断

移除内置代理与 HTTP 捕获，内部 SDK 保留订阅和 API；默认 audited，Harbor 示例默认 codex。
运行失败不再因缺少 ATIF 跳过审查，审核失败可独立恢复；诊断只允许归因、有限重试或拒绝。
