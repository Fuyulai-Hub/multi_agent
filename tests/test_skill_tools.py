"""技能工具（function calling）验证：SKILL.md ↔ 模型调用 ↔ 沙箱执行的完整闭环。

覆盖三层，全部使用真实组件（桩 LLM 只负责按脚本返回 tool_calls / 最终 JSON）：

1. Schema 映射：inputs → OpenAI function schema（properties / required / default / 描述）；
2. 沙箱执行：run_skill_script 的参数白名单 / required 校验 / default 填充 /
   root=技能目录的 Sandbox 真实执行 scripts/*.py / stdout 结构化回灌；
3. 装配与协议：Leader._build_tool_runner 的技能工具注册（含 skills-only 模式）、
   BaseAgent._generate_with_tools 闭环（tool_calls → 沙箱执行 → tool 消息回灌 → 续跑）。

技能产物直接用仓库里真实的 config/skills/contract-schema-guard（已声明 tool.script），
测试启动时把整个 config 目录复制到临时目录，保证不污染仓库。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from mas.agents.base import BaseAgent, Perception
from mas.config import load_config
from mas.experience import RetrievalResult
from mas.leader import Leader
from mas.llm import LLMClient
from mas.schemas import NodeTask
from mas.skills import build_tool_schema, load_skill, run_skill_script, validate_skill

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REAL_SKILLS_DIR = PROJECT_ROOT / "config" / "skills"
SKILL_ID = "contract-schema-guard"
TOOL_NAME = "skill_contract_schema_guard"


# =========================================================================== 桩 LLM
class _StubLLMHandler(BaseHTTPRequestHandler):
    """脚本化 OpenAI /chat/completions 桩：记录全部请求，按脚本序列返回。"""

    requests: list = []
    script: list = []

    def log_message(self, fmt, *args):  # 静音
        return

    def do_POST(self):  # noqa: N802（标准库命名）
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        type(self).requests.append(payload)
        index = min(len(type(self).requests) - 1, len(type(self).script) - 1)
        body = type(self).script[index](payload)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _StubServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _tool_call_payload(payload):
    """第一轮：模型请求调用技能工具（参数按 SKILL.md inputs 构造）。"""
    return {
        "choices": [{"message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call-skill-0001",
                "type": "function",
                "function": {
                    "name": TOOL_NAME,
                    "arguments": json.dumps({
                        "contract_spec": {"fields": [
                            {"field_path": "cases", "expected_type": "list",
                             "required": True, "element_type": "dict"},
                            {"field_path": "summary.total", "expected_type": "int",
                             "required": True, "default": 0},
                        ]},
                        "payload": {"cases": ["坏元素"], "summary": {"failed": 1}},
                        "node_id": "intake",
                    }, ensure_ascii=False),
                },
            }],
        }}],
        "usage": {"total_tokens": 42},
    }


def _final_payload(payload):
    """第二轮：模型基于工具 stdout 给出最终 JSON。"""
    return {
        "choices": [{"message": {
            "role": "assistant",
            "content": json.dumps({"ok": True, "consumed": "skill-report"}, ensure_ascii=False),
        }}],
        "usage": {"total_tokens": 18},
    }


class _RecordingEvents:
    def __init__(self):
        self.records = []

    def emit(self, type_, **kwargs):
        self.records.append({"type": type_, **kwargs})


class _SkillE2EAgent(BaseAgent):
    """最小 Agent：只验证生成闭环，apply 记录 generation。"""

    role_id = "developer"
    task_kinds = {"default": "requirement_spec"}

    def __init__(self, ctx):
        super().__init__(ctx)
        self.applied = None

    def perceive(self, task):
        return Perception()

    def retrieve_experience(self, task, perception):
        return RetrievalResult()

    def build_prompt(self, task, perception, experience):
        return SimpleNamespace(path=None, system="你是研发工程师", user="请执行任务",
                               experiences=[], skills=[], tier_counts={})

    def build_context(self, task, perception):
        return {}

    def apply(self, task, generation, perception, context, result):
        result.status = "pass"
        result.summary = "applied"
        self.applied = generation


class _SkillToolsTestBase(unittest.TestCase):
    """公共脚手架：真 config 复制到临时目录 + 技能工具开关打开。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_dir = self.root / "config"
        shutil.copytree(
            PROJECT_ROOT / "config", self.config_dir,
            ignore=shutil.ignore_patterns("credentials.yaml", "__pycache__"),
        )
        self.config = load_config(self.config_dir, workspace=self.root / "workspace")
        self.config.runtime["verbose"] = False
        skills_cfg = dict(self.config.runtime.get("skills") or {})
        skills_cfg.update({"enabled": True, "tools_enabled": True})
        self.config.runtime["skills"] = skills_cfg

    def tearDown(self):
        self.tmp.cleanup()

    def skill(self):
        return load_skill(self.config.skills_dir, SKILL_ID)


