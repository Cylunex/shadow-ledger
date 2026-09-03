# v1 实施计划

## 1. 交付原则

- 按纵向可用闭环交付，不先创建所有未来表和空页面；
- 每阶段包含迁移、领域规则、API、页面、测试和文档；
- 先确定性解析，后接 AI；先事实积累，后做 Forecast；
- 每阶段结束都能安全部署或回滚，不跨多个阶段积累不可运行改动；
- 不因“以后可能需要”引入 Account、Posting、Allocation 或独立消息中间件。

## 2. Phase 0：工程与身份地基

交付：

- Python/FastAPI 项目骨架、配置、结构化日志和 request ID；
- PostgreSQL、SQLAlchemy、Alembic 与测试数据库；
- LedgerRecord 基础迁移和 owner identity；
- 原生 OIDC Code + PKCE、多入口精确 callback allowlist；
- 本地不透明 Session、CSRF、CSP、健康检查；
- Web/Worker 两个启动入口和 PostgreSQL Job/Outbox 基础；
- 示例配置、Supervisor/systemd 与 Nginx 模板（全部脱敏）。

验收：

- 未登录页面进入唯一 issuer，回调严格校验并建立 host-only Session；
- 本地 HTTPS alias 与 canonical 各自使用精确 callback；
- 无 `ledger-users` 返回 403；伪造 Remote-* 无效；
- `/healthz` 不访问 DB，`/readyz` 能反映 DB 故障；
- 秘密文件缺失时 fail closed，日志无 secret/token/cookie/state。

## 3. Phase 1：Record 核心闭环

交付：

- LedgerRecord、MoneyEntry、MoneyCategory、ConsumptionEvent/Line 迁移；
- 创建、读取、修改、确认、撤销、补金额和退款 API；
- revision/ETag、Idempotency-Key、审计和 Outbox；
- 时间线与 Record 详情的最小页面；
- JSON 完整导出基础。

验收：

- money-only、金额消费、金额未知消费三种 Record 均可创建确认；
- 状态只有一份且只允许 draft→confirmed→voided；
- Event.money_entry_id 唯一且属于同 Record；
- 并发修改稳定返回 409；
- voided 不参与统计，refund 不覆盖原支出；
- Decimal 往返不丢精度，任何路径不使用 float。

## 4. Phase 2：低负担录入与基础洞察

交付：

- 手机优先首页、金额键盘、最近建议、复制上次；
- 确定性自然语言金额/日期/类型/场景解析，原文进入 CaptureSource；
- 草稿箱和确认页；
- 分类管理、筛选、搜索；
- 月度汇总、分类趋势和场景频率；
- 响应式、可访问性和 Shadow App WebView 适配。

验收：

- 普通金额记录只需金额和一次提交；
- 解析失败不阻塞记录；原文始终可追溯；
- 金额未知消费只参与频率，不参与金额统计；
- 390px 手机和桌面无横向溢出，键盘/读屏可完成核心录入；
- 网络失败保留表单并使用同一幂等 key 重试。

## 5. Phase 3：Asset、截图与导入

交付：

- Asset upload init/complete、局域网 HTTPS 优先和 canonical 回退；
- AssetBinding/Reference Outbox 与对账；
- OCR/可选 AI parser port、Capture Job、失败重试；
- 截图/小票草稿确认；
- CSV/JSON 预览，以及京东、淘宝、美团、饿了么 Markdown 账单映射、去重和批量草稿；
- CSV 导出及完整导出 Job。

验收：

- Ledger 不保存文件字节和 Upload Token；
- CORS 只允许配置中的精确 Origin；
- Asset 完成后 Ledger 失败可通过对账恢复；
- OCR 明细与总金额不一致仍可确认；
- 重试解析不覆盖 confirmed 事实；
- 相同 external ID/Idempotency-Key 不重复导入。

## 6. Phase 4：消费身份与计划

交付：

- Merchant、Alias、ItemIdentity、Alias；
- 原始商家、渠道、明细文本与规范身份并存；
- 合并建议、接受/拒绝、canonical 重定向；
- SpendingIntent 全流程和从 Intent 创建草稿；
- 可选的 BudgetTarget 与月度目标进度；
- RecurringCommitment、Reminder、周期 Job 和生成草稿；
- 消费页与计划页。

验收：

