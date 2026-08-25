---
name: ledger-assistant
description: 读取用户已授权的账目与预算摘要；仅在当前 Profile 明确提供草稿工具时创建待审核草稿。
---

# Ledger Assistant

仅在用户需要查看自己的收支、预算进度或明确要求保存记账草案时使用本 Skill。必须以当前 Profile
实际提供的工具为能力真相，Skill 文字本身不授予写权限。

## 操作顺序

1. 汇总问题优先调用 `ledger.summary.get`；只有需要逐条核对时才调用 `ledger.records.list`。
2. 预算问题调用 `ledger.budgets.list`，并保留接口返回的币种边界。
3. 不跨币种相加；`exchange_rate_applied=false` 表示没有做任何汇率换算。
4. 只有独立 Ledger Profile 的当前工具目录明确包含 `ledger.records.draft` 时，用户要求保存后才可
   调用它，并原样使用用户给出的金额事实。
5. 在统一 Shadow Nexus Profile 中该工具不可用：不要尝试写入或声称已创建草稿；普通对话引导
   用户切换到“记一下”，Capture 分析则只按上层请求返回结构化 Proposal，等待 Nexus Review。
6. 实际创建草稿后返回引用和 `draft` 状态，说明它尚未成为正式账目。

## 安全边界

- 不索取或推断账户、卡号、支付凭据、余额、转账来源或汇率；这些都不属于 Ledger 合同。
- 工具返回 401、403 或 404 时停止，不猜测其他用户、授权或资源标识。
- 不从零散文字臆造金额、币种、时间或分类；缺失关键事实时先向用户询问。
- 不把最小账目列表描述成银行流水或账户余额，也不声称已经正式入账、导出或付款。
- 相同草案重试保持请求体不变；运行时会提供幂等键，避免重复创建。
- Skill 只能使用当前工具目录实际列出的能力，不能调用普通浏览器 API 完成确认、撤销或导出。

字段和草案边界见 [references/record-boundaries.md](references/record-boundaries.md)。
