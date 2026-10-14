"""Reviewer Prompt 增强的回归测试。

背景（attempt03/04/05 实测）：reviewer 的 build_context 计算了 AST 静态分析，
但 openai_compatible 路径的 _generate_openai 不消费 context—— reviewer 只能
看到黑板上的 manifest 清单，拿不到源码正文，连续以「未提供可读取的源文件
内容」驳回。修复：review 节点的 build_prompt 把静态证据与源码补进 Prompt。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mas.agents.reviewer import ReviewerAgent
from mas.blackboard import Blackboard
from mas.bus import MessageBus
from mas.config import load_config
from mas.events import EventLog
from mas.experience import RetrievalResult
from mas.prompts import PromptBuilder
from mas.sandbox import Sandbox
from mas.schemas import NodeTask

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ReviewerPromptEnhancementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.workspace = root / "workspace"
        self.config = load_config(PROJECT_ROOT / "config", workspace=self.workspace)
        self.config.runtime["verbose"] = False
        self.run_dir = root / "run"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.events = EventLog(self.run_dir / "events.jsonl", "test-reviewer-prompt")
        self.bus = MessageBus(self.run_dir / "messages.jsonl", self.events, "test-reviewer-prompt")
        self.blackboard = Blackboard(self.run_dir / "artifacts", self.events, "test-reviewer-prompt")
        self.sandbox = Sandbox(self.run_dir / "code", self.config, events=self.events)
        self.prompts = PromptBuilder(self.config, self.run_dir, enabled=True)
        self.ctx = SimpleNamespace(
            config=self.config, events=self.events, run_dir=self.run_dir,
            blackboard=self.blackboard, bus=self.bus, sandbox=self.sandbox,
            prompts=self.prompts, llm=None, tools=None, experience=None,
            log=lambda *a, **k: None, run_state={}, consult=None,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _manifest_with_files(self, files):
        for relative, content in files.items():
            self.sandbox.write_file(relative, content)
        self.blackboard.publish(
            "code_manifest",
            {"package": "demo", "files": list(files), "loc": 10, "functions": []},
            producer="developer", node="implement", iteration=1, summary="test",
        )

    def _review_bundle(self, perception=None):
        agent = ReviewerAgent(self.ctx)
        task = NodeTask(node_id="review", title="代码审查", role="reviewer", kind="task", iteration=1)
        return agent.build_prompt(task, perception or agent.perceive(task), RetrievalResult())

    def test_review_prompt_contains_source_and_static_evidence(self):
        self._manifest_with_files({
            "demo/__init__.py": "\"\"\"demo package.\"\"\"\n\n\ndef add(a: int, b: int) -> int:\n    \"\"\"Add.\"\"\"\n    return a + b\n",
            "demo/core.py": "\"\"\"demo core.\"\"\"\n\n\ndef bad(x):\n    return x\n",
        })
        bundle = self._review_bundle()
        self.assertIn("静态分析证据", bundle.user)
        self.assertIn("交付源码", bundle.user)
        self.assertIn("def add(a: int, b: int) -> int:", bundle.user)   # 真实源码正文
        self.assertIn("missing_type_hints", bundle.user)               # AST 确定性发现
        # 落盘的 prompt 证据与发送内容一致
        self.assertTrue(bundle.path.exists())
        self.assertIn("交付源码", bundle.path.read_text(encoding="utf-8"))

    def test_non_review_node_prompt_unchanged(self):
        """只有 review 节点增强；solution_review/acceptance 不带源码段。"""
        self._manifest_with_files({"demo/__init__.py": "\"\"\"demo.\"\"\"\n"})
        agent = ReviewerAgent(self.ctx)
        task = NodeTask(node_id="solution_review", title="方案评审", role="reviewer",
                        kind="task", iteration=1)
        bundle = agent.build_prompt(task, agent.perceive(task), RetrievalResult())
        self.assertNotIn("交付源码", bundle.user)

    def test_review_prompt_without_manifest_degrades_gracefully(self):
        """无 code_manifest 时不增强也不崩（缺产物由感知段声明）。"""
        agent = ReviewerAgent(self.ctx)
        task = NodeTask(node_id="review", title="代码审查", role="reviewer", kind="task", iteration=1)
        bundle = agent.build_prompt(task, agent.perceive(task), RetrievalResult())
        self.assertNotIn("交付源码", bundle.user)
        self.assertTrue(bundle.user)


if __name__ == "__main__":
    unittest.main()
