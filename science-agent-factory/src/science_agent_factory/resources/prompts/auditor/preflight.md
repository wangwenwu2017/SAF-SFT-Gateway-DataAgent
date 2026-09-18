# 生成完成后的任务审查

阶段 `preflight`，读取 `inputs/task/` 完整 Harbor Task、`inputs/metadata/`、`inputs/seed/`
和 `inputs/strategy_card.json`。只读审查，不能执行自测或修改任务。

检查：

1. Oracle=1、Nop=0 的真实 Trial 结果与 metadata 及最终任务一致，资源清理有记录。
2. Instruction、Solution、Tests 的语义和路径一致；verifier 实际检查中心要求，能拒绝典型坏解。
3. 对比 Seed 与候选的最短求解路径、核心能力及五个难度维度。仅改名或明显降低难度须修订。
   读取 `inputs/metadata/design_card.json`，核对设计中的验收声明、错误解法和捷径拒绝计划是否由
   最终 Instruction、Solution、Tests 和实际证据支持，并检查其与 exploration_report 的一致性。
4. 公开环境没有私有参考或测试泄漏；策略卡由公开约束支持，有可执行的方法而无答案和源码复制。

`accept` 要求中心闭环可信、难度 `preserved`，`target=none`。任务缺陷用 `revise` + `builder`
和 BuilderReflection；仅策略问题用 `revise` + `distiller` 和公开 DistillerReflection；不可修复用 `reject`。
不要以额外测试广度或非核心风格阻塞已成立的中心闭环。

只写 `outputs/audit.json`（saf.audit.v1，phase=preflight）。
