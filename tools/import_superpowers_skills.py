#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 superpowers（github.com/obra/superpowers）的技能适配为 multi_agent 技能。

用法（项目根目录执行）：

    python tools/import_superpowers_skills.py            # 生成/覆盖 config/skills/sp-*
    python tools/import_superpowers_skills.py --dry-run  # 只打印将要生成的技能

设计要点（为什么需要这层转换）：
- multi_agent 的技能通道按通用 Agent 宿主的做法做**全文注入**：节点命中即把
  SKILL.md 正文整篇放进上下文（`mas.skills.render_for_prompt`）。因此上游的方法论
  **原样保留**为本地正文——不改写、不压缩、不蒸馏，模型读到的就是上游原文。
  下文ADAPTATIONS 里因此不再维护 steps / checkpoints / error_handling：
  steps 由上游正文的二级标题机械抽取（仅作目录），内容重心在正文。
- 需要转换的只有**元信息**：上游 front-matter 只有 `name` + `description`，
  而 multi_agent 要求 `when_to_use` / `trigger` / `inputs` / `steps` 且
  `trigger` 必须绑定到流水线节点，否则无法参与 `mas.skills.score_skill` 的打分。
- 触发条件按流水线节点重新绑定：nodes/roles 命中 +1，keywords 命中需求文本 +2/项，
  intents 命中节点标签 +3。每个节点最多注入 `runtime.skills.max_items`（默认 3）
  个技能，故每个节点绑定的 superpowers 技能控制在 2 个以内，其余只靠 keywords
  在真正相关时才参与竞争。
- 两个与流水线无对应语义的技能（using-superpowers / diagnosing-superpowers）
  仍导入，但用 `requires_artifacts: ["__disabled__"]` 停用（该产物永不就位 →
  永不命中），保留资料可追溯，避免误注入。

脚本只依赖 PyYAML 与标准库；可重复执行（幂等覆盖）。
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List

import yaml

ROOT = Path(__file__).resolve().parent.parent
UPSTREAM = ROOT / "tdd" / "skills"
TARGET = ROOT / "config" / "skills"

UPSTREAM_VERSION = "7.0.0"
UPSTREAM_COMMIT = "bb92a77"

# 停用标记：requires_artifacts 是硬门禁，声明一个永不就位的产物即永远不命中
DISABLED_ARTIFACT = "__disabled__"

PREFIX = "sp-"

# 与 mas.skills._FRONT_MATTER 同一口径：切出上游 front-matter，只搬正文
_FRONT_MATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)


# --------------------------------------------------------------------------- 适配表
def _trigger(
    keywords: List[str] = None,
    intents: List[str] = None,
    nodes: List[str] = None,
    roles: List[str] = None,
    requires_artifacts: List[str] = None,
) -> Dict[str, Any]:
    return {
        "keywords": list(keywords or []),
        "intents": list(intents or []),
        "nodes": list(nodes or []),
        "roles": list(roles or []),
        "requires_artifacts": list(requires_artifacts or []),
    }


def _input(name: str, description: str, type_: str = "string", required: bool = True,
           default: Any = None) -> Dict[str, Any]:
    item = {"name": name, "type": type_, "required": required, "description": description}
    if default is not None:
        item["default"] = default
    return item


