# Ledger Shadow Agent 插件

Ledger 仍是独立部署、独立存储和独立授权的领域应用。仓库内插件文件只描述远程能力，既不包含
DSH/Cordis 依赖，也不把业务代码或领域数据搬入 Platform。Platform 为独立 `shadow-ledger`
Profile 生成一份通用 Bundle，DSH 使用 Ledger 专属凭据直连本服务，Platform 不代理财务数据。

## 首期能力

| capability | 风险 | 结果 |
|---|---|---|
| `ledger.summary.read` | L0 | 指定月份、单币种的收入/支出/退款摘要 |
| `ledger.records.read` | L0 | 最多 50 条确认账目的最小字段与 `shadow://` 引用 |
| `ledger.budgets.read` | L0 | 预算目标、净支出、剩余额度和金额未知记录数 |
| `ledger.records.draft` | L1 | 幂等创建等待用户审核的 money-only 草案 |

Ledger 产品不拥有 Account、余额、卡号、支付方式或转账，因此插件不虚构账户 API。不同币种
分别查询，`exchange_rate_applied` 恒为 false。账目读取不返回标题、备注、商家/渠道原文、消费
明细、Capture 原文、账号或支付凭据。草案输入也不接受账户、支付方式、汇率或确认标志。

## 鉴权与资源授权

部署环境通过 `LEDGER_AGENT_REGISTRY_PATH` 和 `LEDGER_AGENT_SECRETS_DIR` 指向受限文件。机器
Bearer 固定验证 `ledger` audience，并针对每个 capability 验证精确 scope。通过身份鉴权后，
`ledger_agent_grants` 再把 agent ID 映射到唯一 owner，并分别控制 summary、records、budgets、
drafts。Bearer 有效但 scope 不足返回 403；资源 grant 缺失返回 404；grant 存在但动作未授权
返回 403。

grant 由 Ledger 受控管理流程创建，不通过 Agent Tool 自助扩大。首期尚无 grant 管理 UI；这是
明确的 P1 运维边界，不允许通过配置中的 owner 请求参数绕过。

## 草案、幂等与审计

`POST /api/machine/v1/agent/drafts` 要求 8—128 字符的 `Idempotency-Key`。服务把 agent ID 纳入
幂等命名空间；同 key 同 payload 返回原草案，同 key 不同 payload 返回稳定 409。金额使用
Decimal 且必须为正数，币种为三个 ASCII 字母，发生时间必须带 UTC offset，timezone 必须是
有效 IANA 名称。

草案写入现有 LedgerRecord，状态保持 `draft`，不参与正式汇总并可由 Ledger 用户删除。最终
确认只能由 Ledger 用户会话完成。审计采用允许列表：读取仅记录月份、币种、结果数和截断标志；
草案仅记录 agent ID、record ID、状态与 record kind，不记录金额、标题、备注或 Bearer。

## Platform/DSH 验证

Platform validator 校验 Definition、Manifest、OpenAPI 和所有描述符。独立 Profile 应固定 DSH
distribution 与 Tools API 为 `0.1.1-rc.2`，选择本插件的四个 capability，并通过实例配置只登记
`SHADOW_LEDGER_BASE_URL` 与 `SHADOW_LEDGER_AGENT_TOKEN` 环境变量名。生成 Bundle 只把
`@deepseek-ai/dsh-tools` 放入 peer dependency；本仓库不维护领域专属 DSH npm 包。

普通 Profile 不注册正式入账、导出、难撤销调整或资金执行。正式入账至少 L2；导出和难撤销
调整至少 L3，并在开放前实现 ConfirmationReceipt；任何资金执行均为 L4 且不进入普通 Profile。