- 自动化不能永久合并身份；拒绝建议不会反复出现；
- 原始文本不因改名/合并丢失；
- 周期 Job 重复执行只产生一个 occurrence Reminder；
- Commitment 永不自动创建 confirmed MoneyEntry；
- Intent 只有与 confirmed Record 同事务关联后才 completed。

## 7. Phase 5：跨项目、完善洞察与上线加固

交付：

- ExternalReference 和 Platform URI resolver；
- Health 草稿、Travel Place/Visit/Trip、Foliant 摘要的适配器合同；
- 商家/内容频率、消费间隔和简单事实型提示；
- 备份恢复、运行指标、限流、隐私日志检查；
- NAS 部署、云端 canonical、局域网 HTTPS alias；
- 用户验收和操作手册。

验收：

- 外部服务不可用不阻塞 Ledger 本地确认；
- 跨服务写失败进入 Outbox 并可幂等恢复；
- 不自动把购买变成 Health 摄入；
- Foliant 持仓/行情不复制到 Ledger；
- 最近备份可恢复，恢复后不变量和 Asset 引用对账通过；
- Platform capability lifecycle 可接收 Ledger observed evidence，隔离恢复演练可生成 restore-tested evidence；
- 生产日志采样确认无自由文本、金额详情和凭据泄露。

## 8. Phase 6：使用周期、确定性预测、MCP 与自动 intake

已按 ADR 0006 解冻并交付：

- 显式 UseCycle 状态、API、页面和版本冲突保护；
- 带完整输入快照与输入/输出哈希的确定性 Forecast；
- 官方 SDK v2 stdio MCP，默认只读、显式开关后只增加草稿；
- 结构化 Webhook 与受控目录 intake，原文保留、external ID 幂等、凭据拒绝；
- 所有自动路径继续只产提醒、建议或 draft。

验收重点是历史 as_of 不读取未来消费、同输入得到相同 run/output hash、MCP 工具列表不含确认和
导出、目录/Webhook 重放不重复建草稿。

## 9. 轻量支付方式（ADR 0008）

可选的受控方式标签贯通存储、API、导入导出、MCP/Agent 和页面。重点验收未知留空、旧客户端
不清空、幂等兼容、版本冲突、金额不变和权限边界。历史修正仅使用明确证据，不运行备注猜测迁移。
不引入账户或混合支付金额分摊。

## 10. 后续门槛

只有满足相应数据与需求门槛后才开始：

| 能力 | 开始条件 |
|---|---|
| 机器学习 Forecast | 确定性基线有足够回测数据，并定义准确率/误报评估 |
| Agent 正式写入 | read + draft 插件稳定，ConfirmationReceipt 与正式写入风险测试通过 |
| 新来源适配器 | 有稳定合法来源、幂等 external ID 和明确维护成本 |
| 高级预算 | 简单分类月度统计无法满足实际决策需求 |

## 11. 测试策略

### 单元/性质测试

- 状态机和 Record 不变量；
- Decimal 汇总、退款和币种隔离；
- recurrence 时区、月末和夏令时；
- 文本规范化不覆盖原始值；
- URI、return_to、filename、MIME 和 Origin 校验。

### 数据库集成测试

- 所有唯一/外键/检查约束；
- 并发确认、revision 冲突和 Job 竞争领取；
- 同事务 Audit/Outbox；
- migration upgrade、空库和生产前一版本升级。

### HTTP/安全测试

- OIDC state 浏览器绑定、PKCE、nonce、issuer、audience、签名和过期；
- CSRF、CORS、CSP、限流、开放重定向和 Host header；
- Service Token scope、轮换和日志脱敏；
- Idempotency-Key 重放与 payload mismatch。

### 端到端测试

- 金额快速记录到时间线；
- 金额未知消费后补金额；
- 截图直传→解析→草稿→确认；
- 退款、撤销、周期提醒→草稿；
- Asset/Health/Travel 上游故障恢复；
- Android WebView 与普通浏览器关键路径。

## 12. Definition of Done

一个阶段只有同时满足以下条件才算完成：

- 领域不变量有数据库约束或事务测试；
- API、页面、迁移、失败恢复和文档同步；
- 自动测试通过且没有跳过关键安全用例；
- 生产配置示例不含真实入口或秘密；
- 部署前备份，部署后探活和核心负向测试通过；
- 只提交本阶段相关文件，中文提交说明包含验证结果；
- 未经明确要求不构建 Android APK、不推送额外仓库。
