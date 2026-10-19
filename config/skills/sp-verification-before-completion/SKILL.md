---
name: sp-verification-before-completion
description: 先有证据再下结论：任何「通过 / 完成 / 已修复」的声明之前，都必须刚跑完验证命令并读完输出
version: 7.0.0
source: manual
when_to_use:
- 测试 Agent 在 test 节点给出测试结论时
- 任何节点准备声明「已完成 / 已修复 / 全部通过」之前
trigger:
  keywords:
  - 验证
  - 测试通过
  - 完成声明
  - 回归验证
  - 证据
  - verify
  intents:
  - test
  - tester
  nodes:
  - test
  roles:
  - tester
  requires_artifacts: []
inputs:
- name: claim
  type: string
  required: true
  description: 准备给出的结论（如「全部用例通过」）
- name: verify_command
  type: string
  required: false
  description: 能证明该结论的命令
  default: ''
steps:
- Overview
- The Iron Law
- The Gate Function
- Common Failures
- Red Flags - STOP
- Rationalization Prevention
- Key Patterns
- When To Apply
tags:
- superpowers
- verification
- tester
---

# sp-verification-before-completion（适配自 superpowers v7.0.0）

> 上游：`obra/superpowers` · `skills/verification-before-completion/SKILL.md` · commit `bb92a77`
> 原始文件快照：`references/original-SKILL.md`（本目录下的其余文件亦为上游原文）
> 生成方式：`python tools/import_superpowers_skills.py`（可重复执行，幂等覆盖）

## 适配说明

完整保留上游的铁律（没有刚跑过的验证证据就不许下完成结论）、五步门禁函数（IDENTIFY→RUN→READ→VERIFY→CLAIM）以及各类声明的「必要条件 vs 不足以证明」对照。与本项目 tester 的证据要求方向一致，可直接叠加。

## 注入范围

- 节点：test
- 角色：tester
- 关键词命中需求文本时额外加权：验证、测试通过、完成声明、回归验证、证据、verify
- 意图标签：test、tester

## 上游原文

以下为 `skills/verification-before-completion/SKILL.md` 的正文全文，逐字保留、未做压缩或改写。
技能命中时由 `mas.skills.render_for_prompt` 全文注入；front-matter 的 steps
只是上文小节标题抽成的目录，方法本体以本节为准。
上游面向可交互的编码宿主，其中涉及人类搭档的对话动作（提问、确认、选择）在本
流水线里无人应答，按上面的适配说明落到等价的可核对动作上；其余方法论原样执行。

---
# Verification Before Completion

## Overview

**Core principle:** Evidence before claims, always.

**Violating the letter of this rule is violating the spirit of this rule.**

## The Iron Law

```
NO COMPLETION CLAIMS WITHOUT FRESH VERIFICATION EVIDENCE
```

If you haven't run the verification command in this message, you cannot claim it passes.

## The Gate Function

```
BEFORE claiming any status or expressing satisfaction:

1. IDENTIFY: What command proves this claim?
2. RUN: Execute the FULL command (fresh, complete)
3. READ: Full output, check exit code, count failures
4. VERIFY: Does output confirm the claim?
   - If NO: State actual status with evidence
   - If YES: State claim WITH evidence
5. ONLY THEN: Make the claim

Skip any step = lying, not verifying
```

## Common Failures

| Claim | Requires | Not Sufficient |
|-------|----------|----------------|
| Tests pass | Test command output: 0 failures | Previous run, "should pass" |
| Linter clean | Linter output: 0 errors | Partial check, extrapolation |
| Build succeeds | Build command: exit 0 | Linter passing, logs look good |
| Bug fixed | Test original symptom: passes | Code changed, assumed fixed |
| Regression test works | Red-green cycle verified | Test passes once |
| Agent completed | VCS diff shows changes | Agent reports "success" |
| Requirements met | Line-by-line checklist | Tests passing |

## Red Flags - STOP

- Using "should", "probably", "seems to"
- Expressing satisfaction before verification ("Great!", "Perfect!", "Done!", etc.)
- About to commit/push/PR without verification
- Trusting agent success reports
- Relying on partial verification
- Thinking "just this once"
- Tired and wanting work over
- **ANY wording implying success without having run verification**

## Rationalization Prevention

| Excuse | Reality |
|--------|---------|
| "Should work now" | RUN the verification |
| "I'm confident" | Confidence ≠ evidence |
| "Just this once" | No exceptions |
| "Linter passed" | Linter ≠ compiler |
| "Agent said success" | Verify independently |
| "I'm tired" | Exhaustion ≠ excuse |
| "Partial check is enough" | Partial proves nothing |
| "Different words so rule doesn't apply" | Spirit over letter |

## Key Patterns

**Tests:**
```
✅ [Run test command] [See: 34/34 pass] "All tests pass"
❌ "Should pass now" / "Looks correct"
```

**Regression tests (TDD Red-Green):**
```
✅ Write → Run (pass) → Revert fix → Run (MUST FAIL) → Restore → Run (pass)
❌ "I've written a regression test" (without red-green verification)
```

**Build:**
```
✅ [Run build] [See: exit 0] "Build passes"
❌ "Linter passed" (linter doesn't check compilation)
```

**Requirements:**
```
✅ Re-read plan → Create checklist → Verify each → Report gaps or completion
❌ "Tests pass, phase complete"
```

**Agent delegation:**
```
✅ Agent reports success → Check VCS diff → Verify changes → Report actual state
❌ Trust agent report
```

## When To Apply

**ALWAYS before:**
- ANY variation of success/completion claims
- ANY expression of satisfaction
- ANY positive statement about work state
- Committing, PR creation, task completion
- Moving to next task
- Delegating to agents

**Rule applies to:**
- Exact phrases
- Paraphrases and synonyms
- Implications of success
- ANY communication suggesting completion/correctness
