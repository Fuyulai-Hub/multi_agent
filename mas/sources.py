"""输入源适配层：需求输入的「分类 → 抓取/抽取 → 落盘」统一入口。

对应 docs/16 的三个输入类能力（docs/16 的 03 / 04 / 06 文档）：

- 源分类 ``classify_source``：L1 确定性规则，只认域名 + 路径形状，不联网（``mas/intent.py`` 同款范本）；
- 文档抽取 ``extract_text``：三级路由 stdlib → MCP → 明确失败（不静默、不伪造）；
- 飞书文档 ``fetch_feishu_document``：stdlib urllib 直连飞书开放平台，token 进程内缓存；
- 上传暂存 ``save_upload``：前端 ``/api/upload`` 的校验与落盘（与运行目录分离）。

配置走 ``config/inputs.yaml`` 按需加载（照抄 ``config/mcp.yaml`` 的 ``load_servers`` 模式，
不经 ``load_config`` 的 bundle 白名单——新顶层配置段会被静默丢弃）。
默认「不配置即不启用」：非飞书 URL、非待抽取后缀的输入行为与改造前**逐字节一致**。
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from xml.etree import ElementTree

import yaml

from .config import SystemConfig
from .errors import ConfigError
from .mcp import MCPClient, expand_env, get_server

__all__ = [
    "INPUTS_FILE",
    "TEXT_SUFFIXES",
    "UPLOAD_SUFFIXES",
    "IMAGE_SUFFIXES",
    "MAX_UPLOAD_BYTES",
    "MAX_IMAGES",
    "classify_source",
    "load_inputs",
    "file_backend",
    "extract_text",
    "extract_docx_text",
    "fetch_feishu_document",
    "save_upload",
    "prepare_images",
    "MCPDocumentParser",
]

INPUTS_FILE = "inputs.yaml"

#: stdlib 直读的纯文本后缀（零依赖默认路径）
TEXT_SUFFIXES = {
    ".md", ".markdown", ".txt", ".text", ".csv", ".json",
    ".yaml", ".yml", ".rst", ".log", ".ini", ".cfg", ".toml",
}

#: docx 由 stdlib 抽取（zipfile + ElementTree）
DOCX_SUFFIX = ".docx"

#: 上传白名单：能否解析由后端三级路由决定（pdf 未配置 MCP 时如实报错）
UPLOAD_SUFFIXES = TEXT_SUFFIXES | {DOCX_SUFFIX, ".pdf", ".pptx", ".xlsx"}

#: 上传大小硬上限（base64 解码后判定）
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

#: 附图后缀白名单与 mime 映射（图片只进 intake，见 docs/16 的 01 文档）
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
IMAGE_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif",
}
#: 单次运行附图张数上限（超出截断并告警）
MAX_IMAGES = 4

# 飞书开放平台域名（国内 feishu.cn / 国际 larksuite.com）
_FEISHU_HOSTS = ("feishu.cn", "larksuite.com")
_FEISHU_PATH = re.compile(r"/(docx|docs|wiki|sheets|base|slides)/", re.IGNORECASE)

#: 进程内 tenant_access_token 缓存：key=(api_base, app_id) -> (token, 到期时间戳)
_TOKEN_CACHE: Dict[Tuple[str, str], Tuple[str, float]] = {}


# --------------------------------------------------------------------------- 配置
def inputs_path(config: SystemConfig) -> Path:
    return config.config_dir / INPUTS_FILE


def load_inputs(config: SystemConfig) -> Dict[str, Any]:
    """读取 config/inputs.yaml；文件不存在时返回空 dict（未配置属正常情况）。"""
    path = inputs_path(config)
    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} 解析失败: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} 顶层必须是映射(dict)")
    return raw


# --------------------------------------------------------------------------- 分类
def classify_source(value: str, *, as_text: bool = False) -> str:
    """L1 规则判定输入源类型：``text`` / ``feishu`` / ``url`` / ``file``（确定性，不联网）。"""
    if as_text:
        return "text"
    text = str(value or "").strip()
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme in ("http", "https") and parsed.hostname:
        host = parsed.hostname.lower()
        if any(host == item or host.endswith("." + item) for item in _FEISHU_HOSTS) \
                and _FEISHU_PATH.search(parsed.path or ""):
            return "feishu"
        return "url"
    return "file"


def file_backend(path: Path, config: Optional[SystemConfig] = None) -> str:
    """判定文件后缀应走哪条通道：``text``（stdlib 直读）/ ``docx``（stdlib 抽取）/
    ``mcp``（配置了文档解析 MCP 服务）/ ``unsupported``（都不支持 → 明确失败）。"""
    suffix = Path(path).suffix.lower()
    if suffix == DOCX_SUFFIX:
        return "docx"
    formats = _document_parse(config).get("formats") or []
    if suffix in {str(item).lower() for item in formats}:
        return "mcp"
    if suffix in TEXT_SUFFIXES:
        return "text"
    return "unsupported"


def _document_parse(config: Optional[SystemConfig]) -> Dict[str, Any]:
    if config is None:
        return {}
    section = load_inputs(config).get("document_parse") or {}
    return section if isinstance(section, dict) else {}


# --------------------------------------------------------------------------- 抽取
def extract_text(path: Path, config: Optional[SystemConfig] = None, *, encoding: str = "utf-8"
                 ) -> Tuple[str, str]:
    """按后缀三级路由抽取文档文本，返回 ``(text, backend)``；``backend`` ∈ stdlib / mcp。

    都不支持时抛 :class:`ConfigError`（如实失败，不静默返回空串）。
    """
    path = Path(path)
    backend = file_backend(path, config)
    if backend == "text":
        return path.read_text(encoding=encoding), "stdlib"
    if backend == "docx":
        return extract_docx_text(path), "stdlib"
    if backend == "mcp":
        return MCPDocumentParser(config).parse(path), "mcp"
    supported = "、".join(sorted(TEXT_SUFFIXES | {DOCX_SUFFIX}))
    raise ConfigError(
        f"格式 {path.suffix or '(无后缀)'} 暂不能解析：可直接读取的格式为 {supported}；"
        f"PDF/pptx/xlsx 等需在 config/mcp.yaml 配置文档解析 MCP 服务，"
        f"并在 config/inputs.yaml 的 document_parse.formats 中登记该后缀"
        f"（详见 docs/16-五项能力扩展最小改动技术方案/06-MCP文档解析接入评估.md）"
    )


def extract_docx_text(path: Path) -> str:
    """stdlib 抽取 docx：zipfile 取 ``word/document.xml`` → ElementTree 遍历 ``w:t``。

    保真度有限（表格拼成空格、图片/页眉页脚丢失），在结果头部如实标注。
    """
    try:
        with zipfile.ZipFile(path) as archive:
            xml_bytes = archive.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        raise ConfigError(f"docx 抽取失败（{path.name}）: {exc}") from exc
    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError as exc:
        raise ConfigError(f"docx 内容解析失败（{path.name}）: {exc}") from exc
    namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    paragraphs: List[str] = []
    for paragraph in root.iter(f"{{{namespace}}}p"):
        paragraphs.append("".join(node.text or "" for node in paragraph.iter(f"{{{namespace}}}t")))
    body = "\n".join(paragraphs)
    note = "（本文档由自动抽取生成，表格、图片与页眉页脚可能丢失）\n"
    return note + body


class MCPDocumentParser:
    """框架直调 MCP 文档解析服务（不走模型 function calling，确定性且 provider 无关）。

    - 服务条目取自 ``config/mcp.yaml``（如 docparse），工具名/入参名/后缀表取自 ``config/inputs.yaml``；
    - 只契约化「返回文本 content」的 server：``isError=true`` 或空文本 → :class:`ConfigError`；
    - 生命周期 try/finally close（范本 ``mas/mcp.py`` 的 ``check_server``）。
    """

    def __init__(self, config: Optional[SystemConfig]) -> None:
        self.config = config

    def parse(self, path: Path) -> str:
        if self.config is None:
            raise ConfigError("MCP 文档解析需要配置对象（extract_text 未传入 config）")
        section = _document_parse(self.config)
        server_name = str(section.get("server") or "")
        tool = str(section.get("tool") or "")
        path_arg = str(section.get("path_arg") or "file_path")
        if not server_name or not tool:
            raise ConfigError(
                "未配置文档解析 MCP 服务：请检查 config/inputs.yaml 的 document_parse"
                "（server / tool / path_arg / formats）"
            )
        server = get_server(self.config, server_name)
        client = MCPClient(server)
        client.start()
        try:
            client.initialize()
            result = client.call_tool(tool, {path_arg: str(Path(path).resolve())})
        finally:
            client.close()
        if result.get("isError"):
            raise ConfigError(f"文档解析 MCP 服务返回错误（tool={tool}）: {result}")
        texts = [
            str(item.get("text") or "")
            for item in (result.get("content") or [])
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        text = "\n".join(part for part in texts if part)
        if not text:
            raise ConfigError(
                f"文档解析 MCP 服务未返回文本 content（tool={tool}）："
                "本适配层只支持返回文本的 server，返回文件路径的 server 不适配"
            )
        return text


# --------------------------------------------------------------------------- 飞书
def _http_json(
    url: str,
    *,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    body: Optional[Dict[str, Any]] = None,
    timeout: int = 15,
) -> Dict[str, Any]:
    """发一次 HTTP 请求并解析 JSON；网络失败/非 JSON 一律翻译为可读的 ConfigError。"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise ConfigError(f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ConfigError(f"网络请求失败: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"返回非 JSON 内容: {raw[:200]}") from exc
    if not isinstance(payload, dict):
        raise ConfigError(f"返回结构非法: {raw[:200]}")
    return payload


