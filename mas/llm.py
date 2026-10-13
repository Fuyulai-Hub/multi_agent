"""LLM 客户端：统一封装 offline（内置确定性 Provider）与 openai_compatible（真实大模型）。"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .config import SystemConfig, resolve_credentials
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


class _RetryableLLMError(AgentError):
    """可重试错误（网关 504 / 读超时 / 429）重试耗尽后抛出。

    与 length 截断同等对待：交由降级链换 reasoning_effort 重发，而不是直接让节点崩掉。
    （本端点 openresty 在 300s 处强制断开，max_tokens 给大后必然 504。）
    """


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
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)  # function calling 调用请求
    # 输出预算诊断：推理模型把 reasoning token 计入 max_tokens，思考链打满时正文为空
    finish_reason: str = ""           # stop | length（被输出预算截断）| tool_calls | ...
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0         # usage.completion_tokens_details.reasoning_tokens（端点不返回则为 0）
    degraded: bool = False            # 本次结果来自"降低/调整思考强度"的降级重发
    timed_out: bool = False           # 网关超时（504）/读超时：与 length 截断同等对待

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
            "tool_calls": len(self.tool_calls),
            "finish_reason": self.finish_reason,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "degraded": self.degraded,
            "timed_out": self.timed_out,
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
        # function calling：tools 由运行时注入（ToolRunner）或 config.llm.tools 静态声明
        self.tool_choice = str(config.llm.get("tool_choice", "auto"))
        self.tool_max_rounds = max(1, int(config.llm.get("tool_max_rounds", 2)))
        # 网络重试：默认 1 次（不重试，保持既有行为）；config llm.retry 可开启
        retry_raw = dict(config.llm.get("retry") or {})
        self.retry_max_attempts = max(1, int(retry_raw.get("max_attempts", 1)))
        self.retry_backoff_s = float(retry_raw.get("backoff_s", 2.0))
        # 思考预算治理：推理模型（GLM/DeepSeek/Qwen）把 reasoning token 计入 max_tokens 输出预算，
        # 思考链打满预算时正文为空（finish_reason=length）。空正文时按下面配置降级重发。
        self.reasoning_effort = str(config.llm.get("reasoning_effort") or "")   # 空=不下发，保持端点默认
        self.empty_output_retry = max(0, int(config.llm.get("empty_output_retry", 2) or 0))
        # 降级阶梯：依次尝试的 reasoning_effort。默认 "low,high" —— 先 low（实测 33s，
        # 距网关 300s 上限余量大），low 仍不可用时兜底 high（实测 115s，余量较小但能收敛）。
        # 兼容单值写法（"low" / ["low"]），也支持逗号分隔或列表。
        self.downgrade_ladder = self._parse_ladder(config.llm.get("downgrade_reasoning_effort") or "low,high")
        self._offline: Any = None

    @staticmethod
    def _parse_ladder(raw: Any) -> List[str]:
        items = raw.split(",") if isinstance(raw, str) else list(raw or [])
        return [str(item).strip() for item in items if str(item).strip()]

    # finish_reason 中表示"输出被预算截断"的取值
    TRUNCATED_FINISH = "length"

    # ---------------------------------------------------------------- 入口
    def generate(
        self,
        task_kind: str,
        bundle: PromptBundle,
        context: Dict[str, Any],
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        extra_messages: Optional[List[Dict[str, Any]]] = None,
    ) -> Generation:
        started = time.time()
        if self.provider == "offline":
            generation = self._generate_offline(task_kind, bundle, context)
        elif self.provider == "openai_compatible":
            generation = self._generate_openai(task_kind, bundle, tools=tools, extra_messages=extra_messages)
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
    def _generate_openai(
        self,
        task_kind: str,
        bundle: PromptBundle,
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        extra_messages: Optional[List[Dict[str, Any]]] = None,
    ) -> Generation:
        api_key_env = str(self.config.llm.get("api_key_env", "OPENAI_API_KEY"))
        # 凭据统一外注：config/credentials.yaml < 环境变量 < 运行时注入（前端表单 / CLI）
        api_key, base_url = resolve_credentials(
            self.config.llm, self.config.config_dir, override=self.config.llm_overrides
        )
        if not api_key:
            raise AgentError(
                f"llm.provider=openai_compatible 需要 API Key，请任选一种方式注入："
                f"① config/credentials.yaml 的 api_key（前端「设为默认」可写入）；"
                f"② 环境变量 {api_key_env}；③ 前端表单 / CLI --api-key。"
                "如需无 Key 运行请把配置切回 provider=offline"
            )
        payload = self._build_payload(bundle, tools=tools, extra_messages=extra_messages)
        # json_mode 与 tools 互斥（见 _build_payload）：仅在真下了 response_format 时才校验可解析性
        expect_json = bool(self.json_mode and not tools)
        ladder = self.downgrade_ladder[: self.empty_output_retry]

        generation = self._attempt(payload, api_key, base_url)
        step = 0
        # 降级链：首次结果不可用 → 按 ladder 逐级换 reasoning_effort 重发。
        # 不可用 = 被预算截断 / 网关超时，或（降级档）正文为空、解析失败。
        # 注意：function calling 的中间轮正文为空是正常的（模型在请求工具调用），必须排除。
        while step < len(ladder) and self._unusable(
            generation, expect_json=expect_json, downgraded=bool(generation.degraded)
        ):
            retry_payload = dict(payload)
            retry_payload["reasoning_effort"] = ladder[step]
            step += 1
            fallback = self._attempt(retry_payload, api_key, base_url)
            fallback.degraded = True
            generation = fallback
        if not generation.text and not generation.tool_calls and not generation.error:
            generation.error = self._empty_output_error(generation)
        return generation

    def _attempt(self, payload: Dict[str, Any], api_key: str, base_url: str) -> Generation:
        """发一次请求；可重试错误（网关 504 / 读超时）不抛出，返回 timed_out 标记的结果。

        这样超时与 length 截断走同一条降级链，而不是让节点直接崩掉。
        """
        try:
            return self._request_generation(payload, api_key, base_url)
        except _RetryableLLMError as exc:
            generation = Generation(provider="openai_compatible", model=self.model)
            generation.timed_out = True
            generation.error = f"网关超时或可重试错误（重试 {self.retry_max_attempts} 次后仍失败）：{exc}"
            return generation

    def _unusable(self, generation: Generation, *, expect_json: bool, downgraded: bool) -> bool:
        """判断一次结果是否"不可用"，需要继续降级。

        downgraded=False（首次结果）：只有"被预算截断"或"网关超时"才进入降级链——
            空正文但 finish_reason 不是 length 时降级无意义，保持既有口径不重发。
        downgraded=True（降级档结果）：截断 / 超时 / 空正文 / 解析不出 JSON 都算质量不足，
            继续下一级（low 不够 → high）。
        """
        if generation.tool_calls:
            return False
        if generation.timed_out or generation.finish_reason == self.TRUNCATED_FINISH:
            return True
        if not generation.text:
            return downgraded
        return bool(downgraded and expect_json and generation.data is None)

    def _build_payload(
        self,
        bundle: PromptBundle,
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        extra_messages: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """组装 /chat/completions 请求体（token 预算与思考强度集中在此，便于审计）。"""
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": bundle.system},
            {"role": "user", "content": bundle.user},
        ]
        messages.extend(extra_messages or [])
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        if self.json_mode and not tools:
            payload["response_format"] = {"type": "json_object"}
        if tools:
            # function calling：tools 与 json_mode 互斥（部分端点不支持同时开启）
            payload["tools"] = tools
            payload["tool_choice"] = self.tool_choice
        return payload

    def _request_generation(
        self, payload: Dict[str, Any], api_key: str, base_url: str
    ) -> Generation:
        """发一次请求并解析为 Generation（不含任何降级逻辑）。"""
        request = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            method="POST",
        )
        return self._parse_response(self._post_with_retry(request))

    def _parse_response(self, body: Dict[str, Any]) -> Generation:
        """解析响应：正文、思考链、tool_calls，以及 usage 里的输出预算明细。"""
        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message", {})
        text = str(message.get("content") or "")
        # 推理模型思考链：智谱 GLM 用 reasoning_content，其他厂商可能用 reasoning
        reasoning = str(message.get("reasoning_content") or message.get("reasoning") or "")
        usage = body.get("usage") or {}
        # reasoning_tokens：OpenAI 新格式在 completion_tokens_details 里（DeepSeek 实测有，
        # 本网关的 GLM 不返回该明细 → 记 0，不做估算，避免假数据）
        details = usage.get("completion_tokens_details") or {}
        return Generation(
            text=text,
            data=parse_json_block(text) if text else None,
            provider="openai_compatible",
            model=self.model,
            tokens=int(usage.get("total_tokens", 0) or 0),
            reasoning=reasoning,
            tool_calls=self._parse_tool_calls(message),
            finish_reason=str(choice.get("finish_reason") or ""),
            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage.get("completion_tokens", 0) or 0),
            reasoning_tokens=int(details.get("reasoning_tokens") or usage.get("reasoning_tokens") or 0),
        )

    @staticmethod
    def _empty_output_error(generation: Generation) -> str:
        """空正文的诊断信息：把"思考链占满预算"这一根因直接写进 error，避免静默失败。"""
        detail = f"completion_tokens={generation.completion_tokens}"
        if generation.reasoning_tokens:
            detail += f", reasoning_tokens={generation.reasoning_tokens}"
        if generation.reasoning:
            detail += f", reasoning_chars={len(generation.reasoning)}"
        reason = generation.finish_reason or "unknown"
        if reason == LLMClient.TRUNCATED_FINISH:
            return (
                f"模型返回空正文：输出被 max_tokens 截断（finish_reason=length，{detail}），"
                f"思考链占满输出预算，正文无剩余额度"
            )
        return f"模型返回空正文（finish_reason={reason}，{detail}）"

    def _post_with_retry(self, request: urllib.request.Request) -> Dict[str, Any]:
        """带重试的 POST：网络错误与 429/5xx 按 llm.retry 配置重试，4xx 立即失败。"""
        attempts = self.retry_max_attempts
        last_error = ""
        for attempt in range(1, attempts + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:400]
                last_error = f"LLM 调用失败 HTTP {exc.code}: {detail}"
                retryable = exc.code == 429 or exc.code >= 500
            except (TimeoutError, urllib.error.URLError) as exc:
                # 网关在 300s 处强制断开（openresty 504）；客户端读超时是同一语义。
                # 原实现只捕 URLError，而 urlopen 读阶段抛的是 TimeoutError（非 URLError 子类），
                # 导致这类超时光既不重试也未被归类，直接穿透。
                last_error = f"LLM 超时/网络异常: {exc}"
                retryable = True
            if not retryable or attempt == attempts:
                raise _RetryableLLMError(last_error) if retryable else AgentError(last_error)
            time.sleep(self.retry_backoff_s * attempt)
        raise AgentError(last_error or "LLM 调用失败")  # pragma: no cover - 理论上不可达

    @staticmethod
    def _parse_tool_calls(message: Dict[str, Any]) -> List[Dict[str, Any]]:
        """归一化 tool_calls：arguments 的 JSON 字符串解析为 dict；解析失败保留原串（由执行层兜底）。"""
        calls: List[Dict[str, Any]] = []
        for item in message.get("tool_calls") or []:
            function = item.get("function") or {}
            raw_args = function.get("arguments")
            arguments: Any = raw_args
            if isinstance(raw_args, str):
                try:
                    arguments = json.loads(raw_args) if raw_args.strip() else {}
                except json.JSONDecodeError:
                    arguments = raw_args
            calls.append({
                "id": str(item.get("id") or ""),
                "type": str(item.get("type") or "function"),
                "name": str(function.get("name") or ""),
                "arguments": arguments,
            })
        return calls
