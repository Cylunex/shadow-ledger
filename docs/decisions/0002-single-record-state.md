# ADR 0002：使用内部 LedgerRecord 保存唯一状态

- 状态：Accepted
- 日期：2026-08-19

## 背景

MoneyEntry 和 ConsumptionEvent 是两张事实表。若各自维护 draft/confirmed/voided，会出现一边确认、
另一边失败或撤销状态冲突；金额未知消费和独立收入又要求二者都可选。

## 决策

增加内部 `LedgerRecord` 聚合根，唯一保存 owner、record_kind、state、occurred_at、revision 和状态
时间。MoneyEntry 与 ConsumptionEvent 各自最多一条，并共享同一 record_id；
ConsumptionEvent.money_entry_id 保持 UNIQUE NULLABLE，并用复合外键保证属于同一聚合。

所有创建、确认、撤销、补金额和 Intent 完成在单个数据库事务中操作聚合。

## 后果

- 从数据库结构上消除状态冲突；
- 金额未知消费和 money-only 收入自然表达；
- 多一张非常轻的内部表，但不增加用户概念，也不引入会计复杂度。