ADAPTATIONS: List[Dict[str, Any]] = [
    # ---------------------------------------------------------------- 1. intake
    {
        "id": "brainstorming",
        "name": "sp-brainstorming",
        "description": "需求澄清先于拆解：动手拆功能点之前先确认「给谁用、解决什么、什么算成功」，避免把猜测当成共识写进 requirement_spec",
        "when_to_use": [
            "产品 Agent 在 intake 拿到需求原文、准备拆解功能点之前",
            "需求描述含糊、缺少成功标准、交付形态未定的时候",
            "需要在 requirement_spec 里区分「人已确认」与「本方假设」的时候",
        ],
        "trigger": _trigger(
            keywords=["需求不清", "需求模糊", "待确认", "成功标准", "交付形态", "brainstorm"],
            intents=["intake", "product"],
            nodes=["intake"],
            roles=["product"],
            requires_artifacts=["requirement_source"],
        ),
        "inputs": [
            _input("requirement_source", "需求原文或需求文档路径"),
            _input("open_questions", "已有的待澄清问题列表", type_="list", required=False, default=[]),
        ],
        "tags": ["superpowers", "requirement", "product"],
        "note": (
            "提炼了上游 Brainstorming 的对话方法：先判断已知信息是否充分 → 一次一个开放问题 → "
            "200~300 字回放确认 → 记录选型归属。上游的视觉伴侣（visual companion）、spike 原型等"
            "面向人类协作者的交互手段在流水线里没有落点，未保留；「一次一个问题」在多 Agent 场景下"
            "改写为「澄清项写进 open_questions 而不是逐轮对话」，避免阻塞流水线。"
        ),
    },
    # ---------------------------------------------------------------- 2. solution
    {
        "id": "writing-plans",
        "name": "sp-writing-plans",
        "description": "把需求规格写成「没看过这份代码的人也能照做」的实现计划：定文件职责、划任务边界、每步一个可验证动作",
        "when_to_use": [
            "研发 Agent 在 solution 节点产出方案设计与实现计划时",
            "需求含多个模块、需要把实现拆成有序任务时",
            "下游 implement 需要照计划逐步执行、每步都要有验证命令时",
        ],
        "trigger": _trigger(
            keywords=["实现计划", "任务拆分", "方案设计", "模块划分", "plan"],
            intents=["solution", "developer"],
            nodes=["solution"],
            roles=["developer"],
        ),
        "inputs": [
            _input("requirement_spec", "已发布的需求规格（功能点与验收标准）"),
            _input("file_structure", "计划新增/修改的文件与各自职责", type_="list", required=False, default=[]),
        ],
        "tags": ["superpowers", "planning", "developer"],
        "note": (
            "提炼了上游 Writing Plans 的核心约束：文件结构先行、任务右-sizing（每任务自带测试闭环）、"
            "步骤粒度=一个可检查动作、接口块显式声明、文末自审五条。上游要求把计划落到 "
            "docs/superpowers/plans/ 并用 git 提交，multi_agent 的计划产物落在 solution_design，"
            "故改写为「产出可独立验证的交付物」，不引入 git 提交动作。"
        ),
    },
    {
        "id": "dispatching-parallel-agents",
        "name": "sp-dispatching-parallel-agents",
        "description": "2 个以上互不依赖的问题域各派一个独立上下文去处理，让它们并行而不是串行排队",
        "when_to_use": [
            "多个失败分属不同文件、不同子系统、不同根因时",
            "多个模块之间没有共享状态、互不依赖时",
        ],
        "trigger": _trigger(
            keywords=["并行", "独立任务", "多模块", "互不依赖", "parallel"],
            intents=["dispatch"],
        ),
        "inputs": [
            _input("task_domains", "按问题域分组后的任务列表，每项含范围/目标/约束"),
        ],
        "tags": ["superpowers", "parallel", "developer"],
        "note": (
            "上游面向可派发 subagent 的宿主。multi_agent 是固定流水线（进程内串行节点），"
            "没有动态派发子 Agent 的通道，因此未绑定任何节点，只在需求文本出现并行/独立任务类"
            "关键词时参与竞争，语义降级为「如何切分独立问题域」。"
        ),
    },
    # ---------------------------------------------------------------- 3. implement
    {
        "id": "test-driven-development",
        "name": "sp-test-driven-development",
        "description": "先写失败测试再写实现（RED-GREEN-REFACTOR）：没亲眼看过测试失败，就不算证明它测对了东西",
        "when_to_use": [
            "研发 Agent 在 implement 节点写新功能、新模块时",
            "optimize 节点修任何缺陷之前（先写复现缺陷的失败测试）",
            "需要证明某段实现真的被测试覆盖而不是事后补用例时",
        ],
        "trigger": _trigger(
            keywords=["实现", "编码", "功能实现", "单元测试", "TDD", "测试先行"],
            intents=["implement", "developer"],
            nodes=["implement"],
            roles=["developer"],
        ),
        "inputs": [
            _input("behavior", "本次要实现的单个行为描述（一句话）"),
            _input("test_command", "项目使用的测试命令", required=False, default="pytest"),
        ],
        "tags": ["superpowers", "tdd", "developer"],
        "note": (
            "完整保留上游 TDD 的铁律（没有失败测试就不许有实现代码）与 RED / 验证 RED / GREEN / "
            "验证 GREEN / REFACTOR 五段循环，以及「其它测试指的是整个套件而不是单个文件」这条"
            "易被忽略的要求。上游的大量 rationalization 对照表压缩进了 error_handling。"
        ),
    },
    {
        "id": "executing-plans",
        "name": "sp-executing-plans",
        "description": "按计划逐任务执行：每步用测试证明、与计划冲突时做裁定并记账、收尾做一次整体复查",
        "when_to_use": [
            "研发 Agent 在 implement 节点已有上游方案/计划需要照做时",
            "需要逐步推进多个任务且每步都要留可回放记录时",
        ],
        "trigger": _trigger(
            keywords=["按计划", "任务清单", "分步实现", "执行计划", "execute"],
            intents=["implement", "developer"],
            nodes=["implement"],
            roles=["developer"],
        ),
        "inputs": [
            _input("plan", "上游产出的实现计划（任务与步骤）"),
            _input("ledger", "本轮已完成的步骤记录，用于续跑", required=False, default=""),
        ],
        "tags": ["superpowers", "execution", "developer"],
        "note": (
            "保留上游 Executing Plans 的执行纪律：连续执行不停顿、ruling-not-stall（冲突当场裁定并记账）、"
            "四类允许停下来的边界、末尾整分支复查与「一轮修复」。上游的 ledger 文件与 workspace 目录约定"
            "映射到 multi_agent 的运行产物目录与事件流。"
        ),
    },
    {
        "id": "subagent-driven-development",
        "name": "sp-subagent-driven-development",
        "description": "每个独立任务用一份全新上下文执行、配一次任务级复查，全部完成后再做整份复查",
        "when_to_use": [
            "计划里任务彼此独立、可以逐个单独验收时",
            "希望每个任务都有独立审查而不是只在最后看一眼时",
        ],
        "trigger": _trigger(
            keywords=["独立任务", "逐任务验收", "子代理", "分派实现", "subagent"],
            intents=["dispatch"],
        ),
        "inputs": [
            _input("tasks", "待执行的任务列表（每项含范围与目标）"),
        ],
        "tags": ["superpowers", "execution", "developer"],
        "note": (
            "上游依赖宿主的 subagent 派发能力。multi_agent 的流水线节点固定、无动态派发通道，"
            "故不绑定节点；保留的价值在于「每个任务独立上下文 + 任务级复查 + 整份复查」这三层把关结构，"
            "可作为 implement 节点内部切分任务的参考，仅在关键词命中时注入。"
        ),
    },
    {
        "id": "using-git-worktrees",
        "name": "sp-using-git-worktrees",
        "description": "开工前确认隔离工作区：先检测已有隔离，再用宿主原生能力，最后才回退到手工 git worktree",
        "when_to_use": [
            "本次改动需要与其它进行中的工作隔离时",
            "并行推进多条实现、不能互相覆盖产物时",
        ],
        "trigger": _trigger(
            keywords=["工作区隔离", "独立目录", "分支隔离", "并行开发", "worktree"],
            intents=["isolate"],
        ),
        "inputs": [
            _input("workspace", "本次运行可用的产出目录"),
        ],
        "tags": ["superpowers", "isolation", "developer"],
        "note": (
            "multi_agent 的每次运行已天然隔离在 workspace/runs/<run_id>/ 下，因此该技能不绑定节点，"
            "只在需求文本提到隔离/并行开发时参与竞争；正文保留上游的 git worktree 检测与"
            "回退顺序，其中「隔离工作区」这一层与本项目的运行目录天然对应。"
        ),
    },
    # ---------------------------------------------------------------- 4. review
    {
        "id": "requesting-code-review",
        "name": "sp-requesting-code-review",
        "description": "审查要早要勤：派发审查时只给变更与契约，不给本次会话的历史；结论按 Critical/Important/Minor 分级处理",
        "when_to_use": [
            "评审 Agent 在 review 节点产出审查报告时",
            "一个主要功能完成后、或准备进入下一环节之前",
        ],
        "trigger": _trigger(
            keywords=["代码审查", "评审", "审查报告", "阻塞问题", "review"],
            intents=["review", "reviewer"],
            nodes=["review"],
            roles=["reviewer"],
        ),
        "inputs": [
            _input("change_summary", "本次改动的摘要（改了什么）"),
            _input("baseline", "审查依据：需求规格或上一版本产物"),
        ],
        "tags": ["superpowers", "review", "reviewer"],
        "note": (
            "保留上游 Requesting Code Review 的两条硬要求：审查早而勤、派发上下文「精确构造而非会话历史」，"
            "以及 Critical/Important/Minor 三级处置规则。上游依赖 git SHA 取 diff，multi_agent 的审查基线"
            "是需求规格与上一版本产物，故改写为「取可比对的两个端点」。"
        ),
    },
    # ---------------------------------------------------------------- 5. test
    {
        "id": "verification-before-completion",
        "name": "sp-verification-before-completion",
        "description": "先有证据再下结论：任何「通过 / 完成 / 已修复」的声明之前，都必须刚跑完验证命令并读完输出",
        "when_to_use": [
            "测试 Agent 在 test 节点给出测试结论时",
            "任何节点准备声明「已完成 / 已修复 / 全部通过」之前",
        ],
        "trigger": _trigger(
            keywords=["验证", "测试通过", "完成声明", "回归验证", "证据", "verify"],
            intents=["test", "tester"],
            nodes=["test"],
            roles=["tester"],
        ),
        "inputs": [
            _input("claim", "准备给出的结论（如「全部用例通过」）"),
            _input("verify_command", "能证明该结论的命令", required=False, default=""),
        ],
        "tags": ["superpowers", "verification", "tester"],
        "note": (
            "完整保留上游的铁律（没有刚跑过的验证证据就不许下完成结论）、五步门禁函数"
            "（IDENTIFY→RUN→READ→VERIFY→CLAIM）以及各类声明的「必要条件 vs 不足以证明」对照。"
            "与本项目 tester 的证据要求方向一致，可直接叠加。"
        ),
    },
    # ---------------------------------------------------------------- 6. optimize
    {
        "id": "systematic-debugging",
        "name": "sp-systematic-debugging",
        "description": "修缺陷前必须定位根因：根因调查→模式分析→假设验证→实施四阶段，三次修复失败就停下来质疑架构",
        "when_to_use": [
            "研发 Agent 在 optimize 节点处理缺陷报告时",
            "测试失败、行为异常、构建失败、性能问题的定位阶段",
            "已经试过一次修复但没生效的时候（最容易变成猜改）",
        ],
        "trigger": _trigger(
            keywords=["缺陷修复", "根因", "定位", "回归", "失败用例", "修复"],
            intents=["optimize", "developer", "fix"],
            nodes=["optimize"],
            roles=["developer"],
        ),
        "inputs": [
            _input("symptom", "观测到的现象与完整报错信息"),
            _input("tried", "已尝试过的修复与结果", type_="list", required=False, default=[]),
        ],
        "tags": ["superpowers", "debugging", "developer"],
        "note": (
            "完整保留上游四阶段流程与两条硬边界：未走完阶段一不许提修复；3 次修复失败要质疑架构而不是继续试。"
            "上游的 root-cause-tracing / defense-in-depth / condition-based-waiting 三份延伸资料"
            "随原文一起保留在 references/ 下，未压缩进 steps（否则会挤占注入预算）。"
        ),
    },
    {
        "id": "receiving-code-review",
        "name": "sp-receiving-code-review",
        "description": "收到审查意见先验证再改：技术正确性优先于社交礼貌，不清楚就先问，一次只改一项",
        "when_to_use": [
            "研发 Agent 在 optimize 节点按 review_report 整改时",
            "评审意见看起来不清楚、或技术上存疑时",
        ],
        "trigger": _trigger(
            keywords=["审查意见", "整改", "驳回", "评审反馈", "review feedback"],
            intents=["optimize", "developer", "fix"],
            nodes=["optimize"],
            roles=["developer"],
        ),
        "inputs": [
            _input("feedback", "收到的审查意见列表"),
            _input("code_context", "相关代码的当前实现"),
        ],
        "tags": ["superpowers", "review", "developer"],
        "note": (
            "保留上游的 READ→UNDERSTAND→VERIFY→EVALUATE→RESPOND→IMPLEMENT 六步响应模式、"
            "「禁止表演性同意」与外部意见的 YAGNI 检查。上游面向人类评审者，multi_agent 里对应 "
            "review_report 回退后的 optimize 节点整改，故绑定 optimize。"
        ),
    },
    # ---------------------------------------------------------------- 7. report
    {
        "id": "finishing-a-development-branch",
        "name": "sp-finishing-a-development-branch",
        "description": "收尾流程：先跑全量测试，再确认整合方式并交由人选择，按选定方式执行后再清理临时产物",
        "when_to_use": [
            "报告 Agent 在 report 节点汇总验收结论与证据时",
            "实现已完成、测试全绿、需要决定本次成果如何收尾时",
        ],
        "trigger": _trigger(
            keywords=["收尾", "交付", "归档", "完成", "finish"],
            intents=["report", "reporter"],
            nodes=["report"],
            roles=["reporter"],
        ),
        "inputs": [
            _input("test_result", "全量测试的执行结果"),
            _input("deliverables", "本次产出的文件与证据路径清单"),
        ],
        "tags": ["superpowers", "finishing", "reporter"],
        "note": (
            "保留上游的收尾顺序（测试→检测环境→给选项→执行→清理）与「不擅自合并/推送」这条边界。"
            "multi_agent 的交付物落在运行目录而非 git 分支，故整合方式改写为产物归档口径，"
            "具体的 git 命令未保留。"
        ),
    },
    {
        "id": "writing-skills",
        "name": "sp-writing-skills",
        "description": "写技能就是给流程文档做 TDD：先看它在没有技能时如何失败，再写技能，再验证它真的改变了行为",
        "when_to_use": [
            "报告 Agent 在 report 节点把一次通过的运行沉淀为技能时",
            "新增或修改 config/skills/ 下的技能时",
        ],
        "trigger": _trigger(
            keywords=["沉淀技能", "技能库", "蒸馏", "写技能", "skill"],
            intents=["report", "reporter", "distill"],
            nodes=["report"],
            roles=["reporter"],
        ),
        "inputs": [
            _input("baseline", "没有该技能时的基线行为（它当时怎么错的）"),
            _input("target_behavior", "希望技能带来的目标行为"),
        ],
        "tags": ["superpowers", "skill-authoring", "reporter"],
        "note": (
            "保留上游「写技能 = 流程文档的 TDD」这一核心映射（基线场景 / 违规记录 / 针对性写作 / "
            "复验 / 堵新漏洞）。multi_agent 的 reporter 已有蒸馏通道（run→skill），"
            "故绑定 report 节点；上游的目录规范、front-matter 格式等按本项目 mas/skills.py 的规范替换。"
        ),
    },
    # ---------------------------------------------------------------- 8. 停用（保留资料）
    {
        "id": "using-superpowers",
        "name": "sp-using-superpowers",
        "description": "（停用·资料留存）宿主的「先查技能再行动」元约定；multi_agent 由框架在每节点前确定性匹配，无需注入",
        "when_to_use": [
            "仅作资料留存：宿主层要求任何响应前先检查是否有适用技能",
        ],
        "trigger": _trigger(
            keywords=["superpowers 技能清单自检"],
            requires_artifacts=[DISABLED_ARTIFACT],
        ),
        "inputs": [_input("task", "当前任务描述")],
        "tags": ["superpowers", "meta", "disabled"],
        "note": (
            "上游要求宿主在**任何响应之前**先检查并调用适用技能（含澄清提问前的检查）。"
            "multi_agent 的等价机制是 mas/agents/base.py 的 match_skills：每节点执行前按 trigger 做"
            "确定性打分注入，不依赖模型自觉，因此该元指令在这里没有落点。以 requires_artifacts="
            "__disabled__ 停用（硬门禁永不满足 → 永不命中），保留原文以便对照。"
        ),
    },
    {
        "id": "diagnosing-superpowers",
        "name": "sp-diagnosing-superpowers",
        "description": "（停用·资料留存）诊断 superpowers 会话为什么跑偏并生成带 path:line 证据的报告，供上游维护者排查",
        "when_to_use": [
            "仅作资料留存：需要诊断 superpowers 自身会话时使用，与 multi_agent 流水线无关",
        ],
        "trigger": _trigger(
            keywords=["superpowers 会话诊断"],
            requires_artifacts=[DISABLED_ARTIFACT],
        ),
        "inputs": [_input("session_id", "待诊断的会话标识")],
        "tags": ["superpowers", "diagnosis", "disabled"],
        "note": (
            "上游用于和人类协作方一起复盘「一次 superpowers 会话为什么跑偏」，产出带 path:line 的"
            "证据报告与可选 issue 草稿。multi_agent 的运行证据在 workspace/runs/<run_id>/ 的"
            "events.jsonl / prompts/ 下，复盘应走事件流回放，故停用该技能，仅保留原文。"
        ),
    },
]


