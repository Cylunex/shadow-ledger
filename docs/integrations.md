# Shadow 集成设计

## 1. 通用规则

- 服务之间不共享业务数据库；
- 不保存其他项目的内部外键，只保存不透明 `shadow://` URI；
- 所有跨服务写操作使用幂等 key、Outbox 和失败重试；
- 外部服务不可用不能回滚已经确认的 Ledger 本地事实；
- 定期对账发现引用缺失、目标不可达和 AssetReference 漂移；
- 真实入口由 Platform Catalog 和仓库外部署配置提供，不写进代码。

## 2. 资源 URI

Ledger 对外发布：

```text
shadow://ledger/records/{record_id}
shadow://ledger/consumption-events/{event_id}
shadow://ledger/merchants/{merchant_id}
shadow://ledger/items/{item_id}
shadow://ledger/intents/{intent_id}
shadow://ledger/recurring-commitments/{commitment_id}
```

Ledger 可引用：

```text
shadow://health/...
shadow://travel/places/{id}
shadow://travel/visits/{id}
shadow://travel/trips/{id}
shadow://foliant/...
shadow://archive/records/{id}
```

URI 在 Ledger 中只是业务标识。UI 需要跳转时向 Platform Catalog/Resolver 请求当前可用入口，
不得把 URI 自行拼成域名。

## 3. Platform Identity 与 Catalog

Ledger 是独立 OIDC confidential client：

```text
client_id: shadow-ledger
flow: Authorization Code + PKCE S256
scopes: openid profile email groups
required_group: ledger-users
callback: 由部署配置提供的精确 HTTPS /auth/callback
```

浏览器完成登录后，Ledger 用 `(iss, sub)` 生成稳定本地 identity，Session 保存在 Ledger 自己的
数据库或受限 SQLite 中；不复制 Authelia PostgreSQL。

Catalog 示例只使用占位域名：

```yaml
id: ledger
title: Shadow Ledger
auth:
  mode: oidc
  groups: [ledger-users]
capabilities:
  - web
  - ledger.capture
  - ledger.records
  - ledger.planning
```

## 4. Platform Asset

### 文件所有权

```text
Asset：拥有文件字节、版本、权限、派生文件、回收站和 GC
Ledger：拥有附件在某次记录中的消费语义
```

Ledger 表中只保存：

```text
asset_id
asset_reference_id
usage
source_type/source_id
```

### 上传

1. Ledger 使用服务凭据向 Asset 创建 upload session；
2. 浏览器使用返回的短时 Upload Token 直传；
3. 客户端优先尝试受控局域网 HTTPS target，失败回退 canonical target；
4. Ledger 完成 Asset 后创建业务引用；
5. 引用 key 使用稳定格式：

```text
ledger:{source_type}:{source_id}:{usage}
```

Asset target 只能原样转交，Ledger 不接受浏览器自定义主机。Ledger 服务凭据绝不返回浏览器。

### 解绑与删除

- 删除草稿或附件时，Ledger 只释放自己的 AssetReference；
- Ledger 不调用物理文件删除；
- 已确认记录的证据附件默认随 voided Record 保留；
- Platform 根据其他引用、ownership_mode 和 retention_policy 决定是否回收。

## 5. Health

购买与摄入严格分开：

```text
Ledger：购买/点了什么、金额、商家和时间
Health：实际吃了什么、份量、营养和健康影响
```

允许流程：

```text
Ledger ConsumptionLine
→ 用户选择“发送到 Health 草稿”
→ Ledger 调用 Health 幂等草稿接口
→ Health 返回 shadow://health/... URI
→ Ledger 保存 ExternalReference
→ 用户在 Health 确认实际摄入
```

禁止：确认 Ledger 订单后自动把全部明细写成 Health 摄入。

Health 可以向 Ledger 提供目标或建议引用，例如减脂期饮食计划；Ledger 据此创建
SpendingIntent 草稿，而不是自动产生支出。

## 6. Travel

Ledger 的 `place_ref`、ExternalReference 可以指向 Place、Visit 或 Trip。Travel 拥有地点、到访
和旅程；Ledger 拥有消费。

典型查询由 Ledger 本地引用完成：

- 某 Trip 关联的 confirmed Record；
- 某 Place 的消费次数、净支出和评分；
- 旅行计划中的 SpendingIntent 与实际 Record。

Ledger 不复制 Place 名称作为真相，可缓存展示快照并标注更新时间；原始商家文本仍保留。

## 7. Foliant

Foliant 继续拥有证券持仓、交易决策、行情、策略和回测。Ledger v1 不做净资产，因此只定义未来
只读适配器：

```text
估值摘要
投入/取出现金流摘要
已实现收益摘要
valued_at 与 currency
```

这些数据用于未来洞察，不进入 MoneyEntry，除非用户明确把真实现金流作为 Ledger 记录导入。
Ledger 不复制持仓明细和行情历史。

## 8. Archive 与 Garden

- 说明书、保修材料、长期发票档案由 Archive 管语义，文件仍在 Asset；Ledger 可以保存 Archive URI。
- Garden 是公开内容。Ledger 的私人评价和消费历史不自动发布；用户显式发布时由 Garden 创建自己
  的记录并保存 Ledger URI。

## 9. 通知

RecurringCommitment 到期先创建 Ledger Reminder，再通过 Platform 通知适配器投递。通知 payload
只含最少展示信息和 Ledger URI，不包含原始小票、完整解析文本或 OIDC/服务 Token。

通知投递失败不改变 Reminder 和 Commitment 状态。

## 10. 一致性与对账

Outbox 事件示例：

```text
ledger.asset.reference.create
ledger.asset.reference.release
ledger.health.draft.create
ledger.notification.requested
ledger.record.confirmed
ledger.record.voided
```

定期对账任务：

- AssetBinding 存在但 Platform Reference 缺失；
- Platform Reference 指向不存在/错误的 Ledger URI；
- ExternalReference 长期不可解析；
- CaptureSource 已 parsed 但没有草稿；
- Commitment 已过 due 时间但没有 occurrence Reminder。

对账只报告或安全补建幂等关系，不自动删除事实。
