# Ledger Shadow Agent 插件

> 2026-09-07 设计衔接：统一鉴权、Agent 与 Nexus 目标规范以 [本项目接入设计](nexus-integration-design.md) 为准。不再新增领域自管 OIDC/Session、Agent registry/Grant/审批中心或模型/工具通用循环；普通明确写入采用中央 current_intent。新决策由 ADR 0011 接续；旧 428 与历史凭证不放宽、不重写。以下相关条目仅描述旧实现/历史阶段，不能作为新增实现继续复制；未迁移接口仍保留当前安全限制。

> 1.3 安全升级：ADR 0010 覆盖本文早期 commit/reject 的长期授权语义。allow_confirm 仅为审核资格，
> 还必须有 Ledger 用户签发的精确一次性 approval_grant_id；缺失返回 428。真实 Receipt 使用独立 URI。
> v2 工具目录、Skill 和 MCP 接入详见 [Agent 方案](agent-optimization-2026-09.md)。

Ledger 仍是独立部署、独立存储和独立授权的领域应用。仓库内插件文件只描述远程能力，既不包含
DSH/Cordis 依赖，也不把业务代码或领域数据搬入 Platform。Platform 为独立 `shadow-ledger`
Profile 生成一份通用 Bundle，DSH 使用 Ledger 专属凭据直连本服务，Platform 不代理财务数据。

## 首期能力

| capability | 风险 | 结果 |
|---|---|---|
| `ledger.summary.read` | L0 | 指定月份、单币种的收入/支出/退款摘要 |
| `ledger.records.read` | L0 | 最多 50 条确认账目的最小字段与 `shadow://` 引用 |
| `ledger.budgets.read` | L0 | 预算目标、净支出、剩余额度和金额未知记录数 |
| `ledger.records.draft` | L1 | 模型可见入口创建 money-only 草案；隐藏 Nexus Review 可保留富消费字段 |
| `ledger.records.write` | L2 | 仅由 Nexus 在用户审核后提交同一条 Agent 草稿；对模型隐藏 |

Ledger 产品不拥有 Account、余额、卡号或转账，因此插件不虚构账户 API。不同币种
分别查询，`exchange_rate_applied` 恒为 false。账目读取不返回标题、备注、商家/渠道原文、消费
明细、Capture 原文、账号或支付凭据。草案支持 ADR 0008 的可选支付方式标签，但不接受账户、汇率或确认标志。按
ADR 0007，模型隐藏的 Nexus Review 创建入口可额外保留明确提供的消费场景、原始商家/渠道、
消费明细与 `shadow://` 证据引用；普通 Agent draft 和 stdio MCP 仍保持 money-only。

## 鉴权与资源授权

部署环境通过 `LEDGER_AGENT_REGISTRY_PATH` 和 `LEDGER_AGENT_SECRETS_DIR` 指向受限文件。机器
Bearer 固定验证 `ledger` audience，并针对每个 capability 验证精确 scope。通过身份鉴权后，
`ledger_agent_grants` 再把 agent ID 映射到唯一 owner，并分别控制 summary、records、budgets、
drafts、confirm。Bearer 有效但 scope 不足返回 403；资源 grant 缺失返回 404；grant 存在但动作未授权
返回 403。

grant 由 Ledger 受控管理流程创建，不通过 Agent Tool 自助扩大。首期尚无 grant 管理 UI；这是
明确的 P1 运维边界，不允许通过配置中的 owner 请求参数绕过。

## 草案、幂等与审计

`POST /api/machine/v1/agent/drafts` 要求 8—128 字符的 `Idempotency-Key`。服务把 agent ID 纳入
幂等命名空间；同 key 同 payload 返回原草案，同 key 不同 payload 返回稳定 409。金额使用
Decimal 且必须为正数，币种为三个 ASCII 字母，发生时间必须带 UTC offset，timezone 必须是
有效 IANA 名称。

草案写入现有 LedgerRecord，状态保持 `draft`，不参与正式汇总并可由 Ledger 用户删除。Nexus
Review 中的用户确认可通过隐藏 L2 能力提交同一条 Agent 草稿；接口要求独立 write scope、
`allow_confirm` grant 和 revision，且不能修改草稿内容。审计采用允许列表：读取仅记录月份、币种、
结果数和截断标志；创建与确认只记录 agent ID、record ID、状态与 record kind，不记录金额、标题、
备注或 Bearer。

统一 Nexus Profile 不再向模型选择 `ledger.records.draft`。隐藏 write 边界还允许 Nexus 列出同一
Agent 的 pending 草稿，并在用户退回时删除指定草稿。Nexus 只保存审核快照和 `shadow://` 引用，
Ledger 仍是草稿与正式事实的唯一所有者。退回会在同一数据库事务中删除草稿并写入最小审计；
重复请求依据同一 Agent 的拒绝审计安全返回 replay，不会把批量 Review 卡在半完成状态。

## Platform/DSH 验证

Platform validator 校验 Definition、Manifest、OpenAPI 和所有描述符。独立 Profile 应固定 DSH
distribution 与 Tools API 为 `0.1.1-rc.2`。统一 Nexus Profile 只选择三个读取 capability；独立
Ledger Profile 可按自身审核边界选择草稿 capability。实例配置只登记
`SHADOW_LEDGER_BASE_URL` 与 `SHADOW_LEDGER_AGENT_TOKEN` 环境变量名。生成 Bundle 只把
`@deepseek-ai/dsh-tools` 放入 peer dependency；本仓库不维护领域专属 DSH npm 包。

普通 Profile 不注册隐藏的正式入账、导出、难撤销调整或资金执行。Nexus 用户审核后可调用隐藏
L2 入账能力；导出和难撤销调整至少 L3，并在开放前实现 ConfirmationReceipt；任何资金执行均为
L4 且不进入普通 Profile。

## 与 stdio MCP 的区别

`ledger-mcp` 是用户主动配置、在本机由 MCP host 启动的独立入口，不进入 Platform Agent Manifest，
也不复用 Agent Bearer/grant。它通过权限受限 owner 文件绑定单一工作区，默认只读；显式开启写入
后也只注册 money-only draft。两条入口都不向模型开放正式确认、撤销、导出或资金操作。