# --------------------------------------------------------------------------- 生成
def render_skill_text(meta: Dict[str, Any], body: str) -> str:
    """与 mas/skills.py 的 render_skill_text 保持同构（此处不 import，避免依赖运行时配置）。"""
    dumped = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False,
                            default_flow_style=False).rstrip("\n")
    return f"---\n{dumped}\n---\n\n{(body or '').strip()}\n"


def read_upstream_body(upstream_dir: Path) -> str:
    """读上游 SKILL.md 的正文（剥掉它自己的 front-matter），逐字返回。

    与 `mas.skills.parse_skill_text` 同一口径：上游 front-matter 只有 name/description，
    本地 front-matter 由本脚本重建，故正文以 `---` 之后的整段原样搬运。
    """
    text = (upstream_dir / "SKILL.md").read_text(encoding="utf-8")
    match = _FRONT_MATTER.match(text)
    return (text[match.end():] if match else text).strip()


def extract_steps(body: str) -> List[str]:
    """把上游正文的二级标题抽成 steps（front-matter 的必填字段，仅作目录用）。

    全文注入后方法本体在正文里，steps 不再承载方法论，机械抽取即可——
    不手工维护一份平行摘要，避免与上游原文漂移。无二级标题时回落到文档主标题。
    """
    headings = [line[3:].strip() for line in body.splitlines() if line.startswith("## ")]
    if headings:
        return headings
    for line in body.splitlines():
        if line.startswith("# "):
            return [line[2:].strip()]
    return ["（上游文档无小节标题，按全文执行）"]


