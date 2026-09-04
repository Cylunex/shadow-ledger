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

## 0006 迁移

`20260903_0006` 只给 `money_entries` 增加 nullable `payment_method` 和方式白名单约束，历史值
全部留空，不改金额。备份后在隔离 PostgreSQL 验证空库及 0005→0006；回滚应用时保留新列即可，
不要在有新标签数据时执行降级删列。先升级数据库再重启 Web/Worker。

## 可选进程

- `ledger-web`：浏览器与 API；
- `ledger-worker`：提醒、Outbox、任务以及可选受控目录 intake；
- `ledger-mcp`：由 MCP host 按需启动的 stdio 子进程，不作为公网 HTTP 服务。

intake 目录必须位于同一文件系统，生产者以临时文件写完并原子 rename 为 `.json`。owner 文件和
数据库凭据使用只读文件权限提供，不写进服务日志或仓库。

## 1.2 / 0007 升级

0007 新增 source_observations、suggestion_feedback，保留所有金额、来源和旧预测；
将旧 ForecastItem 的 dismissed 按事件键回填为反馈。原来源 baseline 采用按需补齐，不全库改写原文。
升级前备份；先隔离验证 0006→0007，再一次性执行迁移，随后共同切换 Web 与 Worker。
部署配置在仓库外运维中心维护，本仓库没有可直接套用的 NAS 地址或部署目录。

数据库回滚不等于切回旧程序：旧版 Worker 不识别跨运行反馈和来源观察，因此不保证业务语义兼容。
有新观察／反馈后，不执行 downgrade 删表；优先保留扩展模式并前向修复，必要恢复必须经用户批准，
检查备份时间后的写入损失及 Asset 引用。未处理来源观察不能由旧确认界面绕过服务端校验。

静态资源 Cache-Control 为 no-cache，HTML/API 为 no-store；更新后应检查旧标签页的提交失败提示、
重新加载与代理子路径。尚未启用 Service Worker，不需要清理旧离线应用缓存。

## 可移植 wheel 构建

开发命令 scripts/build_release.py 接受 --output（必须新的空目录）、--platform（SDK 源码目录）、
--cache-dir，构建两个 wheel 并生成 SHA-256 清单。其余第三方依赖仍需按 uv.lock 准备，
不是一次构建就获得完全离线安装包。构建不部署、不读生产配置。

安装两个 wheel 后使用 ledger-migrate 执行打包迁移；ledger-web、ledger-worker、ledger-mcp 不变。
运行证据 CLI 位于 app.cli，避免覆盖 SDK 的同名 scripts 模块。wheel 的 app/release 内含迁移、
alembic.ini 与插件合同，可供发布工具定位；部署工具须显式使用该位置，不假设源码 checkout 存在。

## 回归与 NAS 发布硬门槛

- 快速回归：pytest；前端逻辑：node --test tests/frontend/client.test.mjs；静态检查：ruff check。
- PostgreSQL：用环境变量 LEDGER_TEST_POSTGRES_URL 指向专用 ledger_test_* 测试库再运行 pytest。
  测试会建表、清表及临时 schema，绝不能指向生产或有业务数据的库。测试库名称校验不是备份措施。
- 已有 test_postgres_migration 验证空库到 0006 再升级 0007及历史忽略回填；
  test_postgres_concurrency 验证幂等、确认、身份合并、同来源并发。
- 发布前核验目标 CPU 架构、锁定依赖／镜像摘要、迁移兼容、Web/Worker 起停、HTTPS/OIDC
  callback/退出、Origin/CSRF/代理路径、Asset 上传引用、采集失败重试和队列积压。
- 使用真实备份做隔离恢复并核对数据库与 Asset 引用，按现有 ledger-restore-verify 记录证据；
  只有脚本存在或本地测试通过，不代表 NAS 恢复演练成功。
- 记录实际内存／磁盘峰值、查询耗时与导入耗时。未有真实基线前不承诺 P95 或资源上限。
