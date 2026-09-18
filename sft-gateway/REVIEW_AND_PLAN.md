# 0.3.2 修复与验收记录

日期：2026-09-12。交付定位为受控部署的文本 SFT 采集项目，不声称所有黑盒任务都能完整恢复。

0.3.1 修复了混合父边的批量排序、Chat 扩展字段和历史流终态复核。0.3.2 继续修复内容块与 SSE 畸形记录隔离、多候选流误准入，以及采集解析错误中断原始转发的问题。

## 本轮修复

| 原审查问题 | 当前处理 |
|---|---|
| 路径前缀处理后可形成外部 URL | 处理后白名单 + 最终 origin 检查，不跟随重定向 |
| 管理 API 默认匿名、匿名使用固定计费 key | 管理 key 缺省禁用；固定上游 key 必须配独立 gateway key |
| Chat DONE 覆盖截断、矛盾 Responses 被恢复 | 自然终态判断；失败/冲突不升级为完成 |
| 省略 instructions 时错误继承 | 本轮 instructions 独立生效，状态变更保留谱系但拒绝完整静态 SFT |
| 相同 prompt 重试先判歧义 | 完整 context+target 证据筛选后选最长候选 |
| 参数序列化噪声 | 仅无重复键的 function JSON 参数规范化，custom 文本不变 |
| 乱序父响应、重复 ID、循环 | 批量依赖排序及显式拒绝原因 |
| 未知/压缩/多模态被删除后仍训练 | 统一资格检查；不支持就拒绝，保留原始证据 |
| 动态 tools 套用旧调用 | 静态配置一致性检查；不从祖先猜测叶 tools |
| 插入 assistant 历史被当作监督 | 每个监督位置须有实际生成请求，并验证有效上下文保留 |
| SSE 长事务锁住其他 worker | 单事件短提交、线程执行、等待提交形成背压；取消期间保护持久化 |
| CLI import/--help 建默认库 | 懒初始化 ASGI，离线导出只读一致快照 |
| run URL 被旧环境覆盖、退出码丢失 | 明确覆盖 URL，传播子进程退出码，并修正 argparse 子命令字段冲突 |
| debug 与正常集混合、旧文件残留 | 拒绝集独立，清理旧便利文件；发布锁 + 最后发布校验 manifest |
| 原始 bytes 不可逆 | schema v3 保存接收/转发正文；导出 bytes 使用 base64，旧库标 unavailable |
| Harbor 预检镜像与实际镜像不符 | 不一致直接拒绝；随机 instance ID 验证当前启动实例 |
| Harbor 生成配置含 key | umask 077、忽略配置文件、独立 admin key 不进容器 |
| 无内存/并发边界 | 可配置请求/响应/帧/流字节和活跃请求上限 |

## 0.3.2 新增修复

- 内容块 `type` 为对象或数组时，旧资格检查抛 TypeError；现在拒绝该记录并保留其他任务。
- SSE 负数/稀疏索引、错误容器、Chat 非对象 delta 与错误工具索引不会中断整批导出。
- Chat 多 choice 流保留不支持标记，不能静默只训练首候选。
- 非流式异常响应仍保存并原样返回；SSE 语义累积失败仍继续原始转发，失败捕获不进入训练。
- 前缀交错回归枚举到达顺序，验证迟到工具结果没有被挪到调用旁边，两个同 prompt 任务不串轨迹。

## 验证范围

回归包含：HTTPX 模拟上游端到端转发、计费 key 不离开固定 origin、gzip/cache header、DONE 与截断、原始正文 bytes、请求上限、缺失/未知上下文、重试消歧、乱序父 ID、循环、异步工具晚返回、双连接并发序号、双 worker 慢 SSE、CLI 退出码及 manifest/残留清理。

测试全用临时数据库和虚构凭据，不调用真实模型。最终结果见 DELIVERY.md。所有生产模块、类、函数均有 docstring，两个 shell 脚本做语法检查。

## 两个真实捕获的只读重放

| 案例 | 请求 | 参与建图 | 叶轨迹 | 正常 SFT | 结论 |
|---|---:|---:|---:|---:|---|
| Aquifer GLM | 114 | 112 | 1 | 0 | 112 个调用、111 个结果，叶仍 pending |
| Hysteretic Aquifer | 40 | 39 | 39 | 0 | 前轮响应与后轮输入的 encrypted reasoning 不同，无法证明完整前缀；同时存在 pending |

Hysteretic 历史说明曾称能拼成一条，本次按严格原始证据重放不能支持该结论。不会丢掉 ciphertext 差异来强行拼接。两案例均用于采集集成汇报，不能描述为完成且验证通过的训练成果。项目保留原始输出和 v0.3.0 重放，避免改写历史。

## 明确限制与计划差异

- 不是 Polar token 算法的等价实现。原生网关缺少 token/EOT 证据，采用 message/item 级严格比较。
- 使用有界请求 + 逐条线程写入背压，没有另设专用队列线程；已测试慢 SSE 不跨网络持有 SQLite 写锁。吞吐和多进程内存仍需部署压测。
- manifest 是可检测的完整文件集合，不是目录整体原子替换。读者必须验证 SHA-256；竞争导出写入者由文件锁拒绝。依赖 Linux `fcntl`。
- 查询串不持久化，避免保存 query 凭据；旧请求没有 wire bytes，不伪造。无法做到完整 TCP/HTTP 报文重放。
- `SFT_PREWARMED_IMAGE` 与任务镜像不一致时拒绝启动，未自动改写任务文件。
- 不自动解密 reasoning、不展开服务端 conversation/compaction、不支持多模态完整训练。Chat 扩展 reasoning/refusal 未完整支持时拒绝正常样本。
- 任务状态默认 unknown；仅依据外部 Harbor 结果描述 verified/reward，不由工具闭合推断成功。
- 本轮真实 Harbor 短任务与历史重放分开记录，实际结果见 DELIVERY.md 和 VALIDATION.json。串行实测不替代异步工具回归测试。

## 参考

- Polar stable `6a1ead6bfac054fce6c1e62d1a77b330d96c58db`：`src/polar/trajectory/builder/prefix_merging.py`、`src/polar/gateway/completion_writer.py`。
- [Responses create](https://developers.openai.com/api/reference/cli/resources/responses/methods/create)：request instructions 不自动继承。
- [Responses compact](https://developers.openai.com/api/reference/java/resources/responses/methods/compact)：opaque compaction 不等于可恢复的原始对话。
