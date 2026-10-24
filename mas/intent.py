"""交付形态意图识别（docs/15-前后端交付意图路由与AB档）。

需求规格中的 `delivery_shape` 决定这条流水线按哪种形态交付，以及用哪条证据通道验收：

- ``backend_only``   纯后端（库 / CLI / 服务），无前端产物。
                     **安全默认值**：该形态下本模块不触发任何新行为，与改造前完全一致。
- ``full_stack_ssr`` 前后端一起，页面由服务端渲染（A 档）。
                     证据通道 = Python 测试对 HTTP 响应体做断言。
- ``full_stack_spa`` 前后端一起，浏览器前端（B 档）。
                     证据通道 = Python 测试 + 浏览器断言；浏览器通道当前未接入，
                     该形态必须**如实报失败**，不允许退化成「Python 测试全过就放行」。

识别分三层，判定权不全在 LLM 手里：

=========  ==================================================  ==========  ==================
层          位置                                                性质        产出
=========  ==================================================  ==========  ==================
L1 规则     ``classify_delivery_shape(text)``（本模块）          确定性      intent_hint + 命中词
L2 LLM     product 节点契约字段（``mas/agents/product.py``）     概率性      delivery_shape
L3 框架    产品 Agent 发布前（``ProductAgent.apply``）           确定性      非法值兜底 + 事件
=========  ==================================================  ==========  ==================

L1 只产提示不产结论、不覆盖 L2：它的作用是给 LLM 一条可追溯的确定性参考，
并在事件里留下命中词，便于线上复盘「这个形态是怎么定下来的」。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

__all__ = [
    "DELIVERY_SHAPES",
    "DEFAULT_DELIVERY_SHAPE",
    "UNKNOWN_SHAPE",
    "LEGACY_MODE_MAP",
    "SHAPE_STRICTNESS",
    "classify_delivery_shape",
    "normalize_delivery_shape",
    "probe_delivery_shape",
    "resolve_delivery_shape",
    "shape_family",
]

#: 三值枚举（顺序即文档 §3.1 表格顺序）
DELIVERY_SHAPES: Tuple[str, ...] = ("backend_only", "full_stack_ssr", "full_stack_spa")

#: 兜底形态。取 backend_only 的理由：现有已验证能力全在这个形态下，兜底不改变任何既有行为。
DEFAULT_DELIVERY_SHAPE = "backend_only"

#: L1 无法判定时的提示值（不是合法形态，仅供提示与事件记录）
UNKNOWN_SHAPE = "unknown"

#: 历史命名 ``delivery_mode`` -> ``delivery_shape``。
#: 改造前已存在的需求文档（如 examples/requirements/web_todo_app.md）与黑板产物用旧名，
#: 这里做一次性翻译，避免同一概念出现两套字段。
LEGACY_MODE_MAP: Dict[str, str] = {
    "library": "backend_only",
    "web_api": "backend_only",       # 纯 HTTP 接口服务 = 无前端产物，按文档 §3.1 归入纯后端
    "web_fullstack": "full_stack_ssr",
}

# ---------------------------------------------------------------- L1 关键词表
# 显式句式优先于特征词：需求方明确写了「不需要前端」时，正文里出现「页面」也不该翻案。
_BACKEND_ONLY_PATTERNS: Tuple[str, ...] = (
    "只做后端", "仅后端", "只需要后端", "只要后端", "不需要前端", "无需前端",
    "不含前端", "无前端", "后端即可", "纯后端",
)
_FULLSTACK_PATTERNS: Tuple[str, ...] = (
    "前后端", "全栈", "包含页面", "带页面", "带界面", "要界面", "需要界面",
    "前端也要", "含前端", "web 界面", "web界面", "web页面", "web 页面",
)
# 无显式声明时的特征词（弱信号，冲突即 unknown）
_BACKEND_FEATURE_WORDS: Tuple[str, ...] = ("接口", "SDK", "sdk", "类库", "命令行", "CLI", "cli", "算法")
_FRONTEND_FEATURE_WORDS: Tuple[str, ...] = ("页面", "前端", "界面", "浏览器", "点击", "表单", "网页", "渲染", "UI", "HTML", "html")


def normalize_delivery_shape(value: Any) -> Optional[str]:
    """把任意输入归一化为三值枚举之一；无法归一化返回 ``None``（由调用方按 L3 兜底）。

    同时接受历史命名 ``delivery_mode`` 的取值（见 :data:`LEGACY_MODE_MAP`），
    大小写与首尾空白不敏感。
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if text in DELIVERY_SHAPES:
        return text
    return LEGACY_MODE_MAP.get(text)


