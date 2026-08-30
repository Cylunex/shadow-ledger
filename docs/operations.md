# 通用运行说明

真实域名、IP、端口、数据库 DSN、OIDC 凭据、服务 Token、owner ID 和备份位置不属于本仓库，统一
由 Shadow 工作区外的运维中心管理。本页只描述可移植的启动顺序和检查项。

## 发布顺序

1. 备份 PostgreSQL 与当前应用发布目录；
2. 使用与生产同版本的 PostgreSQL 在隔离库执行 `alembic upgrade head`；
3. 安装锁定依赖，切换 Web/Worker 发布并重启；
4. 检查 `/healthz`、`/readyz`、登录、草稿创建/确认负向边界；
5. 如启用 intake，投递一个脱敏测试 envelope，确认只生成 draft；
6. 如启用 MCP，用 MCP 客户端检查工具列表，确认没有 confirm、void、export 工具；
7. 运行备份恢复验证并记录 Platform observed/restore-tested 证据。

## 0005 迁移

`20260831_0005` 只新增 `use_cycles`、`forecast_runs` 和 `forecast_items`，不改写已有 Record、金额或
CaptureSource。升级前仍需数据库备份。回滚会删除新表及其建议/周期数据，但不会删除既有账目。

## 可选进程

- `ledger-web`：浏览器与 API；
- `ledger-worker`：提醒、Outbox、任务以及可选受控目录 intake；
- `ledger-mcp`：由 MCP host 按需启动的 stdio 子进程，不作为公网 HTTP 服务。

intake 目录必须位于同一文件系统，生产者以临时文件写完并原子 rename 为 `.json`。owner 文件和
数据库凭据使用只读文件权限提供，不写进服务日志或仓库。