def _parse_feishu_target(url: str) -> Tuple[str, str]:
    """从飞书 URL 解析 ``(token, kind)``；kind ∈ docx / wiki / other。"""
    parsed = urllib.parse.urlparse(url)
    parts = [part for part in (parsed.path or "").split("/") if part]
    if len(parts) < 2:
        raise ConfigError(f"无法从飞书链接解析文档 token: {url}")
    kind = parts[0].lower()
    token = parts[1]
    if kind in ("docx", "docs"):
        return token, "docx"
    if kind == "wiki":
        return token, "wiki"
    return token, kind


def _tenant_token(api_base: str, app_id: str, app_secret: str, timeout: int) -> str:
    """获取 tenant_access_token（进程内缓存至到期前 60s）。"""
    cache_key = (api_base, app_id)
    cached = _TOKEN_CACHE.get(cache_key)
    now = datetime.now().timestamp()
    if cached and cached[1] > now + 60:
        return cached[0]
    body = _http_json(
        f"{api_base}/open-apis/auth/v3/tenant_access_token/internal",
        method="POST",
        body={"app_id": app_id, "app_secret": app_secret},
        timeout=timeout,
    )
    if body.get("code") != 0:
        raise ConfigError(f"飞书 token 获取失败 code={body.get('code')}: {body.get('msg')}")
    token = str(body.get("tenant_access_token") or "")
    if not token:
        raise ConfigError(f"飞书 token 响应缺少 tenant_access_token: {body}")
    expire = int(body.get("expire") or 7200)
    _TOKEN_CACHE[cache_key] = (token, now + expire)
    return token


