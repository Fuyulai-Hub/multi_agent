"""LLM 客户端：统一封装 offline（内置确定性 Provider）与 openai_compatible（真实大模型）。"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import SystemConfig, resolve_credentials
from .context import ContextManager
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
        # 流式输出（默认开）：本端点网关（openresty）proxy_read_timeout=300s 按"两次读之间
        # 的间隔"计；非流式下模型生成完才吐首字节，300s 即被判超时断开（504），长生成
        # （implement/optimize 全量代码）三次降级尝试全部空输出（实测 3×300s）。
        # 流式下 TTFB <1s、chunk 间隔远小于 300s，长生成不再触发网关上限。
        self.stream = bool(config.llm.get("stream", True))
        # 流式总时长看门狗：urlopen 的 timeout 只约束单次 socket 读，服务端持续推流
        # （思考链/心跳 chunk）时总时长无上限——实测 2026-10-24 一次请求挂 40 分钟无返回。
        # 超过该值主动断开，抛 TimeoutError 走既有可重试通道，而不是无限等待。
        self.stream_total_timeout = int(config.llm.get("stream_total_timeout", 1800))
        # function calling：tools 由运行时注入（ToolRunner）或 config.llm.tools 静态声明
        self.tool_choice = str(config.llm.get("tool_choice", "auto"))
        self.tool_max_rounds = max(1, int(config.llm.get("tool_max_rounds", 2)))
        # 网络重试：代码默认 1 次（不重试，未配置时的既有行为）；config llm.retry 可开启，
        # 当前 system.yaml 实配 3 次（连接类异常 10054/10061 纳入可重试分类，backoff 3s）
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
        # 图片输入（docs/16 的 01 文档）：默认关闭；开启且 payload 带图时 user content 改 parts 数组。
        # 端点是否接受 image_url 属未验证项，故默认 false；收到 400 且详情提及 image 时自动去图重发。
        vision = dict(config.llm.get("vision") or {})
        self.vision_enabled = bool(vision.get("enabled", False))
        self.vision_max_image_bytes = int(vision.get("max_image_bytes", 5242880))
        self.vision_url_style = str(vision.get("url_style", "data_uri"))
        self._offline: Any = None
        # 上下文治理（docs/35）：Codex 式预算治理，唯一生效点在 _build_payload。
        # 代码默认关闭（runtime.context.enabled 未配置时），开启后也只在超限时才动手，
        # 因此未配置 / 未超限两种情况下请求体与改造前逐字节一致。
        self.context = ContextManager.from_config(config)
        self.last_fit = None      # 最近一次治理结果（供 Agent 层留痕，未治理时为 None）

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
        # 每次生成前清空上一次的治理结果：一个节点内多轮生成时，只留痕「最近一轮」的治理，
        # 避免上一轮的治理结果被下一个节点误当作自己的事件发出去。
        self.last_fit = None
        if self.provider == "offline":
            generation = self._generate_offline(task_kind, bundle, context)
        elif self.provider == "openai_compatible":
            generation = self._generate_openai(
                task_kind, bundle, context=context, tools=tools, extra_messages=extra_messages,
            )
        else:
            raise AgentError(f"未知的 llm.provider: {self.provider}")
        # 校准：端点返回的 prompt_tokens 是真实值，本地字节/4 只是粗估（Codex 同口径：
        # 有服务端 usage 就优先信服务端）。offline provider 不返回 usage，此处为 no-op。
        self.context.observe(generation)
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
        context: Optional[Dict[str, Any]] = None,
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
        payload = self._build_payload(bundle, context=context, tools=tools, extra_messages=extra_messages)
        # json_mode 与 tools 互斥（见 _build_payload）：仅在真下了 response_format 时才校验可解析性
        expect_json = bool(self.json_mode and not tools)
        ladder = self.downgrade_ladder[: self.empty_output_retry]

        try:
            generation = self._attempt(payload, api_key, base_url)
        except AgentError as exc:
            # 图片降级（docs/16 的 01 文档 §3.2）：端点拒绝 image parts（400 且详情提及
            # image/multimodal/vision）时去图重发一次，节点不失败，degraded 标记随 trace 留痕。
            if self._payload_has_images(payload) and self._is_vision_error(str(exc)):
                bare_payload = self._build_payload(
                    bundle, context=None, tools=tools, extra_messages=extra_messages
                )
                generation = self._attempt(bare_payload, api_key, base_url)
                generation.degraded = True
            else:
                raise
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
        context: Optional[Dict[str, Any]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        extra_messages: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """组装 /chat/completions 请求体（token 预算与思考强度集中在此，便于审计）。"""
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": bundle.system},
            {"role": "user", "content": self._user_content(bundle, context)},
        ]
        messages.extend(extra_messages or [])
        # 上下文治理（docs/35）：唯一组装点。未启用或未超限时 fit 原样返回，请求体逐字节不变。
        fit = self.context.fit(messages)
        messages = fit.messages
        self.last_fit = fit
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

    # ---------------------------------------------------------------- 图片输入
    def _user_content(self, bundle: PromptBundle, context: Optional[Dict[str, Any]]) -> Any:
        """user content：默认纯文本；images 非空且 vision.enabled 时改 parts 数组（唯一组装点）。

        base64 读取与大小二次校验都在这里完成（Leader 已校验过一次，这里是防线）。
        """
        images = list((context or {}).get("images") or [])
        if not images or not self.vision_enabled:
            return bundle.user
        parts: List[Dict[str, Any]] = [{"type": "text", "text": bundle.user}]
        for item in images:
            part = self._image_part(item)
            if part:
                parts.append(part)
        # 所有图片都不可用时退回纯文本，不阻断节点（图片只进 intake，缺图不影响下游）
        return parts if len(parts) > 1 else bundle.user

    def _image_part(self, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        path = Path(str(item.get("path") or ""))
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise AgentError(f"读取附图失败（{path}）: {exc}") from exc
        if len(data) > self.vision_max_image_bytes:
            raise AgentError(
                f"附图 {path.name} 超过单张上限 {self.vision_max_image_bytes // 1024}KB"
            )
        mime = str(item.get("mime") or "image/png")
        encoded = base64.b64encode(data).decode("ascii")
        url = f"data:{mime};base64,{encoded}" if self.vision_url_style == "data_uri" else encoded
        return {"type": "image_url", "image_url": {"url": url}}

    @staticmethod
    def _payload_has_images(payload: Dict[str, Any]) -> bool:
        for message in payload.get("messages") or []:
            content = message.get("content")
            if isinstance(content, list) and any(
                isinstance(part, dict) and part.get("type") == "image_url" for part in content
            ):
                return True
        return False

    @staticmethod
    def _is_vision_error(detail: str) -> bool:
        lowered = str(detail).lower()
        return any(word in lowered for word in ("image", "multimodal", "vision"))

    def _request_generation(
        self, payload: Dict[str, Any], api_key: str, base_url: str
    ) -> Generation:
        """发一次请求并解析为 Generation（不含任何降级逻辑）。"""
        if self.stream:
            return self._request_generation_stream(payload, api_key, base_url)
        request = self._build_request(payload, api_key, base_url)
        return self._parse_response(self._post_with_retry(request))

    @staticmethod
    def _build_request(payload: Dict[str, Any], api_key: str, base_url: str) -> urllib.request.Request:
        return urllib.request.Request(
            f"{base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            method="POST",
        )

    def _request_generation_stream(
        self, payload: Dict[str, Any], api_key: str, base_url: str
    ) -> Generation:
        """流式（SSE）请求：逐 chunk 读取，规避网关 300s 非流式总时长上限。

        重试语义与 _post_with_retry 一致（网络错误与 429/5xx 重试，4xx 立即失败）；
        读完后按非流式响应结构组装 body，复用 _parse_response，保证字段口径一致。
        容错：服务端忽略 stream=true 而返回普通 JSON 时（按 Content-Type 判定），
        退化为一次性解析，行为与非流式路径相同。
        """
        stream_payload = dict(payload)
        stream_payload["stream"] = True
        request = self._build_request(stream_payload, api_key, base_url)
        attempts = self.retry_max_attempts
        last_error = ""
        text_parts: List[str] = []
        reasoning_parts: List[str] = []
        tool_calls_by_index: Dict[int, Dict[str, Any]] = {}
        finish_reason = ""
        usage: Dict[str, Any] = {}
        for attempt in range(1, attempts + 1):
            text_parts, reasoning_parts, tool_calls_by_index = [], [], {}
            finish_reason, usage = "", {}
            deadline = time.monotonic() + self.stream_total_timeout
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    content_type = str(response.headers.get("Content-Type") or "")
                    if "text/event-stream" not in content_type:
                        # 服务端未按 SSE 返回（如测试桩/不透流式的网关）：按普通 JSON 解析
                        return self._parse_response(json.loads(response.read().decode("utf-8")))
                    for raw_line in response:
                        if time.monotonic() > deadline:
                            raise TimeoutError(
                                f"流式响应总时长超过 {self.stream_total_timeout}s，主动断开重试"
                            )
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line.startswith("data:"):
                            continue
                        data_str = line[len("data:"):].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        if chunk.get("usage"):
                            usage = chunk["usage"]
                        for choice in chunk.get("choices") or []:
                            if choice.get("finish_reason"):
                                finish_reason = str(choice["finish_reason"])
                            delta = choice.get("delta") or {}
                            if delta.get("content"):
                                text_parts.append(str(delta["content"]))
                            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                            if reasoning:
                                reasoning_parts.append(str(reasoning))
                            for call in delta.get("tool_calls") or []:
                                index = int(call.get("index") or 0)
                                slot = tool_calls_by_index.setdefault(
                                    index, {"id": "", "type": "function",
                                            "function": {"name": "", "arguments": ""}}
                                )
                                if call.get("id"):
                                    slot["id"] = str(call["id"])
                                function = call.get("function") or {}
                                if function.get("name"):
                                    slot["function"]["name"] = str(function["name"])
                                if function.get("arguments"):
                                    slot["function"]["arguments"] += str(function["arguments"])
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:400]
                last_error = f"LLM 调用失败 HTTP {exc.code}: {detail}"
                retryable = exc.code == 429 or exc.code >= 500
            except (TimeoutError, urllib.error.URLError, ConnectionError) as exc:
                # 与 _post_with_retry 同一语义：读阶段超时是 TimeoutError（非 URLError 子类）；
                # 连接建立 / 响应头读取阶段被对端 RST 或拒连时，urlopen 抛的是裸
                # ConnectionResetError / ConnectionRefusedError（WinError 10054 / 10061，
                # 同样不是 URLError 子类）——实测这两类异常会绕过本 except 直接穿透到
                # Agent 层把节点判死，瞬时网络抖动因此一次致命，必须一并按可重试处理。
                last_error = f"LLM 超时/网络异常: {exc}"
                retryable = True
            if not retryable or attempt == attempts:
                raise _RetryableLLMError(last_error) if retryable else AgentError(last_error)
            time.sleep(self.retry_backoff_s * attempt)
        message: Dict[str, Any] = {}
        text = "".join(text_parts)
        reasoning = "".join(reasoning_parts)
        if text:
            message["content"] = text
        if reasoning:
            message["reasoning_content"] = reasoning
        if tool_calls_by_index:
            message["tool_calls"] = [
                tool_calls_by_index[index] for index in sorted(tool_calls_by_index)
            ]
        body = {"choices": [{"message": message, "finish_reason": finish_reason}], "usage": usage}
        return self._parse_response(body)

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
            except (TimeoutError, urllib.error.URLError, ConnectionError) as exc:
                # 网关在 300s 处强制断开（openresty 504）；客户端读超时是同一语义。
                # 原实现只捕 URLError，而 urlopen 读阶段抛的是 TimeoutError（非 URLError 子类），
                # 导致这类超时光既不重试也未被归类，直接穿透。
                # 连接建立 / 响应头读取阶段的裸 ConnectionResetError / ConnectionRefusedError
                # （WinError 10054 / 10061）同理，一并纳入可重试（与流式路径同一分类口径）。
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
