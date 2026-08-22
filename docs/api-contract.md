# API 合同

## 1. 通用约定

- 前缀：`/api/v1`；
- JSON 字段使用 `snake_case`；
- 时间使用带时区的 RFC 3339；
- 金额在 JSON 中使用十进制字符串，禁止二进制浮点；
- 资源 ID 使用 UUID；
- 分页使用不透明 cursor，不暴露数据库 offset；
- 写请求返回最新 `revision` 和 `ETag: "<revision>"`；
- PATCH、确认和撤销必须带 `If-Match`；
- 创建类接口接受 `Idempotency-Key`，同 key 不同 payload 返回 409；
- 浏览器 Cookie 写请求执行同源/CSRF 校验；服务 Bearer 不使用浏览器 Cookie。

错误格式：

```json
{
  "error": {
    "code": "revision_conflict",
    "message": "记录已被更新，请刷新后重试",
    "request_id": "...",
    "details": {}
  }
}
```

稳定状态码：400 输入错误、401 未认证、403 无权限、404 不存在、409 幂等/状态/版本冲突、
413 文件或请求过大、422 语义校验失败、429 限流、502 上游服务失败、503 暂不可用。

## 2. 浏览器认证

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/login` | OIDC Authorization Code + PKCE |
| GET | `/auth/callback` | 校验 state、浏览器绑定、nonce、签名与 group |
| POST | `/logout` | 撤销 Ledger 本地 Session 后统一登出 |
| GET | `/api/v1/me` | 当前用户、默认币种、时区和能力 |

OIDC Token 不返回浏览器；浏览器只持有 `Secure + HttpOnly + SameSite=Lax` 的不透明 Session。

## 3. Record API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/records` | 创建金额、消费或二者组合的 draft |
| GET | `/records` | 时间线、筛选和 cursor 分页 |
| GET | `/records/{id}` | 获取完整聚合 |
| PATCH | `/records/{id}` | 修改 draft 或修正确认记录 |
| POST | `/records/{id}/confirm` | 原子确认 |
| POST | `/records/{id}/void` | 原子撤销 confirmed 记录 |
| POST | `/records/{id}/money-entry` | 给金额未知的消费补金额 |
| DELETE | `/records/{id}` | 仅删除 draft；已确认记录必须 void |

创建组合草稿示例：

```json
{
  "occurred_at": "2026-08-19T12:30:00+08:00",
  "timezone": "Asia/Shanghai",
  "money_entry": {
    "type": "expense",
    "amount": "27.50",
    "currency": "CNY",
    "category_key": "food",
    "title": "午餐"
  },
  "consumption": {
    "scene": "delivery",
    "merchant_name_raw": "原始商家文本",
    "channel_name_raw": "原始渠道文本",
    "rating": 3,
    "lines": [
      {"raw_name": "主要内容", "sort_order": 0}
    ]
  }
}
```

响应聚合：

```json
{
  "id": "uuid",
  "record_kind": "consumption",
  "state": "draft",
  "revision": 1,
  "occurred_at": "2026-08-19T12:30:00+08:00",
  "money_entry": {},
  "consumption": {"lines": []},
  "sources": [],
  "assets": [],
  "external_references": []
}
```

列表筛选首版支持：

```text
state, money_type, scene, category_key, merchant_id, item_identity_id,
occurred_from, occurred_to, amount_min, amount_max, query
```

默认只返回 confirmed；界面草稿箱显式请求 `state=draft`。全文 query 搜索标题、原始商家/渠道、
明细原名和备注。

