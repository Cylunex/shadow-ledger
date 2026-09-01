# Ledger Agent 字段边界

- 金额使用十进制字符串，必须大于零且最多四位小数。
- 币种使用三个 ASCII 字母并由服务端转为大写；不同币种分别统计，不进行隐式换算。
- `records.list` 只返回金额、币种、类型、分类、场景、时间和不透明记录引用，不返回备注、商家原文、消费明细或支付信息。
- `records.draft` 不接受 account、payment_method、exchange_rate、confirm 等字段。
- 草案保持 `draft` 且可由 Ledger 用户删除；用户确认前不参与正式汇总。
- 统一 Nexus 的隐藏 Review 可把明确提供的消费语义与金额放入同一个草稿；普通模型工具和 MCP
  仍只创建 money-only 草稿。
- `consumptionItemsJson` 只保存用户或附件中明确出现的原始消费明细，不要求明细金额与最终实付金额
  对平；附件本身留在 Asset，Ledger 只保存 Host 提供的 `shadow://` 证据引用。
