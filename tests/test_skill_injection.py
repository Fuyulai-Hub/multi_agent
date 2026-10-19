"""技能注入（``runtime.skills``）的确定性验证：匹配语义 + Prompt 注入 + 关闭态零回归。

全程不触网、不调 LLM：匹配与渲染是 `mas/skills.py` 的纯确定性实现，Prompt 注入走真实
`PromptBuilder`，断言只看落盘文本与事件流——因此同一输入必得同一输出，可回放。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mas.agents.base import BaseAgent, Perception
from mas.config import load_config
from mas.experience import RetrievalResult
from mas.prompts import PromptBuilder
from mas.schemas import AgentContext, NodeTask
from mas.skills import SkillMatch, match_skills, render_for_prompt, save_skill

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_BASE_META = {
    "description": "测试用技能描述",
    "when_to_use": ["测试场景"],
    "inputs": [{"name": "x", "type": "string", "required": True, "description": "入参"}],
    "steps": ["步骤一：做 X", "步骤二：核对 Y"],
}


class _RecordingEvents:
    def __init__(self):
        self.records = []

    def emit(self, type_, **kwargs):
        self.records.append({"type": type_, **kwargs})


def _write_skill(skills_dir: Path, skill_id: str, trigger: dict) -> None:
    save_skill(skills_dir, skill_id, {"name": skill_id, "trigger": trigger, **_BASE_META})


class MatcherTests(unittest.TestCase):
    """打分与硬门禁：适用性证据必须来自 keywords/intents/nodes/roles，产物只是前置条件。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "skills"
        self.addCleanup(self.tmp.cleanup)

    def test_artifact_gate_is_precondition_not_applicability(self):
        _write_skill(self.dir, "artifact-only", {
            "keywords": [], "intents": ["alpha"], "requires_artifacts": ["requirement_spec"]})

        # 意图没命中：产物齐备也不注入（否则依赖齐备的节点会被灌进无关技能）
        self.assertEqual(match_skills(
            self.dir, intent_tags=["beta"], artifacts=["requirement_spec"],
        ), [])
        # 意图命中 + 产物齐备：注入
        matched = match_skills(self.dir, intent_tags=["alpha"], artifacts=["requirement_spec"])
        self.assertEqual([item.id for item in matched], ["artifact-only"])
        # 意图命中但产物缺失：前置条件不满足，仍然不注入
        self.assertEqual(match_skills(self.dir, intent_tags=["alpha"], artifacts=[]), [])

    def test_nodes_and_roles_are_hard_scopes(self):
        _write_skill(self.dir, "review-only", {"keywords": ["审查"], "intents": [], "nodes": ["review"]})
        _write_skill(self.dir, "role-only", {"keywords": [], "intents": [], "roles": ["reviewer"]})

        hit = match_skills(self.dir, node_id="review", role_id="reviewer", context_text="需要审查证据")
        self.assertIn("review-only", [item.id for item in hit])

        # 作用域声明了但不匹配 → 直接淘汰，关键词命中也无效
        self.assertEqual(match_skills(
            self.dir, node_id="implement", role_id="developer", context_text="需要审查证据"), [])

    def test_scoring_order_limit_and_min_score(self):
        _write_skill(self.dir, "s-intent", {"keywords": [], "intents": ["alpha"]})
        _write_skill(self.dir, "s-keyword", {"keywords": ["计价"], "intents": []})
        _write_skill(self.dir, "s-scope", {"keywords": [], "intents": [], "nodes": ["intake"]})

        scores = {
            item.id: item.score for item in match_skills(
                self.dir, node_id="intake", intent_tags=["alpha"], context_text="订单计价规则")
        }
        self.assertEqual(scores, {"s-intent": 3, "s-keyword": 2, "s-scope": 1})

        ordered = match_skills(
            self.dir, node_id="intake", intent_tags=["alpha"], context_text="订单计价规则", limit=2)
        self.assertEqual([item.id for item in ordered], ["s-intent", "s-keyword"])

        only_strong = match_skills(
            self.dir, node_id="intake", intent_tags=["alpha"], context_text="订单计价规则", min_score=3)
        self.assertEqual([item.id for item in only_strong], ["s-intent"])

    def test_missing_field_skills_are_skipped_not_fatal(self):
        _write_skill(self.dir, "good", {"keywords": ["计价"], "intents": []})
        # 缺 steps 的技能元信息不合法：自身不参与，不影响同目录其他技能
        save_skill(self.dir, "broken", {"name": "broken", "trigger": {"keywords": ["计价"]}})
        matched = match_skills(self.dir, context_text="订单计价")
        self.assertEqual([item.id for item in matched], ["good"])


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.match = SkillMatch(
            id="demo-skill", name="演示技能", score=5, hits=["关键词「计价」命中需求文本"],
            steps=["先算金额", "再核运费"], checkpoints=["每个功能点都有用例"], error_handling=["浮点误差用 Decimal"],
            body="## 方法本体\n\n先算金额再核运费，浮点误差用 Decimal。")

    def test_sections_are_labeled(self):
        text = render_for_prompt([self.match])
        self.assertIn("# 技能注入", text)
        self.assertIn("## 演示技能（技能 id=demo-skill，匹配度 5）", text)
        self.assertIn("命中依据：关键词「计价」命中需求文本", text)
        self.assertIn("执行步骤", text)
        self.assertIn("  1. 先算金额", text)
        self.assertIn("  2. 再核运费", text)
        self.assertIn("自检点（产出前逐条核对）：", text)
        self.assertIn("已知坑位与处置：", text)
        self.assertIn("  - 浮点误差用 Decimal", text)

    def test_body_is_injected_verbatim(self):
        """全文注入回归：SKILL.md 正文必须逐字进 Prompt（不只是 front-matter 条目）。"""
        text = render_for_prompt([self.match])
        self.assertIn("技能文档正文（SKILL.md 全文，按此执行）：", text)
        self.assertIn("## 方法本体", text)
        self.assertIn("先算金额再核运费，浮点误差用 Decimal。", text)

    def test_empty_matches_render_empty_and_truncation_is_marked(self):
        self.assertEqual(render_for_prompt([]), "")
        self.assertTrue(render_for_prompt([self.match], max_chars=60).endswith("…（技能段已截断）"))


