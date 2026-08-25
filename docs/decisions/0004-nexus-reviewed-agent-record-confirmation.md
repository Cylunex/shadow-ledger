# ADR 0004：允许 Nexus 审核后的 Agent 草稿正式入账

## 状态

已接受，2026-08-25。

## 背景

ADR 0003 将 Agent 能力限制为最小披露读取和可撤销草稿，正式确认只允许 Ledger 浏览器用户
会话执行。Shadow Nexus 已提供独立于模型的 Review 界面，但此前用户在 Nexus 点“确认草稿”后，
系统只创建 `draft` LedgerRecord，界面确认与领域正式入账的语义不一致。

## 决策

Ledger 新增隐藏的 L2 capability `ledger.records.write`，只接受 Nexus 在用户明确审核后提交同一条
Agent 草稿。调用必须同时满足独立的 `ledger.records.write` scope 和 `allow_confirm` owner 资源
授权，并携带草稿 revision。接口只允许确认由同一 Agent 创建、属于同一 owner 的 LedgerRecord，
不接受新金额、标题、账户或支付字段；已确认记录以幂等 replay 返回。

模型可见的 DSH Profile 仍只选择读取和 `ledger.records.draft`，不选择隐藏写能力。Nexus 先创建
草稿并展示内容，只有用户在 Review 中点击确认后才调用提交接口。服务以 Agent 身份记录确认审计，
继续产生既有 `ledger.record.confirmed` Outbox 事件。正式记录仍可按现有规则撤销。

## 后果

- Nexus 的“确认草稿”现在等于 Ledger 正式入账，而非仅保存草稿；
- 两阶段边界、revision 冲突、scope、资源 grant 与审计共同阻止模型绕过用户确认；
- 不引入 Account、Posting、余额、支付方式、转账、导出或隐式汇率换算；
- DSH 普通 Profile 不暴露正式写工具，未来若改变这一点必须另行评审；
- 部署时需要执行 grant 字段迁移，并显式为 Nexus 使用的 Agent 开启 write scope 与
  `allow_confirm`。
