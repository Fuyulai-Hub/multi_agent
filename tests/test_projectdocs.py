"""项目指令文件（AGENTS.md 等价物，docs/36）回归测试。

锁定的契约（任何一条被破坏都会让「用户可纠正框架」退化成「框架猜测」）：

1. **无文件时零影响**：收集结果为空 → Prompt 逐字节不变（既有 438 个用例的基线）；
2. 向上逐级收集到项目根，顺序「外 → 内」（离需求越近越权威）；
3. ``AGENTS.override.md`` 覆盖同目录 ``AGENTS.md``（Codex 同名机制）；
4. 32 KiB 累积上限，超限即停并标记 truncated（不截断到半句话）；
5. 起点不在项目根之下时只读起点目录，不越界读仓库外的文件；
6. front-matter 只认白名单键；非法形态值忽略而不是带进流水线；
7. 项目指令声明的 delivery_shape **覆盖**模型判定，并留 ``intent_declared_by_project_doc`` 事件。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mas.agents.product import ProductAgent
from mas.blackboard import Blackboard
from mas.bus import MessageBus
from mas.config import load_config
from mas.events import EventLog
from mas.llm import Generation
from mas.projectdocs import (
    DEFAULT_MAX_BYTES,
    collect_project_docs,
    directive_shape,
    render_section,
)
from mas.prompts import PromptBuilder
from mas.sandbox import Sandbox
from mas.schemas import AgentResult, NodeTask

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class CollectTests(unittest.TestCase):
    """收集规则：遍历顺序 / override / 上限 / 边界。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_files_yields_empty(self):
        docs = collect_project_docs(self.root, root=self.root)
        self.assertEqual(docs.text, "")
        self.assertEqual(docs.files, [])
        self.assertFalse(docs.truncated)
        # 空结果渲染出的段落也必须是空串：Prompt 与改造前逐字节一致
        self.assertEqual(render_section(docs), "")

    def test_collects_upward_outside_in(self):
        _write(self.root / "AGENTS.md", "## 项目根指令")
        _write(self.root / "sub" / "AGENTS.md", "## 子目录指令")
        leaf = self.root / "sub" / "leaf"
        _write(leaf / "AGENTS.md", "## 需求目录指令")
        docs = collect_project_docs(leaf, root=self.root)
        self.assertEqual(len(docs.files), 3)
        # 外层在前、内层在后（内层离需求更近，后文更权威）
        self.assertLess(docs.text.index("项目根指令"), docs.text.index("子目录指令"))
        self.assertLess(docs.text.index("子目录指令"), docs.text.index("需求目录指令"))

    def test_override_replaces_agents_md(self):
        _write(self.root / "AGENTS.md", "## 会被覆盖")
        _write(self.root / "AGENTS.override.md", "## 覆盖生效")
        docs = collect_project_docs(self.root, root=self.root)
        self.assertEqual(len(docs.files), 1)
        self.assertIn("覆盖生效", docs.text)
        self.assertNotIn("会被覆盖", docs.text)

    def test_max_bytes_stops_and_marks_truncated(self):
        # 单份约 650 字节（中文按 UTF-8 三字节计）：预算 800 只够第一份，第二份整份不收
        _write(self.root / "AGENTS.md", "## 第一份\n" + "甲" * 200)
        _write(self.root / "sub" / "AGENTS.md", "## 第二份\n" + "乙" * 200)
        docs = collect_project_docs(self.root / "sub", root=self.root,
                                    runtime={"project_docs": {"max_bytes": 800}})
        self.assertTrue(docs.truncated)
        self.assertIn("第一份", docs.text)
        self.assertNotIn("第二份", docs.text)
        self.assertLessEqual(docs.bytes, 800)

    def test_start_outside_root_is_not_traversed(self):
        other = self.root / "outside"
        _write(other / "AGENTS.md", "## 仓库外指令")
        _write(self.root / "project" / "AGENTS.md", "## 项目内指令")
        docs = collect_project_docs(other, root=self.root / "project")
        self.assertEqual(len(docs.files), 1)
        self.assertIn("仓库外指令", docs.text)
        self.assertNotIn("项目内指令", docs.text)

    def test_disabled_skips_entirely(self):
        _write(self.root / "AGENTS.md", "## 指令")
        docs = collect_project_docs(self.root, root=self.root,
                                    runtime={"project_docs": {"enabled": False}})
        self.assertEqual(docs.text, "")
        self.assertIn("enabled=false", docs.skipped)

    def test_default_max_bytes_is_codex_32k(self):
        self.assertEqual(DEFAULT_MAX_BYTES, 32 * 1024)


