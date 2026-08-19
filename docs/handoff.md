# 开发交接

## 1. 当前状态

仓库目前是 **Shadow Ledger v1 的完整设计基线**，没有应用骨架、依赖锁文件、数据库迁移或可运行
服务。下一会话应从 Phase 0 开始实现，不要把本仓库误认为半成品应用，也不要一次性生成所有阶段。

设计阅读顺序：

1. [`product-boundary.md`](product-boundary.md)：先确认做什么和不做什么；
2. [`domain-model.md`](domain-model.md) 与 [`schema.sql`](schema.sql)：实现聚合和不变量；
3. [`workflows.md`](workflows.md)：按完整流程理解事务边界；
4. [`api-contract.md`](api-contract.md) 与 [`ui-design.md`](ui-design.md)：实现 transport 和页面；
5. [`integrations.md`](integrations.md) 与 [`architecture-security.md`](architecture-security.md)：接入 Platform；
6. [`implementation-plan.md`](implementation-plan.md)：按 Phase 0—5 交付。

## 2. 已冻结决定

- 产品是以消费为中心的个人收支记录与规划，不是传统账户账本；
- 不建立 Account、Posting、PaymentAllocation、余额、转账和对账；
- `LedgerRecord` 只是内部聚合根，不增加用户理解成本；
- 状态唯一存放在 Record，且只能 `draft → confirmed → voided`；
- `MoneyEntry.amount` 是金额真相，始终为正数；类型决定收入、支出或退款；
- `ConsumptionEvent.money_entry_id` 是可空唯一关联，金额未知时允许为空；
- 消费明细不强制与最终金额对平；
- 周期事项只能提醒或生成草稿，不能自动生成 confirmed 事实；
- 原始商家、渠道、商品和解析文本必须保留；
- 文件字节全部由 Asset 管理，Ledger 只保存业务绑定；
- UseCycle、Forecast、Agent/MCP 不属于 v1 首轮实现；
- BudgetTarget 仅是可选月度目标，不表示余额、资金预留或结转。

若实现需要改变上述内容，应先新增 ADR 并在开发前确认，不要通过迁移或页面细节悄悄改变边界。

## 3. 下一会话的第一个任务

只实施 [`implementation-plan.md`](implementation-plan.md) 的 Phase 0：

```text
工程骨架
→ 配置与秘密文件加载
→ PostgreSQL/Alembic
→ OIDC + 本地 Session
→ Web/Worker 启动入口
→ healthz/readyz
→ 最小安全与测试基线
```

Phase 0 可以创建 `ledger_records` 的最小迁移验证数据库链路，但不要提前实现消费、计划、洞察、
Asset 上传或 Agent。每个后续阶段单独形成可部署、可验证的纵向闭环。

## 4. 需要在部署时注入、不能写入仓库的值

- canonical 与 NAS HTTPS 入口及精确 callback；
- PostgreSQL DSN 和独立数据库用户密码；
- OIDC client secret、Asset service token、服务 token；
- 外部 Capture/OCR/LLM provider、base URL 和 key；
- 默认币种、时区、上传上限和可信代理网段。

仓库可以提供 `.example` 键名，但示例必须使用保留域名和无效占位值。不要从其他 Shadow 仓库复制
真实 `.env`、域名、端口、IP、Cookie、证书或密钥。

## 5. 实现纪律

- 以领域规则驱动迁移和测试，不把 [`schema.sql`](schema.sql) 直接当生产 migration 执行；
- 跨表 owner、一对一、状态转换等不变量必须同时有数据库约束和服务测试；
- 创建接口实现 Idempotency-Key，状态写入实现 revision/ETag；
- 确认、撤销、Intent 完成和 Outbox 写入必须位于单一数据库事务；
- 外部失败不能回滚本地已确认事实，使用 Outbox/Job 重试与对账；
- 解析、OCR 和 AI 只生成候选草稿，不能拥有确认 scope；
- 每个阶段同步更新设计、ADR、迁移说明和运维说明。

## 6. Phase 0 完成检查

```text
[ ] 空数据库可完整迁移，重复启动不修改 schema
[ ] OIDC state/nonce/PKCE/issuer/audience/callback 均有正反向测试
[ ] canonical 与 NAS alias 都只能命中各自 allowlist callback
[ ] Session、CSRF、CSP、可信代理与日志脱敏测试通过
[ ] Web 与 Worker 可独立启动和优雅停止
[ ] healthz 不依赖外部服务，readyz 正确反映数据库状态
[ ] 秘密缺失时 fail closed，仓库与构建产物均不含真实秘密
[ ] README 给出脱敏的本地测试和部署入口
```

Phase 0 验收后，再进入 Record 核心闭环。不要在缺少实际数据前实现 UseCycle、Forecast 或复杂预算。
