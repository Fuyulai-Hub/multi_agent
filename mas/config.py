"""配置加载与校验。

设计要点：
- 单一入口 `load_config(config_dir, profile)`，把 system/flow/collaboration/learning/roles
  合并成一个强类型配置对象；
- Profile 采用深合并（deep merge），可覆盖任意片段：新增节点、裁剪节点、调整门禁、改回退策略；
- 加载期做结构校验（节点/角色/流转/产物/表达式），把配置错误尽早暴露，而不是运行到一半失败。
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:  # pragma: no cover - 环境依赖检查
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("缺少依赖 PyYAML，请执行: pip install pyyaml") from exc

from .errors import ConfigError
from .expr import check_config_expressions

__all__ = [
    "load_config",
    "SystemConfig",
    "RoleConfig",
    "FlowConfig",
    "NodeConfig",
    "Criterion",
    "CollaborationConfig",
    "LearningConfig",
    "TierConfig",
    "deep_merge",
    "credentials_path",
    "load_credentials",
    "load_credentials_raw",
    "save_credentials",
    "resolve_credentials",
    "mask_secret",
]


# --------------------------------------------------------------------------- 工具
def read_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"配置文件不存在: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件解析失败 {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件顶层必须是映射(dict): {path}")
    return data


def deep_merge(base: Any, override: Any) -> Any:
    """深合并：dict 递归合并，list/标量整体覆盖（便于用 Profile 精确替换门禁清单）。"""
    if isinstance(base, dict) and isinstance(override, dict):
        merged = copy.deepcopy(base)
        for key, value in override.items():
            merged[key] = deep_merge(merged[key], value) if key in merged else copy.deepcopy(value)
        return merged
    return copy.deepcopy(override)


# --------------------------------------------------------------------------- 凭据
# API Key / BASE_URL 不写死在代码里，统一由三个通道注入（后者覆盖前者）：
#   1) config/credentials.yaml（本地文件，前端界面「设为默认」写入）
#   2) 环境变量 OPENAI_API_KEY / OPENAI_BASE_URL（可用 llm.api_key_env / base_url_env 改名）
#   3) 运行时注入 SystemConfig.llm_overrides（前端表单单次运行、CLI --api-key/--base-url）
CREDENTIALS_FILE = "credentials.yaml"
DEFAULT_API_KEY_ENV = "OPENAI_API_KEY"
DEFAULT_BASE_URL_ENV = "OPENAI_BASE_URL"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
CREDENTIAL_KEYS = ("api_key", "base_url", "token_name")


def credentials_path(config_dir: str | Path | None = None) -> Path:
    """本地凭据文件路径（含密钥，已加入 .gitignore）。"""
    base = Path(config_dir) if config_dir is not None else Path(__file__).resolve().parent.parent / "config"
    return (base / CREDENTIALS_FILE).resolve()


def load_credentials(config_dir: str | Path | None = None) -> Dict[str, str]:
    """读取本地凭据文件；文件不存在或为空时返回空字典（不报错，回落到其他注入通道）。"""
    path = credentials_path(config_dir)
    if not path.exists():
        return {}
    try:
        raw = read_yaml(path)
    except ConfigError:
        return {}
    return {key: str(raw[key]).strip() for key in CREDENTIAL_KEYS if str(raw.get(key) or "").strip()}


def load_credentials_raw(config_dir: str | Path | None = None) -> Dict[str, Any]:
    """读取凭据文件全文（含 store 等非 LLM 段）；文件不存在/损坏时返回 {}。

    LLM 凭据段仍由 load_credentials 按 CREDENTIAL_KEYS 过滤消费；本函数供
    mas/store 读取同文件里的 store 段（MySQL/Redis 连接参数），避免第二份秘密文件。
    """
    path = credentials_path(config_dir)
    if not path.exists():
        return {}
    try:
        raw = read_yaml(path)
    except ConfigError:
        return {}
    return raw if isinstance(raw, dict) else {}


def save_credentials(
    config_dir: str | Path | None = None,
    *,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    token_name: Optional[str] = None,
) -> Dict[str, str]:
    """把凭据写入 config/credentials.yaml：只覆盖传入字段，未传的保持原值。

    必须基于**全文**合并写入：该文件除 LLM 三键外还承载 store 段
    （MySQL/Redis 连接参数，由 mas/store 经 load_credentials_raw 读取）。
    若按 LLM 键过滤后再整文件重写，store 段会被静默冲掉——前端每点一次
    「保存」就丢一次 MySQL 密码，存储后端随后按设计静默降级（连接失败
    不抛异常），现象是「数据莫名只进 jsonl、不进 MySQL」。
    """
    path = credentials_path(config_dir)
    data = dict(load_credentials_raw(config_dir))
    if api_key and api_key.strip():
        data["api_key"] = api_key.strip()
    if base_url and base_url.strip():
        data["base_url"] = base_url.strip().rstrip("/")
    if token_name and token_name.strip():
        data["token_name"] = token_name.strip()
    header = (
        "# =============================================================================\n"
        "# LLM 凭据（本地文件，已加入 .gitignore，禁止提交到版本库）\n"
        "#\n"
        "# 三种注入方式，生效优先级（后者覆盖前者）：\n"
        "#   1. config/credentials.yaml —— 本文件，前端「设为默认」写入，也可手工编辑\n"
        "#   2. 环境变量 —— 变量名由 system.yaml 的 llm.api_key_env / base_url_env 指定\n"
        "#                （未配置时回落 OPENAI_API_KEY / OPENAI_BASE_URL）\n"
        "#   3. 运行时注入              —— 前端表单单次运行、或 CLI 的 --api-key / --base-url\n"
        "# =============================================================================\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    # newline="\n"：Windows 下 write_text 默认把 \n 转成 \r\n，整份文件行尾被改写（同 system.yaml 回写约定）
    path.write_text(header + body, encoding="utf-8", newline="\n")
    return {key: str(data[key]).strip() for key in CREDENTIAL_KEYS if str(data.get(key) or "").strip()}


def mask_secret(value: str, keep: int = 4) -> str:
    """密钥脱敏展示：sk-0gW…yyxW3。"""
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= keep + 4:
        return "*" * len(text)
    return f"{text[:3]}…{text[-keep:]}"


def resolve_credentials(
    llm_cfg: Dict[str, Any],
    config_dir: str | Path | None = None,
    override: Optional[Dict[str, str]] = None,
) -> Tuple[str, str]:
    """按优先级解析 (api_key, base_url)：凭据文件 < 环境变量 < 运行时注入。"""
    file_creds = load_credentials(config_dir)
    runtime = {k: str(v).strip() for k, v in (override or {}).items() if str(v or "").strip()}
    api_key_env = str(llm_cfg.get("api_key_env") or DEFAULT_API_KEY_ENV)
    base_url_env = str(llm_cfg.get("base_url_env") or DEFAULT_BASE_URL_ENV)
    api_key = runtime.get("api_key") or os.environ.get(api_key_env, "") or str(file_creds.get("api_key") or "")
    base_url = (
        runtime.get("base_url")
        or os.environ.get(base_url_env, "")
        or str(file_creds.get("base_url") or "")
        or str(llm_cfg.get("base_url") or "")   # system.yaml 的 llm.base_url 作为最低优先级默认
        or DEFAULT_BASE_URL
    )
    return api_key, base_url.rstrip("/")


def _dig(data: Dict[str, Any], path: str, default: Any = None) -> Any:
    cursor: Any = data
    for part in path.split("."):
        if not isinstance(cursor, dict) or part not in cursor:
            return default
        cursor = cursor[part]
    return cursor


# --------------------------------------------------------------------------- 数据结构
@dataclass
class Criterion:
    """验收门禁判定项。"""

    id: str
    expr: str
    description: str = ""
    weight: float = 1.0

    @classmethod
    def from_raw(cls, raw: Dict[str, Any]) -> "Criterion":
        if not isinstance(raw, dict) or "id" not in raw:
            raise ConfigError(f"门禁判定项非法（需要 id/expr）: {raw!r}")
        return cls(
            id=str(raw["id"]),
            expr=str(raw.get("expr", "")),
            description=str(raw.get("description", "")),
            weight=float(raw.get("weight", 1.0)),
        )


@dataclass
class RetryPolicy:
    max_attempts: int = 1

    @classmethod
    def from_raw(cls, raw: Optional[Dict[str, Any]]) -> "RetryPolicy":
        raw = raw or {}
        return cls(max_attempts=int(raw.get("max_attempts", 1)))


@dataclass
class NodeConfig:
    """流程节点。"""

    id: str
    title: str
    role: Optional[str]
    kind: str = "task"                 # task | gate
    inputs: List[str] = field(default_factory=list)
    outputs: List[str] = field(default_factory=list)
    transitions: Dict[str, str] = field(default_factory=dict)
    criteria: List[Criterion] = field(default_factory=list)
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    iterable: bool = True
    options: Dict[str, Any] = field(default_factory=dict)
    skip: bool = False

    @classmethod
    def from_raw(cls, node_id: str, raw: Dict[str, Any]) -> "NodeConfig":
        if not isinstance(raw, dict):
            raise ConfigError(f"节点 {node_id} 配置必须是映射")
        kind = str(raw.get("kind", "task"))
        if kind not in {"task", "gate"}:
            raise ConfigError(f"节点 {node_id} 的 kind 非法: {kind}（只支持 task/gate）")
        gate = raw.get("gate") or {}
        criteria = [Criterion.from_raw(item) for item in (gate.get("criteria") or [])]
        transitions = {str(k): str(v) for k, v in (raw.get("transitions") or {}).items()}
        return cls(
            id=node_id,
            title=str(raw.get("title", node_id)),
            role=raw.get("role"),
            kind=kind,
            inputs=[str(item) for item in (raw.get("inputs") or [])],
            outputs=[str(item) for item in (raw.get("outputs") or [])],
            transitions=transitions,
            criteria=criteria,
            retry=RetryPolicy.from_raw(raw.get("retry")),
            iterable=bool(raw.get("iterable", True)),
            options=dict(raw.get("options") or {}),
            skip=bool(raw.get("skip", False)),
        )

    def target(self, branch: str) -> Optional[str]:
        return self.transitions.get(branch) or self.transitions.get("default")


@dataclass
class FlowConfig:
    entry: str
    max_steps: int
    terminal: List[str]
    nodes: Dict[str, NodeConfig]

    def node(self, node_id: str) -> NodeConfig:
        try:
            return self.nodes[node_id]
        except KeyError as exc:  # pragma: no cover - 校验期已保证
            raise ConfigError(f"流程节点不存在: {node_id}") from exc

    def order(self) -> List[str]:
        """按入口开始的流转顺序给出节点推荐顺序（仅用于展示）。"""
        seen: List[str] = []
        cursor: Optional[str] = self.entry
        while cursor and cursor in self.nodes and cursor not in seen:
            seen.append(cursor)
            node = self.nodes[cursor]
            nxt = node.target("pass")
            cursor = nxt
        for node_id in self.nodes:
            if node_id not in seen:
                seen.append(node_id)
        return seen

    def is_backward(self, source: str, target: str) -> bool:
        """判断 source -> target 是否为回退（按节点在配置中的声明顺序，即流水线顺序）。"""
        order = list(self.nodes)
        if source not in order or target not in order:
            return False
        return order.index(target) < order.index(source)


@dataclass
class RoleConfig:
    """角色配置（职责可配置）。"""

    id: str
    name: str
    mission: str = ""
    responsibilities: List[str] = field(default_factory=list)
    inputs: List[str] = field(default_factory=list)
    outputs: List[str] = field(default_factory=list)
    permissions: Dict[str, Any] = field(default_factory=dict)
    persona: str = ""
    guidelines: List[str] = field(default_factory=list)
    quality_bar: List[str] = field(default_factory=list)
    output_format: str = "json"
    subscribe: List[str] = field(default_factory=list)
    publish: List[str] = field(default_factory=list)
    learning_tiers: List[str] = field(default_factory=lambda: ["L3", "L2"])
    learning_max_items: int = 3
    learning_tags: List[str] = field(default_factory=list)
    policies: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, role_id: str, raw: Dict[str, Any]) -> "RoleConfig":
        if not isinstance(raw, dict):
            raise ConfigError(f"角色 {role_id} 配置必须是映射")
        prompt = raw.get("prompt") or {}
        perception = raw.get("perception") or {}
        learning = raw.get("learning") or {}
        return cls(
            id=str(raw.get("id", role_id)),
            name=str(raw.get("name", role_id)),
            mission=str(raw.get("mission", "")),
            responsibilities=[str(item) for item in (raw.get("responsibilities") or [])],
            inputs=[str(item) for item in (raw.get("inputs") or [])],
            outputs=[str(item) for item in (raw.get("outputs") or [])],
            permissions=dict(raw.get("permissions") or {}),
            persona=str(prompt.get("persona", "")),
            guidelines=[str(item) for item in (prompt.get("guidelines") or [])],
            quality_bar=[str(item) for item in (prompt.get("quality_bar") or [])],
            output_format=str(prompt.get("output_format", "json")),
            subscribe=[str(item) for item in (perception.get("subscribe") or [])],
            publish=[str(item) for item in (perception.get("publish") or [])],
            learning_tiers=[str(item) for item in (learning.get("tiers") or ["L3", "L2"])],
            learning_max_items=int(learning.get("max_items", 3)),
            learning_tags=[str(item) for item in (learning.get("tags") or [])],
            policies=dict(raw.get("policies") or {}),
        )

    def policy(self, key: str, default: Any = None) -> Any:
        return self.policies.get(key, default)


@dataclass
class RollbackPolicy:
    node_id: str
    target: str
    max_attempts: int
    reason: str


@dataclass
class CollaborationConfig:
    rule_set: str
    perception: Dict[str, List[str]]
    broadcast: Dict[str, List[str]]
    negotiation: Dict[str, Any]
    rollback: Dict[str, Dict[str, Any]]
    escalation: Dict[str, Any]
    status_aliases: Dict[str, List[str]]
    guards: Dict[str, Any]

    @classmethod
    def from_raw(cls, raw: Dict[str, Any]) -> "CollaborationConfig":
        return cls(
            rule_set=str(raw.get("rule_set", "")),
            perception={k: [str(i) for i in (v or [])] for k, v in (raw.get("perception") or {}).items()},
            broadcast={k: [str(i) for i in (v or [])] for k, v in (raw.get("broadcast") or {}).items()},
            negotiation=dict(raw.get("negotiation") or {}),
            rollback={k: dict(v or {}) for k, v in (raw.get("rollback") or {}).items()},
            escalation=dict(raw.get("escalation") or {}),
            status_aliases={k: [str(i).lower() for i in (v or [])] for k, v in (raw.get("status_aliases") or {}).items()},
            guards=dict(raw.get("guards") or {}),
        )

    def branch_of(self, status: str) -> str:
        """把 Agent 返回状态归一化成 pass / fail 分支。"""
        normalized = str(status or "").strip().lower()
        for branch, aliases in self.status_aliases.items():
            if normalized in aliases:
                return branch
        return "pass" if normalized == "pass" else "fail"

    def broadcast_targets(self, event_type: str) -> List[str]:
        return list(self.broadcast.get(event_type, []))

    def perception_of(self, role_id: str) -> List[str]:
        return list(self.perception.get(role_id, []))

    def rollback_policy(self, node_id: str, node: NodeConfig) -> RollbackPolicy:
        raw = dict(self.rollback.get(node_id) or {})
        target = str(raw.get("target") or node.target("fail") or "")
        max_attempts = int(raw.get("max_attempts", self.guards.get("default_rollback_attempts", 2)))
        reason = str(raw.get("reason") or f"{node.title}未通过，触发回退")
        return RollbackPolicy(node_id=node_id, target=target, max_attempts=max_attempts, reason=reason)


@dataclass
class TierConfig:
    key: str
    name: str
    scope: str
    enabled: bool
    max_items: int
    min_confidence: float
    budget_tokens: int
    weight: float


@dataclass
class LearningConfig:
    enabled: bool
    storage: Dict[str, str]
    tiers: Dict[str, TierConfig]
    scoring: Dict[str, float]
    extraction: Dict[str, Any]
    promotion: Dict[str, int]
    injection: Dict[str, Any]
    feedback: Dict[str, float]
    synonyms: Dict[str, List[str]] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, raw: Dict[str, Any]) -> "LearningConfig":
        tiers: Dict[str, TierConfig] = {}
        for key, value in (raw.get("tiers") or {}).items():
            value = value or {}
            tiers[str(key)] = TierConfig(
                key=str(key),
                name=str(value.get("name", key)),
                scope=str(value.get("scope", "global")),
                enabled=bool(value.get("enabled", True)),
                max_items=int(value.get("max_items", 3)),
                min_confidence=float(value.get("min_confidence", 0.0)),
                budget_tokens=int(value.get("budget_tokens", 300)),
                weight=float(value.get("weight", 1.0)),
            )
        synonyms: Dict[str, List[str]] = {}
        for key, value in (raw.get("synonyms") or {}).items():
            if isinstance(value, str):
                synonyms[str(key)] = [value]
            elif isinstance(value, list):
                synonyms[str(key)] = [str(item) for item in value]
        return cls(
            enabled=bool(raw.get("enabled", True)),
            storage={k: str(v) for k, v in (raw.get("storage") or {}).items()},
            tiers=tiers,
            scoring={k: float(v) for k, v in (raw.get("scoring") or {}).items()},
            extraction=dict(raw.get("extraction") or {}),
            promotion={k: int(v) for k, v in (raw.get("promotion") or {}).items()},
            injection=dict(raw.get("injection") or {}),
            feedback={k: float(v) for k, v in (raw.get("feedback") or {}).items()},
            synonyms=synonyms,
        )

    def tier(self, key: str) -> Optional[TierConfig]:
        return self.tiers.get(key)


@dataclass
class SystemConfig:
    """合并后的系统配置。"""

    config_dir: Path
    project_root: Path
    profile: Optional[str]
    system: Dict[str, Any]
    llm: Dict[str, Any]
    runtime: Dict[str, Any]
    flow: FlowConfig
    collaboration: CollaborationConfig
    learning: LearningConfig
    roles: Dict[str, RoleConfig]
    raw: Dict[str, Any]
    source_files: List[Path] = field(default_factory=list)
    workspace_override: Optional[Path] = None
    # 运行时注入的 LLM 凭据（{"api_key": ..., "base_url": ...}），优先级高于环境变量与凭据文件
    llm_overrides: Dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------ 便捷访问
    @property
    def name(self) -> str:
        return str(self.system.get("name", "多 Agent 任务协作系统"))

    @property
    def workspace_dir(self) -> Path:
        if self.workspace_override:
            return Path(self.workspace_override).resolve()
        return (self.project_root / str(self.system.get("workspace", "workspace"))).resolve()

    @property
    def runs_dir(self) -> Path:
        return self.workspace_dir / "runs"

    @property
    def store(self) -> Dict[str, Any]:
        """runtime.store 段（MySQL/Redis 接入配置，docs/25）。

        缺省为空 dict → mas.store 全部后端返回 None，走既有 jsonl/内存路径。
        """
        return dict(self.runtime.get("store") or {})

    @property
    def knowledge_dir(self) -> Path:
        return self.workspace_dir / "knowledge"

    @property
    def reports_dir(self) -> Path:
        return self.workspace_dir / "reports"

    @property
    def skills_dir(self) -> Path:
        """技能目录（每个技能一个子目录，见 mas/skills.py 的目录规范）。"""
        return (self.config_dir / str(self.system.get("skills_dir", "skills"))).resolve()

    def role(self, role_id: str) -> RoleConfig:
        try:
            return self.roles[role_id]
        except KeyError as exc:
            raise ConfigError(f"未定义角色: {role_id}") from exc

    def get(self, path: str, default: Any = None) -> Any:
        return _dig(self.raw, path, default)

    def describe(self) -> str:
        """配置摘要（用于 config show / 运行头部输出）。"""
        lines = [
            f"配置名称   : {self.name}",
            f"配置目录   : {self.config_dir}",
            f"Profile    : {self.profile or '(base)'}",
            f"Workspace  : {self.workspace_dir}",
            f"LLM        : provider={self.llm.get('provider')} model={self.llm.get('model')}",
            f"角色({len(self.roles)})   : " + ", ".join(f"{rid}({role.name})" for rid, role in self.roles.items()),
            f"流程节点({len(self.flow.nodes)}) : " + " → ".join(self.flow.order()),
        ]
        for node_id, node in self.flow.nodes.items():
            if node.kind == "gate":
                lines.append(f"  门禁 {node_id}: " + "; ".join(f"{c.id}" for c in node.criteria))
        lines.append("回退策略   : " + ", ".join(
            f"{node}→{cfg.get('target', '?')}×{cfg.get('max_attempts', '?')}"
            for node, cfg in self.collaboration.rollback.items()
        ))
        lines.append("经验分层   : " + ", ".join(
            f"{tier.key}={tier.name}({tier.max_items}条/{tier.budget_tokens}tok)"
            for tier in self.learning.tiers.values()
        ))
        return "\n".join(lines)


# --------------------------------------------------------------------------- 加载
def _load_roles(roles_dir: Path) -> Dict[str, Dict[str, Any]]:
    if not roles_dir.exists():
        raise ConfigError(f"角色配置目录不存在: {roles_dir}")
    roles: Dict[str, Dict[str, Any]] = {}
    for path in sorted(roles_dir.glob("*.yaml")):
        raw = read_yaml(path)
        role_id = str(raw.get("id") or path.stem)
        roles[role_id] = raw
    if not roles:
        raise ConfigError(f"角色配置目录为空: {roles_dir}")
    return roles


def _apply_skips(flow_raw: Dict[str, Any]) -> Dict[str, Any]:
    """处理 profile 中 `skip: true` 的节点：删除节点并把指向它的流转改接到其后继。"""
    nodes = dict(flow_raw.get("nodes") or {})
    skipped = {node_id for node_id, node in nodes.items() if (node or {}).get("skip")}
    if not skipped:
        return flow_raw
    for node_id in skipped:
        nodes.pop(node_id, None)
    for node_id, node in nodes.items():
        transitions = dict((node or {}).get("transitions") or {})
        for branch, target in list(transitions.items()):
            if target in skipped:
                replacement = None
                for candidate_branch in ("pass", "success", "default", "fail"):
                    candidate = (flow_raw["nodes"][target] or {}).get("transitions", {}).get(candidate_branch)
                    if candidate and candidate not in skipped:
                        replacement = candidate
                        break
                if not replacement:
                    raise ConfigError(
                        f"Profile 裁剪节点 {target} 后无法为 {node_id}.{branch} 找到后继节点"
                    )
                transitions[branch] = replacement
        node["transitions"] = transitions
    flow_raw = dict(flow_raw)
    flow_raw["nodes"] = nodes
    return flow_raw


def load_config(
    config_dir: str | Path = "config",
    profile: Optional[str] = None,
    workspace: Optional[str | Path] = None,
) -> SystemConfig:
    """加载并校验配置。"""
    config_dir = Path(config_dir).resolve()
    if not config_dir.exists():
        raise ConfigError(f"配置目录不存在: {config_dir}")
    project_root = config_dir.parent
    source_files: List[Path] = []

    system_raw = read_yaml(config_dir / "system.yaml")
    source_files.append(config_dir / "system.yaml")
    files = system_raw.get("files") or {}

    flow_path = config_dir / str(files.get("flow", "flow.yaml"))
    collab_path = config_dir / str(files.get("collaboration", "collaboration.yaml"))
    learning_path = config_dir / str(files.get("learning", "learning.yaml"))
    # 兼容“带顶层包裹键”与“直接就是分节内容”两种写法
    flow_data = read_yaml(flow_path)
    collab_data = read_yaml(collab_path)
    learning_data = read_yaml(learning_path)
    source_files += [flow_path, collab_path, learning_path]

    bundle: Dict[str, Any] = {
        "system": system_raw.get("system") or {},
        "llm": system_raw.get("llm") or {},
        "runtime": system_raw.get("runtime") or {},
        "files": files,
        "flow": flow_data.get("flow", flow_data) or {},
        "collaboration": collab_data.get("collaboration", collab_data) or {},
        "learning": learning_data.get("learning", learning_data) or {},
        "roles": _load_roles(config_dir / str(files.get("roles_dir", "roles"))),
    }

    # profile 合并（Profile 使用与 system.yaml 相同的分节键：system/llm/runtime/flow/...）
    profile_name = profile or (bundle["system"].get("default_profile") if isinstance(bundle["system"], dict) else None)
    if profile_name:
        profiles_dir = config_dir / str(files.get("profiles_dir", "profiles"))
        profile_path = profiles_dir / f"{profile_name}.yaml"
        source_files.append(profile_path)
        profile_raw = read_yaml(profile_path)
        overlay = {
            key: profile_raw.get(key)
            for key in ("system", "llm", "runtime", "flow", "collaboration", "learning", "roles")
            if profile_raw.get(key) is not None
        }
        if overlay:
            bundle = deep_merge(bundle, overlay)

    # 裁剪被 skip 的节点
    bundle["flow"] = _apply_skips(bundle["flow"])
    # 回退策略中引用被裁剪节点的条目一并移除（避免校验失败）
    active_nodes = set((bundle["flow"].get("nodes") or {}).keys())
    collaboration_raw = dict(bundle["collaboration"] or {})
    collaboration_raw["rollback"] = {
        node_id: policy for node_id, policy in (collaboration_raw.get("rollback") or {}).items()
        if node_id in active_nodes
    }
    bundle["collaboration"] = collaboration_raw

    # 构造强类型对象
    flow_raw = bundle["flow"] or {}
    nodes: Dict[str, NodeConfig] = {}
    for node_id, node_raw in (flow_raw.get("nodes") or {}).items():
        nodes[str(node_id)] = NodeConfig.from_raw(str(node_id), node_raw or {})
    flow = FlowConfig(
        entry=str(flow_raw.get("entry", "")),
        max_steps=int(flow_raw.get("max_steps", 40)),
        terminal=[str(item) for item in (flow_raw.get("terminal") or ["end", "halt"])],
        nodes=nodes,
    )
    collaboration = CollaborationConfig.from_raw(bundle["collaboration"] or {})
    learning = LearningConfig.from_raw(bundle["learning"] or {})
    roles = {
        role_id: RoleConfig.from_raw(role_id, role_raw or {})
        for role_id, role_raw in (bundle["roles"] or {}).items()
    }
    config = SystemConfig(
        config_dir=config_dir,
        project_root=project_root,
        profile=profile_name,
        system=dict(bundle["system"] or {}),
        llm=dict(bundle["llm"] or {}),
        runtime=dict(bundle["runtime"] or {}),
        flow=flow,
        collaboration=collaboration,
        learning=learning,
        roles=roles,
        raw=bundle,
        source_files=source_files,
        workspace_override=Path(workspace).resolve() if workspace else None,
    )
    _validate(config)
    return config


# --------------------------------------------------------------------------- 校验
def _validate(config: SystemConfig) -> None:
    problems: List[str] = []
    flow = config.flow

    if not flow.entry:
        problems.append("flow.entry 未配置")
    elif flow.entry not in flow.nodes:
        problems.append(f"flow.entry 指向不存在的节点: {flow.entry}")

    for node_id, node in flow.nodes.items():
        if node.role and node.role not in config.roles:
            problems.append(f"节点 {node_id} 引用了未定义角色: {node.role}")
        if not node.transitions:
            problems.append(f"节点 {node_id} 缺少 transitions 配置")
        for branch in ("pass", "fail"):
            target = node.transitions.get(branch)
            if target and target not in flow.nodes and target not in flow.terminal:
                problems.append(f"节点 {node_id}.{branch} 指向未知节点: {target}")
        if node.kind == "task" and not node.outputs:
            problems.append(f"任务节点 {node_id} 必须声明 outputs")
        if node.kind == "gate" and not node.criteria:
            problems.append(f"门禁节点 {node_id} 必须声明 gate.criteria")
        if flow.terminal and node.transitions.get("fail") is None:
            problems.append(f"节点 {node_id} 缺少 fail 分支（建议显式配置为 halt）")

    for criterion in [c for node in flow.nodes.values() for c in node.criteria]:
        problem = check_config_expressions([(f"节点门禁[{criterion.id}]", criterion.expr)])
        problems.extend(problem)

    guards = config.collaboration.guards
    if guards.get("forbid_self_loop", True):
        for node_id, node in flow.nodes.items():
            for branch, target in node.transitions.items():
                if target == node_id:
                    problems.append(f"节点 {node_id}.{branch} 自循环被禁止（请通过 rollback 配置声明回退）")

    for node_id, policy in config.collaboration.rollback.items():
        if node_id not in flow.nodes:
            problems.append(f"collaboration.rollback 引用了不存在的节点: {node_id}")
        target = policy.get("target")
        if target and target not in flow.nodes:
            problems.append(f"collaboration.rollback.{node_id}.target 指向未知节点: {target}")

    for role_id, node_list in config.collaboration.perception.items():
        if role_id not in config.roles:
            problems.append(f"collaboration.perception 引用了未定义角色: {role_id}")
    for event_type, targets in config.collaboration.broadcast.items():
        for target in targets:
            if target not in config.roles and target != "all":
                problems.append(f"collaboration.broadcast.{event_type} 引用了未定义角色: {target}")

    for tier_key in list(config.learning.tiers):
        if tier_key not in {"L1", "L2", "L3"}:
            problems.append(f"learning.tiers 只支持 L1/L2/L3，发现: {tier_key}")

    if config.llm.get("provider") not in {"offline", "openai_compatible"}:
        problems.append(f"llm.provider 非法: {config.llm.get('provider')}（offline | openai_compatible）")

    if problems:
        raise ConfigError("配置校验失败:\n  - " + "\n  - ".join(problems))


def load_requirement(requirement: str, *, as_text: bool = False, project_root: Optional[Path] = None,
                     encoding: str = "utf-8") -> Tuple[str, str]:
    """读取需求：`--text` 直接当作文字需求，否则按文件路径读取（支持相对项目根）。"""
    if as_text:
        return "inline-requirement", str(requirement)
    candidate = Path(requirement)
    if not candidate.is_absolute() and project_root is not None:
        candidate = project_root / candidate
    if not candidate.exists():
        raise ConfigError(f"需求输入既不是存在的文件，也不是 --text 文字需求: {requirement}")
    return candidate.stem, candidate.read_text(encoding=encoding)