class RepoSkillScopeTests(unittest.TestCase):
    """手写技能的 `trigger.nodes` 必须与 SKILL.md 的 when_to_use 一致，且不得外溢。"""

    def setUp(self):
        self.dir = PROJECT_ROOT / "config" / "skills"

    def test_declared_nodes_match_and_do_not_leak(self):
        intake = match_skills(self.dir, node_id="intake", role_id="product",
                              intent_tags=["intake", "product"], artifacts=["requirement_source"])
        self.assertIn("spec-scope-guard", [item.id for item in intake])

        review = match_skills(self.dir, node_id="review", role_id="reviewer",
                              intent_tags=["review", "reviewer"],
                              artifacts=["requirement_spec", "code_manifest"])
        self.assertIn("evidence-cited-review", [item.id for item in review])

        # 同一技能不得飘到实现节点：作用域门禁优先于产物齐备
        implement = match_skills(self.dir, node_id="implement", role_id="developer",
                                 intent_tags=["implement", "developer"],
                                 artifacts=["requirement_spec", "code_manifest"])
        self.assertNotIn("evidence-cited-review", [item.id for item in implement])
        self.assertNotIn("spec-scope-guard", [item.id for item in implement])

    def test_repo_skills_carry_body_and_render_verbatim(self):
        """全文注入回归：命中的技能必须带正文，渲染结果包含上游原句。"""
        matched = match_skills(self.dir, node_id="implement", role_id="developer",
                               intent_tags=["implement", "developer"],
                               artifacts=["requirement_spec", "code_manifest"])
        by_id = {item.id: item for item in matched}
        self.assertIn("sp-test-driven-development", by_id)
        for item in matched:
            self.assertTrue(item.body.strip(), f"{item.id} 命中但正文为空，全文注入会落空")

        text = render_for_prompt(matched, max_chars=40000)
        self.assertIn("NO PRODUCTION CODE WITHOUT A FAILING TEST FIRST", text)
        self.assertFalse(text.endswith("…（技能段已截断）"), "implement 节点的正常注入不应被预算截断")


