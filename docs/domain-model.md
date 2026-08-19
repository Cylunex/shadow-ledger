# 领域模型

## 1. 聚合设计

`MoneyEntry` 和 `ConsumptionEvent` 是两种事实，但一次消费在产品上是一条记录。为实现“只有
一个状态真相”，数据库增加内部聚合根 `LedgerRecord`：

```text
LedgerRecord                          唯一状态、时间、所有者、并发版本
├── MoneyEntry?                       最终金额事实
└── ConsumptionEvent?                 消费语义
    └── ConsumptionLine[]             可选内容
```

`LedgerRecord` 不出现在界面，也不是 Posting 或会计分录。它只解决以下问题：

- 金额未知时先保存消费草稿；
- 收入只有金额事实；
- 金额与消费语义一次确认、一次撤销；
- 乐观并发和审计只维护一份状态。

## 2. LedgerRecord

```text
id UUID
owner_id TEXT
record_kind money_only | consumption
state draft | confirmed | voided
occurred_at TIMESTAMPTZ
timezone TEXT
note TEXT
revision INTEGER
confirmed_at TIMESTAMPTZ?
voided_at TIMESTAMPTZ?
created_at TIMESTAMPTZ
updated_at TIMESTAMPTZ
```

不变量：

- 状态只能 `draft → confirmed → voided`；不提供从 voided 恢复的首版接口。
- `revision` 每次更新加一，写接口使用 `If-Match` 或请求体 revision 防止覆盖。
- `record_kind=money_only` 时不能存在 ConsumptionEvent。
- `record_kind=consumption` 时必须存在 ConsumptionEvent，MoneyEntry 可为空。
- confirmed 消费可以暂时没有金额，但界面必须显式标记“金额未知”。
- voided 只做逻辑撤销，不物理删除事实、来源和审计记录。

## 3. MoneyEntry

```text
id UUID
record_id UUID UNIQUE
type expense | income | refund
amount NUMERIC(18,4)
currency CHAR(3)
category_id UUID?
title TEXT
related_entry_id UUID?
created_at TIMESTAMPTZ
updated_at TIMESTAMPTZ
```

约束：

- `amount > 0`，禁止使用浮点数；类型决定汇总方向。
- expense 对支出汇总为正向增加，income/refund 从净支出中扣除。
- refund 的 `related_entry_id` 可指向原 expense；允许不知道原记录时为空。
- related entry 必须属于同一 owner，且不能自引用。
- 不包含账户、支付方式、余额或转账字段。
- 修改已确认金额必须提供原因并写审计；首版可直接更新同一条记录，不做版本账本。

汇总公式由查询层统一定义：

```text
net_spending = expense - refund
net_income   = income - expense + refund
```

voided Record 不参与任何汇总。

## 4. ConsumptionEvent

```text
id UUID
record_id UUID UNIQUE
money_entry_id UUID UNIQUE NULLABLE
scene TEXT
merchant_id UUID?
merchant_name_raw TEXT?
channel_key TEXT?
channel_name_raw TEXT?
place_ref TEXT?
rating SMALLINT?
would_repeat BOOLEAN?
note TEXT
created_at TIMESTAMPTZ
updated_at TIMESTAMPTZ
```

数据库用复合外键保证 `money_entry_id`（存在时）属于同一个 LedgerRecord。它实现正式约束：

```text
ConsumptionEvent.money_entry_id UNIQUE NULLABLE
```

其他约束：

- rating 首版为 1～5；“一般”等自然语言由解析层映射，不直接成为枚举。
- `place_ref` 只接受合法的 `shadow://travel/...` URI，但 Ledger 不解析其业务字段。
- merchant 规范身份和 `merchant_name_raw` 可以同时存在；原始文本不得被覆盖。
- channel 首版用稳定 key + 原始文本，不单独建立复杂渠道主数据。

稳定场景：

```text
online_purchase  offline_purchase  delivery  dine_in  drink
service          subscription      transport entertainment travel other
```

## 5. ConsumptionLine

```text
id UUID
event_id UUID
item_identity_id UUID?
raw_name TEXT
quantity NUMERIC(12,4)?
unit TEXT?
amount NUMERIC(18,4)?
content_category TEXT?
note TEXT
sort_order INTEGER
created_at TIMESTAMPTZ
updated_at TIMESTAMPTZ
```

规则：

- 明细全部可选，只有存在明细行时 `raw_name` 必填。
- 行金额仅用于展示和粗略分析，可以为空，也不要求合计等于 MoneyEntry.amount。
- 不强制建模配送费、税费、包装费、优惠券或补贴。
- `raw_name` 永久保留；规范 ItemIdentity 只是附加关联。

## 6. MoneyCategory

```text
id UUID
owner_id TEXT
key TEXT
name TEXT
color TEXT?
icon TEXT?
sort_order INTEGER
active BOOLEAN
```

部署初始化十个左右的简单分类：餐饮、购物、交通、居住、生活服务、娱乐、旅行、健康、
订阅、其他。用户可以改名、停用和排序，但系统 key 不随展示名变化。历史记录指向停用分类
仍然有效。

## 7. CaptureSource

