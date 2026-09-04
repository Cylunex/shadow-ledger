# 开发交接

## 1. 当前状态

Shadow Ledger v1 已从冻结设计基线实现为可运行的 Python 3.12/FastAPI 应用。Phase 0—5 的领域模型、
HTTP 合同、OIDC/Session、安全中间件、Web 页面、Worker、迁移、部署模板与自动测试已落地。

实现入口：

```text
app/main.py                 Web、middleware、health/readiness
app/api.py                  /api/v1 合同
app/services/records.py     Record 聚合与事务规则
app/models.py               SQLAlchemy v1 模型
app/oidc.py                 Code + PKCE 与不透明 Session
app/integrations.py         Asset 与 Capture provider ports
app/worker.py               PostgreSQL Job/Outbox/Reminder
app/machine.py              Shadow Agent read + draft 机器 API
app/services/import_review.py 导入复核、可撤销商家规则与质量指标
app/operational_evidence.py Platform observed evidence 构建
app/services/forecast.py   可重放的确定性预测
app/services/intake.py     Webhook/目录统一 draft intake
app/mcp_server.py          官方 SDK stdio MCP
shadow-plugin.yaml          运行时无关插件 Definition
migrations/                 Alembic 历史
scripts/                    observed evidence 与隔离恢复验证入口
tests/                      领域、HTTP、安全、OIDC、PostgreSQL 并发与前端测试
app/cli/                    wheel 可用的迁移与运行证据命令
```

本地开发、验证和生产操作见仓库根 README 与 [`operations.md`](operations.md)。

## 2. 保持冻结的边界

- 不引入 Account、Posting、PaymentAllocation、余额、转账或对账；
- MoneyEntry.amount 仍是 Decimal 最终金额事实，消费明细不要求对平；
- Record 持有唯一 `draft → confirmed → voided` 状态；
- 原始商家、渠道、商品和 Capture 原文不会被规范身份覆盖；
- 周期事项只创建 Reminder 或 draft，从不自动确认；
- 文件字节只进入 Asset；跨项目对象只保存 `shadow://` URI；
- Agent 对模型仅开放最小披露读取和可撤销草案；Nexus 用户审核后的隐藏正式入账见 ADR 0004；
  隐藏 Nexus Review 可按 ADR 0007 在同一草稿保留原始商家、场景、消费明细和证据引用，但模型
  可见 draft 与 stdio MCP 仍为 money-only；
  UseCycle、Forecast、MCP 与自动抓单已按 ADR 0006 实现，且所有自动化仍只创建建议或草稿。

改变这些内容前必须新增 ADR，并同步模型、迁移、API、页面和实施文档。

## 3. 部署时必须注入

- PostgreSQL DSN 和独立用户密码；
- OIDC issuer/client secret 与每个入口的精确 HTTPS callback；
- Session secret、Asset service token、服务 Token 哈希；
- 可选 Capture/OCR provider URL/key 与 Platform resolver；
- 默认币种、时区、上传上限、允许 Origin 和可信代理。

真实域名、内网地址、端口、用户、Cookie、证书和密钥不得进入仓库。

## 4. 轻量支付方式（ADR 0008）

已增加可空 `MoneyEntry.payment_method`、迁移 0006、API/MCP/Agent 筛选与草稿、导入导出、页面
展示与修正。旧数据默认留空，省略更新字段保留原值；不追踪银行账号、余额、还款或支付分摊。

## 5. 后续维护优先级

1. 在实际 Shadow Identity/Asset 测试环境完成契约联调；
2. 使用 PostgreSQL 16 执行空库与上一发布版本迁移演练；
3. 接入生产备份、指标抓取和告警；
4. 以确定性 Forecast 作为基线积累回测数据，不提前引入机器学习或自动确认。

当前版本为 1.2.0，迁移头为 0007。生产升级前执行最新迁移，并在仓库外提供 MCP/intake owner
文件和最小权限配置，不允许从早期 0005 状态直接启动新应用。

## 6. 消费工作台优化（ADR 0009）

实现、操作入口、格式范围、反馈语义与验收边界见
[optimization-implementation.md](optimization-implementation.md)。修改这一部分前同时阅读
[ADR 0009](decisions/0009-consumption-workbench.md)。

新增来源观察、跨运行反馈、统一待处理、部分退款候选、显式批量选中、消费记忆、建议回看、
文本本机队列与模块化前端。旧 Record/URI 和金额不迁移重写。本地隔离 PostgreSQL 已做迁移／并发
验收；NAS 实际发布／恢复与支付宝、微信实际导出样本仍是外部验收门槛。