class _ProbeAgent(BaseAgent):
    """最小 Agent：只提供感知与契约，用于驱动真实的 build_prompt 注入路径。"""

    role_id = "product"
    contracts = {"default": {"title": "string"}}

    def __init__(self, ctx, perception: Perception):
        super().__init__(ctx)
        self._perception = perception

    def perceive(self, task: NodeTask) -> Perception:
        return self._perception

    def retrieve_experience(self, task: NodeTask, perception: Perception):
        return RetrievalResult()

    def build_context(self, task, perception):
        return {}

    def apply(self, *args, **kwargs):  # pragma: no cover - 本文件不触发生成
        return None


class InjectionGateTests(unittest.TestCase):
    MARKER = "# 交付契约"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.config = load_config(PROJECT_ROOT / "config", workspace=self.root / "workspace")
        self.perception = Perception(
            found={"requirement_source": {"text": "做一个订单计价库", "images": []}},
            missing=[], artifacts_brief="（产物摘要）", bus_digest="（无协作消息）")
        self.task = NodeTask(node_id="intake", title="解析需求文档", role="product",
                             kind="task", iteration=1)

    def _build(self, *, enabled: bool):
        self.config.runtime["skills"] = {
            "enabled": enabled, "max_items": 3, "min_score": 1, "max_chars": 40000}
        events = _RecordingEvents()
        prompts = PromptBuilder(self.config, self.root, enabled=True)
        ctx = AgentContext(
            config=self.config, run_id="unit-test", run_dir=self.root, events=events,
            blackboard=None, bus=None, llm=None, sandbox=None, prompts=prompts, experience=None)
        agent = _ProbeAgent(ctx, self.perception)
        bundle = agent.build_prompt(self.task, self.perception, RetrievalResult())
        return bundle, events

    def test_disabled_switch_keeps_prompt_identical(self):
        # 生产配置默认开启技能注入（config/system.yaml 的 runtime.skills.enabled=true）；
        # 本用例验证的是「开关关闭」这一分支本身——关闭时 Prompt 与改造前逐字节一致。
        self.assertTrue(bool(dict(self.config.runtime.get("skills") or {}).get("enabled")),
                        "runtime.skills.enabled 必须默认开启（生产配置断言）")
        bundle, events = self._build(enabled=False)
        self.assertEqual(bundle.skills, [])
        self.assertNotIn("技能注入", bundle.user)
        self.assertEqual([r["type"] for r in events.records], [])

    def test_enabled_injects_section_before_contract_and_emits_event(self):
        off, _ = self._build(enabled=False)
        on_bundle, events = self._build(enabled=True)

        self.assertIn("spec-scope-guard", [item["id"] for item in on_bundle.skills])
        matched = [r for r in events.records if r["type"] == "skill_matched"]
        self.assertEqual(len(matched), 1)
        self.assertIn("spec-scope-guard", [item["id"] for item in matched[0]["payload"]["matched"]])

        # 注入只发生在「经验注入之后、交付契约之前」：契约段及其后逐字节不变
        suffix_off = off.user.split(self.MARKER, 1)[1]
        suffix_on = on_bundle.user.split(self.MARKER, 1)[1]
        self.assertEqual(suffix_off, suffix_on)
        prefix_off = off.user.split(self.MARKER, 1)[0]
        prefix_on = on_bundle.user.split(self.MARKER, 1)[0]
        self.assertTrue(prefix_on.startswith(prefix_off))
        self.assertTrue(prefix_on[len(prefix_off):].lstrip("\n").startswith("# 技能注入"))

    def test_prompt_file_header_records_injected_skills(self):
        bundle, _ = self._build(enabled=True)
        self.assertIsNotNone(bundle.path)
        head = Path(bundle.path).read_text(encoding="utf-8").splitlines()[0]
        # 只断言「命中列表里有 spec-scope-guard」：intake 同时会命中 sp-brainstorming
        # （适配自 superpowers 的技能，匹配度更高所以排在前面），首位不是稳定契约。
        self.assertIn("'spec-scope-guard'", head)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
