# MCP 与自动抓单

1.3 新增可选 v2 任务目录和远程 Streamable HTTP；兼容 stdio 默认不变。
开关、最小披露、固定任务路径及 OAuth 联调限制见 [Agent 方案](agent-optimization-2026-09.md)。

## Ledger MCP

`ledger-mcp` 使用官方 MCP Python SDK v2 的 stdio transport，由客户端作为本地子进程启动。它直接
使用 Ledger 数据库，因此必须为进程提供独立最小权限数据库用户，并通过
`LEDGER_MCP_OWNER_ID_FILE` 绑定唯一 owner。owner 不从模型参数读取。

默认工具：

- `ledger_monthly_summary`：单月、单币种已确认摘要；
- `ledger_records`：最多 50 条最小披露确认记录，可返回/筛选支付方式标签；
- `ledger_forecasts`：最近一次可解释预测。

只有显式设置 `LEDGER_MCP_ALLOW_DRAFTS=true` 时才注册 `ledger_create_draft`。它只创建 money-only
`draft`，要求调用方提供幂等键，可选填受控 `payment_method` 标签，不接受 confirm、账户或汇率。MCP 永远不注册正式确认、
撤销、导出或资金工具。

## Webhook intake

`POST /api/v1/intake/webhooks/{adapter}` 要求具备 `ledger.capture` 的 Bearer 或用户会话。请求提供：

```json
{
  "source_external_id": "provider-order-id",
  "captured_at": "2026-08-31T10:00:00+08:00",
  "metadata": {"provider": "example", "original_status": "paid"},
  "records": [{"occurred_at": "...", "money_entry": {}, "confirm": false}]
}
```

external ID 与 `webhook:{adapter}` 构成来源幂等边界；重放相同内容返回已有来源和草稿，内容不同
返回 409。metadata 会原样进入 CaptureSource，但任何层级出现 token、password、secret、cookie、
authorization 或 API key 等凭据字段都会被拒绝。适配器应在进入 Ledger 前移除认证报文，只提交
稳定来源事实。

## 受控目录 intake

Worker 在同时配置 `LEDGER_INTAKE_DIRECTORY` 和 `LEDGER_INTAKE_OWNER_ID_FILE` 时扫描目录顶层的
`.json` 文件。生产者必须先写临时文件，再原子 rename 为 `.json`。文件格式是：

```json
{"adapter":"receipt","payload":{"source_external_id":"...","records":[...]}}
```

文件稳定两秒后才处理；成功文件移动到 `processed/`，无效或冲突文件移动到 `failed/`。目录和
owner 文件必须由 Ledger 进程专用账号控制，不能放在 Web 可写目录。无论 Webhook 还是目录，最终
都只创建 CaptureSource、来源关联和 Record 草稿，用户仍需在 Ledger 或 Nexus Review 中确认。

## 自动预测

Worker 默认按 `LEDGER_DEFAULT_TIMEZONE` 每个自然日为每个已有 owner 幂等生成一次 90 天预测，
可用 `LEDGER_AUTO_FORECAST_HORIZON_DAYS` 在 1–365 天内调整，或通过
`LEDGER_AUTO_FORECAST_ENABLED=false` 关闭。自动任务只写 `ForecastRun` 与 `ForecastItem` 建议，
不会创建 Money/Consumption 事实，也不会确认任何草稿；用户仍可在页面手动重算和忽略建议。
