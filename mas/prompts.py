"""Prompt 组装与分层经验注入。

每次 Agent 调用的 Prompt 都由五段构成，全部落盘作为证据：
    1) 角色系统提示（来自 roles/*.yaml：人设/职责/准则/质量标准）
    2) 任务指令（节点 + 轮次 + Leader 指令）
    3) 感知段（黑板上游产物 + 通讯摘要：感知其他 Agent 已完成的工作）
    4) 经验注入段（L3/L2/L1 分层，含命中记录与 token 预算）
    5) 交付契约与约束（输出字段、禁止事项）
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import NodeConfig, RoleConfig, SystemConfig
from .experience import RetrievalResult

__all__ = ["PromptBundle", "PromptBuilder"]


@dataclass
class PromptBundle:
    """一次完整的 Prompt（含结构化上下文，供 Provider 使用）。"""

    role: str
    node: str
    task_kind: str
    iteration: int
    system: str
    user: str
    context: Dict[str, Any] = field(default_factory=dict)
    experiences: List[Dict[str, Any]] = field(default_factory=list)
    skills: List[Dict[str, Any]] = field(default_factory=list)
    token_estimate: int = 0
    tier_counts: Dict[str, int] = field(default_factory=dict)
    path: Optional[Path] = None

    def full_text(self) -> str:
        return f"<<<SYSTEM>>>\n{self.system}\n<<<USER>>>\n{self.user}"

    def summary(self) -> str:
        return (
            f"role={self.role} node={self.node} iteration={self.iteration} "
            f"tokens≈{self.token_estimate} experience_injected={len(self.experiences)} tiers={self.tier_counts}"
            + (f" skills={[item['id'] for item in self.skills]}" if self.skills else "")
        )


class PromptBuilder:
    """按角色与节点组装 Prompt，并负责落盘。"""

    def __init__(self, config: SystemConfig, run_dir: Path, *, enabled: bool = True) -> None:
        self.config = config
        self.run_dir = Path(run_dir)
        self.enabled = enabled
        self.dir = self.run_dir / "prompts"
        if self.enabled:
            self.dir.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- 组装
    def build(
        self,
        *,
        role: RoleConfig,
        node: NodeConfig,
        task_kind: str,
        iteration: int,
        directive: str,
        artifacts_brief: str,
        bus_digest: str,
        perception: List[str],
        missing: List[str],
        experience: RetrievalResult,
        contract: Dict[str, Any],
        skills_text: str = "",
        skill_matches: Optional[List[Dict[str, Any]]] = None,
        extra: Optional[Dict[str, Any]] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> PromptBundle:
        system = self._system_prompt(role)
        user = self._user_prompt(
            role=role,
            node=node,
            task_kind=task_kind,
            iteration=iteration,
            directive=directive,
            artifacts_brief=artifacts_brief,
            bus_digest=bus_digest,
            perception=perception,
            missing=missing,
            experience_text=experience.text,
            skills_text=skills_text,
            contract=contract,
            images=list((context or {}).get("images") or []),
        )
        bundle = PromptBundle(
            role=role.id,
            node=node.id,
            task_kind=task_kind,
            iteration=iteration,
            system=system,
            user=user,
            context=context or {},
            experiences=[
                {
                    "id": entry.id,
                    "tier": entry.tier,
                    "scope": entry.scope,
                    "lesson": entry.lesson,
                    "action": entry.action,
                    "action_key": entry.action_key,
                    "confidence": entry.confidence,
                    "hits": entry.hits,
                }
                for entry in experience.entries
            ],
            tier_counts={tier: len(items) for tier, items in experience.per_tier.items()},
            skills=list(skill_matches or []),
            token_estimate=_estimate_tokens(system) + _estimate_tokens(user),
        )
        if extra:
            bundle.context.setdefault("extra", {}).update(extra)
        if self.enabled:
            bundle.path = self._save(bundle)
        return bundle

    # ---------------------------------------------------------------- 段落
    def _system_prompt(self, role: RoleConfig) -> str:
        lines = [
            f"# 角色：{role.name}（{role.id}）",
            role.mission,
            "",
            "## 人设",
            role.persona or "你是一名专业的协作型 Agent。",
            "",
            "## 职责",
        ]
        lines += [f"- {item}" for item in role.responsibilities] or ["- （未配置）"]
        if role.guidelines:
            lines += ["", "## 工作准则"] + [f"- {item}" for item in role.guidelines]
        if role.quality_bar:
            lines += ["", "## 质量标准"] + [f"- {item}" for item in role.quality_bar]
        lines += [
            "",
            "## 协作规则",
            "- 你不与其他 Agent 直接对话；通过共享黑板读取上游成果、通过消息总线发布状态。",
            "- 只对自己职责内的结论负责，不臆造未经验证的事实。",
            f"- 输出格式：{role.output_format}。",
        ]
        return "\n".join(lines)

    def _user_prompt(
        self,
        *,
        role: RoleConfig,
        node: NodeConfig,
        task_kind: str,
        iteration: int,
        directive: str,
        artifacts_brief: str,
        bus_digest: str,
        perception: List[str],
        missing: List[str],
        experience_text: str,
        skills_text: str = "",
        contract: Dict[str, Any],
        images: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        lines: List[str] = [
            "# 任务",
            f"- 流程节点：{node.id}（{node.title}）",
            f"- 执行角色：{role.name}",
            f"- 任务类型：{task_kind}",
            f"- 当前轮次：第 {iteration} 轮",
        ]
        if directive:
            lines.append(f"- Leader 指令：{directive}")
        lines += [
            "",
            "# 感知：上游已完成的工作",
            f"- 需感知的产物：{', '.join(perception) if perception else '(无)'}",
        ]
        if missing:
            lines.append(f"- 缺失产物（请勿臆造，按缺失处理）：{', '.join(missing)}")
        lines += ["", artifacts_brief, ""]
        lines += ["## 协作消息摘要", bus_digest, ""]
        if images:
            names = "、".join(str(item.get("name") or item.get("path") or "") for item in images)
            # 措辞必须与 llm.vision.enabled 对齐（唯一事实来源）：开启时图片以 image_url
            # parts 真正进入请求体；关闭时图片只存档到运行目录、内容从不发送——若仍告知
            # 模型"图片随请求一并提供"，模型可能假装看过图并臆造功能点（实测该类需求
            # 的文字常是"识别图片中内容……"，看不到图时必须走澄清而不是编造）。
            vision_enabled = bool(dict(self.config.llm.get("vision") or {}).get("enabled", False))
            if vision_enabled:
                lines += [
                    "# 附带图片",
                    f"- 本次需求附带 {len(images)} 张图片（{names}），图片随请求一并提供（多模态输入）。",
                    "- 图片仅作需求理解依据，不得据此臆造文本中未声明的功能点。",
                    "",
                ]
            else:
                lines += [
                    "# 附带图片（已存档，但不在本次请求中）",
                    f"- 本次需求附带 {len(images)} 张图片（{names}），已存档到运行目录（留痕可审查）。",
                    "- 当前未开启多模态输入（llm.vision.enabled=false），图片内容未随请求发送，你无法看到图片。",
                    "- 不得声称看过图片或据此臆造功能点；若需求理解依赖图片内容，"
                    "请在 open_questions 中提出澄清问题。",
                    "",
                ]
        if experience_text:
            lines += ["# 经验注入（按层级注入历史运行沉淀的经验）", experience_text, ""]
        if skills_text:
            lines += [skills_text, ""]
        lines += [
            "# 交付契约",
            "必须输出一个 JSON 对象，字段如下：",
            json.dumps(contract, ensure_ascii=False, indent=2),
            "",
            "# 约束",
            "- 结论必须可被证据校验（文件路径、命令输出、用例结果）。",
            "- 不得输出 JSON 之外的解释性文字。",
        ]
        return "\n".join(lines)

    # ---------------------------------------------------------------- 落盘
    def _save(self, bundle: PromptBundle) -> Path:
        stamp = datetime.now().astimezone().strftime("%H%M%S")
        path = self.dir / f"{bundle.node}#{bundle.iteration}-{bundle.role}-{stamp}.md"
        header = [
            f"<!-- run={self.run_dir.name} role={bundle.role} node={bundle.node} "
            f"task_kind={bundle.task_kind} iteration={bundle.iteration} "
            f"tokens≈{bundle.token_estimate} injected={[e['id'] for e in bundle.experiences]} "
            f"skills={[item['id'] for item in bundle.skills]} -->",
            "",
        ]
        path.write_text("\n".join(header) + bundle.full_text(), encoding="utf-8")
        return path


def _estimate_tokens(text: str) -> int:
    import re

    if not text:
        return 0
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    other = max(0, len(text) - cjk)
    return int(cjk + other / 3.2) + 1
