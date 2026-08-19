# Shadow Ledger

> 以消费为中心的个人收支记录与规划系统。

Shadow Ledger 记录金额事实、消费记忆和未来计划。它不是账户账本，不处理银行卡余额、支付方式、
转账、复式记账或对账。

## 已实现能力

- 支出、收入、退款，以及金额未知的消费记录；
- 单一 `LedgerRecord` 状态机、Decimal 金额、revision/ETag、幂等创建、审计与 Outbox；
- 原始商家、渠道、商品文本与规范 Merchant/Item Identity 并存；
- 确定性文本解析、Asset 直传适配器、OCR/AI draft-only parser port、CSV/JSON 导入；
- Spending Intent、周期事项、提醒、月度总额/分类目标；
- 时间线、消费、计划和洞察四个移动端优先页面；
- JSON/CSV 导出任务合同、`shadow://` 跨项目引用、PostgreSQL Worker；
- Shadow Identity OIDC Code + PKCE、本地不透明 Session、CSRF、CSP 与服务 Token scope；
- Alembic、结构化日志、health/readiness、systemd/Supervisor/Nginx 部署模板。

UseCycle、Forecast、Agent/MCP、自动抓单和高级预算仍按冻结设计留在 v2 门槛之外。

## 本地开发

需要 Python 3.12 和 `uv`。下面的配置只使用本地 SQLite 和开发身份，不包含真实部署信息：

```bash
UV_CACHE_DIR=/tmp/shadow-ledger-uv-cache uv sync --all-groups

export LEDGER_ENV=test
export LEDGER_DATABASE_URL=sqlite+pysqlite:////tmp/shadow-ledger-dev.sqlite3
export LEDGER_OIDC_CLIENT_SECRET=local-invalid-secret
export LEDGER_SESSION_SECRET=local-invalid-session-secret
export LEDGER_ALLOWED_ORIGINS='["http://127.0.0.1:8000"]'
export LEDGER_DEV_AUTH=true

uv run alembic upgrade head
uv run ledger-web
```

另一个终端启动任务进程：

```bash
uv run ledger-worker
```

开发身份模式在 `production` 环境会拒绝启动。生产必须通过文件注入数据库、OIDC、Session、Asset
和服务 Token 秘密；键名见 [`.env.example`](.env.example)。

## 验证

```bash
uv run ruff check app migrations tests
uv run ruff format --check app migrations tests
uv run pytest
uv run alembic upgrade head
```

`/healthz` 只检查进程，`/readyz` 检查数据库。Swagger 合同在运行时可从 `/docs` 查看。

## 部署

1. 创建独立 PostgreSQL 数据库用户和文件外秘密；
2. 复制 `.env.example` 的键名到仓库外环境文件，填入精确 HTTPS callback/origin allowlist；
3. 运行 `alembic upgrade head`；
4. 分别启动 `ledger-web` 与 `ledger-worker`；
5. 使用 [Nginx 示例](deploy/nginx.conf.example)清空 `Remote-*` 并反代到回环监听；
6. 部署后检查 `/healthz`、`/readyz`、登录负向用例、Worker backlog 与最近备份恢复。

服务模板位于 [`deploy/`](deploy/)。真实域名、NAS 地址、端口、账号和密钥不得写入仓库。

## 设计与运维文档

完整冻结设计仍是实现合同：

| 文档 | 内容 |
|---|---|
| [`docs/product-boundary.md`](docs/product-boundary.md) | 产品定位、范围和跨项目边界 |
| [`docs/domain-model.md`](docs/domain-model.md) | 聚合、实体、状态和不变量 |
| [`docs/api-contract.md`](docs/api-contract.md) | HTTP API、幂等、并发和错误合同 |
| [`docs/architecture-security.md`](docs/architecture-security.md) | 架构、认证、安全和部署原则 |
| [`docs/implementation-plan.md`](docs/implementation-plan.md) | v1 Phase 0—5 验收基线 |
| [`docs/operations.md`](docs/operations.md) | 配置、迁移、启动、备份与故障恢复 |
| [`docs/handoff.md`](docs/handoff.md) | 当前实现状态和后续维护入口 |
