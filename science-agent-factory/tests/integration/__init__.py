"""Science Agent Factory 工作流集成测试包。

测试目的：组织四 Agent Job、Batch、Harness、恢复和导出的跨模块闭环测试。
被测接口：``ScienceAgentFactory``、``BatchController`` 及其文件交付协议。
输入与夹具：临时 SeedBundle、Fake Agent Runtime、模拟环境与 verifier 制品。
预期结果：不启动真实模型的情况下验证完整状态转换、隔离、回退和导出行为。
"""
