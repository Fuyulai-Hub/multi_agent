"""LLM 网络异常分类与重试的回归测试。

背景（2026-10-16 实测，两次带图需求运行）：intake 节点首次 LLM 调用即遭遇
TCP 级失败——WinError 10054（远程主机强迫关闭连接，连接被 RST）与
WinError 10061（目标计算机积极拒绝，拒连）。两次运行分别表现为：

- 10054：``ConnectionResetError`` 从 ``llm.generate()`` 裸穿透到 Agent 层，
  ``base.py`` 的 ``except Exception`` 把它格式成节点失败——**零重试**；
- 10061：被 ``urlopen`` 包成 ``URLError``，走了可重试通道，但
  ``retry.max_attempts=1`` 实际一次都没重试。

根因：``urlopen`` 在连接建立 / 响应头读取阶段抛的裸
``ConnectionResetError`` / ``ConnectionRefusedError`` 既不是 ``URLError``
也不是 ``TimeoutError`` 的子类，``mas/llm.py`` 两处 except 元组都漏掉了它们。
修复：两处异常分类都补上 ``ConnectionError``（前两者的公共基类），
与 URLError/TimeoutError 同按可重试处理。

本测试锁定该分类口径：连接被重置 → 重试后成功；一直拒连 → 用尽重试后以
可重试错误通道返回（带 error 的 Generation），而不是裸异常穿透杀死节点。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mas.config import load_config
from mas.llm import LLMClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_OK_BODY = json.dumps({
    "choices": [{"message": {"role": "assistant", "content": '{"ok": true}'}}],
    "usage": {"total_tokens": 1},
}).encode("utf-8")


class _FakeResponse:
    """最小 SSE 探测桩：Content-Type 非 event-stream → 走一次性 JSON 解析分支。"""

    headers = {"Content-Type": "application/json"}

    def read(self):
        return _OK_BODY

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeTextResponse:
    """非流式路径桩：只需要 read()。"""

    def read(self):
        return _OK_BODY

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _sse_lines(text: str):
    """最小 SSE 帧：一个 content chunk + [DONE]。"""
    chunk = json.dumps({"choices": [{"delta": {"content": text}}]})
    return [f"data: {chunk}\n".encode("utf-8"), b"data: [DONE]\n"]


class _FakeSSEResponse:
    """Content-Type 为 event-stream 且按行迭代，模拟持续推流的服务端。"""

    headers = {"Content-Type": "text/event-stream"}

    def __init__(self, lines):
        self._lines = lines

    def __iter__(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class RetryClassificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = load_config(PROJECT_ROOT / "config", workspace=self.root / "workspace")
        self.config.runtime["verbose"] = False
        self.config.llm["provider"] = "openai_compatible"
        self.config.llm["model"] = "stub-model"
        self.config.llm["timeout"] = 5
        # 只验证异常分类与重试次数：关掉思考强度降级链，避免它叠加额外重发
        self.config.llm["empty_output_retry"] = 0
        self.config.llm["retry"] = {"max_attempts": 3, "backoff_s": 0}
        self.config.llm_overrides = {"api_key": "stub-key", "base_url": "http://127.0.0.1:1/v1"}
        self.bundle = SimpleNamespace(
            path=None, system="sys", user="请解析需求", experiences=[], skills=[], tier_counts={})

    def tearDown(self):
        self.tmp.cleanup()

    # ---------------------------------------------------------------- 流式路径
    def test_connection_reset_retried_then_succeeds_stream(self):
        calls = {"n": 0}

        def fake_urlopen(request, timeout=None):
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接")
            return _FakeResponse()

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            generation = LLMClient(self.config).generate("requirement_spec", self.bundle, {})
        self.assertEqual(calls["n"], 3, "前两次连接被重置后应重试到第三次成功")
        self.assertFalse(generation.error)
        self.assertEqual(generation.data, {"ok": True})

    def test_connection_refused_exhausts_retries_stream(self):
        calls = {"n": 0}

        def fake_urlopen(request, timeout=None):
            calls["n"] += 1
            raise ConnectionRefusedError(10061, "由于目标计算机积极拒绝，无法连接")

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            generation = LLMClient(self.config).generate("requirement_spec", self.bundle, {})
        self.assertEqual(calls["n"], 3, "max_attempts=3 应三次全部用尽")
        self.assertIsNotNone(generation.error)
        self.assertIn("网络异常", generation.error)   # 归入可重试错误通道，非裸异常穿透
        self.assertTrue(generation.timed_out)

    # ---------------------------------------------------------------- 非流式路径
    def test_connection_reset_retried_then_succeeds_non_stream(self):
        self.config.llm["stream"] = False
        calls = {"n": 0}

        def fake_urlopen(request, timeout=None):
            calls["n"] += 1
            if calls["n"] < 2:
                raise ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接")
            return _FakeTextResponse()

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            generation = LLMClient(self.config).generate("requirement_spec", self.bundle, {})
        self.assertEqual(calls["n"], 2)
        self.assertFalse(generation.error)

    def test_connection_refused_exhausts_retries_non_stream(self):
        self.config.llm["stream"] = False

        def fake_urlopen(request, timeout=None):
            raise ConnectionRefusedError(10061, "由于目标计算机积极拒绝，无法连接")

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            generation = LLMClient(self.config).generate("requirement_spec", self.bundle, {})
        self.assertIsNotNone(generation.error)
        self.assertIn("网络异常", generation.error)

    # ---------------------------------------------------------------- 4xx 不重试
    def test_non_retryable_http_error_fails_immediately(self):
        import io
        import urllib.error

        from mas.errors import AgentError

        calls = {"n": 0}

        def fake_urlopen(request, timeout=None):
            calls["n"] += 1
            raise urllib.error.HTTPError(
                "http://127.0.0.1:1/v1/chat/completions", 401, "Unauthorized",
                {"Content-Type": "application/json"}, io.BytesIO(b'{"error":"invalid token"}'))

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with self.assertRaises(AgentError) as ctx:
                LLMClient(self.config).generate("requirement_spec", self.bundle, {})
        self.assertEqual(calls["n"], 1, "401 是明确的鉴权失败，重试无意义，应立即失败")
        self.assertIn("401", str(ctx.exception))


    # ------------------------------------------------------ 流式总时长看门狗
    def test_stream_total_timeout_retries_instead_of_hanging(self):
        """持续推流但总时长超限：必须主动断开并重试，而不是无限挂起（2026-10-24 实测）。"""
        self.config.llm["stream_total_timeout"] = 10
        calls = {"n": 0}
        clock = {"t": 0.0}

        def fake_monotonic():
            clock["t"] += 100.0      # 每次调用前进 100s，模拟读行累计耗时超限
            return clock["t"]

        def fake_urlopen(request, timeout=None):
            calls["n"] += 1
            return _FakeSSEResponse(_sse_lines('{"ok": true}'))

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), \
                mock.patch("time.monotonic", side_effect=fake_monotonic):
            generation = LLMClient(self.config).generate("requirement_spec", self.bundle, {})
        self.assertEqual(calls["n"], 3, "超时应按 max_attempts 重试，而不是永久阻塞")
        self.assertIsNotNone(generation.error)
        self.assertIn("网络异常", generation.error)

    def test_stream_normal_response_not_killed_by_watchdog(self):
        """总时长未超限的正常流式响应：看门狗不得误伤。"""
        self.config.llm["stream_total_timeout"] = 600

        def fake_urlopen(request, timeout=None):
            return _FakeSSEResponse(_sse_lines('{"ok": true}'))

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), \
                mock.patch("time.monotonic", return_value=0.0):
            generation = LLMClient(self.config).generate("requirement_spec", self.bundle, {})
        self.assertFalse(generation.error)
        self.assertEqual(generation.data, {"ok": True})


if __name__ == "__main__":
    unittest.main()