class FrontMatterTests(unittest.TestCase):
    """front-matter：白名单键 + 非法值忽略。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_declared_shape_is_read(self):
        _write(self.root / "AGENTS.md",
               "---\ndelivery_shape: full_stack_ssr\n---\n\n本项目页面由服务端渲染。")
        docs = collect_project_docs(self.root, root=self.root)
        self.assertEqual(docs.directives["delivery_shape"], "full_stack_ssr")
        self.assertEqual(directive_shape(docs), "full_stack_ssr")
        self.assertIn("服务端渲染", docs.text)

    def test_illegal_declared_shape_is_ignored(self):
        _write(self.root / "AGENTS.md", "---\ndelivery_shape: 单页游戏\n---\n\n说明文字。")
        docs = collect_project_docs(self.root, root=self.root)
        self.assertIsNone(directive_shape(docs))
        self.assertIn("说明文字", docs.text)   # 声明被忽略，正文仍然注入

    def test_no_front_matter_is_plain_text(self):
        _write(self.root / "AGENTS.md", "## 纯 Markdown")
        docs = collect_project_docs(self.root, root=self.root)
        self.assertEqual(docs.directives, {})
        self.assertIn("纯 Markdown", docs.text)

    def test_unknown_keys_are_not_directives(self):
        _write(self.root / "AGENTS.md", "---\nowner: someone\n---\n\n正文")
        docs = collect_project_docs(self.root, root=self.root)
        self.assertEqual(docs.directives, {})

    def test_inner_declaration_wins(self):
        _write(self.root / "AGENTS.md", "---\ndelivery_shape: full_stack_spa\n---\n外层")
        _write(self.root / "sub" / "AGENTS.md", "---\ndelivery_shape: backend_only\n---\n内层")
        docs = collect_project_docs(self.root / "sub", root=self.root)
        self.assertEqual(directive_shape(docs), "backend_only")

    def test_dict_payload_accepted(self):
        """黑板里存的是 to_dict() 结果，directive_shape 必须同时认它。"""
        self.assertEqual(directive_shape({"directives": {"delivery_shape": "full_stack_ssr"}}),
                         "full_stack_ssr")
        self.assertIsNone(directive_shape(None))


class PromptInjectionTests(unittest.TestCase):
    """注入点：有内容才出现，没内容 Prompt 不变。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = load_config(PROJECT_ROOT / "config", workspace=self.root / "workspace")

    def tearDown(self):
        self.tmp.cleanup()

    def _build(self, docs_text: str) -> str:
        builder = PromptBuilder(self.config, self.root / "run", enabled=False,
                                 project_docs=docs_text)
        bundle = builder.build(
            role=self.config.roles["product"],
            node=self.config.flow.node("intake"),
            task_kind="requirement_spec",
            iteration=1,
            directive="",
            artifacts_brief="(暂无上游产物)",
            bus_digest="(无)",
            perception=[],
            missing=[],
            experience=_empty_retrieval(),
            contract={"id": "需求编号"},
        )
        return bundle.user

    def test_prompt_unchanged_when_no_docs(self):
        user = self._build("")
        self.assertNotIn("项目指令", user)

    def test_prompt_contains_docs_section(self):
        user = self._build("# 项目指令（来自项目根目录的 AGENTS.md）\n\n### 项目指令：AGENTS.md\n只做后端")
        self.assertIn("只做后端", user)
        # 指令段必须排在感知段之前：模型先看约束再看材料
        self.assertLess(user.index("项目指令"), user.index("感知：上游已完成的工作"))