## 4. 快速录入与解析

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/capture/text` | 保存原文并解析为 draft |
| POST | `/capture/assets/init` | 创建 Asset 上传会话 |
| POST | `/capture/assets/complete` | 完成 Asset、创建绑定和解析 Job |
| GET | `/capture-sources/{id}` | 查看解析状态、错误和生成草稿 |
| POST | `/capture-sources/{id}/retry` | 用指定 parser version 重试 |
| POST | `/imports/preview` | CSV/JSON 映射与去重预览 |
| POST | `/imports/commit` | 幂等创建批量草稿 |

`/capture/assets/init` 只转交 Platform 返回的受控 canonical/alternate targets，不接受客户端提供
任意上传主机。`/complete` 请求只携带 Ledger upload ID，不回传服务凭据。

解析结果必须区分：

```json
{
  "value": "delivery",
  "confidence": 0.91,
  "source_span": [0, 4]
}
```

低置信度不会阻止建草稿，但界面突出待核对字段。

## 5. 分类、商家和商品身份

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `/categories` | 列出或创建金额分类 |
| PATCH | `/categories/{id}` | 改名、排序、停用 |
| GET/POST | `/merchants` | 搜索或创建 Merchant |
| GET/PATCH | `/merchants/{id}` | 详情和修正 |
| POST | `/merchants/{id}/aliases` | 添加原始别名 |
| POST | `/merchants/{id}/merge` | 用户确认合并 |
| GET/POST | `/items` | 搜索或创建 ItemIdentity |
| GET/PATCH | `/items/{id}` | 详情和修正 |
| POST | `/items/{id}/aliases` | 添加别名 |
| POST | `/items/{id}/merge` | 用户确认合并 |
| GET | `/identity-suggestions` | 待确认候选 |
| POST | `/identity-suggestions/{id}/accept` | 接受建议 |
| POST | `/identity-suggestions/{id}/reject` | 拒绝并保存负反馈 |

合并接口不删除来源身份，只设置 canonical 重定向并迁移活动关联。

## 6. 周期事项与提醒

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `/recurring-commitments` | 列表和创建 |
| GET/PATCH | `/recurring-commitments/{id}` | 修改、暂停或结束 |
| POST | `/recurring-commitments/{id}/draft` | 从本次 occurrence 生成草稿 |
| GET | `/reminders` | 待处理提醒 |
| POST | `/reminders/{id}/read` | 标记已读 |
| POST | `/reminders/{id}/dismiss` | 忽略本次提醒 |

创建草稿接口以 occurrence 时间为幂等边界，多次调用返回同一 draft。

## 7. SpendingIntent

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `/intents` | 列表和创建 |
| GET/PATCH | `/intents/{id}` | 修改内容或状态 |
| POST | `/intents/{id}/draft` | 生成预填消费草稿 |
| POST | `/intents/{id}/complete` | 关联已确认 Record |

不能直接把 Intent 标记 completed 而不提供 confirmed Record；明确无需消费事实的 cancel Intent
除外，它通过 cancelled 结束。

## 8. BudgetTarget

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `/budget-targets` | 查询或创建月度总额/分类目标 |
| GET/PATCH | `/budget-targets/{id}` | 调整、延长或停用目标 |
| GET | `/insights/budgets?month=YYYY-MM` | 查询目标进度和未计金额记录 |

目标是洞察辅助数据，不参与 Record 状态转换，也不代表可支配余额。

## 9. 洞察与导出

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/insights/summary` | 月度收入、支出、退款、净支出 |
| GET | `/insights/categories` | 金额分类趋势 |
| GET | `/insights/scenes` | 消费场景频率 |
| GET | `/insights/merchants` | 商家次数和金额 |
| GET | `/insights/items` | 常买/常点内容和间隔 |
| POST | `/exports` | 创建完整 JSON 或 CSV 导出 Job |
| GET | `/exports/{id}` | 状态和短时 Asset 下载地址 |

洞察只读取 confirmed 且非 voided 事实。统计响应必须带查询时间范围、币种和生成时间，避免把
缓存误解为实时事实。

## 10. 跨项目引用

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/records/{id}/references` | 添加受支持的 `shadow://` URI |
| DELETE | `/records/{id}/references/{reference_id}` | 解绑引用 |
| GET | `/records/{id}/references` | 列出引用及可用状态 |

Ledger 不通过目标 URI 拼接内网 HTTP 请求。解析、权限和跳转由 Platform Catalog/SDK 负责；
目标暂不可用不影响本地事实。

## 11. 服务与未来 Agent 鉴权

服务 Token 使用文件注入的哈希映射，并分配最小 scope：

```text
ledger.read
ledger.capture
ledger.write-draft
ledger.confirm       默认不授予 Agent
ledger.integrations
```

Shadow Agent 使用独立 `/api/machine/v1/agent` 合同，不复用浏览器 Session 或通用服务 Token：

| 方法 | 路径 | capability | 说明 |
|---|---|---|---|
| GET | `/summary` | `ledger.summary.read` | 按月、币种读取金额摘要，不做汇率换算 |
| GET | `/records` | `ledger.records.read` | 读取最小化确认账目，不返回备注、商家原文和支付信息 |
| GET | `/budgets` | `ledger.budgets.read` | 读取月度预算目标与净支出进度 |
| POST | `/drafts` | `ledger.records.draft` | 幂等创建可撤销 money-only 草案 |

每次请求依次校验独立 Bearer audience、scope 与 owner 级 grant。草案不接受账户、支付方式、
汇率或 `confirm` 字段，金额使用 Decimal，币种和 IANA timezone 由确定性代码验证。正式确认仍
只能由 Ledger 用户会话或未来受控确认合同完成；普通 `shadow-ledger` Profile 不注册正式入账、
导出、难撤销调整或资金执行能力。
