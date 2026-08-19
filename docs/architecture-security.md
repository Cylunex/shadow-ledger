# 技术架构、安全与部署

## 1. 推荐技术栈

```text
Python 3.12
FastAPI + Pydantic
SQLAlchemy 2 + Alembic
PostgreSQL 16+
Jinja2 + HTMX（渐进增强）
Tailwind 构建产物（生产无 Node 运行时）
Nginx
```

理由：延续现有 Shadow 服务的运维和 OIDC/Asset 模式；服务端渲染适合手机 WebView 与低复杂度
表单；PostgreSQL 同时承担事实存储、全文搜索基础、Outbox 和个人规模任务队列。

v1 不引入 Redis、Kafka、Elasticsearch、对象存储 SDK 或独立前端 SPA。达到明确容量瓶颈后再以
测量结果决定拆分。

## 2. 进程边界

```text
ledger-web
├── HTML 页面
├── /api/v1
├── OIDC callback / Session
└── 健康检查

ledger-worker
├── Capture 解析
├── Outbox 投递
├── 周期提醒
├── 导入/导出
└── 对账任务

PostgreSQL
└── 领域事实 + Session + Job/Outbox
```

Web 和 Worker 使用同一代码包但不同启动入口。Worker 通过 `FOR UPDATE SKIP LOCKED` 领取任务，
每个 Job 有幂等 key、最大尝试次数、退避和稳定错误码。

## 3. 模块边界

```text
app/
├── domains/
│   ├── records/          LedgerRecord + MoneyEntry
│   ├── consumption/      Event/Line/Merchant/Item
│   └── planning/         Commitment/Intent/Reminder
├── capture/              parser ports 与草稿编排
├── integrations/         identity/asset/health/travel/foliant
├── web/                  页面、表单与 ViewModel
├── api/                  v1 transport
├── jobs/                 PostgreSQL worker
└── infrastructure/       db、session、outbox、logging
```

领域层不 import FastAPI、HTTP client、OIDC SDK 或具体 AI SDK。解析器、Asset 和跨项目调用通过 Port
接口注入。

## 4. 数据库与事务

- 单一 PostgreSQL 数据库，独立数据库用户，最小 schema 权限；
- 所有时间为 TIMESTAMPTZ，同时在 Record/计划保存解释本地时间所需的 IANA timezone；
- 金额为 NUMERIC，Python 使用 Decimal；
- 草稿确认、撤销、Intent 完成以及 Outbox 写入同事务；
- 跨服务不使用分布式事务；
- 写接口使用 revision 乐观锁，关键状态转换再 `SELECT FOR UPDATE`；
- Migration 只向前发布，破坏性列删除经过“停止写入 → 迁移数据 → 延迟删除”三阶段。

## 5. OIDC 与浏览器 Session

Shadow Identity 是唯一 issuer，不在 NAS 部署第二套认证数据库。

```text
Authorization Code + PKCE S256
state 一次性存储 + Host-only 事务 Cookie 绑定浏览器
nonce、iss、aud、exp、iat、签名严格校验
JWKS 按短 TTL 缓存，kid 未命中仅强制刷新一次
userinfo subject 必须与 ID Token 一致
```

Ledger 支持部署配置中明确列出的多个 HTTPS 入口，例如 canonical 和 NAS HTTPS alias。每个入口有
精确注册的 callback；登录事务保存实际选择的 redirect URI，token exchange 必须使用同一值。
Host 只能从可信 allowlist 选择，不能根据任意 `Host`/`X-Forwarded-Host` 构造 callback。

浏览器 Session：

- Cookie 名使用 `__Host-` 前缀，Secure、HttpOnly、SameSite=Lax、Path=/；
- Cookie 只含随机 handle，数据库只保存 SHA-256；
- Session 可撤销、有绝对 TTL 和最后活动时间；
- 登录事务 10 分钟过期且只能消费一次；
- 登出先撤销 Ledger Session，再跳统一登出；
- owner identity 由 `(issuer, subject)` 稳定映射，显示名和邮箱不是主键。

## 6. 服务鉴权

- 服务 Token 原文只存在受限文件；数据库/配置保存哈希和 scope；
- Bearer 只用于 `/api/v1` 的非浏览器调用，不建立 Web Session；
- 日志不记录 Authorization、Cookie、Upload Token、OIDC code/state/nonce；
- Token 支持双 key 轮换窗口和最后使用审计；
- v1 默认不授予任何客户端确认正式事实的 scope。