def build_body(item: Dict[str, Any], upstream_body: str) -> str:
    """正文 = 来源与适配说明 + 上游原文全文（逐字，不改写）。"""
    trigger = item["trigger"]
    scope = []
    if trigger.get("nodes"):
        scope.append("节点：" + "、".join(trigger["nodes"]))
    if trigger.get("roles"):
        scope.append("角色：" + "、".join(trigger["roles"]))
    if trigger.get("keywords"):
        scope.append("关键词命中需求文本时额外加权：" + "、".join(trigger["keywords"]))
    if trigger.get("intents"):
        scope.append("意图标签：" + "、".join(trigger["intents"]))
    if DISABLED_ARTIFACT in (trigger.get("requires_artifacts") or []):
        scope.append("状态：**已停用**（requires_artifacts 声明了永不就位的产物，永不命中）")

    lines = [
        f"# {item['name']}（适配自 superpowers v{UPSTREAM_VERSION}）",
        "",
        f"> 上游：`obra/superpowers` · `skills/{item['id']}/SKILL.md` · commit `{UPSTREAM_COMMIT}`",
        f"> 原始文件快照：`references/original-SKILL.md`（本目录下的其余文件亦为上游原文）",
        f"> 生成方式：`python tools/import_superpowers_skills.py`（可重复执行，幂等覆盖）",
        "",
        "## 适配说明",
        "",
        item["note"],
        "",
        "## 注入范围",
        "",
    ]
    lines += [f"- {line}" for line in scope] or ["- （无）"]
    lines += [
        "",
        "## 上游原文",
        "",
        f"以下为 `skills/{item['id']}/SKILL.md` 的正文全文，逐字保留、未做压缩或改写。",
        "技能命中时由 `mas.skills.render_for_prompt` 全文注入；front-matter 的 steps",
        "只是上文小节标题抽成的目录，方法本体以本节为准。",
        "上游面向可交互的编码宿主，其中涉及人类搭档的对话动作（提问、确认、选择）在本",
        "流水线里无人应答，按上面的适配说明落到等价的可核对动作上；其余方法论原样执行。",
        "",
        "---",
        "",
    ]
    return "\n".join(lines) + upstream_body + "\n"


