# Shadow Ledger

> 以消费为中心的个人收支记录与规划系统。

Shadow Ledger 记录三类事实：发生了多少金额、这次消费是什么、未来可能或计划发生什么。
它不是传统账户账本，不处理银行卡余额、支付方式、转账、复式记账和对账。

```text
Money Log             收了、花了、退了多少
Consumption           买了什么、在哪里、体验如何
Planning & Insights   以后想买什么、哪些周期事项将到期
```

## 当前状态

本仓库目前只包含已冻结的 v1 产品与技术设计，不包含可运行应用。后续开发会话应从
[`docs/handoff.md`](docs/handoff.md) 开始，并按实施阶段逐步落地。

## 设计索引

| 文档 | 内容 |
|---|---|
| [`docs/product-boundary.md`](docs/product-boundary.md) | 产品定位、范围、术语和跨项目边界 |
| [`docs/domain-model.md`](docs/domain-model.md) | 聚合、实体、状态、不变量和数据库约束 |
| [`docs/schema.sql`](docs/schema.sql) | 供实现参考的 PostgreSQL 逻辑 DDL |
| [`docs/workflows.md`](docs/workflows.md) | 快速录入、截图草稿、退款、周期计划等流程 |
| [`docs/api-contract.md`](docs/api-contract.md) | HTTP API、幂等、并发和错误合同 |
| [`docs/ui-design.md`](docs/ui-design.md) | 页面结构、移动端交互和状态展示 |
| [`docs/integrations.md`](docs/integrations.md) | Asset、Health、Travel、Foliant、Platform 接口 |
| [`docs/architecture-security.md`](docs/architecture-security.md) | 技术架构、认证、安全、任务与部署原则 |
| [`docs/implementation-plan.md`](docs/implementation-plan.md) | 分阶段实现、测试与验收门槛 |
| [`docs/handoff.md`](docs/handoff.md) | 下一开发会话的执行入口 |

## 冻结结论

```text
项目：Shadow Ledger
App ID：ledger
资源 URI：shadow://ledger/...
首版导航：记录 / 消费 / 计划 / 洞察
默认币种：由部署配置决定，个人部署默认 CNY
```

真实入口、NAS 地址、端口、账号和密钥不属于仓库内容。
