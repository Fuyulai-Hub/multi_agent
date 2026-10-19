---
name: contract-schema-guard
description: 在消费模型结构化输出之前做字段级结构校验，把「JSON 能解析但结构不对」转成带字段路径的结构化失败，而不是抛出无定位信息的裸异常
version: 0.1.0
source: manual
when_to_use:
  - 任务节点拿到模型返回的结构化 JSON，正准备加工后写入黑板产物
  - 拿到 AttributeError / KeyError 这类「消费时才炸」的异常，需要知道到底哪个字段类型不对
  - 模型偶发返回畸形结构（如列表元素是字符串而非对象）时，希望降级而不是整个节点崩掉
trigger:
  keywords:
    - 结构校验
    - 契约校验
    - 字段类型
    - 类型不符
    - schema
  intents:
    - validate_contract
  requires_artifacts: []
inputs:
  - name: contract_spec
    type: object
    required: true
    description: 本节点交付契约声明的字段清单，每项含 field_path、expected_type、required、default
  - name: payload
    type: object
    required: true
    description: 已通过语法解析、但尚未做结构校验的模型输出
  - name: node_id
    type: string
    required: true
    description: 当前流程节点 id，用于错误信息定位
outputs:
  - name: guard_report
    type: object
    description: 结构 {checked, missing[], type_mismatch[], skipped, fatal}，写入 result.metrics 的 schema_guard 键
tool:
  script: scripts/guard.py
  description: 对已解析的结构化输出做字段级校验，返回带 field_path 的结构化报告（六键：checked/missing/type_mismatch/skipped/repaired/fatal，fatal 为空即放行）
steps:
  - 取本节点交付契约中声明的字段清单，逐项取出 field_path、expected_type、required、default 四项，缺任一项视为契约定义不完整并报错
  - 按 field_path 逐层下钻 payload，记录每个字段的实到类型；下钻过程中遇到 None 立即记 missing 并停止该分支
  - 类型比对：实到类型与 expected_type 不符时写入 type_mismatch，条目格式为「<field_path> 期望 <expected_type> 实为 <actual_type>」
  - 缺失字段处理：required 为 true 且无 default 记 fatal；有 default 则补默认值并计入 repaired
  - 容器元素级别的畸形（例如 cases 列表里混入了字符串）单独记录 field_path 与下标，计入 skipped，从后续处理中剔除该元素但保留其余元素继续处理
  - fatal 为空时放行，把结构为 {checked, missing, type_mismatch, skipped, repaired, fatal} 的报告写入 result.metrics["schema_guard"]
  - fatal 非空时判定节点失败，错误信息必须写成「结构校验失败：<field_path> 期望 <expected_type> 实为 <actual_type>」，禁止抛出不带字段信息的原始异常
error_handling:
  - 禁止在错误信息里只写异常类名；必须携带 field_path，否则视为本技能未生效
  - 容器元素畸形只降级不中断，单条脏数据不得导致整份产物丢失
  - 契约本身定义不完整（缺 expected_type 等）时报错指向契约定义者，而不是把责任推给模型输出
checkpoints:
  - 节点失败时 events.jsonl 的 node_error.payload.error 含 "结构校验失败" 字样
  - 同一次运行的 node_error 中不再出现无 field_path 的 AttributeError 或 KeyError
  - fatal 为空才允许 publish 产物；skipped 大于 0 时必须在 node_end 摘要里说明跳过了多少项
smoke:
  command:
    - python
    - scripts/selfcheck.py
  expect_exit: 0
  timeout_s: 30
tags:
  - robustness
  - contract
---

# contract-schema-guard 补充说明

## 为什么需要它

语法层面的容错（`parse_json_block`）只能保证「这是合法 JSON」，保证不了「结构符合交付契约」。
两者的差距正是本项目已经发生的真实故障：模型把 `cases` 返回成字符串列表，下游直接按 dict 消费，
在遍历中抛出 `AttributeError: 'str' object has no attribute 'get'`。该故障在两次历史运行中重复出现，
且错误信息不含任何字段位置，只能靠读代码反推。

本技能把这个缺口补上：在「解析成功」与「开始消费」之间插入一层字段级校验，
使得任何结构问题都能被定位到具体的 `field_path`。

## 执行边界

- 只做结构与类型校验，**不做业务语义校验**（值是否合理由下游负责）；
- 只报告，不猜测修正；有 `default` 才补默认值，否则一律记为问题项；
- 校验结果必须可断言：`result.metrics["schema_guard"]` 的六个键是固定结构。

## 可调用形态（tool.script）

front-matter 的 `tool.script` 声明后，本技能注册为 function calling 工具
`skill_contract_schema_guard`，模型可直接调用（无需自己重写校验逻辑）：

- 入参即上面的 inputs（`contract_spec` / `payload` / `node_id`），由框架按 JSON 经
  `sys.argv[1]` 传入 `scripts/guard.py`；
- `contract_spec` 可传「字段清单列表」本身，或含 `fields` 列表的对象；每项含
  `field_path`（点号路径）/ `expected_type`（str/dict/list/int）/ `required` /
  `default`（可选，缺失时补默认值计入 repaired）/ `element_type`（可选，列表元素类型）；
- stdout 输出 `{"ok": ..., "skill": ..., "report": {六键报告}}`，框架原样回灌；
  `fatal` 非空即 `ok=false`，调用方应把每条 fatal 当作节点失败信息处理。
