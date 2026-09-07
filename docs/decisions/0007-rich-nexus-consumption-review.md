# ADR 0007：Nexus Review 保留富消费事实与证据引用

> 2026-09-07 设计衔接：统一鉴权、Agent 与 Nexus 目标规范以 [本项目接入设计](../nexus-integration-design.md) 为准。不再新增领域自管 OIDC/Session、Agent registry/Grant/审批中心或模型/工具通用循环；普通明确写入采用中央 current_intent。新决策由 ADR 0011 接续；旧 428 与历史凭证不放宽、不重写。以下相关条目仅描述旧实现/历史阶段，不能作为新增实现继续复制；未迁移接口仍保留当前安全限制。

## 状态

已接受，2026-09-01。

## 背景

统一 Nexus 能从订单文字和附件中同时得到实付金额、分类、消费场景、原始商家/渠道、商品或菜品
明细及 Shadow Asset 引用。既有隐藏 Review 创建入口只把金额字段写入 money-only 草稿，导致用户
明确提供且 Ledger 已有领域模型承载的信息在正式入账前丢失，后续无法按商家、场景或消费内容检索，
也无法从记录追溯证据。

## 决策

隐藏的 `ledger.nexus.review.create` 可在金额字段之外接受可选消费字段：`scene`、原始商家/渠道、
消费备注，以及 JSON 编码的最多 100 条消费明细。字段由 Ledger 使用既有 `ConsumptionInput` 严格
校验，并与 MoneyEntry 在同一个 `LedgerRecord` 草稿中创建。Nexus 提供的 `source_refs` 作为
`relation=evidence` 的 ExternalReference 与草稿在同一事务保存，并在 Review 列表和提交回执中返回。

模型可见的 `POST /drafts` 与 stdio MCP 仍保持 money-only；丰富字段只进入模型隐藏、由 Nexus Host
调用的标准 Review 协议。正式确认仍只提交同一草稿和 revision，不允许在 commit 时改变金额或内容。
Nexus Host 会把附件的稳定 `shadow://` 引用与普通来源引用一起交给领域 Review。

## 后果

- 明确提供的商家、场景、消费明细和 Asset 证据不再因统一对话入口而丢失；
- Ledger 仍不保存文件字节，也不新增账户、支付方式、余额、转账或隐式汇率；
- 缺少 `scene` 时不根据零散商家或明细自行推断消费类型，入口返回 422；
- 同一幂等草稿的证据引用发生变化时返回 409，避免重试悄悄改写来源；
- 不是所有对话字段都进入 Ledger：只保存影响检索、统计、追溯和纠错的既有领域事实。