def fetch_feishu_document(url: str, config: SystemConfig) -> Tuple[str, str]:
    """把飞书文档链接解析成 ``(title, text)``，与 ``load_requirement`` 返回值口径一致。

    端点：① tenant_access_token ② wiki 链接先取 node（非 docx 如实报错）
    ③ 文档元信息取标题 ④ raw_content 取纯文本正文。
    """
    section = load_inputs(config).get("feishu") or {}
    if not section.get("enabled", True):
        raise ConfigError("飞书文档解析未启用（config/inputs.yaml 的 feishu.enabled=false）")
    api_base = str(section.get("api_base") or "https://open.feishu.cn").rstrip("/")
    timeout = int(section.get("timeout_s", 15))
    origin = "inputs.yaml feishu"
    app_id = expand_env(section.get("app_id", ""), origin=f"{origin}.app_id")
    app_secret = expand_env(section.get("app_secret", ""), origin=f"{origin}.app_secret")
    doc_token, kind = _parse_feishu_target(url)
    token = _tenant_token(api_base, app_id, app_secret, timeout)
    headers = {"Authorization": f"Bearer {token}"}

    if kind == "wiki":
        node = _http_json(
            f"{api_base}/open-apis/wiki/v2/spaces/get_node?token={urllib.parse.quote(doc_token)}",
            headers=headers, timeout=timeout,
        )
        if node.get("code") != 0:
            raise ConfigError(f"飞书 wiki 节点解析失败 code={node.get('code')}: {node.get('msg')}")
        node_data = node.get("data") or {}
        obj_type = str(node_data.get("obj_type") or "")
        if obj_type != "docx":
            raise ConfigError(
                f"飞书 wiki 指向的对象类型为 {obj_type or '未知'}，本方案仅支持 docx 文档"
                "（sheet / 幻灯片 / 多维表格暂不支持）"
            )
        doc_token = str(node_data.get("obj_token") or "")
    elif kind != "docx":
        raise ConfigError(f"飞书链接类型 {kind} 暂不支持（仅支持 docx 文档与 wiki 中的 docx）")

    title = ""
    meta = _http_json(f"{api_base}/open-apis/docx/v1/documents/{doc_token}", headers=headers,
                      timeout=timeout)
    if meta.get("code") == 0:
        title = str(((meta.get("data") or {}).get("document") or {}).get("title") or "")
    content = _http_json(
        f"{api_base}/open-apis/docx/v1/documents/{doc_token}/raw_content",
        headers=headers, timeout=timeout,
    )
    if content.get("code") != 0:
        raise ConfigError(
            f"飞书文档正文获取失败 code={content.get('code')}: {content.get('msg')}"
            "（确认应用已开通 docx:document:readonly 权限并被授予该文档）"
        )
    text = str((content.get("data") or {}).get("content") or "")
    if not text.strip():
        raise ConfigError(f"飞书文档正文为空（document_id={doc_token}）")
    return (title or doc_token or "feishu-document"), text


