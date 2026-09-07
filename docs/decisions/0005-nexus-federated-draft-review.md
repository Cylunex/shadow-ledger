# ADR 0005：Agent 草稿以引用方式汇入 Nexus Review

> 2026-09-07 设计衔接：统一鉴权、Agent 与 Nexus 目标规范以 [本项目接入设计](../nexus-integration-design.md) 为准。不再新增领域自管 OIDC/Session、Agent registry/Grant/审批中心或模型/工具通用循环；普通明确写入采用中央 current_intent。新决策由 ADR 0011 接续；旧 428 与历史凭证不放宽、不重写。以下相关条目仅描述旧实现/历史阶段，不能作为新增实现继续复制；未迁移接口仍保留当前安全限制。

## 状态

已接受，2026-08-25。

## 背景

模型可见的 `ledger.records.draft` 允许普通 Conversation 绕过 Nexus Review，直接在 Ledger 中
批量创建草稿。虽然草稿尚未正式入账，但用户无法在统一审核入口看到、批量确认或退回这些记录，
模型还可能在探测分类时制造重复草稿。

## 决策

统一 Shadow Nexus Profile 不再选择任何领域 draft/write capability。模型只负责向 Nexus 返回
结构化 Proposal。Ledger 继续拥有草稿数据，并在隐藏的 `ledger.records.write` 边界下提供：列出
同一 Agent 创建的 pending 草稿、提交指定草稿、删除被 Nexus 退回的指定草稿。

Nexus 只保存审核所需的字段快照、revision、来源和 `shadow://ledger/records/{id}` 引用。所有隐藏
操作仍要求独立 write scope、`allow_confirm` owner grant，并验证草稿确由同一 Agent 创建。正式
入账和退回均由用户在 Nexus Review 中触发；模型不能调用这些接口。

## 后果

- 旧版本或领域流程产生的 Agent 草稿可以汇入统一 Review，而不迁移领域所有权；
- Nexus 拒绝一个已存在的 Ledger 草稿时会同步删除该草稿，并留下元数据审计；
- 删除与拒绝审计在同一事务提交，重复退回按 Agent 和记录引用幂等重放；
- 领域浏览器自身的普通编辑草稿不自动汇入，除非后续显式标记为 Nexus-reviewable；
- Account、Payment、余额、转账和导出边界保持不变。