# =========================================================================== 1. schema 映射
class SchemaMappingTests(_SkillToolsTestBase):
    def test_inputs_to_function_schema(self):
        """inputs 的 name/type/required/description 正确映射；when_to_use 进描述。"""
        schema = build_tool_schema(self.skill())
        self.assertIsNotNone(schema)
        self.assertEqual(schema["type"], "function")
        function = schema["function"]
        self.assertEqual(function["name"], TOOL_NAME)
        parameters = function["parameters"]
        self.assertEqual(parameters["type"], "object")
        self.assertEqual(sorted(parameters["properties"]),
                         ["contract_spec", "node_id", "payload"])
        self.assertEqual(parameters["required"], ["contract_spec", "payload", "node_id"])
        self.assertEqual(parameters["properties"]["node_id"]["type"], "string")
        self.assertIn("field_path", parameters["properties"]["contract_spec"]["description"])
        self.assertIn("适用场景：", function["description"])

    def test_default_becomes_schema_default(self):
        """带 default 的 input 透传为 JSON Schema default（模型可省略）。"""
        skill = self.skill()
        skill.meta["inputs"][2] = {"name": "node_id", "type": "string", "required": False,
                                   "default": "intake", "description": "节点 id"}
        schema = build_tool_schema(skill)
        properties = schema["function"]["parameters"]["properties"]
        self.assertEqual(properties["node_id"]["default"], "intake")
        self.assertEqual(schema["function"]["parameters"]["required"],
                         ["contract_spec", "payload"])

    def test_unknown_type_falls_back_to_string(self):
        skill = self.skill()
        skill.meta["inputs"][0]["type"] = "weird-type"
        schema = build_tool_schema(skill)
        self.assertEqual(
            schema["function"]["parameters"]["properties"]["contract_spec"]["type"], "string")

    def test_no_tool_declaration_returns_none(self):
        """未声明 tool.script 的技能不生成 schema（继续走文本注入路径）。"""
        skill = load_skill(self.config.skills_dir, "demo-delivery")
        self.assertIsNone(build_tool_schema(skill))

    def test_invalid_tool_structure_reported(self):
        skill = self.skill()
        skill.meta["tool"] = {"description": "缺少 script"}
        problems = validate_skill(skill)
        self.assertTrue(any("tool 必须是含 script" in item for item in problems))


# =========================================================================== 2. 沙箱执行
class SandboxExecutionTests(_SkillToolsTestBase):
    def test_run_ok_and_stdout(self):
        """真实沙箱执行 guard.py：stdout 是结构化 JSON，含报告六键。"""
        result = run_skill_script(
            self.skill(),
            {
                "contract_spec": {"fields": [
                    {"field_path": "cases", "expected_type": "list",
                     "required": True, "element_type": "dict"},
                ]},
                "payload": {"cases": ["坏元素"]},
                "node_id": "intake",
            },
            self.config,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["script"], "scripts/guard.py")
        report = json.loads(result["stdout"])["report"]
        self.assertEqual(report["skipped"], 1)
        self.assertIn("cases[0] 期望 dict 实为 str", report["type_mismatch"])

    def test_missing_required_blocks_execution(self):
        """缺必填参数：返回可回灌错误，脚本不执行（stdout 为空）。"""
        result = run_skill_script(self.skill(), {"node_id": "intake"}, self.config)
        self.assertFalse(result["ok"])
        self.assertIn("缺少必填参数", result["error"])
        self.assertIn("contract_spec", result["error"])
        self.assertNotIn("stdout", result)

    def test_default_filled_and_extra_args_filtered(self):
        """default 自动填充；未声明字段被白名单过滤（脚本看不到多余参数）。"""
        skill = self.skill()
        skill.meta["inputs"] = [
            {"name": "contract_spec", "type": "object", "required": True, "description": "契约"},
            {"name": "payload", "type": "object", "required": True, "description": "数据"},
            {"name": "node_id", "type": "string", "required": False, "default": "auto",
             "description": "节点"},
        ]
        result = run_skill_script(
            skill,
            {
                "contract_spec": {"fields": [{"field_path": "a", "expected_type": "str",
                                              "required": True}]},
                "payload": {"a": "ok"},
                "evil_extra": "不该出现",
            },
            self.config,
        )
        self.assertTrue(result["ok"], result)
        report = json.loads(result["stdout"])["report"]
        self.assertEqual(report["node_id"], "auto")   # default 生效
        self.assertEqual(report["fatal"], [])

    def test_missing_script_returns_error(self):
        skill = self.skill()
        skill.meta["tool"] = {"script": "scripts/not-exist.py"}
        result = run_skill_script(skill, {}, self.config)
        self.assertFalse(result["ok"])
        self.assertIn("技能脚本不存在", result["error"])


