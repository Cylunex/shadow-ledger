# Shadow Ledger

Shadow Ledger 是以消费为中心的个人收支记录与规划系统。它同时保存金额事实和消费记忆，关注
“花了多少钱、买了什么、体验如何、以后是否还会发生”，但不做专业会计或银行卡对账。

## 理念

- 金额事实与消费语义统一在一个可确认、可撤销的记录聚合中；
- 原始商家、渠道和商品文本始终保留，规范化结果可以修正；
- 周期计划只生成提醒或草稿，不自动伪造真实消费；
- 与 Foliant、Health、Travel 通过稳定 URI 和最小数据交换协作。

## 主要功能

- 支出、收入、退款和金额未知的消费记录；
- 消费明细、商家与商品身份；
- 时间线、消费、计划和洞察页面；
- Spending Intent、周期事项和分类目标；
- 显式使用周期，以及可解释、可回算的周期/复购预测；
- CSV/JSON 导入导出，京东、淘宝、美团、饿了么 Markdown 账单导入；
- Asset 附件、解析草稿与来源级幂等去重；
- 统一待处理、部分退款候选、来源原文对照／版本观察与可撤销商家规则；
- 显式选中批量确认、URL 筛选、跨运行建议反馈与消费记忆；
- 可选 7 天本机文本草稿队列（只同步草稿，不离线确认）；
- 通过稳定引用把消费凭证交给 Archive，不复制文件；
- OIDC、审计、Outbox 和后台 Worker。
- 只读优先的 stdio MCP，以及只创建草稿的结构化 Webhook/受控目录抓单。
- Agent v2 任务目录、确定性数字凭证、对话草稿和一次性精确审批／执行 Receipt；远程 MCP 默认关闭。

## Nexus 快捷操作

Ledger 向 Nexus 数据面板提供支出、收入、退款和净支出摘要，并提供快速记支出、记收入动作。
动作复用现有 Draft/Review 协议，不引入账户、分录或余额模型；金额事实、业务校验、幂等和审计
仍由 Ledger 所有。

## 本地开发

```bash
uv sync --all-groups
uv run alembic upgrade head
uv run ledger-web
```

MCP 与自动抓单的配置和安全边界见
[MCP 与自动抓单](docs/mcp-and-intake.md)。两者默认关闭写入；任何自动来源只会创建草稿。

实际数据库、OIDC、Session 和 Asset 凭据仅通过被忽略的本地配置或秘密文件提供。

## 文档

- [Agent 1.3 优化方案、研究来源与兼容门槛](docs/agent-optimization-2026-09.md)

- [产品边界](docs/product-boundary.md)
- [领域模型](docs/domain-model.md)
- [API 合同](docs/api-contract.md)
- [架构与安全](docs/architecture-security.md)
- [跨项目集成](docs/integrations.md)
- [Shadow Agent 插件接入](docs/agent-plugin.md)
- [MCP 与自动抓单](docs/mcp-and-intake.md)
- [通用运行说明](docs/operations.md)
- [消费事实导入复核与运行证据](docs/import-review-and-evidence.md)
- [1.2 消费闭环优化与验收范围](docs/optimization-implementation.md)