# --------------------------------------------------------------------------- 上传
def _safe_name(name: str) -> str:
    """文件名净化：取 basename + 去除控制字符，拒绝路径穿越（`..`/绝对路径）。"""
    raw = str(name or "").replace("\\", "/")
    base = Path(raw).name
    base = "".join(ch for ch in base if ch.isprintable() and ch not in '\r\n\t')
    if not base or base in (".", ".."):
        raise ConfigError(f"上传文件名非法: {name}")
    if len(base) > 120:
        base = base[-120:]
    return base


def save_upload(
    workspace: Path,
    filename: str,
    content_base64: str,
    *,
    allowed_suffixes: Optional[Set[str]] = None,
    max_bytes: Optional[int] = None,
) -> Dict[str, Any]:
    """校验并暂存一次上传，返回元数据；失败抛 :class:`ConfigError`（调用方转 400）。

    边界：后缀白名单 / 解码后 ≤ 10MB / 文件名净化 / 落盘 resolve 校验在 uploads 目录内。

    ``allowed_suffixes`` 可覆盖后缀白名单（对话窗口附图上传传 ``IMAGE_SUFFIXES``）；
    ``max_bytes`` 可覆盖大小上限。两者默认值分别为 ``UPLOAD_SUFFIXES`` / ``MAX_UPLOAD_BYTES``，
    **不传时行为与本函数既有契约逐字节一致**（需求文档上传路径不受影响）。
    """
    safe_name = _safe_name(filename)
    suffix = Path(safe_name).suffix.lower()
    allowed = UPLOAD_SUFFIXES if allowed_suffixes is None else {str(item).lower() for item in allowed_suffixes}
    if suffix not in allowed:
        raise ConfigError(
            f"不支持的文件后缀 {suffix or '(无后缀)'}：允许 "
            + "、".join(sorted(allowed))
        )
    try:
        content = base64.b64decode(str(content_base64 or ""), validate=True)
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"content_base64 不是合法 base64: {exc}") from exc
    if not content:
        raise ConfigError("上传文件为空")
    limit = MAX_UPLOAD_BYTES if max_bytes is None else int(max_bytes)
    if len(content) > limit:
        raise ConfigError(f"上传文件超过大小上限 {limit // (1024 * 1024)}MB")

    uploads_dir = Path(workspace) / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    upload_id = uuid.uuid4().hex[:12]
    target = uploads_dir / f"{upload_id}__{safe_name}"
    resolved = target.resolve()
    if uploads_dir.resolve() not in resolved.parents:
        raise ConfigError(f"上传落盘路径越界: {resolved}")
    target.write_bytes(content)

    kind = file_backend(target)
    chars: Optional[int] = None
    if kind == "text":
        chars = len(content.decode("utf-8", errors="replace"))
    return {
        "upload_id": upload_id,
        "path": str(target),
        "name": safe_name,
        "kind": kind,
        "bytes": len(content),
        "chars": chars,
    }


# --------------------------------------------------------------------------- 附图
def prepare_images(
    images: List[str],
    run_dir: Path,
    *,
    project_root: Optional[Path] = None,
    max_bytes: int = 5242880,
    max_count: int = MAX_IMAGES,
) -> Tuple[List[Dict[str, Any]], int]:
    """校验需求附图并复制到 ``run_dir/input/images/``，返回 ``(meta 列表, 被截断张数)``。

    meta 只存元数据与相对 run_dir 的路径（**不存 base64**，避免 artifact 膨胀）；
    后缀白名单 / 单张大小上限 / 张数上限在此校验，越界一律 :class:`ConfigError`（明确报错）。
    """
    if not images:
        return [], 0
    images_dir = Path(run_dir) / "input" / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    metas: List[Dict[str, Any]] = []
    truncated = max(0, len(images) - max_count)
    for raw in list(images)[:max_count]:
        path = Path(raw)
        if not path.is_absolute() and project_root is not None:
            path = Path(project_root) / path
        if not path.exists():
            raise ConfigError(f"附图不存在: {path}")
        suffix = path.suffix.lower()
        if suffix not in IMAGE_SUFFIXES:
            raise ConfigError(
                f"不支持的图片后缀 {suffix or '(无后缀)'}：允许 "
                + "、".join(sorted(IMAGE_SUFFIXES))
            )
        size = path.stat().st_size
        if size > max_bytes:
            raise ConfigError(f"附图 {path.name} 超过单张上限 {max_bytes // 1024}KB")
        target = images_dir / path.name
        target.write_bytes(path.read_bytes())
        metas.append({
            "name": path.name,
            "path": (Path("input") / "images" / path.name).as_posix(),
            "mime": IMAGE_MIME.get(suffix, "image/png"),
            "bytes": size,
        })
    return metas, truncated
