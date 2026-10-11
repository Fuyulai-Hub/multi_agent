---
name: spec-scope-guard
description: 需求评估阶段量化交付规模并在超阈值时给出分批建议，避免单次交付范围过大导致下游实现节点超时或产出非法 JSON
version: 0.1.0
source: manual
when_to_use:
  - 产品 Agent 完成需求拆解、准备发布 requirement_spec 之前
  - 需求文档体量大、功能点明显偏多，担心下游 implement 单次生成撑不住
  - 历史上出现过「本节点未超时，但下游节点反复回退」的情况，需要在源头找原因
trigger:
  keywords:
    - 交付规模
    - 功能点过多
    - 分批交付
    - 需求过大
    - scope
  intents:
    - assess_scope
  requires_artifacts:
    - requirement_source
inputs:
  - name: function_points
    type: list
    required: true
    description: 已拆解的需求功能点列表，每项至少含 id 与 title
  - name: module_count
    type: int
    required: true
    description: 技术方案预估的模块数量；需求评估阶段可用功能点分组数近似
  - name: acceptance_criteria
    type: list
    required: true
    description: 验收标准列表，每项至少含 id 与 description
  - name: thresholds
    type: object
    required: false
    default: {max_function_points: 10, max_estimated_loc: 300}
    description: 规模阈值，可按项目基线调整；默认取本项目历史运行的经验值
outputs:
  - name: scope_guard
    type: object
    description: 结构 {function_point_count, estimated_loc, batching_suggestion, unmeasurable_criteria}，写入 requirement_spec
steps:
  - 统计功能点数量 function_point_count，统计 acceptance_criteria 条数
  - 估算代码规模 estimated_loc，按「模块数 × 每模块平均行数」估算；无模块信息时按功能点数 × 40 行作为下限估算
  - 与 thresholds 比对，任一维度超阈值即判定 scope_risk 为真，并记录是哪一个维度超标
  - scope_risk 为真时生成 batching_suggestion：按功能点的业务耦合度分组，建议首批只交付核心闭环所需的那一组，其余列为后续批次
  - 逐条检查 acceptance_criteria 是否可翻译成可求值表达式（形如 x.rate >= 1.0 或 y.failed == 0）；只含「尽量、合理、适当」等不可量化表述的条目写入 unmeasurable_criteria
  - 把 {function_point_count, estimated_loc, batching_suggestion, unmeasurable_criteria} 写入 requirement_spec 的 scope_guard 键
  - batching_suggestion 非空时，同步把分批建议追加到 open_questions，让后续环节可见而不是只躺在字段里
error_handling:
  - 缺少 module_count 时不得跳过估算，必须退化为按功能点数的保守估算并在报告中标注估算方式为「退化估算」
  - 阈值本身不是判失败的依据：超阈值只触发建议，不改变验收结论，不得因此拒绝发布 requirement_spec
  - estimated_loc 是估算而非精确值，摘要里必须写明「估算」二字，避免下游当作真实代码行数引用
checkpoints:
  - requirement_spec 出现 scope_guard 键，且四个子键齐全
  - scope_risk 为真时 open_questions 中能找到分批建议条目
  - unmeasurable_criteria 中的每条都给出了原始描述文本，便于人工改写
smoke:
  command:
    - python
    - scripts/selfcheck.py
  expect_exit: 0
  timeout_s: 30
tags:
  - product
  - scope-control
---

# spec-scope-guard 补充说明

## 阈值从哪里来

默认阈值（功能点 > 10、估算行数 > 300）不是拍脑袋定的，取自本项目历史运行：

| 运行 | 功能点数 | 下游实际产出 | 结果 |
| --- | --- | --- | --- |
| 20261011-010835-13e0 | 13 | 11 个文件 / 520 行 | implement 先是返回非法 JSON，随后 test 节点崩溃，15 步 9 回退后未通过 |
| 20261011-050247-1610（离线） | 7 | 小规模模块 | 7 步 0 回退通过 |

两条运行的能力落差足够明显，因此把 10 与 300 设为默认警戒线。项目换了模型或调整了超时设置后，
这两个值应重新标定，不要一直沿用。

## 它解决什么、不解决什么

- 解决：需求粒度失控导致的**下游**失败——这类失败在需求节点看不出来，等到回退几轮才暴露；
- 不解决：单个功能点内部的逻辑错误；
- 不做硬性拦截：超阈值只是建议，是否分批由人决定。这条边界很重要，
  否则技能会变成需求阶段的阻塞门禁，与本项目「intake 不做门禁」的设计冲突。
