# ADR 0011：统一 Platform 鉴权与普通意图直接执行

状态：设计已接受，2026-09-07；尚未实现。依据用户本轮明确要求，统一鉴权与 Agent 管理，不再由领域各自维护。

## 决策与被替代内容

本 ADR 在目标架构上接续 ADR 0003/0004/0005/0007/0010：Agent 身份、owner 委托、Session、目录策略、批准发放与通用 MCP/Runtime 迁入 Platform。普通已明确的用户意图通过中央 current_intent 票据直接执行；仅实际高影响操作使用中央内联确认，不新增 Ledger 审核页或本地审批控制面。

旧 ADR 的实现与历史凭证仍真实存在：旧 commit/reject 缺 approval_grant_id 返回 428，未经新接口/合同验收不能放宽；旧 allow_confirm 不是新的写授权，历史批准不转换成有效票据。

## 保留的不变量

- Record/MoneyEntry/ConsumptionEvent 聚合、Decimal 金额、原始来源/规范身份分离、单币种查询。
- 不引入账户余额、银行账号、转账、支付、对账或复式记账。
- 周期/预测/自动抓取只产生提醒、来源观察或草稿，不自动确认未发生事实。
- 领域 owner、资源可见性、版本、业务校验、幂等、事务与 Outbox。
- QueryRun/事实计算与真实执行 Receipt 留 Ledger，中央只保存授权与关联元数据。

## 迁移方法

详见 [Ledger Nexus 详细设计](../nexus-integration-design.md)。新增中央模式 Principal、命令与 Receipt 版本；有效 delegation 按旧权限交集导入；原 AgentGrant/Intent/ApprovalGrant 历史只读保留，不删除历史 hash 或伪填 approved_by。按 capability 唯一选择 legacy/central，中央失败不回退旧凭据。

现有领域成员/owner 权限不是 Agent 管理，继续在查询与事务中校验。采用统一 SDK 是统一实现与中央授权真相，不是让各域复制同一份 registry 配置。

## 实施门槛

完成中央影子判定、跨 owner/撤销/目录缩减、普通直接记账、回执/并发/事务失败恢复、旧 428 兼容和历史凭证不变测试后，才能逐能力切换。本文只修正设计与冻结边界，不声称相应代码、数据库或部署已改变。
