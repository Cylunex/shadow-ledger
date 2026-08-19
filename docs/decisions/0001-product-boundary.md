# ADR 0001：以消费为中心，不建立账户账本

- 状态：Accepted
- 日期：2026-08-19

## 背景

传统个人财务项目以账户、余额、转账和对账为中心，会要求用户维护并不用于实际决策的大量支付
细节。Shadow Ledger 的真实用途是低负担记录金额、消费内容、体验和未来计划。

## 决策

v1 只支持 expense、income、refund 三类 MoneyEntry，不建立 Account、Posting、Transfer、
PaymentAllocation、Reconciliation 或资产负债表。

最终金额是唯一金额真相；消费明细不强制对平。项目仍使用 Ledger 名称，因为它统一个人金额、
消费与计划，但产品副标题必须明确“个人财务与消费”。

## 后果

- 录入更快，模型和页面更符合实际使用；
- 无法回答某张卡余额、支付方式占比或账户现金流；
- 未来若真实需求改变，必须新增 ADR 和独立迁移，不能把 Account 字段偷偷塞回 MoneyEntry。
