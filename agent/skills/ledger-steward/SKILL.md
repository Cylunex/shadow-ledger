---
name: ledger-steward
description: 整理 Ledger 待处理草稿、周期事项和预测建议，帮助用户决定下一步，但不执行合并或确认。
---

只使用当前 steward 目录的 attention/explain。按 kind 分别读取，不把分页摘要当全部事项。
预测与周期金额都是 projected；unknown_amount 只是缺金额提醒，不等于零元消费。
解释确定性理由，必要时提供待处理页面或 shadow 引用。是否续订、关联、合并、确认或删除由用户
在审核界面决定；不得给模型注册隐藏 L2 工具，也不把建议“已处理”解释成已发生消费。