def resolve_delivery_shape(spec: Dict[str, Any]) -> Tuple[str, str, bool]:
    """从需求规格解析交付形态，返回 ``(shape, source, fell_back)``。

    ``source`` 取值：``declared``（spec 自带合法值）/ ``legacy_mode``（旧 delivery_mode 翻译）/
    ``default``（缺失或非法，走 L3 兜底）。``fell_back`` 与 ``source == "default"`` 等价，
    调用方据此决定是否发 ``intent_fallback`` 事件。
    """
    spec = spec if isinstance(spec, dict) else {}
    shape = normalize_delivery_shape(spec.get("delivery_shape"))
    if shape:
        return shape, "declared", False
    shape = normalize_delivery_shape(spec.get("delivery_mode"))
    if shape:
        return shape, "legacy_mode", False
    return DEFAULT_DELIVERY_SHAPE, "default", True


def classify_delivery_shape(text: str) -> Dict[str, Any]:
    """L1 规则预分类：确定性、不依赖 LLM，只产提示不产结论。

    返回 ``{"hint": backend_only|full_stack_ssr|unknown, "explicit": bool, "keywords": [...]}``。

    - 命中显式句式 → 直接给提示，不再看特征词；
    - 无显式句式 → 看特征词；两类特征词都命中（或都不命中）→ ``unknown``；
    - ``hint`` 只区分「纯后端 / 全栈」，不区分 SSR 与 SPA：两者无法从需求文本可靠判定，
      全栈一律提示 A 档（``full_stack_ssr``），是否走 B 档由 L2 结合技术约束定夺。
    """
    body = str(text or "")
    lowered = body.lower()
    keywords: List[str] = []

    matched_backend = [word for word in _BACKEND_ONLY_PATTERNS if word in body]
    matched_fullstack = [word for word in _FULLSTACK_PATTERNS if word in body]
    if matched_backend and not matched_fullstack:
        return {"hint": "backend_only", "explicit": True, "keywords": matched_backend}
    if matched_fullstack and not matched_backend:
        return {"hint": "full_stack_ssr", "explicit": True, "keywords": matched_fullstack}
    if matched_backend and matched_fullstack:
        # 显式句式自相矛盾（如「只做后端，但要管理页面」）→ 交给 L2
        return {"hint": UNKNOWN_SHAPE, "explicit": True,
                "keywords": matched_backend + matched_fullstack}

    backend_hits = [word for word in _BACKEND_FEATURE_WORDS if word in body]
    frontend_hits = [word for word in _FRONTEND_FEATURE_WORDS if word in body]
    keywords = backend_hits + frontend_hits
    if backend_hits and not frontend_hits:
        return {"hint": "backend_only", "explicit": False, "keywords": keywords}
    if frontend_hits and not backend_hits:
        return {"hint": "full_stack_ssr", "explicit": False, "keywords": keywords}
    return {"hint": UNKNOWN_SHAPE, "explicit": False, "keywords": keywords}


def shape_family(shape: str) -> str:
    """形态所属档位：``backend`` / ``full_stack``（未知值按 backend 处理，取安全侧）。"""
    return "full_stack" if str(shape or "").startswith("full_stack_") else "backend"


#: 形态严格度序（文件探测只允许"收紧"，不允许放松——fail-closed 方向）。
SHAPE_STRICTNESS: Dict[str, int] = {
    "backend_only": 0,
    "full_stack_ssr": 1,
    "full_stack_spa": 2,
}

