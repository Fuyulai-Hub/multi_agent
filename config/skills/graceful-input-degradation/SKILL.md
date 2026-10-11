---
name: graceful-input-degradation
description: 当上游产物缺失导致本节点不具备执行条件时，输出带缺失清单的降级说明并给出回退建议，而不是崩溃或产出空摘要
version: 0.1.0
source: manual
when_to_use:
  - 任意任务节点感知到所需黑板产物缺失（perception.missing 非空）
  - 上游节点失败，本节点被调度执行但不具备完整输入
  - 需要判断「该回退」还是「可降级继续」，而不是无条件判失败
trigger:
  keywords:
    - 输入缺失
    - 降级
    - 上游失败
    - 产物缺失
    - degradation
  intents:
    - degrade_gracefully
  requires_artifacts: []
inputs:
  - name: missing
    type: list
    required: true
    description: 感知阶段得到的缺失产物名列表（perception.missing）
  - name: found
    type: list
    required: true
    description: 已就绪的产物名列表（perception.found 的键）
  - name: node_id
    type: string
    required: true
    description: 当前节点 id
  - name: producer_map
    type: object
    required: true
    description: 产物名到产出角色/节点的映射，用于说明「缺的这个应该由谁产出」
outputs:
  - name: degradation_note
    type: object
    description: 结构 {missing, expected_producers, suggestion, can_continue}，写入 result.metrics 的 degradation 键
steps:
  - 取 perception.missing 与 perception.found，两者同时为空时视为感知阶段本身异常，先报感知错误而不是降级
  - 对每个缺失产物查 producer_map，得到 expected_producers 列表；查不到的记「未知产出方（需补 flow.yaml 的 outputs 声明）」
  - 判断 can_continue：只有当缺失项全部属于「可选输入」（不在交付契约的必填字段链上）时才为真，否则为假
  - 生成摘要文案，固定形如「本轮不具备<职责>的输入条件：上游仅提供<found 列表>，缺失<missing 列表>（应由<producer>产出）」
  - 把 {missing, expected_producers, suggestion, can_continue} 写入 result.metrics["degradation"]
  - can_continue 为假时判定节点 fail，摘要里给出回退目标建议；为真时继续按降级路径产出
  - 摘要必须是非空字符串；历史上出现过 node_end 摘要为空串的情况，那会让排查完全失去线索
error_handling:
  - 严禁为了凑齐输入而臆造内容填充产物，缺失就是缺失
  - 禁止把 AttributeError / KeyError 这类崩溃当作降级结果，崩溃与降级是两回事
  - producer_map 查不到产出方时显式标注未知，不要猜一个角色名
checkpoints:
  - perception.missing 非空时，node_end.payload.summary 必含「输入条件」四字
  - node_end.payload.summary 不得为空字符串
  - events.jsonl 中不出现因输入缺失直接抛出的未捕获异常
smoke:
  command:
    - python
    - scripts/selfcheck.py
  expect_exit: 0
  timeout_s: 30
tags:
  - robustness
  - collaboration
---

# graceful-input-degradation 补充说明

## 这个技能的本质是「推广已有的正确做法」

它不是凭空发明的规则。Reviewer Agent 在 `20261011-010835-13e0` 这次运行里已经表现出
教科书级的正确行为，其 node_end 摘要原文为：

> 本轮不具备代码审查的输入条件：上游仅提供 requirement_spec 与 solution_design，
> code_manifest、implementation_summary 全部缺失

这条摘要同时做到了三件事：说清缺什么、说清手上有什么、不硬编内容凑数。
问题是这只是 reviewer 一处的孤例，同一个项目里 tester 面对同类情况直接抛出了
`AttributeError`，而且 implement 节点出现过摘要为空串的 node_end。

所以本技能做的是把已验证的个例固化成全节点通用的规范，风险低、收益明确。

## 摘要为什么要求非空

实测中 implement 节点存在 summary 为空串的 node_end 记录。这类空摘要在回退排查时毫无价值——
不知道是成功了一半还是完全没做。强制非空是本技能最容易见效的一条约束。
