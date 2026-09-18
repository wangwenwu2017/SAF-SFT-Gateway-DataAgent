# SFT Gateway 0.3.2 提交说明

## 项目目的

为原生 Codex 等黑盒 harness 提供中转 API，保持请求转发、采集原始证据，按父响应/完整前缀重建可追溯轨迹，导出固定 `task/tools/messages` 文本 SFT。外部计费 key 只属于网关；harness 使用独立 gateway key。

## 本次交付

- 安全转发、保守终态检查、上下文/前缀/异步工具算法、统一训练资格、短事务并发存储和可校验导出。
- README 为启动与使用入口；ARCHITECTURE 说明模块输入输出和算法；REVIEW_AND_PLAN 记录修复证据及限制。
- 21 个生产模块、18 个类、142 个函数全部有 docstring。
- 最终回归：**138 passed in 2.49s**（独立安装的 wheel，pytest 8.4.2）。新增回归覆盖混合父边、异常记录隔离、Chat 扩展字段和历史流终态。两个 shell 脚本通过 `bash -n`。测试不调用付费模型。
- 原生 Harbor 短任务：Codex 0.153.4、`glm-5.3-responses`、预热镜像、原生 `/v1` URL，Harbor 退出码 0，独立 verifier reward **1.0**，最终复测 7 个请求组成 1 条合格 SFT（25 条消息），6 个工具调用全部闭合；运行约 57 秒，未提前中止。
- 真实请求使用普通 `/v1/responses`，不注入 run_id；使用原生 session 头归属。实测为串行调用，异步因果顺序另由回归验证。输入 64,260 tokens，其中 61,440 为缓存 tokens，证明本次转发保留了缓存命中。
- [机器可读验收记录](VALIDATION.json) 包含最终实测、安装包回归、历史重放和来源检查结果。

## 保留案例

| 目录 | 当前版本正常 SFT | 汇报定位 |
|---|---:|---|
| `artifacts/harbor-aquifer-codex1534-glm53-full-20260911` | 0 | 114 请求，112 完成节点组成一条轨迹，缺一个工具结果；Harbor 取消，无验证结果 |
| `artifacts/harbor-hysteretic-aquifer-control-20260911` | 0 | 40 请求，39 完成节点；加密 reasoning 前缀不一致，严格算法不强拼 |

各目录原有文件保留，历史结果请看 `reviewed-v0.3.0/`。其 `provenance.json` 解释拒绝原因，`manifest.json` 校验输出。历史 incomplete 文件和调试投影不是可直接训练的完整数据。案例中的 benchmark-only 标记保留，案例用于采集集成汇报。新的短任务验收保存在项目外 `/data/hy/wenwu/sft-gateway-validation-032/final-native`。

`PRESERVED_ARTIFACTS.sha256.json` 记录整理前已有文件的摘要，全部验证未变。capture.sqlite 和已有版本化重放不覆盖原文件；0.3.2 重放报告放在项目外，并汇总至 VALIDATION.json。CLEANUP.json 记录移除的 12 个中间 artifacts 目录。

原始数据库另保存在项目外 `/data/hy/wenwu/sft-gateway-local-captures-20260911/data`，不混入源码交付。该本机目录可能含旧配置凭据，不能作为公共代码发布内容。

## 提交方式

完整交付包包含源码、文档、必要回归测试及上述两个案例。捕获含任务提示、响应和工具输出，仅供授权内部汇报；公开发布建议只使用单独的 source 包，不上传 artifacts。真实 `.env`、含凭据配置、日志、本机缓存均不进入包。交付包额外的 `validation-v032/` 保存最终短任务导出和脱敏复现配置，位于源码项目目录之外；源码包不含这些捕获数据。

本项目目录目前未初始化 Git；包内 SOURCE_SHA256.json 提供源文件版本校验。未添加未经确认的开源许可证，也未复制 Polar 的实现代码。

## 接受边界

这是可审查、可复现的受控采集交付，不是“所有任务恢复必然正确”的保证。未知服务端上下文、密文差异、多模态、任务身份缺失或动态配置无法表示时，严格拒绝训练并保留证据。SFT 合格、任务完成、Harbor verified/reward 分开判断。