class ProductOverrideTests(unittest.TestCase):
    """L0 项目指令声明覆盖模型判定（纠正权回到需求方）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        workspace = root / "workspace"
        self.config = load_config(PROJECT_ROOT / "config", workspace=workspace)
        self.config.runtime["verbose"] = False
        self.run_dir = root / "run"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.events = EventLog(self.run_dir / "events.jsonl", "test-projectdocs")
        self.blackboard = Blackboard(self.run_dir / "artifacts", self.events, "test-projectdocs")
        self.ctx = SimpleNamespace(
            config=self.config, events=self.events, run_dir=self.run_dir,
            blackboard=self.blackboard, bus=MessageBus(self.run_dir / "messages.jsonl",
                                                      self.events, "test-projectdocs"),
            sandbox=Sandbox(self.run_dir / "code", self.config, events=self.events),
            prompts=PromptBuilder(self.config, self.run_dir, enabled=False),
            llm=None, tools=None, experience=None, log=lambda *a, **k: None,
            run_state={}, consult=None,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _apply(self, model_shape: str, declared: str = ""):
        if declared:
            self.blackboard.publish(
                "project_docs", {"files": [], "directives": {"delivery_shape": declared}},
                producer="leader", node="intake", iteration=0, summary="t",
            )
        spec = {
            "id": "REQ-1", "title": "需求", "delivery_shape": model_shape,
            "function_points": [{"id": "FP-01", "title": "p",
                                 "samples": [{"input": "a", "expect": "b"}]}],
        }
        from mas.agents.base import Perception
        result = AgentResult()
        ProductAgent(self.ctx).apply(
            NodeTask(node_id="intake", title="需求评估", role="product", kind="task", iteration=1),
            Generation(data=spec), Perception(), {}, result,
        )
        return result, spec

    def test_declared_shape_overrides_model(self):
        # 模型判成 SPA（本机无浏览器通道时必然验收不通过），用户声明 SSR → 以声明为准
        result, spec = self._apply("full_stack_spa", "full_stack_ssr")
        self.assertEqual(spec["delivery_shape"], "full_stack_ssr")
        self.assertEqual(spec["delivery_shape_source"], "project_doc")
        events = [item for item in self.events.events if item.type == "intent_declared_by_project_doc"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].payload["model_shape"], "full_stack_spa")

    def test_model_shape_kept_without_declaration(self):
        _result, spec = self._apply("full_stack_spa")
        self.assertEqual(spec["delivery_shape"], "full_stack_spa")
        self.assertEqual(spec["delivery_shape_source"], "declared")
        self.assertEqual([item for item in self.events.events
                          if item.type == "intent_declared_by_project_doc"], [])

    def test_declaration_matching_model_still_records_source(self):
        # 声明与模型一致时不发「纠正」事件，但来源必须记为 project_doc：
        # 复盘时要能看出这个形态是需求方确认过的，而不是模型碰巧判对的。
        _result, spec = self._apply("full_stack_ssr", "full_stack_ssr")
        self.assertEqual(spec["delivery_shape_source"], "project_doc")
        self.assertEqual([item for item in self.events.events
                          if item.type == "intent_declared_by_project_doc"], [])

    def test_illegal_declaration_does_not_corrupt_shape(self):
        _result, spec = self._apply("backend_only", "乱写的形态")
        self.assertEqual(spec["delivery_shape"], "backend_only")
        self.assertEqual(spec["delivery_shape_source"], "declared")


def _empty_retrieval():
    from mas.experience import RetrievalResult

    return RetrievalResult(text="", entries=[], per_tier={})


if __name__ == "__main__":
    unittest.main()
