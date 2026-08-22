# ADR 0003：开放 Shadow Agent 只读与草案能力

## 状态

已接受，2026-08-22。

## 背景

冻结基线把 Agent Tools 推迟到 API、权限与 draft-only 风险测试稳定之后。Shadow Platform 已经
定义运行时无关的 Domain Plugin v0.1、Agent Manifest v2 和 DSH 通用 Adapter，Ledger 现有
Record、BudgetTarget、Decimal、幂等与审计规则也已可复用。继续完全关闭 Agent 会阻止独立
`shadow-ledger` Profile 验证，但直接开放正式入账、导出或资金能力会越过当前确认边界。

## 决策

Ledger 提交运行时无关的 `shadow-plugin.yaml`、Manifest、Skill、Eval 和 contracts，并新增独立
机器 Bearer API。首期只开放：按币种的月度摘要、最小披露确认账目、预算进度和 money-only
草案。Agent 必须同时通过 `ledger` audience、capability scope 与 owner 级资源 grant。

草案复用 LedgerRecord 的 `draft` 状态、Decimal/币种/时区校验与幂等存储，不新建 Agent 事实，
不自动确认。机器 API 不接受 Account、Payment、ExchangeRate 或 `confirm` 字段。正式确认仍由
Ledger 浏览器用户会话完成。

## 后果

- 冻结边界中的“无 Agent”调整为“只读与可撤销草案已开放”；
- Ledger 仍不引入 Account、Posting、余额、支付方式、转账或隐式汇率换算；
- 普通 Profile 不包含正式入账、导出、难撤销调整或资金执行；
- grant 管理 UI、ConfirmationReceipt、L2 正式入账、L3 导出/调整和任何 L4 资金执行保持 P1；
- Platform 生成唯一通用 DSH Bundle，Ledger 不发布领域专属 npm 包。
