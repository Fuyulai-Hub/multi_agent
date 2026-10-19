---
name: sp-using-superpowers
description: （停用·资料留存）宿主的「先查技能再行动」元约定；multi_agent 由框架在每节点前确定性匹配，无需注入
version: 7.0.0
source: manual
when_to_use:
- 仅作资料留存：宿主层要求任何响应前先检查是否有适用技能
trigger:
  keywords:
  - superpowers 技能清单自检
  intents: []
  nodes: []
  roles: []
  requires_artifacts:
  - __disabled__
inputs:
- name: task
  type: string
  required: true
  description: 当前任务描述
steps:
- The Rule
- Skill Priority
- Red Flags
- Platform Adaptation
- User Instructions
tags:
- superpowers
- meta
- disabled
---

# sp-using-superpowers（适配自 superpowers v7.0.0）

> 上游：`obra/superpowers` · `skills/using-superpowers/SKILL.md` · commit `bb92a77`
> 原始文件快照：`references/original-SKILL.md`（本目录下的其余文件亦为上游原文）
> 生成方式：`python tools/import_superpowers_skills.py`（可重复执行，幂等覆盖）

## 适配说明

上游要求宿主在**任何响应之前**先检查并调用适用技能（含澄清提问前的检查）。multi_agent 的等价机制是 mas/agents/base.py 的 match_skills：每节点执行前按 trigger 做确定性打分注入，不依赖模型自觉，因此该元指令在这里没有落点。以 requires_artifacts=__disabled__ 停用（硬门禁永不满足 → 永不命中），保留原文以便对照。

## 注入范围

- 关键词命中需求文本时额外加权：superpowers 技能清单自检
- 状态：**已停用**（requires_artifacts 声明了永不就位的产物，永不命中）

## 上游原文

以下为 `skills/using-superpowers/SKILL.md` 的正文全文，逐字保留、未做压缩或改写。
技能命中时由 `mas.skills.render_for_prompt` 全文注入；front-matter 的 steps
只是上文小节标题抽成的目录，方法本体以本节为准。
上游面向可交互的编码宿主，其中涉及人类搭档的对话动作（提问、确认、选择）在本
流水线里无人应答，按上面的适配说明落到等价的可核对动作上；其余方法论原样执行。

---
<SUBAGENT-STOP>
If you were dispatched as a subagent to execute a specific task, ignore this skill.
</SUBAGENT-STOP>

<EXTREMELY-IMPORTANT>
If you think there is even a 1% chance a skill might apply to what you are doing, you ABSOLUTELY MUST invoke the skill.

IF A SKILL APPLIES TO YOUR TASK, YOU DO NOT HAVE A CHOICE. YOU MUST USE IT.

This is not negotiable. You cannot rationalize your way out of this.
</EXTREMELY-IMPORTANT>

## The Rule

**Invoke relevant or requested skills BEFORE any response or action** — including clarifying questions, exploring the codebase, or checking files. If it turns out wrong for the situation, you don't have to use it.

**Before entering plan mode:** if you haven't already brainstormed, invoke the brainstorming skill first.

Then announce "Using [skill] to [purpose]" and follow the skill exactly. If it has a checklist, create a todo per item.

## Skill Priority

When multiple skills apply, process skills come first — they set the approach, then implementation skills (frontend-design, etc.) carry it out. Brainstorming and systematic-debugging are Superpowers' most common process skills, but the rule holds for any of them.

- "Let's build X" → superpowers:brainstorming first, then implementation skills.
- "Fix this bug" → superpowers:systematic-debugging first, then domain skills.

## Red Flags

These thoughts mean STOP—you're rationalizing:

| Thought | Reality |
|---------|---------|
| "This is just a simple question" | Questions are tasks. Check for skills. |
| "I need more context first" | Skill check comes BEFORE clarifying questions. |
| "Let me explore the codebase first" | Skills tell you HOW to explore. Check first. |
| "I can check git/files quickly" | Files lack conversation context. Check for skills. |
| "Let me gather information first" | Skills tell you HOW to gather information. |
| "This doesn't need a formal skill" | If a skill exists, use it. |
| "I remember this skill" | Skills evolve. Read current version. |
| "This doesn't count as a task" | Action = task. Check for skills. |
| "The skill is overkill" | Simple things become complex. Use it. |
| "I'll just do this one thing first" | Check BEFORE doing anything. |
| "This feels productive" | Undisciplined action wastes time. Skills prevent this. |
| "I know what that means" | Knowing the concept ≠ using the skill. Invoke it. |

## Platform Adaptation

If your harness appears here, read its reference file for special instructions:

- Claude Code: `references/claude-code-tools.md`
- Codex: `references/codex-tools.md`
- Pi: `references/pi-tools.md`
- Antigravity: `references/antigravity-tools.md`
- Hermes Agent: `references/hermes-tools.md`
- Muse: `references/muse-tools.md`

## User Instructions

User instructions (CLAUDE.md, AGENTS.md, GEMINI.md, etc, direct requests) take precedence over skills, which in turn override default behavior. Only skip skill workflows or instructions when your human partner has explicitly told you to.
