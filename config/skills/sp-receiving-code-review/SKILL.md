---
name: sp-receiving-code-review
description: 收到审查意见先验证再改：技术正确性优先于社交礼貌，不清楚就先问，一次只改一项
version: 7.0.0
source: manual
when_to_use:
- 研发 Agent 在 optimize 节点按 review_report 整改时
- 评审意见看起来不清楚、或技术上存疑时
trigger:
  keywords:
  - 审查意见
  - 整改
  - 驳回
  - 评审反馈
  - review feedback
  intents:
  - optimize
  - developer
  - fix
  nodes:
  - optimize
  roles:
  - developer
  requires_artifacts: []
inputs:
- name: feedback
  type: string
  required: true
  description: 收到的审查意见列表
- name: code_context
  type: string
  required: true
  description: 相关代码的当前实现
steps:
- Overview
- The Response Pattern
- Forbidden Responses
- Handling Unclear Feedback
- Source-Specific Handling
- YAGNI Check for "Professional" Features
- Implementation Order
- When To Push Back
- Acknowledging Correct Feedback
- Gracefully Correcting Your Pushback
- Common Mistakes
- Real Examples
- GitHub Thread Replies
tags:
- superpowers
- review
- developer
---

# sp-receiving-code-review（适配自 superpowers v7.0.0）

> 上游：`obra/superpowers` · `skills/receiving-code-review/SKILL.md` · commit `bb92a77`
> 原始文件快照：`references/original-SKILL.md`（本目录下的其余文件亦为上游原文）
> 生成方式：`python tools/import_superpowers_skills.py`（可重复执行，幂等覆盖）

## 适配说明

保留上游的 READ→UNDERSTAND→VERIFY→EVALUATE→RESPOND→IMPLEMENT 六步响应模式、「禁止表演性同意」与外部意见的 YAGNI 检查。上游面向人类评审者，multi_agent 里对应 review_report 回退后的 optimize 节点整改，故绑定 optimize。

## 注入范围

- 节点：optimize
- 角色：developer
- 关键词命中需求文本时额外加权：审查意见、整改、驳回、评审反馈、review feedback
- 意图标签：optimize、developer、fix

## 上游原文

以下为 `skills/receiving-code-review/SKILL.md` 的正文全文，逐字保留、未做压缩或改写。
技能命中时由 `mas.skills.render_for_prompt` 全文注入；front-matter 的 steps
只是上文小节标题抽成的目录，方法本体以本节为准。
上游面向可交互的编码宿主，其中涉及人类搭档的对话动作（提问、确认、选择）在本
流水线里无人应答，按上面的适配说明落到等价的可核对动作上；其余方法论原样执行。

---
# Code Review Reception

## Overview

Code review requires technical evaluation, not emotional performance.

**Core principle:** Verify before implementing. Ask before assuming. Technical correctness over social comfort.

## The Response Pattern

```
WHEN receiving code review feedback:

1. READ: Complete feedback without reacting
2. UNDERSTAND: Restate requirement in own words (or ask)
3. VERIFY: Check against codebase reality
4. EVALUATE: Technically sound for THIS codebase?
5. RESPOND: Technical acknowledgment or reasoned pushback
6. IMPLEMENT: One item at a time, test each
```

## Forbidden Responses

**NEVER:**
- "You're absolutely right!" (explicit instruction-file violation)
- "Great point!" / "Excellent feedback!" (performative)
- "Let me implement that now" (before verification)

**INSTEAD:**
- Restate the technical requirement
- Ask clarifying questions
- Push back with technical reasoning if wrong
- Just start working (actions > words)

## Handling Unclear Feedback

```
IF any item is unclear:
  STOP - do not implement anything yet
  ASK for clarification on unclear items

WHY: Items may be related. Partial understanding = wrong implementation.
```

**Example:**
```
your human partner: "Fix 1-6"
You understand 1,2,3,6. Unclear on 4,5.

❌ WRONG: Implement 1,2,3,6 now, ask about 4,5 later
✅ RIGHT: "I understand items 1,2,3,6. Need clarification on 4 and 5 before proceeding."
```

## Source-Specific Handling

### From your human partner
- **Trusted** - implement after understanding
- **Still ask** if scope unclear
- **No performative agreement**
- **Skip to action** or technical acknowledgment