```text
id UUID
owner_id TEXT
source_type TEXT
source_external_id TEXT?
asset_id UUID?
raw_text TEXT?
raw_payload JSONB?
parser TEXT?
parser_version TEXT?
capture_state received | processing | parsed | failed
error_code TEXT?
captured_at TIMESTAMPTZ
created_at TIMESTAMPTZ
```

`UNIQUE(owner_id, source_type, source_external_id)` 在 external ID 非空时成立，用于幂等导入。
一个 Record 可以有多个来源，一个来源也可能经用户拆分生成多条草稿，因此通过
`LedgerRecordSource(record_id, source_id, role)` 连接，而不是在两张事实表重复 source_id。

`raw_payload` 只保存解析需要的结构化结果，不保存认证凭据、完整邮件访问令牌或文件字节。
原始文件由 Asset 保存。

## 8. Merchant 与 ItemIdentity

```text
Merchant
├── id
├── owner_id
├── canonical_name
├── merchant_type
├── place_ref?
├── active
└── timestamps

MerchantAlias
├── merchant_id
├── alias
├── normalized_alias
└── source

ItemIdentity
├── id
├── owner_id
├── kind
├── canonical_name
├── brand?
├── variant?
├── merchant_id?
├── barcode?
├── external_ids JSONB
└── timestamps

ItemAlias
├── item_identity_id
├── alias
├── normalized_alias
└── source
```

自动识别只创建建议。永久合并要求用户确认，并保留：来源 ID、目标 ID、确认人、时间与原因。
合并采用“重定向到 canonical ID”，不物理删除旧身份，保证历史引用可解释。

## 9. RecurringCommitment

```text
id UUID
owner_id TEXT
kind subscription | membership | regular_service | regular_purchase |
     fixed_expense | regular_income
title TEXT
merchant_id UUID?
item_identity_id UUID?
expected_amount NUMERIC(18,4)?
currency CHAR(3)
recurrence_rule TEXT
timezone TEXT
next_due_at TIMESTAMPTZ
auto_renew BOOLEAN?
remind_before INTERVAL
state active | paused | ended
last_record_id UUID?
revision INTEGER
timestamps
```

`recurrence_rule` 使用 RFC 5545 RRULE 子集，首版 UI 只生成日、周、月、年和固定间隔。后台任务
到期时创建 Reminder，并可按用户动作创建 draft Record；绝不直接创建 confirmed Record。

## 10. SpendingIntent

```text
id UUID
owner_id TEXT
intent_type buy | eat | drink | visit | subscribe | renew | cancel | replace | other
title TEXT
item_identity_id UUID?
merchant_id UUID?
place_ref TEXT?
expected_amount NUMERIC(18,4)?
currency CHAR(3)
desired_start TIMESTAMPTZ?
desired_end TIMESTAMPTZ?
priority low | normal | high
state inbox | considering | planned | due | completed | skipped | cancelled
reason TEXT?
completed_record_id UUID?
revision INTEGER
timestamps
```

完成 Intent 和确认消费记录必须在同一事务内连接；取消或跳过不会创建金额事实。

## 11. BudgetTarget（v1 可选）

预算只表达“希望本月消费不超过多少”，不预留资金，不产生账户余额，也不跨月结转。

```text
BudgetTarget
├── id
├── owner_id
├── category_id?        # 为空时表示总消费目标
├── budget_month        # 月初日期
├── monthly_amount
├── currency
├── active
├── revision
├── created_at
└── updated_at
```

约束：

- 同一用户、币种和分类在一个月份最多有一个有效目标；
- 退款按净支出抵减进度；
- 金额未知的记录不计入进度，并单独提示；
- 它是辅助洞察，不影响消费记录的确认与撤销。

## 12. v2 扩展对象

### UseCycle

用于少量消耗品的开始/结束观察，不表示库存：

```text
item_identity_id, purchase_line_id?, started_at, finished_at?, outcome, note
```

### Forecast

是带有效期的可重建缓存：

```text
forecast_type, target_type, target_id, predicted_start, predicted_end,
expected_amount, confidence, basis JSONB, model_key, model_version,
calculated_at, expires_at
```

v1 不建立依赖 Forecast 的用户流程；删除全部 Forecast 后必须可以从事实重新计算。

## 13. 通用基础表

- `ExternalReference`：业务对象到 `shadow://health/...`、`shadow://travel/...` 等 URI 的关系；
- `AssetBinding`：record/source 与 Asset 的业务使用关系及 Platform `reference_id`；
- `Reminder`：到期提醒，拥有幂等 key 和已读/处理状态；
- `OutboxEvent`：跨服务操作和通知的可靠投递；
- `BackgroundJob`：PostgreSQL 任务队列；
- `AuditEvent`：确认、撤销、金额修正、身份合并、导入等重要动作。

## 14. 删除与保留

- draft 可由用户删除；服务先释放 AssetReference，再标记删除，保留短期审计。
- confirmed 只能 void，不提供硬删除 API。
- 商家、分类和 ItemIdentity 被引用后只能停用或合并。
- CaptureSource 的文件删除遵循 Asset 生命周期；Ledger 只能释放自己的引用。
- 导出后删除个人数据属于单独运维流程，必须可审计且需要二次确认。
