# Ledger Agent 字段边界

- 金额使用十进制字符串，必须大于零且最多四位小数。
- 币种使用三个 ASCII 字母并由服务端转为大写；不同币种分别统计，不进行隐式换算。
- `records.list` 只返回金额、币种、类型、分类、场景、时间、支付方式标签和不透明记录引用，不返回备注、商家原文、消费明细或账户信息。
- `records.draft` 可选填 payment_method；不接受 account、exchange_rate、confirm 等字段。
- 支付方式只记录明确事实：支付宝 alipay、微信 wechat、京东支付 jd_pay、白条 jd_baitiao、花呗 huabei、
  礼品卡 gift_card、现金 cash、银行卡 bank_card、银行转账 bank_transfer、混合 mixed、其他 other。
  未提供则省略；不要按商家猜测、索取银行或账号。Nexus Proposal 对应字段为 `paymentMethod`。
  礼品卡不是优惠或免费；金额按用户明确的消费口径，不自动补记充值或推断购卡折扣。
- 草案保持 `draft` 且可由 Ledger 用户删除；用户确认前不参与正式汇总。
- 统一 Nexus 的隐藏 Review 可把明确提供的消费语义与金额放入同一个草稿；普通模型工具和 MCP
  仍只创建 money-only 草稿。
- `consumptionItemsJson` 只保存用户或附件中明确出现的原始消费明细，不要求明细金额与最终实付金额
  对平；附件本身留在 Asset，Ledger 只保存 Host 提供的 `shadow://` 证据引用。