# =========================================================================== 3. 装配门槛
class LeaderRegistrationTests(_SkillToolsTestBase):
    def test_skills_only_mode_registers_tool(self):
        """llm.tools_enabled=false 但 skills 双开关打开：仅技能工具进入 tools 列表。"""
        events = _RecordingEvents()
        runner = Leader(self.config)._build_tool_runner(events)
        self.assertIsNotNone(runner)
        try:
            names = [tool["function"]["name"] for tool in runner.schemas]
            self.assertEqual(names, [TOOL_NAME])
            registered = [r for r in events.records if r["type"] == "skill_tool_registered"]
            self.assertEqual(len(registered), 1)
            self.assertEqual(registered[0]["payload"]["skill"], SKILL_ID)
            ready = [r for r in events.records if r["type"] == "tool_runner_ready"]
            self.assertEqual(ready[0]["payload"]["skills"], 1)
        finally:
            runner.close()

    def test_all_disabled_returns_none(self):
        """llm.tools_enabled=false 且 skills.tools_enabled=false：行为与改造前一致（None）。"""
        skills_cfg = dict(self.config.runtime["skills"])
        skills_cfg["tools_enabled"] = False
        self.config.runtime["skills"] = skills_cfg
        self.assertIsNone(Leader(self.config)._build_tool_runner(_RecordingEvents()))

    def test_missing_script_skipped_with_warning(self):
        """声明了 tool.script 但脚本不存在：跳过该技能 + 告警，其余技能不受影响。"""
        broken_dir = self.config.skills_dir / "broken-skill"
        (broken_dir / "scripts").mkdir(parents=True)
        (broken_dir / "SKILL.md").write_text(
            "---\nname: broken\ndescription: 断链技能\nwhen_to_use: [演示]\n"
            "trigger: {keywords: [演示]}\ninputs: []\nsteps: [步骤一]\n"
            "tool: {script: scripts/gone.py}\n---\n\n正文\n", encoding="utf-8")
        events = _RecordingEvents()
        runner = Leader(self.config)._build_tool_runner(events)
        self.assertIsNotNone(runner)
        try:
            self.assertEqual([tool["function"]["name"] for tool in runner.schemas], [TOOL_NAME])
            skipped = [r for r in events.records if r["type"] == "skill_tool_skipped"]
            self.assertEqual(len(skipped), 1)
            self.assertEqual(skipped[0]["payload"]["skill"], "broken-skill")
        finally:
            runner.close()


