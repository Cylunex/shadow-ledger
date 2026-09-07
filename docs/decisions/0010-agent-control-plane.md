# ADR 0010：Agent 提案、精确签收与确定性查询

> 2026-09-07 设计衔接：统一鉴权、Agent 与 Nexus 目标规范以 [本项目接入设计](../nexus-integration-design.md) 为准。不再新增领域自管 OIDC/Session、Agent registry/Grant/审批中心或模型/工具通用循环；普通明确写入采用中央 current_intent。新决策由 ADR 0011 接续；旧 428 与历史凭证不放宽、不重写。以下相关条目仅描述旧实现/历史阶段，不能作为新增实现继续复制；未迁移接口仍保留当前安全限制。

已接受，2026-09-04；用户要求研究并直接实施 Agent 增量优化。

## 决策

- Agent 是非可信提案者。保留 Record、MoneyEntry、ConsumptionEvent，不引入账户、余额或新账务内核。
- 新 Gateway 按 scope、owner grant、任务 Skill 和披露 Profile 交集提供工具；调用时重新授权，
  目录哈希绑定授权快照。remote_minimal 不返回标题、备注、来源原文；local_private 显式配置。
- 数字由受限 QuerySpec 和 Decimal/SQL 计算，返回不可变查询结果、指纹、截止点、未知/排除数、
  exact/partial/projected 口径及 facts_text。查询结果不是完整历史账本快照，外部 Host 的自然语言不可控。
- L1 仅创建/修改本 Agent 草稿与关联已有来源；规则解析缺字段时返回缺失项，不猜金额、不访问 URL。
- L2 不进入工具目录。Agent 可请求精确草稿审核，但 allow_confirm 仅代表请求/执行审核资格，
  不能替代逐动作授权。浏览器用户审核冻结内容后签发短时一次性 Grant；执行重新核对 owner、
  Agent、内容哈希、revision、policy 和当前 grant，在同一事务确认、消费 Grant、写 Receipt 与 Outbox。
- 旧 Nexus commit/reject 没有有效 approval_grant_id 时 fail closed；旧长期布尔权限不自动生成批准。
  Nexus 需适配 Ledger 审核链接和 Grant 引用；本轮不修改其他项目或生产 Identity 配置。
- Receipt 为 Ledger 数据库持久凭证及内容哈希，不宣称公钥签名、抵抗数据库管理员篡改或外部公证。
- Agent 审核只接受真实浏览器 Session（开发模式除外），不把 LAN bypass、Bearer 或自报 approved_by
  当人类签收。批量批准只能冻结逐项 ID/版本；不得批准动态筛选集合。
- 远程 MCP 可使用已配置的 Ledger Agent Bearer，默认关闭；协议由已锁定官方 SDK 实现。
  不伪装成 OAuth 授权服务器，不自建 issuer/CIMD/DCR，不为未联调 Identity 宣称 OAuth 即插即用。

## 验收

审批前后变更、过期、重放、权限撤回、跨 owner/Agent、事务中断、数值口径、工具越权、提示注入、
输出最小化及迁移必须有测试。原有用户手工确认仍可用，不能被 Agent 调用。
