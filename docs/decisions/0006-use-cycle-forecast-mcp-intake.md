# ADR 0006：解冻 UseCycle、确定性 Forecast、MCP 与自动抓单

## 状态

已接受，2026-08-31。

## 背景

初版冻结设计要求先积累消费事实，再评估 UseCycle、Forecast、MCP 和自动来源接入。现在已有稳定的
ItemIdentity、Record 聚合、CaptureSource、幂等、draft 审核与权限边界，可以在不引入账户账本的
前提下补齐这些能力。用户明确要求解冻实现，但仍要求自动化不能伪造已发生消费。

## 决策

1. `UseCycle` 只表示用户明确声明的实际使用开始与结束，可选引用已确认 Record；购买本身不会自动
   开始使用，也不表示库存数量。
2. `ForecastRun` 保存确定性算法的完整输入快照、算法版本、输入哈希和输出哈希；`ForecastItem` 是
   可过期建议。首版仅计算明确周期事项、至少三次确认消费的典型复购间隔，以及用户预计或历史
   使用周期结束时间。不训练模型，不将预测写成 Record。
3. Ledger MCP 使用官方 SDK 的 stdio transport。进程必须通过受限 owner 文件绑定唯一用户；默认
   只读，只有显式开启开关才注册 money-only 草稿工具。MCP 不提供 confirm、void、export、账户或
   资金动作。
4. 自动抓单统一进入 `StructuredIntake`：Bearer scope、来源 external ID、原始结构化载荷、适配器名
   和一到多条 Record 候选。Webhook 和受控导入目录复用同一服务；只创建 draft，并拒绝携带凭据
   字段或 `confirm=true`。相同 external ID 内容不同返回冲突。

## 后果

- 预测结果可以从保存的输入快照独立验算；删除预测表不会丢失消费事实。
- UseCycle、预测和抓单不改变 MoneyEntry 金额真相、Record 状态机或统计口径。
- 不引入 Account、Posting、PaymentAllocation、余额、转账、对账、库存或自动补货。
- 周期任务、目录抓单、Webhook 和 MCP 都不能直接生成 confirmed 消费事实。
- 生产启用目录抓单或 MCP 前必须在仓库外提供最小权限数据库凭据、owner 文件和精确开关。
