# 运维手册

## 1. 进程与依赖

生产运行两个独立进程：`ledger-web` 提供 HTML/API/OIDC，`ledger-worker` 处理 Capture、Outbox、
提醒和导出任务。二者共享 PostgreSQL，但不共享内存状态。Asset、Identity、Platform Resolver 和可选
解析 Provider 均为外部依赖；它们失败时已确认 Ledger 事实不会回滚。

## 2. 秘密与配置

使用仓库外只读文件注入以下值：数据库 URL、OIDC client secret、Session secret、Asset service
token、服务 Token 哈希和可选 Capture provider key。生产启动会对缺失数据库/OIDC/Session secret、
非 HTTPS issuer、空 callback/origin allowlist 和 `dev_auth=true` 执行 fail closed。

callback 必须是精确 HTTPS URL；canonical 与 LAN alias 分别登记，不根据任意 Host 或
`X-Forwarded-Host` 动态拼接。Nginx 必须覆盖 `Host`/`X-Forwarded-Proto` 并清空客户端提供的
`Remote-*`。

## 3. 发布与迁移

发布前执行数据库逻辑备份，再运行：

```bash
uv run alembic upgrade head
uv run ruff check app migrations tests
uv run pytest
```

迁移只向前应用。应用启动不会隐式修改 schema；重复 `upgrade head` 必须为空操作。Web 与 Worker
都应通过 SIGTERM 优雅停止，再由 systemd 或 Supervisor 拉起。

## 4. 探活与告警

- `/healthz`：只证明 Web 进程可响应；
- `/readyz`：执行最小数据库查询，失败返回 503；
- Worker：监控 `background_jobs` 的 failed/backlog 和 `outbox_events` 未投递数量；
- Capture：监控 `capture_sources.capture_state=failed` 及稳定 `error_code`；
- 周期提醒：检查 active commitment 的到期 occurrence 是否有唯一 reminder。

日志只包含 request ID、route、status、duration、聚合/任务标识和稳定错误码。不得增加 Cookie、
Authorization、OIDC code/state/nonce、Upload Token、自由文本、原始商家或金额明细。

## 5. 备份与恢复

每日 PostgreSQL 逻辑备份并定期执行物理/快照备份；Asset 文件由 Platform 独立备份，Ledger 只备份
Asset ID/Reference 清单。Session 可丢弃，领域事实不可丢弃。恢复演练流程：

1. 恢复最近备份到隔离数据库；
2. 运行 Alembic 到 head；
3. 验证 Record 状态、子对象一对一、Decimal、owner 隔离和 Intent/Record 链接；
4. 对账 AssetBinding/Reference、ExternalReference、Outbox 和 Reminder；
5. 用非生产入口完成 OIDC 与核心录入负向测试。

## 6. 常见故障

| 现象 | 处理 |
|---|---|
| `/readyz` 503 | 检查 secret file、数据库连通性、用户权限和 migration revision |
| OIDC callback 400/401 | 检查当前入口是否精确在 allowlist、Identity 注册值、服务器时间与 JWKS |
| Capture failed | 查看 `error_code`，修复 Provider/Asset 后调用 retry；不会覆盖 confirmed Record |
| Outbox 重试 | 恢复上游后等待指数退避；幂等引用可安全补建 |
| 周期提醒缺失 | 检查 Worker、RRULE、timezone 和唯一 reminder key |
| 版本冲突 409 | 刷新最新 revision，人工合并，不覆盖另一页面的更新 |
