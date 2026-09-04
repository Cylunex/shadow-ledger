# Agent 增量优化方案与实现（1.3.0）

研究日期：2026-09-04。代码基线：1.2.0 / c54d9dc，而非附件引用的旧 main / 5f71602。
决策见 [ADR 0010](decisions/0010-agent-control-plane.md)。本文区分已实现能力和外部联调门槛。

## 1. 研究结论与取舍

附件提出“Agent 是非可信提案者”方向合理，但不能照搬 Activity/Transaction/Pocket：本项目仍使用
LedgerRecord + MoneyEntry + ConsumptionEvent，没有账户、余额、对账或资金执行。
工作台、来源观察、退款候选和消费身份已经在 1.2 完成，本轮不重复重写。

核验的近期项目、文章和方案：

| 一手来源（检索截至上述日期） | 借鉴机制 | 本项目的取舍 |
| --- | --- | --- |
| [Agent-Safe Pipeline](https://github.com/decionis/agent-safe-pipeline) | 不可信提案与独立执行器、精确内容绑定、一次性授权 | 用本地事务实现，不引入其外部策略/审批服务 |
| [personal-finance-agent](https://github.com/albeorla/personal-finance-agent) | 重要数字可追溯、grounding 验证、候选不冒充事实 | 借鉴证据封装，不吸收账户管理、现金流真相或数十个工具 |
| [GitHub Agentic Workflows Safe Outputs](https://github.github.com/gh-aw/reference/safe-outputs/) | Agent 输出结构化请求，独立受控执行环节写入 | 新增参考：工具目录和隐藏执行器分离；不把金融写权限交给模型 |
| [LangGraph Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts) | 人工介入、持久恢复以及重放副作用的处理 | 新增参考：框架暂停不是授权真相；Ledger 自己持久保存审批和执行结果 |
| [Google AP2](https://github.com/google-agentic-commerce/AP2) | 分离授权意图与实际结果 | 只参考分层机制，不接入支付协议或资金操作 |
| [MCP 2026-07-28 changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog) | 无状态调用、server/discover、显式状态、DCR 弃用方向 | 使用仓库锁定的官方 Python SDK 2.1.1；不手写协议兼容分支 |
| [MCP 授权规范](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization) | 资源/受众绑定与授权服务器职责 | Agent Bearer 不冒充 OAuth；Identity 未联调前不发布虚假的 Protected Resource Metadata |

这些项目的 README 是机制参考，不是安全认证。没有把附件中所有项目、工具数量、论文或营销结论
都视作已验证事实；没有引入论文实验代码或个人 Internet-Draft 作为硬协议依赖。

## 2. 这次实现的完整闭环

### Research：代码算数，保存结果

- `ledger_overview`：月、时区、单币种支出/收入/退款/净支出，预算按独立权限投影。
- `ledger_search`：确认记录的有界检索，支持月份、商家/商品 ID、场景、类型、支付方式；不接受 SQL。
- `ledger_entity_insights`：规范身份家族的购买次数、平均间隔、评分分布；排除收入/退款。
- `ledger_attention`：预测、周期、草稿、金额未知、身份合并建议的摘要；不执行建议。
- `ledger_explain`：取回同 Agent、同 owner、同目录上下文的查询结果并核对结构化数字声明。

QueryRun 保存结果及内容哈希，7 天后不再允许 explain；数据保留由受控运维管理，没有后台自动删库。
QueryFingerprint 标识工具、受限 QuerySpec、聚合版本；相同查询不同时间可能得到不同结果，
由 QueryRun ID、generated_at、result_hash 区分。不是历史账本快照，不承诺能还原查询时整个数据库。

金额使用 Decimal 字符串。`exact` 表示所选范围内已知记录的确定性值，不保证用户没有漏记；
`partial` 表示有未知金额或达到扫描上限；`projected` 只用于预测/计划。不同币种不相加。
overview 单次最多扫描 10,000 条，超过后明确 truncated/partial，不能把截断合计说成月度总计。
provenance_refs 最多 50 个并单独标截断，不因来源引用列表截断而改变完整汇总的状态。
entity 的平均间隔单位为天，保留四位小数；不推导多商品单价。

前端“洞察 → 问账”直接显示同一 Gateway 的 facts_text 和结构化数字，支持有限中文问题；
不是任意聊天机器人，不启用外部 LLM。复杂自然语言交给已配置 Host 选择工具。
ClaimVerifier 只核验提交的 metric_id/value/currency/status；不声称能控制外部 Host 的最终自然语言。

### Capture：不可信输入，草稿优先

- parse_capture：严格解析明确带元/¥ 的金额、支付标签和调用者明确提供的时间；多个金额、非正金额、
  转账/还款等非消费语义、币种歧义返回 missing_fields/review_flags，不选第一个数字落账。
- create_draft：money-only，金额/时间/币种/schema 校验；用户提供的 source_text 独立保存为原文。
- revise_draft：仅本 Agent 未确认的 money-only 草稿，版本检查，不修改已确认事实。
- attach_source：仅同 owner 且已出现在本 Agent 捕获链的来源 ID；不凭任意 Asset URI 越权绑定。

三种写操作均有 Agent 命名空间的幂等命令。工具不上传文件、访问原文中的 URL 或自动确认。
真实文件上传仍走 Asset，富消费字段仍走既有隐藏 Nexus Review。未引入 ASR、Mem0 或默认云模型上传。
待处理页面新增文本解析预览与“核对后保存草稿”；普通消费录入保持可用。

### Effect：逐次批准与持久凭证

1. 隐藏 Host 用 write scope + allow_confirm 请求审核本 Agent 的指定草稿/revision。
2. Ledger 冻结完整记录和证据引用，保存 action + snapshot 的 SHA-256 与策略决定；有效期 24 小时。
3. 已登录用户在工作台核对内容，勾选确认，签发 10 分钟一次性 ApprovalGrant。
4. 浏览器或同 Agent 的隐藏执行接口消费 Grant；重新验证内容、revision、owner、Agent、当前权限和策略。
5. 同一事务完成确认/受保护的草稿删除、Grant 消费、Receipt、Audit 和确认 Outbox。
6. 重试返回原 Receipt，不重复确认；中途失败整笔事务回滚。

AgentIntent 同时承担审核请求和冻结显示快照，避免再建一个一一对应的空 ApprovalRequest 表。
PolicyDecision 分别记录提请人工、批准/拒绝；AgentApprovalGrant 和 AgentExecutionReceipt 独立保存。
Receipt 包含真实批准人、提议 Agent、动作、参数哈希、前后 revision、执行器和策略版本。
Receipt 是数据库持久证据和内容哈希，不是数字签名，不抵御有数据库写权限的管理员篡改。
没有伪造历史 Receipt，没有根据旧 allow_confirm 自动迁移批准。

“拒绝提案”保留草稿；“批准删除草稿”是另一种需要精确签收的动作。有业务引用/附件的草稿仍拒绝删除。
记录已被普通界面修改或删除时旧批准失效；用户需重新发起审核。没有动态集合的批量审批。

## 3. 工具目录与披露

research 5 个工具；capture 4 个；steward 2 个（复用 attention/explain），总共 9 个稳定工具。
服务每次重新求 scope ∩ owner grant ∩ Skill，目录哈希绑定授权字段、更新时间、Skill 内容、schema 和 policy。
旧目录调用返回 `tool_catalog_changed`；没有按 Agent ID 单独缓存工具权限。
Skill 文字随目录返回；MCP 启动按任务加载对应能力包。旧 ledger-assistant 保留兼容说明。

- remote_minimal：无标题、备注、商家原文、来源正文或附件内容；保留受控标签、规范身份及 URI。
- local_private：仅显式配置的本机 v2 MCP，搜索额外返回标题；不是任意原文读取授权。
- Nexus/浏览器审核：独立隐藏界面展示完整冻结草稿，不作为可选模型披露 Profile。

Grant 的布尔字段、作用域和更新时间进入上下文哈希；权限更新须通过正常数据库更新时间流程。
审计只保存固定工具名/动作/资源 ID/Agent ID，不记录工具输入、金额、原文或 Bearer。
目录快照不保存模型/供应商身份猜测，没有隐藏思维链日志。

## 4. 接口与兼容

机器 API 前缀 `/api/machine/v1/agent`：

- GET `/catalog?skill=research|capture|steward`
- POST `/tools/call`：skill、catalog_hash、tool、arguments（严格 schema）
- POST `/review-requests`：record_id、revision、action；Idempotency-Key 必填，返回审核引用和路径
- GET `/review-requests/{id}`：仅同 Agent 读取状态/批准引用
- 旧 `/drafts/{id}/commit|reject` 及 Nexus 对应接口新增 `approval_grant_id`；没有批准返回 428。

浏览器 `/api/v1/agent` 提供 catalog、tools/call、reviews、decision、execute、receipts。
审批不接受 Bearer/LAN bypass 或自报批准人；需要正常 Ledger Session，保留 Origin/CSRF。
开发模式可用测试身份，但生产模式禁止 dev_auth。

**兼容性变化**：旧 Nexus commit/reject 不再仅凭长期权限执行。升级 Ledger 前应先升级 Host：
请求审核 → 打开 Ledger 审核页面 → 用户批准 → 取回 Grant → 用原 revision 和 Grant 执行。
浏览器也可批准并直接执行，Host 随后重复提交会拿到原 Receipt。Nexus 合同返回真实
`shadow://ledger/receipts/{id}`，不再把 record_ref 伪装成 receipt。本轮没有修改 Nexus 仓库或生产配置。
旧读取和草稿创建 API 保持兼容；旧 stdio MCP 默认保持兼容模式。

## 5. MCP 接入

本机：`LEDGER_MCP_AGENT_V2=true`，`LEDGER_MCP_SKILL=research|capture|steward`。
保留 owner 文件；写草稿仍需 `LEDGER_MCP_ALLOW_DRAFTS=true`。
`LEDGER_MCP_DISCLOSURE=remote_minimal` 默认；明确需要本机标题时设 local_private。

远程：`LEDGER_MCP_HTTP_ENABLED=true`，必须已有受限 Agent registry/secrets 和精确 allowed_origins。
research `/mcp/`、capture `/mcp-capture/`、steward `/mcp-steward/`，路径不改变每连接权限。
每个请求验证 `ledger` audience Bearer，scope/grant 每次工具调用检查；Cookie 和协议 Session 不授予权限。
MCP `tools/list` 返回参数中绑定当前 catalog_hash，调用应原样携带。默认没有 L2/确认工具。
由官方 SDK 提供 Streamable HTTP、版本协商与 server/discover，不实现第二份 JSON-RPC 状态机。

这是预配置 Bearer 入口，不是 OAuth 自动发现产品：未实现 Identity 的 MCP 资源 audience、CIMD/DCR
和授权服务器部署，不发布虚假元数据。需要 OAuth 的 Host 必须先联调唯一 Identity，不能部署第二个 issuer。
不支持长期审批的 MCP input_required，也不添加远程 grant 管理或任意 Token 接入。

## 6. 验证与发布门槛

新增测试覆盖 canonical vectors、金额歧义/注入、Skill 工具白名单、权限撤回、跨 owner、批准后修改、
过期、策略变化、失败回滚、幂等 Receipt、数字/币种/partial 声明校验、本机与远程 MCP。
PostgreSQL 专用测试覆盖同 Grant 并发消费，迁移验证旧反馈保留且不凭空创建任何批准。

本轮实际验收（2026-09-04）：

- SQLite 全量 125 passed / 6 skipped（跳过 PostgreSQL 专用项）；隔离 PostgreSQL 全量 131 passed。
- 前端 Node 4 项通过，Ruff、锁文件检查、git diff --check 通过，三类 Skill 校验通过。
- 浏览器合成数据跑通人工审批与 Receipt、月度问账、文本捕获只保存草稿；控制台无错误，390px 无横向溢出。
- 应用与 SDK wheel 离线构建成功；仓库外验证打包 Skill、v2 MCP 目录，以及空 PostgreSQL schema 到 0008 的完整迁移。
- 所有数据库/浏览器验收使用临时测试数据；未读取生产消费记录，未推送、部署或改动 Identity/Nexus 配置。

迁移 0008 只加六张 Agent 控制/证据表，不改写 Record 或金额；应用/插件统一为 1.3.0。
先备份，再用专用隔离数据库验证 0007→0008，再升级 Web/Worker/Host。数据库降级会丢新审批凭证，
有数据后不要常规 downgrade；优先前向修复。完整恢复仍用数据库备份。

未包含：生产 NAS 发布、真实远程 Host OAuth 联调、外部 LLM/语音、批量签收、偏好候选引擎、
公钥签名、任意 SQL、账户/余额/支付内核。它们不是这一版已经实现或已验收的能力。