## 7. Web 安全

- Cookie 写请求同时校验 Origin 与 CSRF token；
- HTMX 请求不成为 CSRF 例外；
- CSP 默认 `default-src 'self'`，图片只允许受控 Asset 入口；
- 禁止内联第三方脚本和远程字体；
- 输出统一 HTML escaping，Markdown 使用严格 allowlist sanitizer；
- 登录、capture、导入、导出和搜索分别限流；
- 请求体、文本长度、明细数量、JSON 深度和导入行数有上限；
- 任何客户端提供的 URL、URI、文件名和 MIME 都视为不可信输入；
- 外部跳转只能来自 Platform Catalog 的受控解析结果。

## 8. Capture 与 AI 安全

- OCR/LLM 输入是数据，不是指令；解析任务不具备调用业务写工具的权限；
- 解析器只能输出 schema 校验后的候选；
- prompt、provider、model、parser version 和时间进入来源元数据；
- 不把 OIDC/服务凭据、数据库 DSN 或其他用户记录放入提示词；
- 外部模型启用、Base URL 和 Key 属于仓库外配置；
- Provider 返回的 URL 不自动访问，防止 SSRF；
- 解析原文和 payload 在日志中只记录 ID、大小和稳定错误码。

## 9. Asset 安全

- Ledger 服务凭据只能创建/完成本 app 的 Asset 和 Reference；
- 浏览器获得的 Upload Token 短时、单 session、受大小和 MIME 限制；
- alternate target 只能来自 Platform 配置，必须 HTTPS；
- CORS 使用精确 Origin，不允许 `*`；
- 上传完成后校验实际大小、MIME、哈希和图片解码；
- 访问使用短时 grant，页面不持久化签名 URL。

## 10. 配置与秘密

仓库只提供键名和示例值。生产秘密建议放在：

```text
/data/project/.secrets/shadow-ledger/
├── oidc-client-secret
├── database-url
├── asset-service-token
├── service-token-hashes
└── capture-provider-key        # 仅启用外部解析器时
```

运行配置至少包括：环境、数据库 secret file、OIDC issuer/client/callback allowlist、Session TTL、
Asset base URL/token file、默认币种、默认时区、上传限制和可信代理。真实域名、端口和内网地址不进
Git。

## 11. 部署拓扑

```text
公网浏览器
→ 云端 HTTPS Nginx
→ 受限隧道
→ NAS HTTPS/HTTP Nginx
→ ledger-web (127.0.0.1)

局域网浏览器 / Shadow App
→ NAS HTTPS 映射端口
→ NAS Nginx
→ ledger-web

ledger-worker + PostgreSQL + 文件外秘密
→ NAS

Asset
→ NAS，云端 canonical + NAS HTTPS alternate
```

应用只监听回环。Nginx 清空客户端提交的 Remote-* 和内部身份头。机器 API 仍由 Ledger 自己验证
Bearer，不能依赖云端 Forward Auth。

## 12. 探活与可观测性

```text
/healthz   仅进程存活，不查数据库和外部服务
/readyz    数据库可用、迁移版本兼容
/metrics   仅内网或受控抓取
```

结构化日志字段：request_id、actor_type、route、status、duration、aggregate_id、job_type、
error_code；不得记录自由文本、金额明细、原始商家、Cookie 或 Token。

指标：请求延迟/错误率、DB pool、草稿数量、Capture 成功率与延迟、Job backlog、Outbox 重试、
提醒延迟、Asset/引用对账异常。

## 13. 备份与恢复

- PostgreSQL 每日逻辑备份 + 定期物理/快照备份；
- Ledger 备份不复制 Asset 字节，但导出 Asset ID/Reference 清单；
- Asset 按 Platform 独立策略备份；
- OIDC Session 库可丢弃，领域数据库不可；
- 密钥备份单独加密，不进入数据库 dump；
- 每个发布阶段执行一次空库迁移和最近备份恢复演练；
- 恢复后运行 Record/子对象不变量、Asset 引用和 Outbox 对账。

## 14. 容量假设

个人部署目标：数十万 Record、百万以内 ConsumptionLine、单用户为主。所有列表必须分页；洞察按
月和 owner 建索引，慢查询测量后再增加物化汇总。首版不为假设中的超大规模提前拆服务。
