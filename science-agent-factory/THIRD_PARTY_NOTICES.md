# 第三方设计与代码来源说明

## Polar / ProRL-Agent-Server

`science_agent_factory.agents.gateway` 的 completion/session 数据边界与逐调用制品布局参考并改写自
[NVIDIA-NeMo/ProRL-Agent-Server](https://github.com/NVIDIA-NeMo/ProRL-Agent-Server) 的 Polar Gateway
和 trajectory 模块。当前实现删除了在线 RL、协议转换、内存 SessionStore 和可丢弃异步队列，改为面向
离线 SFT 的透明转发、凭据脱敏与原子落盘。

Polar / ProRL-Agent-Server 以 Apache License 2.0 发布：
<https://github.com/NVIDIA-NeMo/ProRL-Agent-Server/blob/stable/LICENSE>。

## LEGO-RL 与 ClawGym II

每次运行独立临时端口、以模型 API 边界观察黑盒 Harness、保留分叉调用供后续重建的结构，分别参考
[LEGO-RL](https://github.com/LegoX/Lego-RL) 与
[ClawGym II](https://arxiv.org/abs/2608.16798)。本仓库没有引入它们的训练器、vLLM 代理或 RL 代码。