def copy_references(upstream_dir: Path, target_dir: Path) -> int:
    """把上游技能目录的全部文件复制为参考资料（SKILL.md 改名 original-SKILL.md）。"""
    refs = target_dir / "references"
    if refs.exists():
        shutil.rmtree(refs)
    refs.mkdir(parents=True, exist_ok=True)
    count = 0
    for path in sorted(upstream_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(upstream_dir)
        name = "original-SKILL.md" if relative.name == "SKILL.md" else str(relative).replace("\\", "/")
        destination = refs / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="只打印将要生成的技能，不落盘")
    parser.add_argument("--no-references", action="store_true", help="不复制上游原文快照")
    args = parser.parse_args()

    if not UPSTREAM.exists():
        print(f"[失败] 未找到上游技能目录：{UPSTREAM}", file=sys.stderr)
        return 1

    generated = 0
    for item in ADAPTATIONS:
        upstream_dir = UPSTREAM / item["id"]
        if not (upstream_dir / "SKILL.md").exists():
            print(f"[跳过] 上游不存在该技能：{upstream_dir}", file=sys.stderr)
            continue
        skill_id = PREFIX + item["id"]
        upstream_body = read_upstream_body(upstream_dir)
        meta = {
            "name": item["name"],
            "description": item["description"],
            "version": UPSTREAM_VERSION,
            "source": "manual",
            "when_to_use": item["when_to_use"],
            "trigger": item["trigger"],
            "inputs": item["inputs"],
            "steps": extract_steps(upstream_body),
            "tags": item["tags"],
        }
        body = build_body(item, upstream_body)
        if args.dry_run:
            print(f"[将生成] {skill_id}  steps={len(meta['steps'])}  "
                  f"body={len(body)}  nodes={item['trigger'].get('nodes') or '-'}")
            generated += 1
            continue
        target_dir = TARGET / skill_id
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "SKILL.md").write_text(
            render_skill_text(meta, body), encoding="utf-8", newline="\n")
        refs = 0 if args.no_references else copy_references(upstream_dir, target_dir)
        generated += 1
        print(f"[生成] {skill_id}  steps={len(meta['steps'])}  "
              f"body={len(body)}  参考文件={refs}")

    print(f"\n共 {generated} 个技能，输出目录：{TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