# ---------------------------------------------------------------- L4 文件探测
# 交付形态由沙箱里的**真实文件**探测，不由模型自报：模型可以把 SPA 交付申报成
# backend_only 来绕开浏览器证据（历史漏洞：形态声明决定证据通道，声明权却在 LLM）。
# 探测只认确定性文件特征，结果只用于把形态往更严格的方向收紧。
_SPA_CONFIG_BASENAMES = (
    "vite.config.js", "vite.config.ts", "vite.config.mjs",
    "next.config.js", "next.config.mjs", "next.config.ts",
    "nuxt.config.js", "nuxt.config.ts",
    "angular.json", "svelte.config.js", "webpack.config.js", "craco.config.js",
)
_SPA_FRAMEWORK_DEPS = (
    "react", "react-dom", "vue", "svelte", "@angular/core",
    "next", "nuxt", "vite", "@vitejs/plugin-react",
)
_SSR_SERVER_DEPS = ("express", "koa", "fastify", "@nestjs/core", "hapi")
_SPA_HTML_MARKERS = ('id="root"', "id='root'", 'id="app"', 'id="__next"', 'type="module"')
_BROWSER_CODE_SUFFIXES = (".jsx", ".tsx", ".vue", ".svelte")


def probe_delivery_shape(root: Any, files: Iterable[str]) -> Dict[str, Any]:
    """按沙箱工作区的真实文件探测交付形态（L4，确定性，框架侧）。

    返回 ``{"shape": full_stack_spa|full_stack_ssr|None, "evidence": [命中文件/特征]}``；
    ``None`` 表示文件层面没有前端特征（不推翻声明，只放弃收紧）。
    """
    root = Path(root) if root else None
    names = [str(name).replace("\\", "/") for name in (files or [])]
    evidence: List[str] = []

    def read(relative: str) -> str:
        if root is None:
            return ""
        try:
            return (root / relative).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    basenames = {name.rsplit("/", 1)[-1] for name in names}
    html_files = [name for name in names if name.lower().endswith((".html", ".htm"))]
    pkg_files = [name for name in names if name.rsplit("/", 1)[-1] == "package.json"]

    pkg_deps: set = set()
    for pkg_file in pkg_files[:1]:
        try:
            payload = json.loads(read(pkg_file) or "{}")
            pkg_deps = set((payload.get("dependencies") or {})) | set((payload.get("devDependencies") or {}))
        except (ValueError, TypeError):
            evidence.append(f"{pkg_file}（package.json 存在但不可解析）")

    spa_hits = [name for name in names if name.lower().endswith(_BROWSER_CODE_SUFFIXES)]
    config_hits = sorted(basenames & set(_SPA_CONFIG_BASENAMES))
    dep_hits = sorted(pkg_deps & set(_SPA_FRAMEWORK_DEPS))
    marker_hits = [
        name for name in html_files
        if any(marker in read(name) for marker in _SPA_HTML_MARKERS)
    ]

    if spa_hits or config_hits or dep_hits or marker_hits:
        evidence.extend(spa_hits[:5] + config_hits[:3] + [f"package.json 依赖 {dep}" for dep in dep_hits[:3]]
                        + [f"{name} 含 SPA 挂载点/模块脚本标记" for name in marker_hits[:2]])
        return {"shape": "full_stack_spa", "evidence": evidence}

    if html_files:
        server_dep_hits = sorted(pkg_deps & set(_SSR_SERVER_DEPS))
        has_server_py = any(
            name.endswith(".py") and any(framework in read(name) for framework in ("Flask(", "FastAPI(", "Django"))
            for name in names if name.endswith(".py")
        )
        if server_dep_hits or has_server_py or any("/templates/" in name for name in html_files):
            evidence.extend([f"package.json 依赖 {dep}" for dep in server_dep_hits[:3]]
                            + html_files[:3])
            return {"shape": "full_stack_ssr", "evidence": evidence}
        # 纯静态 HTML（无服务端框架）：页面在浏览器里运行，按 SPA 收紧证据通道
        evidence.extend(html_files[:3])
        evidence.append("纯静态 HTML 交付（无服务端渲染框架特征），页面运行在浏览器")
        return {"shape": "full_stack_spa", "evidence": evidence}

    return {"shape": None, "evidence": evidence}
