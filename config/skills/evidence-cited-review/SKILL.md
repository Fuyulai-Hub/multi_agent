---
name: evidence-cited-review
description: 审查结论必须携带可追溯证据，每条阻塞问题都要能对上静态分析规则与具体需求功能点，缺溯源的问题降级为非阻塞
version: 0.1.0
source: manual
when_to_use:
  - Reviewer Agent 输出 review_report、准备按阻塞项拦截本次交付之前
  - 审查结论被研发质疑「到底违反了哪条需求」时，需要给出可核对的溯源依据
  - 需要区分「真阻塞」与「只是看着别扭」，避免把无溯源的意见升级成阻塞项
trigger:
  keywords:
    - 审查证据
    - 阻塞溯源
    - 功能点映射
    - 审查结论
    - evidence
  intents:
    - cite_evidence
  requires_artifacts:
    - code_manifest
    - requirement_spec
inputs:
  - name: findings
    type: list
    required: true
    description: 静态分析与一致性检查结果，每项至少含 rule、file、line、message
  - name: function_points
    type: list
    required: true
    description: 需求功能点列表，每项至少含 id 与 title
  - name: fp_mapping
    type: object
    required: false
    default: {}
    description: 功能点到实现函数的映射（取自 code_manifest），用于把 finding 归属到功能点
outputs:
  - name: review_report
    type: object
    description: 含 blocking_items（每项带 rule / file / line / fp_id / suggestion）与 untraceable（已降级的无溯源项）
steps:
  - 逐条取出 finding 的 rule、file、line、message 四项；任一项缺失的 finding 直接计入 untraceable，不进入阻塞候选
  - 用 file 与所在函数/符号查 fp_mapping，得到该 finding 归属的功能点 id；查不到的进入候选待定
  - 候选待定的 finding 再按 message 中的标识符（函数名、功能点编号）与 function_points 做一次匹配；仍匹配不上的计入 untraceable
  - 对每条已溯源 finding 生成 suggestion：写明「违反了 <fp_id> 的哪条约束」并给出最小改法，不写「建议优化」这类无指向的意见
  - 汇总写回 review_report：blocking_items 只保留四项齐备且 suggestion 非空的条目，untraceable 单独成键保留
  - 统计 blocking 数量写回 review_report.blocking；该数字会被 acceptance 门禁表达式 review_report.blocking == 0 直接求值，必须与 blocking_items 长度一致
  - untraceable 非空时在 node_end 摘要里写明有多少条因无法溯源而被降级，避免它们被静默丢弃
error_handling:
  - 严禁为了凑溯源而把 finding 硬塞给无关功能点，宁可降级也不要错配
  - blocking 计数与 blocking_items 长度必须一致，不一致会让门禁表达式求值出现与实际不符的结果
  - fp_mapping 缺失时按 message 匹配兜底，两条路都走不通才降级，不得跳过兜底直接降级
checkpoints:
  - review_report.blocking_items 每项同时含 rule、file、line、fp_id、suggestion 五个键
  - review_report.blocking 等于 len(blocking_items)
  - review_report.untraceable 中的每项都保留了原始 message，便于后续补充溯源
smoke:
  command:
    - python
    - scripts/selfcheck.py
  expect_exit: 0
  timeout_s: 30
tags:
  - reviewer
  - traceability
---

# evidence-cited-review 补充说明

## 它补的是哪一环

`mas/sandbox.py` 的 AST 静态分析已经能把 rule / file / line 三要素产出得很扎实
（现有 12 条规则，如 `missing_function_docstring`、`bare_except`、`mutable_default`），
所以本技能**不重复做静态分析**。

真正缺的是第四要素：这条问题到底违反的是**哪一条需求功能点**。没有这一环，
审查结论在「拦住了交付」这件事上是站不住的——研发只能看到「第 42 行缺文档字符串」，
看不到「这导致 FP-3 的契约无法满足」。

## 为什么要降级而不是一律阻塞

`flow.yaml` 的验收门禁里有一条 `review_report.blocking == 0`，
任何一条被判为阻塞的 finding 都会挡住整条流水线。如果无溯源的意见也能进阻塞清单，
这条门禁就会失效——要么形同虚设（全部放行），要么过度拦截（什么都挡）。
降级机制是为了让这个计数保持可信。

## 优先级说明

本技能在上轮评估里排第四，因为它修理的是返工质量而非返工次数，
当前历史运行中没有证据表明它是瓶颈。给它留出 `untraceable` 这个可见通道，
正是为了先积累数据：如果 untraceable 长期为空，说明溯源能力本就充足，无需投入；
如果长期偏高，则说明 fp_mapping 的质量才是真问题。
