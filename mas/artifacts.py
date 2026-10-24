"""产物发布策略：结构校验（schema）+ 角色 ACL（门禁加固的最小实现）。

解决的问题（docs/39 续篇）：``require_declared_artifacts`` 此前只写在
``config/collaboration.yaml`` 里，运行时从未校验——任何 Agent 都能发布任意
产物名，测试方也能伪造 ``acceptance_gate``，报告方可以把失败改写成通过。

这里把「谁能写什么」收敛成两道确定性校验，在 ``Blackboard.publish`` 入口执行：

1. **ACL**：允许的发布者由配置推导（角色 ``outputs`` + 各节点 ``outputs``），
   未声明的产物名直接拒绝；``LEADER_ONLY_ARTIFACTS``（门禁/输入事实等框架
   产物）只允许 Leader 发布，即使流程节点的 outputs 里出现也不放行；
2. **schema**：校验框架与门禁依赖的顶层字段及类型，挡住「少字段/类型错」的
   产物。只约束确定性字段，不限制模型自由发挥的其余键。

校验失败抛 ``ConfigError``：``BaseAgent.act`` 会把它转成节点失败，因此不会
有「发布成功但内容非法」的中间态。单元测试直接构造 ``Blackboard`` 时不挂
策略（保持既有 fixture 行为不变），只有 Leader 创建的黑板才启用。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable, Mapping, Optional, Set

from .errors import ConfigError

__all__ = [
    "ArtifactPolicy",
    "ARTIFACT_SCHEMAS",
    "LEADER_ONLY_ARTIFACTS",
    "gate_config_hash",
]

# 框架自身产出的产物：任何角色（包括被流程节点 outputs 声明过的评审方）
# 都不得发布，避免用流程配置绕过「门禁只能由 Leader 裁决」。
LEADER_ONLY_ARTIFACTS = frozenset({
    "requirement_source",
    "env_probe",
    "project_docs",
    "acceptance_gate",
    "hidden_test_report",
})

# 顶层字段 -> 期望类型。bool 必须先于 int 判断（Python 里 bool 是 int 子类）。
ARTIFACT_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "requirement_source": {"text": str},
    "env_probe": {"javascript_ready": bool},
    "requirement_spec": {"function_points": list, "acceptance_criteria": list},
    "requirement_contract": {"contract_hash": str, "signature": str, "clauses": list},
    "hidden_suite": {"manifest_digest": str, "test_ids": list},
    "solution_design": {"evaluation": dict},
    "code_manifest": {"files": list},
    "implementation_summary": {"files": list},
    "test_report": {"summary": dict, "coverage": dict},
    "bug_reports": {"open": int},
    "ui_evidence": {"asserts": list},
    "review_report": {"verdict": str},
    "design_review": {"verdict": str},
    "acceptance_report": {"run_id": str, "verdict": str, "gate_passed": bool},
    "evidence_index": {"count": int, "items": list},
    "fix_summary": {"items": list},
    "fix_test_report": {"results": list},
    "acceptance_gate": {
        "run_id": str, "passed": bool, "profile": str, "gate_config_hash": str,
    },
}

_TYPE_NAMES = {str: "string", int: "integer", bool: "boolean", list: "array", dict: "object"}


def _type_matches(value: Any, expected: Any) -> bool:
    if expected is bool:
        return isinstance(value, bool)
    if expected is int:
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, expected)


class ArtifactPolicy:
    """产物发布的 schema 与 ACL 策略；由配置确定性推导，不做网络/模型推断。"""

    def __init__(
        self,
        allow: Mapping[str, Iterable[str]],
        schemas: Optional[Mapping[str, Mapping[str, Any]]] = None,
    ) -> None:
        self._allow: Dict[str, Set[str]] = {
            str(name): {str(producer) for producer in producers}
            for name, producers in allow.items()
        }
        self._schemas: Dict[str, Dict[str, Any]] = {
            str(name): dict(fields) for name, fields in (schemas or ARTIFACT_SCHEMAS).items()
        }

    @classmethod
    def from_config(cls, config: Any) -> "ArtifactPolicy":
        """从角色/流程声明推导 ACL：角色 outputs ∪ 节点 outputs（按节点 role 归属）。"""
        allow: Dict[str, Set[str]] = {}
        roles = getattr(config, "roles", None) or {}
        for role_id, role in roles.items():
            for name in getattr(role, "outputs", None) or []:
                allow.setdefault(str(name), set()).add(str(role_id))
        flow = getattr(config, "flow", None)
        for node in (getattr(flow, "nodes", None) or {}).values():
            role_id = getattr(node, "role", None)
            if not role_id:
                continue
            for name in getattr(node, "outputs", None) or []:
                allow.setdefault(str(name), set()).add(str(role_id))
        for name in LEADER_ONLY_ARTIFACTS:
            allow[name] = {"leader"}
        return cls(allow)

    # ---------------------------------------------------------------- 校验
    def check(self, name: str, data: Any, producer: str) -> None:
        """发布前校验 ACL 与 schema；不通过抛 ``ConfigError``（节点随即失败）。"""
        name, producer = str(name), str(producer)
        if name in LEADER_ONLY_ARTIFACTS and producer != "leader":
            raise ConfigError(
                f"产物 {name} 属于框架裁决/事实产物，只允许 leader 发布（拒绝 {producer}）"
            )
        if producer != "leader" and name not in self._allow:
            raise ConfigError(f"产物 {name} 未在任何角色/节点 outputs 中声明，拒绝发布")
        if producer != "leader" and producer not in self._allow.get(name, set()):
            declared = "、".join(sorted(self._allow.get(name) or [])) or "无"
            raise ConfigError(
                f"角色 {producer} 无权发布产物 {name}（声明方：{declared}）"
            )
        self._check_schema(name, data)

    def _check_schema(self, name: str, data: Any) -> None:
        schema = self._schemas.get(name)
        if not schema:
            return
        if not isinstance(data, dict):
            raise ConfigError(f"产物 {name} 必须是对象，实际 {type(data).__name__}")
        problems = []
        for key, expected in schema.items():
            if key not in data:
                problems.append(f"缺字段 {key}")
                continue
            value = data[key]
            if not _type_matches(value, expected):
                problems.append(
                    f"字段 {key} 应为 {_TYPE_NAMES.get(expected, getattr(expected, '__name__', '?'))}，"
                    f"实际 {type(value).__name__}"
                )
        if problems:
            raise ConfigError(f"产物 {name} 结构校验失败：" + "；".join(problems))


def gate_config_hash(node: Any, config: Any = None) -> str:
    """门禁配置指纹：节点判定项 + profile + 协作守卫（确定性、可复算）。

    写入裁决记录后用来说明「这次通过是在哪套门禁规则下产生的」。release 模式下
    lean profile 已被拒绝，因此指纹同时覆盖 profile，规则被裁剪可被审计。
    """
    guards = dict(getattr(getattr(config, "collaboration", None), "guards", None) or {})
    criteria = [
        {
            "id": str(getattr(item, "id", "")),
            "expr": str(getattr(item, "expr", "")),
            "weight": float(getattr(item, "weight", 1.0) or 1.0),
        }
        for item in (getattr(node, "criteria", None) or [])
    ]
    payload = {
        "node": str(getattr(node, "id", "")),
        "profile": str(getattr(config, "profile", "") or "base"),
        "criteria": criteria,
        "guards": {key: guards[key] for key in sorted(guards)},
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
