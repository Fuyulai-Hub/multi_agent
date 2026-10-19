---
name: sp-requesting-code-review
description: 审查要早要勤：派发审查时只给变更与契约，不给本次会话的历史；结论按 Critical/Important/Minor 分级处理
version: 7.0.0
source: manual
when_to_use:
- 评审 Agent 在 review 节点产出审查报告时
- 一个主要功能完成后、或准备进入下一环节之前
trigger:
  keywords:
  - 代码审查
  - 评审
  - 审查报告
  - 阻塞问题
  - review
  intents:
  - review
  - reviewer
  nodes:
  - review
  roles:
  - reviewer
  requires_artifacts: []
inputs:
- name: change_summary
  type: string
  required: true
  description: 本次改动的摘要（改了什么）
- name: baseline
  type: string
  required: true
  description: 审查依据：需求规格或上一版本产物
steps:
- When to Request Review
- How to Request
- Example
- Common Rationalizations
- Red Flags
tags:
- superpowers
- review
- reviewer
---

# sp-requesting-code-review（适配自 superpowers v7.0.0）

> 上游：`obra/superpowers` · `skills/requesting-code-review/SKILL.md` · commit `bb92a77`
> 原始文件快照：`references/original-SKILL.md`（本目录下的其余文件亦为上游原文）
> 生成方式：`python tools/import_superpowers_skills.py`（可重复执行，幂等覆盖）

## 适配说明

保留上游 Requesting Code Review 的两条硬要求：审查早而勤、派发上下文「精确构造而非会话历史」，以及 Critical/Important/Minor 三级处置规则。上游依赖 git SHA 取 diff，multi_agent 的审查基线是需求规格与上一版本产物，故改写为「取可比对的两个端点」。

## 注入范围

- 节点：review
- 角色：reviewer
- 关键词命中需求文本时额外加权：代码审查、评审、审查报告、阻塞问题、review
- 意图标签：review、reviewer

## 上游原文

以下为 `skills/requesting-code-review/SKILL.md` 的正文全文，逐字保留、未做压缩或改写。
技能命中时由 `mas.skills.render_for_prompt` 全文注入；front-matter 的 steps
只是上文小节标题抽成的目录，方法本体以本节为准。
上游面向可交互的编码宿主，其中涉及人类搭档的对话动作（提问、确认、选择）在本
流水线里无人应答，按上面的适配说明落到等价的可核对动作上；其余方法论原样执行。

---
# Requesting Code Review

Dispatch a code reviewer subagent to catch issues before they cascade. The reviewer gets precisely crafted context for evaluation — never your session's history.

**Core principle:** Review early, review often.

## When to Request Review

**Mandatory:**
- After each task in subagent-driven development
- After completing major feature
- Before merge to main

**Optional but valuable:**
- When stuck (fresh perspective)
- Before refactoring (baseline check)
- After fixing complex bug

## How to Request

**1. Get git SHAs:**
```bash
BASE_SHA=$(git rev-parse HEAD~1)  # or: git merge-base origin/main HEAD
HEAD_SHA=$(git rev-parse HEAD)
```

**2. Dispatch code reviewer subagent:**

Dispatch a `general-purpose` subagent, filling the template at [code-reviewer.md](code-reviewer.md)

**Placeholders:**
- `{DESCRIPTION}` - Brief summary of what you built
- `{PLAN_OR_REQUIREMENTS}` - What it should do
- `{BASE_SHA}` - Starting commit
- `{HEAD_SHA}` - Ending commit

**3. Act on feedback:**
- Fix Critical issues immediately
- Fix Important issues before proceeding
- Note Minor issues for later
- Push back if reviewer is wrong (with reasoning)

## Example

```
[Just completed Task 2: Add verification function]

You: Let me request code review before proceeding.

BASE_SHA=$(git log --oneline | grep "Task 1" | head -1 | cut -d' ' -f1)
HEAD_SHA=$(git rev-parse HEAD)

[Dispatch code reviewer subagent]
  DESCRIPTION: Added verifyIndex() and repairIndex() with 4 issue types
  PLAN_OR_REQUIREMENTS: Task 2 from docs/superpowers/plans/deployment-plan.md
  BASE_SHA: a7981ec
  HEAD_SHA: 3df7661

[Subagent returns]:
  Strengths: Clean architecture, real tests
  Issues:
    Important: Missing progress indicators
    Minor: Magic number (100) for reporting interval
  Assessment: Ready to proceed

You: [Fix progress indicators]
[Continue to Task 3]
```

## Common Rationalizations

| Excuse | Reality |
|--------|---------|
| "I'll just review the diff myself instead of dispatching a reviewer" | You're the coordinator — reviewing the diff inline burns the context window you need to keep driving the work. Dispatch a reviewer subagent: the diff and the evaluation live in its context, and only the findings come back to you. |
| "The reviewer needs my whole session history to understand the change" | Hand it precisely crafted context, never your session's history. That keeps the reviewer on the work product, not your thought process. |

## Red Flags

**Never:**
- Skip review because "it's simple"
- Ignore Critical issues
- Proceed with unfixed Important issues
- Argue with valid technical feedback

**If reviewer wrong:**
- Push back with technical reasoning
- Show code/tests that prove it works
- Request clarification

See template at: [code-reviewer.md](code-reviewer.md)