# =========================================================================== 4. 全闭环（桩 LLM 协议 + 真实执行）
class ClosingLoopTests(_SkillToolsTestBase):
    def setUp(self):
        super().setUp()
        # 本类验证的是 ReAct 之前的技能工具闭环（docs/21）；system.yaml 现已默认开启
        # ReAct（docs/34），命中节点会分流到 ReAct 分支（metrics 改记 react_steps），
        # 故显式关闭以保持验证目标不变（ReAct 自身验证见 tests/test_react_loop.py）。
        self.config.runtime.pop("react", None)
        _StubLLMHandler.requests = []
        _StubLLMHandler.script = [_tool_call_payload, _final_payload]
        self.server = _StubServer(("127.0.0.1", 0), _StubLLMHandler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.port = self.server.server_address[1]
        self.config.llm["provider"] = "openai_compatible"
        self.config.llm["model"] = "stub-model"
        self.config.llm["timeout"] = 10
        self.config.llm_overrides = {
            "api_key": "stub-key", "base_url": f"http://127.0.0.1:{self.port}/v1",
        }

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=5)
        super().tearDown()

    def test_full_loop_skill_call_via_sandbox(self):
        """工具下发 → 模型 tool_calls → 沙箱真实执行 guard.py → stdout 回灌 → 续跑出 JSON。"""
        events = _RecordingEvents()
        runner = Leader(self.config)._build_tool_runner(events)
        self.assertIsNotNone(runner)
        try:
            llm = LLMClient(self.config)
            ctx = SimpleNamespace(
                config=self.config, events=events, run_dir=self.root, llm=llm, tools=runner,
            )
            agent = _SkillE2EAgent(ctx)
            task = NodeTask(node_id="intake", title="需求评估", role="developer",
                            kind="task", iteration=1)

            result = agent.act(task)

            # 1) 闭环结果：最终 JSON 被消费，工具只调了 1 轮
            self.assertEqual(result.status, "pass", result.error)
            self.assertEqual(agent.applied.data, {"ok": True, "consumed": "skill-report"})
            self.assertEqual(result.metrics.get("tool_rounds"), 1)

            # 2) 事件证据：请求了技能工具 + 沙箱真实执行
            calls = [r for r in events.records if r["type"] == "tool_calls_requested"]
            self.assertEqual(calls[0]["payload"]["calls"][0]["name"], TOOL_NAME)
            executed = [r for r in events.records if r["type"] == "skill_tool_executed"]
            self.assertEqual(len(executed), 1)
            self.assertEqual(executed[0]["payload"]["skill"], SKILL_ID)
            self.assertTrue(executed[0]["payload"]["ok"])
            sandbox_events = [r for r in events.records if r["type"] == "sandbox_exec"]
            self.assertTrue(any("guard.py" in str(r.get("payload", {}).get("cmd", ""))
                                for r in sandbox_events))

            # 3) 线上协议：tools 下发（与 response_format 互斥），第二轮带 assistant + tool 消息
            self.assertEqual(len(_StubLLMHandler.requests), 2)
            first, second = _StubLLMHandler.requests
            self.assertIn("tools", first)
            self.assertNotIn("response_format", first)
            self.assertEqual([m["role"] for m in second["messages"]],
                             ["system", "user", "assistant", "tool"])
            tool_message = second["messages"][3]
            self.assertEqual(tool_message["tool_call_id"], "call-skill-0001")

            # 4) 回灌内容 = 沙箱真实执行的 stdout（含 guard 报告），而非桩造数据
            content = tool_message["content"]
            self.assertIn("cases[0] 期望 dict 实为 str", content)
            self.assertIn('"exit_code": 0', content)

            # 5) LLM 留痕 2 条
            trace_lines = (self.root / "llm_traces.jsonl").read_text(
                encoding="utf-8").strip().splitlines()
            self.assertEqual(len(trace_lines), 2)
        finally:
            runner.close()

    def test_tool_message_carries_execution_error_for_model_self_correction(self):
        """参数不全时不执行脚本，错误作为 tool 内容回灌（模型可自行补齐重试）。"""
        _StubLLMHandler.script = [_tool_call_payload, _final_payload]

        def _broken_call(payload):
            return {
                "choices": [{"message": {
                    "role": "assistant", "content": None,
                    "tool_calls": [{
                        "id": "call-skill-bad", "type": "function",
                        "function": {"name": TOOL_NAME,
                                     "arguments": json.dumps({"node_id": "intake"})},
                    }],
                }}], "usage": {"total_tokens": 9},
            }

        _StubLLMHandler.script = [_broken_call, _final_payload]
        events = _RecordingEvents()
        runner = Leader(self.config)._build_tool_runner(events)
        try:
            llm = LLMClient(self.config)
            ctx = SimpleNamespace(config=self.config, events=events, run_dir=self.root,
                                  llm=llm, tools=runner)
            result = _SkillE2EAgent(ctx).act(
                NodeTask(node_id="intake", title="需求评估", role="developer",
                         kind="task", iteration=1))
            self.assertEqual(result.status, "pass", result.error)
            tool_message = _StubLLMHandler.requests[1]["messages"][3]
            self.assertIn("缺少必填参数", tool_message["content"])
        finally:
            runner.close()


if __name__ == "__main__":
    unittest.main()
