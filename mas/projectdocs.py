"""项目级指令文件（对标 Codex 的 ``AGENTS.md`` / ``AGENTS.override.md``）。

为什么需要这一层：交付形态（``backend_only`` / ``full_stack_ssr`` / ``full_stack_spa``）
由 L1 关键词规则与 L2 模型判定共同决定，两层都可能判错——实测「俄罗斯方块」这类
单页小游戏被判成 ``full_stack_spa``，而该形态在本机没有浏览器证据通道时**必然**
验收不通过（``runtime.ui_probe`` 默认关闭 → tester 如实报 gap → fail）。流水线
此前没有任何入口让用户纠正这个判定，改 ``intent.py`` 的分类规则等于改框架。
项目指令文件把纠正权交回需求方：放在需求文件旁（或项目根），运行自动收集、
注入每个节点的 Prompt；front-matter 里显式声明的 ``delivery_shape`` 由框架
**确定性采纳**（优先于模型判定，见 :func:`directive_shape`）。

与 Codex 的异同（差异部分是有意为之）：

- 相同：向上逐级收集到项目根、32 KiB 上限、``AGENTS.override.md`` 覆盖同名文件、
  超限即停（``config/src/config_toml.rs:75`` / ``core/src/agents_md.rs:42``）。
- 不同 1：**起点是需求文件所在目录，不是 cwd**。本系统的需求常来自
  ``examples/requirements/`` 之类的子目录，从 cwd 遍历会把无关目录的指令带进来。
- 不同 2：支持**可选** YAML front-matter（与本仓库 ``skills`` 的 SKILL.md 同构）。
  Codex 的 AGENTS.md 是纯 Markdown，声明式约束只能写在正文里靠模型理解；
  本实现把「交付形态」这类会被门禁直接求值的字段做成确定性声明，避免
  "模型这次看懂了、下次没看懂"的概率性。未写 front-matter 时退化为纯文本注入，
  与不存在该文件时完全同路。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .errors import ConfigError
from .intent import normalize_delivery_shape
from .skills import parse_skill_text

__all__ = [
    "AGENTS_FILENAME",
    "OVERRIDE_FILENAME",
    "DEFAULT_MAX_BYTES",
    "DIRECTIVE_KEYS",
    "ProjectDocs",
    "collect_project_docs",
    "cfg_of",
    "directive_shape",
]

AGENTS_FILENAME = "AGENTS.md"
OVERRIDE_FILENAME = "AGENTS.override.md"

#: 与 Codex 一致的累积上限（32 KiB）。按 UTF-8 **字节**计，超了就停，不截断到一半。
DEFAULT_MAX_BYTES = 32 * 1024

#: front-matter 中被框架确定性采纳的键（其余键只作文本说明，不参与判定）
DIRECTIVE_KEYS: Tuple[str, ...] = ("delivery_shape",)


@dataclass
class ProjectDocs:
    """一次收集的结果。``text`` 为空表示本次没有任何项目指令（各节点 Prompt 逐字节不变）。"""

    text: str = ""
    files: List[Dict[str, Any]] = field(default_factory=list)
    directives: Dict[str, Any] = field(default_factory=dict)
    chars: int = 0
    bytes: int = 0
    truncated: bool = False
    skipped: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "files": list(self.files),
            "directives": dict(self.directives),
            "chars": self.chars,
            "bytes": self.bytes,
            "truncated": self.truncated,
            "skipped": self.skipped,
        }


def cfg_of(runtime: Dict[str, Any]) -> Dict[str, Any]:
    """读取 ``runtime.project_docs`` 配置段（缺省即默认开启 + 32 KiB）。"""
    section = dict((runtime or {}).get("project_docs") or {})
    return {
        "enabled": bool(section.get("enabled", True)),
        "max_bytes": int(section.get("max_bytes", DEFAULT_MAX_BYTES) or DEFAULT_MAX_BYTES),
        "filenames": [str(item) for item in (section.get("filenames") or [AGENTS_FILENAME])],
    }


def _byte_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _iter_dirs(start: Path, root: Optional[Path]) -> List[Path]:
    """从 ``start`` 向上逐级列到 ``root``（含两端），顺序为「外 → 内」。

    ``start`` 不在 ``root`` 之下（或两者任一不存在）时退化为只取 ``root``/``start``
    单个目录：宁可少收，也不越过项目根去读仓库之外的文件。
    """
    start = Path(start).resolve()
    root = Path(root).resolve() if root is not None else None
    if root is not None and start != root and root not in start.parents:
        return [start]
    chain: List[Path] = [start]
    current = start
    while root is not None and current != root:
        parent = current.parent
        if parent == current:      # 已到文件系统根
            break
        current = parent
        chain.append(current)
    return list(reversed(chain))


def _read_doc(path: Path) -> Tuple[Dict[str, Any], str]:
    """读一个指令文件：返回 ``(directives, body)``。无 front-matter 时 directives 为空。

    front-matter 缺失或非法不算错误——用户手写的文件不该让运行崩在这里，
    整篇按正文处理（``parse_skill_text`` 对技能文件是硬要求，对项目指令是可选）。
    """
    raw = path.read_text(encoding="utf-8", errors="replace")
    try:
        meta, body = parse_skill_text(raw, origin=str(path))
    except ConfigError:
        return {}, raw
    if not isinstance(meta, dict):
        return {}, raw
    return {key: meta[key] for key in DIRECTIVE_KEYS if key in meta}, body


def collect_project_docs(
    start_dir: Path,
    *,
    root: Optional[Path] = None,
    runtime: Optional[Dict[str, Any]] = None,
) -> ProjectDocs:
    """从 ``start_dir`` 向上收集项目指令，返回可直接注入 Prompt 的文本。

    收集规则：
    1. 每个目录先找 ``AGENTS.md``，同目录存在 ``AGENTS.override.md`` 时以后者替代；
    2. 由外（项目根）向内（需求所在目录）拼接，**内层在后**——离需求越近越权威；
    3. 累积字节达 ``max_bytes`` 即停并标记 ``truncated``（Codex 同策略：扣完即停）；
    4. front-matter 声明按同一顺序覆盖，内层覆盖外层。
    """
    cfg = cfg_of(runtime or {})
    if not cfg["enabled"]:
        return ProjectDocs(skipped="runtime.project_docs.enabled=false")
    max_bytes = max(0, int(cfg["max_bytes"]))
    names: Sequence[str] = [AGENTS_FILENAME] + [str(item) for item in cfg["filenames"]
                                                if str(item) not in (AGENTS_FILENAME, OVERRIDE_FILENAME)]

    chunks: List[str] = []
    files: List[Dict[str, Any]] = []
    directives: Dict[str, Any] = {}
    used = 0
    truncated = False

    for directory in _iter_dirs(start_dir, root):
        path: Optional[Path] = None
        override = directory / OVERRIDE_FILENAME
        if override.is_file():
            path = override
        else:
            for name in names:
                candidate = directory / name
                if candidate.is_file():
                    path = candidate
                    break
        if path is None:
            continue
        try:
            doc_directives, body = _read_doc(path)
        except OSError:
            continue
        body = body.strip()
        if not body:
            continue
        directives.update(doc_directives)
        try:
            rel = str(path.resolve().relative_to(Path(root).resolve() if root else path.parent))
        except (ValueError, OSError):
            rel = str(path)
        rendered = f"### 项目指令：{rel}\n{body}"
        need = _byte_len(rendered)
        if used + need > max_bytes:
            truncated = True
            break
        used += need
        chunks.append(rendered)
        files.append({"path": rel, "chars": len(body),
                      "directives": {key: str(value) for key, value in doc_directives.items()}})

    text = "\n\n".join(chunks)
    return ProjectDocs(
        text=text,
        files=files,
        directives=directives,
        chars=len(text),
        bytes=used,
        truncated=truncated,
    )


def directive_shape(docs: Any) -> Optional[str]:
    """取项目指令声明的交付形态；未声明或取值非法返回 ``None``（交回模型判定）。

    接受 :class:`ProjectDocs` 或它 ``to_dict()`` 后的产物（黑板里存的是后者）。
    只认 :data:`intent.DELIVERY_SHAPES` 内的三值与历史命名——写错的值宁可忽略，
    也不能让一次手误把整条流水线带进不可验证的形态。
    """
    if docs is None:
        return None
    raw = docs.get("directives") if isinstance(docs, dict) else getattr(docs, "directives", None)
    return normalize_delivery_shape((raw or {}).get("delivery_shape"))


def render_section(docs: Optional[ProjectDocs], *, title: str = "# 项目指令（来自项目根目录的 AGENTS.md）") -> str:
    """渲染进 Prompt 的段落；无内容返回空串（调用方据此保持 Prompt 逐字节不变）。"""
    if docs is None or not docs.text:
        return ""
    lines = [title, docs.text]
    if docs.directives.get("delivery_shape"):
        lines.append(
            f"\n> 上述指令文件通过 front-matter 明确声明了交付形态 "
            f"`{docs.directives['delivery_shape']}`，框架已按该值执行，不再由模型重新判定。"
        )
    return "\n\n".join(lines)
