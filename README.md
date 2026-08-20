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
- CSV/JSON 导入导出、Asset 附件与解析草稿；
- OIDC、审计、Outbox 和后台 Worker。

## 本地开发

```bash
uv sync --all-groups
uv run alembic upgrade head
uv run ledger-web
```

实际数据库、OIDC、Session 和 Asset 凭据仅通过被忽略的本地配置或秘密文件提供。

## 文档

- [产品边界](docs/product-boundary.md)
- [领域模型](docs/domain-model.md)
- [API 合同](docs/api-contract.md)
- [架构与安全](docs/architecture-security.md)
- [跨项目集成](docs/integrations.md)
