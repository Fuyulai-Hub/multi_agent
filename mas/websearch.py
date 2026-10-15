"""联网搜索（docs/16 的 05 文档，路径 B：模型自主发起的本地 ``web_search`` 工具）。

provider 可插拔（bing / tavily / serpapi），stdlib urllib 实现，零第三方依赖。

与 MCP 工具失败同约定：**搜索失败返回空列表 + 事件，不抛异常**（错误作为 tool 消息内容回传，
让模型自行纠错，闭环不中断）。搜索结果仅作事实来源，prompt 注入风险的缓解见
``config/roles/product.yaml`` 的 guidelines（参考资料不可信）与 intake 产物只到 spec 层。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional

__all__ = ["PROVIDERS", "search"]

PROVIDERS = ("bing", "tavily", "serpapi")

_ENDPOINTS = {
    "bing": "https://api.bing.microsoft.com/v7.0/search",
    "tavily": "https://api.tavily.com/search",
    "serpapi": "https://serpapi.com/search",
}

#: 单条摘要最大字符数（防搜索结果体积挤爆 prompt）
_SNIPPET_MAX = 500

EventSink = Optional[Callable[[str, Dict[str, Any]], None]]


def _emit(sink: EventSink, kind: str, payload: Dict[str, Any]) -> None:
    if sink is None:
        return
    try:
        sink(kind, payload)
    except Exception:  # noqa: BLE001 - 事件留痕失败不影响搜索
        pass


def _get_json(url: str, *, headers: Optional[Dict[str, str]] = None, timeout: int = 15) -> Dict[str, Any]:
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def _post_json(url: str, payload: Dict[str, Any], *, timeout: int = 15) -> Dict[str, Any]:
    request = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def _clip(text: Any) -> str:
    value = str(text or "").strip()
    return value[:_SNIPPET_MAX]


def _results_bing(body: Dict[str, Any], limit: int) -> List[Dict[str, str]]:
    pages = ((body.get("webPages") or {}).get("value")) or []
    return [
        {"title": _clip(item.get("name")), "url": str(item.get("url") or ""),
         "snippet": _clip(item.get("snippet"))}
        for item in pages[:limit]
    ]


def _results_tavily(body: Dict[str, Any], limit: int) -> List[Dict[str, str]]:
    return [
        {"title": _clip(item.get("title")), "url": str(item.get("url") or ""),
         "snippet": _clip(item.get("content"))}
        for item in (body.get("results") or [])[:limit]
    ]


def _results_serpapi(body: Dict[str, Any], limit: int) -> List[Dict[str, str]]:
    return [
        {"title": _clip(item.get("title")), "url": str(item.get("link") or ""),
         "snippet": _clip(item.get("snippet"))}
        for item in (body.get("organic_results") or [])[:limit]
    ]


def search(
    query: str,
    *,
    provider: str = "bing",
    api_key: str = "",
    max_results: int = 5,
    timeout_s: int = 15,
    endpoint: str = "",
    event_sink: EventSink = None,
) -> List[Dict[str, str]]:
    """检索一次并返回 ``[{title, url, snippet}, ...]``；任何失败返回 ``[]``（不抛异常）。

    ``endpoint`` 可覆盖默认地址（测试桩用）。
    """
    keyword = str(query or "").strip()
    if not keyword:
        return []
    limit = max(1, int(max_results or 5))
    _emit(event_sink, "websearch_requested",
          {"query": keyword, "provider": provider, "max_results": limit})
    try:
        if provider not in PROVIDERS:
            raise ValueError(f"不支持的搜索 provider: {provider}")
        if not api_key:
            raise ValueError("缺少搜索 API Key")
        base = endpoint or _ENDPOINTS[provider]
        if provider == "bing":
            url = f"{base}?q={urllib.parse.quote(keyword)}&count={limit}"
            body = _get_json(url, headers={"Ocp-Apim-Subscription-Key": api_key}, timeout=timeout_s)
            results = _results_bing(body, limit)
        elif provider == "tavily":
            body = _post_json(base, {"api_key": api_key, "query": keyword, "max_results": limit},
                              timeout=timeout_s)
            results = _results_tavily(body, limit)
        else:  # serpapi
            url = f"{base}?engine=google&q={urllib.parse.quote(keyword)}&api_key={urllib.parse.quote(api_key)}"
            body = _get_json(url, timeout=timeout_s)
            results = _results_serpapi(body, limit)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError,
            json.JSONDecodeError, ValueError, KeyError, TypeError) as exc:
        _emit(event_sink, "websearch_failed",
              {"query": keyword, "provider": provider, "error": f"{type(exc).__name__}: {exc}"})
        return []
    if not results:
        _emit(event_sink, "websearch_empty", {"query": keyword, "provider": provider})
    return results
