# 任务生产流程

1. 校验 saf.seed.v2，复制 Seed，冻结原生 Harbor 配置。
2. Builder 在同一次 build 调用内先分析 Seed 并形成设计卡，随后连续完成探索和构建，无独立设计审查。
3. Builder 交付完整 Harbor Task，实际执行 Oracle/Nop，保存 Trial result 和任务摘要；设计卡保存为
   `task-metadata/design_card.json`，随当前版本归档。
4. 静态检查结构、可移植性、环境来源及自测证据。SDK 有限交付修复后仍失败，审查模式由 Auditor
   给出结构化 BuilderReflection，按合同或语义预算修订。
5. 审查模式蒸馏公开策略并预审，预审一并检查设计卡与最终任务及证据。Reference 只供 Distiller，Tests 不暂存给 Distiller。
6. Harbor 根据冻结原生配置运行；SAF 不选择具体 harness。授权策略通过额外 instruction，白名单 Seed
   材料通过只读 mount 提供。失败反馈不会注入下一次独立 rollout。
7. 默认审查模式读取真实 Trial 结果和 ATIF；失败或缺少轨迹时读取现有日志进行 diagnosis，不能接受为训练样本。
8. 归档所有 Attempt；完整任务可独立导出，有效失败轨迹可用 selection=all 导出。

任务修改后必须重新自测、重新蒸馏和重新 rollout。旧版本轨迹留档，但不进入新版本默认训练导出。
轻量模式的 curated 表示采集目标完成，不表示通过人工式质量审查。

原生配置中只有一个 agent；禁用 verifier 仅允许 lightweight。科学环境缓存与 agent 安装独立。
审查证据只读，不保留原先 BundleHarness 或由 Auditor 代执行的第二套验证路径。
