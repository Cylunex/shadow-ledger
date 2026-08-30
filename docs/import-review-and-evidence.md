# 消费事实导入复核与运行证据

## 边界

导入复核工作台核对的是“这条消费事实是否重复、退款可能对应哪条支出、金额是否偏离个人历史、
原始商家应关联哪个规范身份”。它不读取账户、银行卡或余额，不执行专业会计式对账，也不会因为
明细合计不同而拒绝消费事实。

导入始终只创建草稿。`CaptureSource.raw_payload`、`ConsumptionEvent.merchant_name_raw` 和
`ConsumptionLine.raw_name` 永久保留导入原文；规范商家只写入独立的 `merchant_id` 关联。

## 导入复核纵切

`POST /api/v1/imports/commit` 以 `Idempotency-Key` 建立 `ImportBatch`。同一键和同一内容返回原批次，
同一键换内容返回 409。每个候选生成机器可读的 `ImportReviewItem`，公开以下复核原因：

- `duplicate`：来源指纹已存在，只引用原记录，不再创建消费草稿；
- `refund_match_suggested` / `refund_match_missing`：退款候选或缺少可靠原支出；
- `amount_anomaly`：至少三条同商家/标题历史后，金额高于中位数三倍或低于三分之一；
- `merchant_confirmation_needed`：保留了原始商家，但没有活动规范化规则。

退款匹配和金额异常都只是建议。只有用户调用复核接口后才关联退款或接受异常；系统不静默伪造
消费关系。重复项必须显式关闭。所有决定带 revision 冲突检测并写入审计。

## 可解释、可撤销的商家规则

用户可在商家确认时选择学习规则。v1 规则只做：

```text
normalize(casefold(raw_merchant_name)) exact match -> Merchant.id
```

规则记录证据次数、来源复核项和解释文本。它只影响后续导入的规范关联，不覆盖来源原文，也不回写
历史原始文本。`POST /merchant-normalization-rules/{id}/revoke` 使用 revision 撤销；历史记录仍保留
当时由用户确认的规范关联与审计。

## Archive 凭证交接

`/records/{record_id}/archive-evidence` 只保存三类稳定标识：Ledger Record、现有 AssetBinding、
`shadow://archive/...` URI。Ledger 不复制文件字节，也不把 Archive 或 Asset 凭据写入数据库。
Outbox 事件只携带 Asset/Reference ID 与稳定 URI。解除关联是可审计的逻辑释放，不删除 Asset 文件。

## 数据完整度与预测就绪度

`GET /api/v1/insights/data-quality` 报告金额、原始商家、规范商家覆盖率以及待复核数量。
`prediction_readiness` 只说明样本与覆盖率是否足以开始评估，并固定返回
`creates_forecast: false`。该查询本身不会生成预测；用户可通过独立 Forecast API 按需运行确定性、
可回算算法，它不会训练模型或生成消费事实。

## Platform lifecycle 与恢复验证

Ledger 使用 Platform 已发布的 `shadow.conformance-evidence.v1` 和 `shadow.restore-drill.v1`：

```bash
ledger-observed-evidence \
  --status build/shadow-capability-status.json \
  --probe-results build/ledger-probe-results.json \
  --evidence-id ledger-observed-example \
  --output build/ledger-observed-evidence.json

ledger-restore-verify \
  --status build/shadow-capability-status.json \
  --drill build/ledger-restore-drill.json \
  --output build/ledger-restore-evidence.json
```

Observed 入口要求每个已选择的 `shadow-ledger` capability 都有明确 probe 结果，并绑定精确的
`deployment_id` / `build_id`。Platform SDK 随后校验 lifecycle 顺序。

恢复验证只接受 Platform schema 中的隔离目标：`target_kind=isolated`、`production=false`、
`cleanup_completed=true`，并要求 contract、data、health 三类检查全部通过。备份内容和用户事实不进入
Platform；输出只是一份可关联的恢复证据。

Forecast 与 UseCycle 已按 ADR 0006 解冻；导入复核仍不会自动触发二者或确认任何消费事实。
