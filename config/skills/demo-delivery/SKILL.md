---
name: demo-delivery
description: 示例技能：演示「需求 → 方案 → 实现 → 审查 → 测试 → 验收」的交付路径与冒烟写法
version: 0.1.0
source: manual
when_to_use:
  - 需要一条已被验证过的交付路径来跑通同类需求
  - 需要确认技能装载与冒烟链路本身是否可用
trigger:
  keywords:
    - 示例
    - demo
    - 交付
  intents:
    - delivery
  requires_artifacts:
    - requirement_spec
inputs:
  - name: requirement
    type: string
    required: true
    default: "{{workspace}}/input/requirement.md"
    description: 需求描述文本或需求文档路径
outputs:
  - requirement_spec
  - code_manifest
  - test_report
steps:
  - 解析需求输入，拆解功能点与验收标准，显式标注待澄清问题
  - 完成技术选型、方案评估与实现计划，覆盖全部功能点并闭环歧义
  - 依据需求规格与技术方案完成编码实现（含异常与边界处理），并通过导入自检
  - 对交付代码执行静态审查与需求一致性检查，阻塞问题必须拦截
  - 依据需求契约独立生成功能/异常/边界用例并真实执行，失败用例提缺陷单
  - 依据门禁证据签署验收意见，未通过则回退修复后重新执行
error_handling:
  - 门禁未通过时按回退策略回到修复节点，不要绕过门禁直接放行
  - 出现回退次数耗尽时中止并输出原因，不要无限重试
checkpoints:
  - 全部功能点均有实现映射
  - 边界用例通过率为 1.0
  - 审查阻塞问题清零
smoke:
  command:
    - python
    - scripts/hello.py
  expect_exit: 0
  timeout_s: 30
tags:
  - demo
  - delivery
---

# 示例技能：交付路径

这是一个**手写**技能（`source: manual`），用来演示 SKILL.md 的元信息规范与冒烟写法：

- 元信息必填项：名称 / 描述 / 适用场景 / 触发条件 / 入参说明 / 执行步骤；
- `smoke.command` 声明后，`python -m mas skill check demo-delivery` 会以本目录为工作目录
  执行该命令并断言退出码；
- 未声明 `smoke` 的技能只做静态校验，检查结果会明确标注「未做动态验证」。

沉淀出来的技能 `source` 为 `distilled`，并带 `source_run` 指回来源运行。
