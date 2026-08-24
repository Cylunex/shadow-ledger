# 核心流程

## 1. 金额快速录入

默认首页只显示金额、可选描述、支出/收入切换；时间默认现在，币种取用户默认值。

```text
输入金额与描述
→ 服务端解析可选描述
→ 创建 LedgerRecord(draft)
→ 创建 MoneyEntry
→ 若识别为消费，同时创建 ConsumptionEvent
→ 返回草稿预览
→ 用户确认
→ 同一事务将 Record 改为 confirmed
```

允许用户配置“简单录入直接确认”。即使启用，也必须由用户点击提交触发，解析置信度低时仍回到
草稿。服务端不得因为分类或商家识别失败而拒绝保存金额。

## 2. 自然语言录入

示例输入仅用于说明解析能力：

```text
昨天中午外卖黄焖鸡，金额 27.5，味道一般
```

解析器输出结构化建议：金额、时间、类型、场景、原始商家/渠道、内容和评分。流程必须满足：

1. 原始文本先写 CaptureSource；
2. 解析结果带 parser、version 和字段级 confidence；
3. 结果只创建 draft；
4. 用户确认前可以只修正金额和主要内容；
5. 无法解析的部分留空，不编造事实；
6. 相同 external ID 或 Idempotency-Key 不重复创建草稿。

v1 可以先使用确定性规则与日期/金额解析器。LLM 是可替换解析适配器，不属于领域层依赖。

## 3. 小票和订单截图

```text
浏览器向 Ledger 请求 Asset 上传会话
→ 优先使用 Platform 返回的局域网 HTTPS target
→ 失败回退 canonical target
→ 完成 Asset
→ Ledger 创建 AssetReference + CaptureSource(received)
→ PostgreSQL Job 执行 OCR/AI
→ CaptureSource(parsed/failed)
→ 生成一个或多个 draft LedgerRecord
→ 用户核对总金额、时间和主要内容
→ 确认
```

关键规则：

- Ledger 永远不接收或保存文件字节；
- Upload Token 只给浏览器短时使用，不能进入数据库和日志；
- OCR 明细合计与 MoneyEntry.amount 不一致不阻塞确认；
- 解析失败仍保留来源和 Asset 绑定，允许重试或手工录入；
- parser version 变化可以重新解析，但不能静默覆盖已确认记录；
- 一个来源拆成多条草稿时，每条都关联同一 CaptureSource。

## 4. 草稿确认

确认请求带当前 `revision`：

```text
BEGIN
SELECT LedgerRecord FOR UPDATE
检查 state=draft 且 revision 匹配
校验 record_kind 与子对象不变量
写 confirmed_at、state=confirmed、revision+1
若完成 SpendingIntent，同事务更新 Intent
写 AuditEvent 与 OutboxEvent
COMMIT
```

冲突返回 409，不自动覆盖用户在另一个页面或 Agent 中完成的修改。

## 5. 修改与撤销

### 修改草稿

允许增删金额、消费事件和明细；每次更新增加 revision。

### 修改已确认记录

首版允许修正金额、分类、时间、商家、内容和评价，但必须：

- 提供 revision；
- 服务端记录变更字段和可选原因；
- 不改写 CaptureSource 原文；
- 不把已确认记录退回 draft。

### 撤销

```text
confirmed → voided
```

撤销记录不参与统计，但仍可查看。撤销不会立即释放作为事实证据的小票 Asset；用户明确删除
附件时才释放 AssetReference。

## 6. 退款

退款是新的 Record：

```text
LedgerRecord(confirmed)
└── MoneyEntry(type=refund, related_entry_id=原支出)
```

如需描述退回的内容，可以附加 ConsumptionEvent 和明细。原支出不被覆盖。部分退款与多次退款
通过多条 refund 表达；查询层汇总原支出关联的全部 confirmed refund。

## 7. 周期承诺

后台任务只生成幂等 Reminder：

```text
RecurringCommitment 到达 next_due_at - remind_before
→ 创建/更新 Reminder
→ 用户选择“生成草稿”
→ 复制预计金额、标题、商家和类型
→ 用户确认
→ 写 last_record_id 并计算下次时间
```

暂停、结束、续费失败和金额变化均不会产生错误事实。任务重复运行通过
`commitment:{id}:occurrence:{time}` 幂等键去重。

## 8. SpendingIntent

```text
inbox → considering → planned → due
                         ├── completed
                         ├── skipped
                         └── cancelled
```

用户从 Intent 创建消费草稿时预填标题、商家、内容和预计金额；实际金额不必等于预计金额。
只有 Record 确认成功后 Intent 才进入 completed，并保存 `completed_record_id`。

## 9. 身份建议与合并

解析层按规范化文本、商家、条码和历史使用生成候选，界面展示：

```text
原始文本 → 建议 Merchant / ItemIdentity → 依据 → 接受 / 拒绝
```

接受只更新关联和别名；合并两个身份时旧 ID 指向 canonical ID。拒绝结果作为负反馈保存，避免
相同建议反复出现。任何自动化都不得删除原始文本。

## 10. CSV/JSON 与平台 Markdown 账单导入

### 导入阶段

1. 文件进入 Asset，或由浏览器读取不超过 1 MB 的 Markdown 文本；
2. 创建 CaptureSource；
3. 映射列和预览；
4. 按外部 ID 或稳定指纹去重；
5. 批量创建 draft；
6. 用户抽查后批量确认。

京东、淘宝、美团和饿了么账单使用平台订单标识生成不可逆稳定指纹；缺少平台标识时才退化到
整行内容指纹。不能仅凭相同日期和金额永久判定重复。

平台映射规则：

- 淘宝的空订单号续行归入上一订单，交易关闭不生成草稿；
- 京东和美团退款生成独立退款草稿，缺少可靠关系时不猜测原支出；
- 饿了么商品列表解析为多条消费明细；
- 最终金额始终采用订单实付/账单金额，明细不要求与它对平；
- 支付方式只保留在来源数据中，不做账户建模；原始商家、渠道和商品文本同时原样进入草稿，
  完整来源行继续留在 CaptureSource。

### 导出

- JSON：完整保留 Record、金额、消费、来源元数据、计划与引用；
- CSV：面向用户的一行一记录扁平视图，明细另表；
- Asset 文件不嵌入普通导出，完整备份通过 Platform Asset 清单引用。

## 11. 失败恢复

| 失败点 | 恢复方式 |
|---|---|
| Asset 上传失败 | 上传会话过期，未创建 Ledger 事实 |
| Asset 完成但 Ledger 绑定失败 | Outbox/对账任务补建引用，Asset 暂不清理 |
| OCR 失败 | CaptureSource=failed，可重试或手工录入 |
| 确认并发冲突 | 409，刷新当前 revision 后人工合并 |
| 提醒任务重复 | reminder_key 唯一约束幂等 |
| 跨项目引用失败 | 本地事实照常确认，Outbox 异步重试 |
| 通知渠道不可用 | Reminder 保留，通知 Job 指数退避 |
