# Ledger 接入 Nexus 的详细设计

设计版本：2026-09-07 / UA-1。状态：目标设计，尚未实现。公共身份、鉴权、Agent、模型、命令与回执以 [Platform 统一规范](https://github.com/Cylunex/shadow-platform/blob/main/docs/nexus-unified-access-design.md) 为准；本文仅定义本领域差异。旧接口安全限制在对应能力通过迁移验收前继续生效。

## 1. 当前基线与决策变更

基线 `1507552` / 1.3.0。Gateway、QueryRun、Intent、ApprovalGrant、ExecutionReceipt、Audit、Outbox 已实现；旧 Nexus commit/reject 缺精确 Grant 返回 428。目标变更由 [ADR 0011](decisions/0011-unified-access-and-direct-intent.md) 接续 ADR 0010，旧行为在切换前仍有效。

Ledger 保留 Record/MoneyEntry/ConsumptionEvent、消费身份、退款关联、来源、预算/Forecast/UseCycle 和事务。Agent 身份、owner delegation、目录策略、审批发放、Token/Session 生命周期交给 Platform；不用“财务领域”作为另建完整控制面的理由。仍不引入余额、转账、复式账或支付内核。

## 2. 代码迁移分界

| 当前模块/表 | 处理 |
| --- | --- |
| `app/oidc.py`、`app/agent.py` | 改 Platform SDK；中央 user 映射不替换业务外键 |
| `ledger_agent_grants` | 转中央精确 owner/capability delegation；旧表只读兼容后停止认证用途 |
| `agent_gateway.py` 的 auth/catalog/disclosure 公共部分、`agent_mcp.py` | 中央目录/Runtime/共享 wrapper；领域保留确定性工具 handler |
| `agent_queries.py`、QueryRun/claim verifier | 留 Ledger，金额与查询口径由领域计算 |
| `agent_effects.py` Intent/Grant/PolicyDecision | 普通新路径不建本地 ApprovalGrant；历史表保留，不再签发新中央模式批准 |
| Receipt/Audit/确认 Outbox | 留 Ledger，与事实变更同事务，引用中央 decision_ref |

通用幂等/canonical fixture 可以共用，MoneyEntry.amount 仍使用 Decimal 字符串。中央 policy_version 与本地业务规则版本分开，不能用同名字段混淆。

## 3. 能力表

| 操作 | 参数/目标 | 结果与交互 |
| --- | --- | --- |
| `record.capture` | kind、amount/currency（或明确 unknown）、occurred_at/timezone、可选商家/渠道/备注/消费项 | current_intent direct；已知金额记录进入 confirmed，未知事实按领域支持语义表达 |
| `record.correct` | record_ref、expected_revision、变更字段 | direct 小范围可修正；保留历史原文与 before/after 引用 |
| `record.void` | record_ref、revision、原因 | 依据领域撤销规则，不冒充银行退款 |
| `refund.record/link` | 金额/币种、原消费引用可选、退款日期 | actual 退款记录；金额约束与关联限制事务内核验 |
| `entity.merge` | 冻结的 source/target IDs、versions、受影响数量 | 小范围可修正 direct；重要批量历史重写 inline_confirm |
| `plan.create/update` | 明确计划字段、version | 只形成计划/提醒，不能自动 confirmed 消费 |
| `draft.save/delete` | 明确草稿目标与 version | 保存草稿/无引用草稿删除按实际可恢复性直接执行 |
| `overview/search/insights/explain` | QuerySpec、币种/时间范围、QueryRun ref | 确定性 result，exact/partial/projected，有限披露 |

这些名称是拟议操作，不代表已注册新工具。Nexus Surface 指向具体命令，不再借统一 Review commit 表达所有修改。来源合并、退款、消费实体合并保持不同 schema，不能让一个任意 JSON patch 写全部表。

## 4. 普通记录的完整链路

用户“打车 36 元” → Nexus current_intent → 中央授权具体 owner/record.capture → SDK verified Principal → 规范化服务检查金额/币种/时间 → 按 command_id 事务落账/写 Receipt/Outbox → 返回实际 record_ref 与 revision。

金额缺失时只追问金额；商家/方式不必补齐。允许未知金额消费事实时结果必须明确 unknown，不把未知当 0。两次相同消费是两个命令；同一命令重试拿同一 Receipt。

修正原始商家或金额必须走现有领域规则，保持原文/来源证据。预计到期、周期任务和自动抓取仍只能生成提醒、来源观察或草稿，不能因 Agent 中央化就把预测确认为已发生。

## 5. 授权与凭证

新路径 Receipt 增加 `authorization_mode`、中央 `decision_ref/delegation_ref`、user/Agent/workload、command/hash、before/after revision 和业务规则版本。若历史 receipt schema 强制 grant_id，新增版本或独立新行类型，不伪填旧 grant_id/approved_by，不改写历史 Receipt hash。

人类确认在中央，Ledger 不再提供中央模式的审批发放接口。执行时中央 claim 和本地唯一 command 结果共同防重；不要求两个数据库分布式提交。中央 claim 后 Ledger 回滚，可同 command 重新领取原许可/重新授权并恢复，不能生成新事实键。

已完成 Receipt 重放仍要当前 owner 读取权限，权限撤回后不可借幂等键取得金额或原文。search 聚合在授权筛选后计算；remote_minimal/local_private 披露规则中央配置，Ledger 投影白名单负责实际输出，不靠 Host 删除敏感字段。

## 6. 页面、数据迁移与发布

新增普通命令及 versioned receipt，数据库只做加法迁移；保留旧 AgentIntent/ApprovalGrant/Receipt/Audit 及旧批准者身份。只把有效、范围明确的 delegation 导入中央，`allow_confirm` 只是旧资格，不能变成无限写权限。

领域普通录入和问账页面调用相同应用服务；通用 Agent 设置/授权/确认入口转到中央管理，消费草稿/来源待整理属于业务待办继续保留。Nexus 中不新增 Ledger 审核工作台。

迁移顺序：中央影子授权 → owner 映射/Query 查询 → record.capture → correct/void/refund → 旧 auth/MCP 注销。每能力只一个 auth_mode；新模式失败不使用旧 grant 回退。旧 commit/reject 的 428 在旧模式下不放宽。

## 7. 验收

中央撤销、跨 owner/Agent、scope/披露缩减、旧目录调用；普通记账无二次确认；同键异内容/事务失败回滚/同 command 并发；修正版本冲突；资金语义不能误记消费；unknown 和多币种不误算；历史 Receipt hash 不变；旧审批权限不扩大。以 `tests/test_agent_control_plane.py`、`test_machine_agent_api.py` 和真实 PostgreSQL 并发 fixture 验证，新增 ADR 不代表代码已通过。
