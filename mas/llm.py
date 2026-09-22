"""LLM 客户端：统一封装 offline（内置确定性 Provider）与 openai_compatible（真实大模型）。"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .config import SystemConfig
from .errors import AgentError
from .prompts import PromptBundle

__all__ = ["Generation", "LLMClient", "parse_json_block"]


def parse_json_block(text: str) -> Optional[Dict[str, Any]]:
    """从 LLM 输出中容错地提取首个 JSON 对象。"""
    if not text:
        return None
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("```")[1] if "```" in stripped[3:] else stripped[3:]
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else stripped
        if stripped.lstrip().startswith("json"):
            stripped = stripped.lstrip()[4:]
    candidates = [stripped]
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end > start:
        candidates.append(stripped[start : end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, TypeError):
            continue
    return None


@dataclass
class Generation:
    """一次模型生成结果。"""

    text: str = ""
    data: Optional[Dict[str, Any]] = None
    provider: str = ""
    model: str = ""
    latency_ms: int = 0
    tokens: int = 0
    task_kind: str = ""
    prompt_path: Optional[str] = None
    experiences: List[str] = field(default_factory=list)
    error: str = ""
    reasoning: str = ""               # 推理模型的思考链（如 GLM reasoning_content）

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "task_kind": self.task_kind,
            "latency_ms": self.latency_ms,
            "tokens": self.tokens,
            "prompt_path": self.prompt_path,
            "experiences": self.experiences,
            "error": self.error,
            "reasoning_chars": len(self.reasoning),
        }


class LLMClient:
    """按配置选择 Provider 的模型调用入口。"""

    def __init__(self, config: SystemConfig) -> None:
        self.config = config
        self.provider = str(config.llm.get("provider", "offline"))
        self.model = str(config.llm.get("model", "offline-deterministic"))
        self.temperature = float(config.llm.get("temperature", 0.2))
        self.max_tokens = int(config.llm.get("max_tokens", 3000))
        self.timeout = int(config.llm.get("timeout", 120))
        self.json_mode = bool(config.llm.get("json_mode", True))
        self._offline: Any = None

    # ---------------------------------------------------------------- 入口
    def generate(self, task_kind: str, bundle: PromptBundle, context: Dict[str, Any]) -> Generation:
        started = time.time()
        if self.provider == "offline":
            generation = self._generate_offline(task_kind, bundle, context)
        elif self.provider == "openai_compatible":
            generation = self._generate_openai(task_kind, bundle)
        else:
            raise AgentError(f"未知的 llm.provider: {self.provider}")
        generation.latency_ms = int((time.time() - started) * 1000)
        generation.task_kind = task_kind
        generation.prompt_path = str(bundle.path) if bundle.path else None
        generation.experiences = [item["id"] for item in bundle.experiences]
        return generation

    # ---------------------------------------------------------------- offline
    def _generate_offline(self, task_kind: str, bundle: PromptBundle, context: Dict[str, Any]) -> Generation:
        if self._offline is None:
            from .providers.offline import OfflineProvider

            self._offline = OfflineProvider(self.config)
        data = self._offline.provide(task_kind, bundle=bundle, context=context)
        text = json.dumps(data, ensure_ascii=False, indent=2)
        return Generation(
            text=text,
            data=data,
            provider="offline",
            model="offline-deterministic",
            tokens=int(len(text) / 3.2) + 1,
        )

    # ---------------------------------------------------------------- openai
    def _generate_openai(self, task_kind: str, bundle: PromptBundle) -> Generation:
        api_key_env = str(self.config.llm.get("api_key_env", "OPENAI_API_KEY"))
        api_key = os.environ.get(api_key_env, "")
        if not api_key:
            raise AgentError(
                f"llm.provider=openai_compatible 需要环境变量 {api_key_env}；"
                "如需无 Key 运行请把配置切回 provider=offline"
            )
        base_url = str(self.config.llm.get("base_url", "https://api.openai.com/v1")).rstrip("/")
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": bundle.system},
                {"role": "user", "content": bundle.user},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if self.json_mode:
            payload["response_format"] = {"type": "json_object"}
        request = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            raise AgentError(f"LLM 调用失败 HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise AgentError(f"LLM 网络异常: {exc}") from exc
        message = body.get("choices", [{}])[0].get("message", {})
        text = str(message.get("content") or "")
        # 推理模型思考链：智谱 GLM 用 reasoning_content，其他厂商可能用 reasoning
        reasoning = str(message.get("reasoning_content") or message.get("reasoning") or "")
        usage = body.get("usage") or {}
        return Generation(
            text=text,
            data=parse_json_block(text),
            provider="openai_compatible",
            model=self.model,
            tokens=int(usage.get("total_tokens", 0) or 0),
            reasoning=reasoning,
        )