### From External Reviewers
```
BEFORE implementing:
  1. Check: Technically correct for THIS codebase?
  2. Check: Breaks existing functionality?
  3. Check: Reason for current implementation?
  4. Check: Works on all platforms/versions?
  5. Check: Does reviewer understand full context?

IF suggestion seems wrong:
  Push back with technical reasoning

IF can't easily verify:
  Say so: "I can't verify this without [X]. Should I [investigate/ask/proceed]?"

IF conflicts with your human partner's prior decisions:
  Stop and discuss with your human partner first
```

**your human partner's rule:** "External feedback - be skeptical, but check carefully"

## YAGNI Check for "Professional" Features

```
IF reviewer suggests "implementing properly":
  grep codebase for actual usage

  IF unused: "This endpoint isn't called. Remove it (YAGNI)?"
  IF used: Then implement properly
```

**your human partner's rule:** "You and reviewer both report to me. If we don't need this feature, don't add it."

## Implementation Order

```
FOR multi-item feedback:
  1. Clarify anything unclear FIRST
  2. Then implement in this order:
     - Blocking issues (breaks, security)
     - Simple fixes (typos, imports)
     - Complex fixes (refactoring, logic)
  3. Test each fix individually
  4. Verify no regressions
```

## When To Push Back

Push back when:
- Suggestion breaks existing functionality
- Reviewer lacks full context
- Violates YAGNI (unused feature)
- Technically incorrect for this stack
- Legacy/compatibility reasons exist
- Conflicts with your human partner's architectural decisions

**How to push back:**
- Use technical reasoning, not defensiveness
- Ask specific questions
- Reference working tests/code
- Involve your human partner if architectural

**If you're uncomfortable pushing back out loud:** Name that tension, then tell your partner about the issue you've seen. They'll appreciate your honesty.

## Acknowledging Correct Feedback

When feedback IS correct:
```
✅ "Fixed. [Brief description of what changed]"
✅ "Good catch - [specific issue]. Fixed in [location]."
✅ [Just fix it and show in the code]

❌ "You're absolutely right!"
❌ "Great point!"
❌ "Thanks for catching that!"
❌ "Thanks for [anything]"
❌ ANY gratitude expression
```

**Why no thanks:** Actions speak. Just fix it. The code itself shows you heard the feedback.

**If you catch yourself about to write "Thanks":** DELETE IT. State the fix instead.

## Gracefully Correcting Your Pushback

If you pushed back and were wrong:
```
✅ "You were right - I checked [X] and it does [Y]. Implementing now."
✅ "Verified this and you're correct. My initial understanding was wrong because [reason]. Fixing."

❌ Long apology
❌ Defending why you pushed back
❌ Over-explaining
```

State the correction factually and move on.

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Performative agreement | State requirement or just act |
| Blind implementation | Verify against codebase first |
| Batch without testing | One at a time, test each |
| Assuming reviewer is right | Check if breaks things |
| Avoiding pushback | Technical correctness > comfort |
| Partial implementation | Clarify all items first |
| Can't verify, proceed anyway | State limitation, ask for direction |

## Real Examples

**Performative Agreement (Bad):**
```
Reviewer: "Remove legacy code"
❌ "You're absolutely right! Let me remove that..."
```

**Technical Verification (Good):**
```
Reviewer: "Remove legacy code"
✅ "Checking... build target is 10.15+, this API needs 13+. Need legacy for backward compat. Current impl has wrong bundle ID - fix it or drop pre-13 support?"
```

**YAGNI (Good):**
```
Reviewer: "Implement proper metrics tracking with database, date filters, CSV export"
✅ "Grepped codebase - nothing calls this endpoint. Remove it (YAGNI)? Or is there usage I'm missing?"
```

**Unclear Item (Good):**
```
your human partner: "Fix items 1-6"
You understand 1,2,3,6. Unclear on 4,5.
✅ "Understand 1,2,3,6. Need clarification on 4 and 5 before implementing."
```

## GitHub Thread Replies

When replying to inline review comments on GitHub, reply in the comment thread (`gh api repos/{owner}/{repo}/pulls/{pr}/comments/{id}/replies`), not as a top-level PR comment.
