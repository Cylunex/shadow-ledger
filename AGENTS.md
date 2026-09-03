# Shadow Ledger 开发约束

本仓库当前处于设计冻结阶段。开始实现前先阅读 `docs/handoff.md` 以及它列出的设计文档。

## 不可破坏的产品边界

- Ledger 是低负担的个人收支、消费记忆与规划系统，可记录轻量支付方式（ADR 0008），不追踪账户余额、银行账号或转账。
- 不引入 Account、Posting、借贷方向、复式记账、对账或 PaymentAllocation。
- `MoneyEntry.amount` 是最终金额事实；消费明细不要求与它对平。
- MoneyEntry 与 ConsumptionEvent 是同一记录聚合的可选组成，状态只保存在聚合根。
- 周期计划只能产生提醒或草稿，不能自动生成已确认事实。
- 原始商家、渠道、商品文本和采集原文不得被规范化结果覆盖。
- 文件只进入 Shadow Asset；跨项目对象只用 `shadow://` URI 引用。

## 实现纪律

- UseCycle、确定性 Forecast、自动抓单和 MCP 已按 ADR 0006 解冻；后续仍不得把预测、周期任务或
  外部来源直接确认为消费事实。
- 所有写接口必须支持幂等或显式版本冲突检测；草稿确认、撤销在单个数据库事务中完成。
- 金额使用十进制定点数，禁止浮点数。
- 真实域名、内网地址、端口、用户和密钥只放在仓库外配置中。
- OIDC 使用 Shadow Identity 唯一 issuer、Authorization Code + PKCE 和本地不透明 Session。
- 修改已冻结的产品边界时，先新增 ADR，再同步数据模型、API、页面和实施计划。
