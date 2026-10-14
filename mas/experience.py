"""经验库与自学习闭环。

闭环链路：
    事件日志(events.jsonl) --extract--> 分级经验库(L1/L2/L3) --retrieve--> Prompt 分层注入
          ^                                                                       |
          |__________________________ reinforce(positive/negative/neutral) _______|

反馈语义（由 Leader 调用，见 mas/leader.py）：
- 回退归因降权：注入的经验未能预防其声称能防的失败（trigger 与失败事件匹配）→ negative，
  置信度下调、hits_failed += 1；
- 运行终态奖励：验收通过 → positive，置信度上调、hits_success += 1；未通过但无法归因 →
  neutral，仅留痕；
- 晋级依据：有效成功次数 hits × success_rate（而非纯 hits），防止「反复注入但每次都导致
  返工」的经验晋升到 L3。

- L1：本次运行内即时经验（当轮缺陷、驳回原因），只注入同一 run 的后续节点
- L2：项目/领域经验（同一需求域内可复用），下次运行同域需求时注入
- L3：全局通用经验（跨项目通用），有效复用次数达到阈值后自动晋级
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import LearningConfig
from .events import EventLog
from .filelock import file_lock

__all__ = ["Experience", "ExperienceLibrary", "RetrievalResult"]

RULE_LABELS = {
    "missing_module_docstring": "模块文档字符串缺失",
    "missing_function_docstring": "函数文档字符串缺失",
    "missing_type_hints": "类型注解缺失",
    "bare_except": "裸 except 吞异常",
    "eval_usage": "危险调用 eval",
    "function_too_long": "函数过长",
    "mutable_default": "可变默认参数",
    "line_too_long": "单行超长",
    "todo_comment": "TODO 残留",
    "unused_import": "未使用导入",
    "syntax_error": "语法错误",
    "unimplemented_function_point": "需求功能点未实现",
}

CATEGORY_LABELS = {
    "functional": "功能",
    "exception": "异常",
    "boundary": "边界",
}

RULE_ACTIONS = {
    "missing_module_docstring": "提交审查前为每个模块补充模块级文档字符串",
    "missing_function_docstring": "提交审查前为每个公开函数补充文档字符串（含参数与返回语义）",
    "missing_type_hints": "提交审查前为全部函数补全参数与返回值类型注解",
    "bare_except": "禁止裸 except，必须捕获具体异常类型并保留上下文",
    "eval_usage": "禁止使用 eval/exec 处理输入数据，改用显式解析",
    "function_too_long": "拆分超长函数，单一函数不超过配置阈值行数",
    "mutable_default": "禁止使用可变对象作为默认参数，改用 None + 内部初始化",
    "line_too_long": "控制单行长度，必要时拆分表达式",
    "todo_comment": "交付前清理 TODO 注释",
    "unused_import": "清理未使用的导入",
    "syntax_error": "提交前先做语法与导入自检",
    "unimplemented_function_point": "实现前先建立需求功能点→实现函数的映射，避免漏实现",
}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _key_of(*parts: str) -> str:
    return hashlib.sha1("||".join(parts).encode("utf-8")).hexdigest()[:12]


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).lower()


def _tokenize_for_match(text: str) -> set:
    """匹配用切词：英文/数字按词，中文按「单字 + 相邻二字(bigram)」。

    bigram 是零依赖条件下对中文最有效的「软匹配」手段：
    「运费计算」与「物流计价」虽无公共子串，但共享单字「计」「算」与
    bigram「计算」的变体，重叠系数即可给出非零相似度，缓解同义改写召回不到的问题。
    """
    normalized = _normalize(text)
    tokens: set = set()
    for word in re.findall(r"[a-z0-9_]+", normalized):
        tokens.add(word)
    for segment in re.findall(r"[\u4e00-\u9fff]+", normalized):
        tokens.update(segment)
        tokens.update(segment[index:index + 2] for index in range(len(segment) - 1))
    return tokens


def _expand_with_synonyms(tokens: set, synonyms: Dict[str, List[str]]) -> set:
    """同义词扩展：把命中词典的 token 追加其同义词 token（词典在 learning.yaml 配置）。"""
    if not synonyms:
        return tokens
    expanded = set(tokens)
    for token in tokens:
        for related in synonyms.get(token, []) or []:
            expanded.update(_tokenize_for_match(related))
    return expanded


def _estimate_tokens(text: str) -> int:
    """粗略 token 估算：中文按 1 字 1 token、英文按 4 字符 1 token 折半处理。"""
    if not text:
        return 0
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    other = max(0, len(text) - cjk)
    return int(cjk + other / 3.2) + 1


@dataclass
class Experience:
    """一条经验。"""

    id: str
    tier: str
    scope: str
    roles: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    trigger: str = ""
    lesson: str = ""
    action: str = ""
    action_key: str = ""
    dedupe: str = ""            # 去重细分维度：同一 action_key 下按此字段再分桶
    confidence: float = 0.5
    hits: int = 0
    hits_success: int = 0       # 被注入后所在运行顺利通过（positive 反馈）的次数
    hits_failed: int = 0        # 被注入后未能预防回退（negative 反馈）的次数
    evidence_count: int = 0
    source_run: str = ""
    source_refs: List[str] = field(default_factory=list)
    created_at: str = ""
    last_used_at: Optional[str] = None
    status: str = "active"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Experience":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        data = {k: v for k, v in raw.items() if k in known}
        data.setdefault("id", f"exp-{_key_of(raw.get('lesson', ''))}")
        return cls(**data)

    @property
    def key(self) -> str:
        # dedupe 参与去重键：同一 action_key（如 guard_FP-06__functional）下的不同症状/
        # 不同歧义不再被合并成一条不断膨胀的 lesson（实测：cron 的 misfire 歧义与
        # 电商的优惠券歧义被拼成一条，且 hits 累到 120、置信度 1.0，注入证据失真）。
        return _key_of(self.scope, self.action_key or _normalize(self.lesson), self.dedupe)

    @property
    def success_rate(self) -> float:
        """经验的有效率：positive 反馈 / 总反馈次数。

        无反馈数据（旧库条目或从未被注入）时取中性值 0.5：既不会因零证据快速晋升，
        也不会把历史经验永久卡在低层级。
        """
        total = self.hits_success + self.hits_failed
        if total <= 0:
            return 0.5
        return self.hits_success / total


@dataclass
class RetrievalResult:
    """检索结果（含注入文本与统计，便于审计“经验是否真的被注入”）。"""

    entries: List[Experience] = field(default_factory=list)
    text: str = ""
    tokens: int = 0
    per_tier: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    skipped: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def ids(self) -> List[str]:
        return [entry.id for entry in self.entries]

    @property
    def action_keys(self) -> List[str]:
        return [entry.action_key for entry in self.entries if entry.action_key]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ids": self.ids,
            "action_keys": self.action_keys,
            "tokens": self.tokens,
            "per_tier": self.per_tier,
            "skipped": self.skipped,
        }


class ExperienceLibrary:
    """经验库：持久化 + 检索 + 提炼 + 晋级 + 反馈。"""

    def __init__(self, workspace_dir: Path, config: LearningConfig, events: Optional[EventLog] = None,
                 run_id: str = "") -> None:
        self.workspace_dir = Path(workspace_dir)
        self.config = config
        self.events = events
        self.run_id = run_id
        self.path = self.workspace_dir / str(config.storage.get("experience", "knowledge/experience.jsonl"))
        self.metrics_path = self.workspace_dir / str(config.storage.get("metrics", "knowledge/metrics.jsonl"))
        self._entries: List[Experience] = []
        self._loaded = False
        self._lock_timeout_s = float((config.extraction or {}).get("lock_timeout_s", 10))
        # 同义词词典（learning.yaml synonyms 段）：键为词，值为同义词列表，检索时做查询扩展
        self._synonyms: Dict[str, List[str]] = {
            str(key): [str(item) for item in (value or [])]
            for key, value in (getattr(config, "synonyms", None) or {}).items()
        }

    def _locked(self):
        """经验库读-改-写锁：多进程（如前端并发起的多次运行）共享同一 experience.jsonl 时，
        保证 load → merge → save 全链路的原子性，避免后写覆盖先写丢经验。"""
        return file_lock(self.path, timeout_s=self._lock_timeout_s)

    # ---------------------------------------------------------------- 持久化
    def load(self) -> List[Experience]:
        self._entries = []
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        try:
                            self._entries.append(Experience.from_dict(json.loads(line)))
                        except (json.JSONDecodeError, TypeError):
                            continue
        self._loaded = True
        return self._entries

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as handle:
            for entry in self._entries:
                handle.write(json.dumps(entry.to_dict(), ensure_ascii=False, default=str) + "\n")

    @property
    def entries(self) -> List[Experience]:
        if not self._loaded:
            self.load()
        return self._entries

    # ---------------------------------------------------------------- 写入
    def add(self, entries: Iterable[Experience], *, source_run: str = "") -> List[Dict[str, Any]]:
        """加入经验：同源经验自动合并（置信度递增 + 证据计数）。

        整个 load→merge→save 在文件锁内完成，多进程共享经验库不会互相覆盖。
        """
        results: List[Dict[str, Any]] = []
        with self._locked():
            self.load() if not self._loaded else None
            step = float(self.config.extraction.get("confidence_step", 0.1))
            for entry in entries:
                entry.source_run = entry.source_run or source_run or self.run_id
                if not entry.created_at:
                    entry.created_at = _now()
                match = next((item for item in self._entries if item.key == entry.key), None)
                if match:
                    match.confidence = min(1.0, round(match.confidence + step, 3))
                    match.evidence_count += max(1, entry.evidence_count)
                    match.tags = sorted(set(match.tags) | set(entry.tags))
                    match.last_used_at = match.last_used_at
                    if entry.lesson and entry.lesson not in match.lesson:
                        match.lesson = f"{match.lesson}；{entry.lesson}"
                    results.append({"entry": match, "merged": True})
                    if self.events is not None:
                        self.events.emit(
                            "experience_reinforced",
                            agent="experience_library",
                            payload={
                                "experience_id": match.id,
                                "tier": match.tier,
                                "confidence": match.confidence,
                                "lesson": match.lesson,
                            },
                        )
                else:
                    entry.id = entry.id or f"exp-{_key_of(entry.key, _now())}"
                    entry.evidence_count = max(1, entry.evidence_count)
                    self._entries.append(entry)
                    results.append({"entry": entry, "merged": False})
                    if self.events is not None:
                        self.events.emit(
                            "experience_learned",
                            agent="experience_library",
                            payload={
                                "experience_id": entry.id,
                                "tier": entry.tier,
                                "scope": entry.scope,
                                "lesson": entry.lesson,
                                "action_key": entry.action_key,
                                "confidence": entry.confidence,
                                "trigger": entry.trigger,
                            },
                        )
            self.save()
        return results

    # ---------------------------------------------------------------- 检索
    def retrieve(
        self,
        *,
        role: str,
        tags: Optional[List[str]] = None,
        task_text: str = "",
        tier_keys: Optional[List[str]] = None,
        max_items: Optional[int] = None,
        current_run: str = "",
    ) -> RetrievalResult:
        """按分层策略检索经验，产出可直接注入 Prompt 的文本。"""
        self.load() if not self._loaded else None
        if not self.config.enabled or not self.config.injection.get("enabled", True):
            return RetrievalResult(text="", skipped=[{"reason": "learning disabled"}])

        tags = [str(tag) for tag in (tags or [])]
        wanted_tiers = tier_keys or list(self.config.tiers.keys())
        scoring = self.config.scoring or {}
        w_relevance = float(scoring.get("relevance", 0.45))
        w_confidence = float(scoring.get("confidence", 0.25))
        w_usage = float(scoring.get("usage", 0.15))
        w_recency = float(scoring.get("recency", 0.15))

        result = RetrievalResult()
        budget_total = int(self.config.injection.get("max_total_tokens", 1000))
        used_tokens = 0

        for tier_key in wanted_tiers:
            tier = self.config.tier(tier_key)
            if tier is None or not tier.enabled:
                continue
            candidates: List[Tuple[float, Experience, Dict[str, Any]]] = []
            for entry in self._entries:
                if entry.status != "active":
                    continue
                entry_tier = entry.tier or self._tier_of_scope(entry.scope)
                if entry_tier != tier_key:
                    continue
                if entry.confidence < tier.min_confidence:
                    result.skipped.append({"id": entry.id, "tier": tier_key, "reason": "置信度低于阈值"})
                    continue
                if entry.scope == "run" and entry.source_run and entry.source_run != (current_run or self.run_id):
                    result.skipped.append({"id": entry.id, "tier": tier_key, "reason": "非本次运行经验"})
                    continue
                score, detail = self._score(
                    entry, role, tags, task_text,
                    w_relevance=w_relevance, w_confidence=w_confidence, w_usage=w_usage, w_recency=w_recency,
                )
                candidates.append((score * tier.weight, entry, detail))
            candidates.sort(key=lambda item: item[0], reverse=True)
            limit = min(tier.max_items, max_items or tier.max_items)
            tier_lines: List[str] = []
            tier_tokens = 0
            for score, entry, detail in candidates:
                if len(tier_lines) >= limit:
                    result.skipped.append({"id": entry.id, "tier": tier_key, "reason": "超出层级条数上限"})
                    continue
                line = self._format_entry(entry, score)
                tokens = _estimate_tokens(line)
                if tier_tokens + tokens > tier.budget_tokens or used_tokens + tokens > budget_total:
                    result.skipped.append({"id": entry.id, "tier": tier_key, "reason": "超出 token 预算"})
                    continue
                tier_lines.append(line)
                tier_tokens += tokens
                used_tokens += tokens
                result.entries.append(entry)
                result.per_tier.setdefault(tier_key, []).append(
                    {"id": entry.id, "score": round(score, 3), "detail": detail, "lesson": entry.lesson}
                )
            if tier_lines:
                header = str(self.config.injection.get("header", "经验注入（{tier} {name}）")).format(
                    tier=tier_key, name=tier.name, count=len(tier_lines)
                )
                result.text += f"【{header}】\n" + "\n".join(tier_lines) + "\n\n"
        if result.text:
            result.text += str(self.config.injection.get("footer", ""))
        result.tokens = _estimate_tokens(result.text)
        if result.entries:
            with self._locked():
                for entry in result.entries:
                    entry.hits += 1
                    entry.last_used_at = _now()
                self.save()
        return result

    def _score(
        self,
        entry: Experience,
        role: str,
        tags: List[str],
        task_text: str,
        *,
        w_relevance: float,
        w_confidence: float,
        w_usage: float,
        w_recency: float,
    ) -> Tuple[float, Dict[str, Any]]:
        role_match = 1.0 if (not entry.roles or role in entry.roles or "all" in entry.roles) else 0.0
        tag_overlap = 0.0
        if tags:
            normalized_tags = {_normalize(tag) for tag in tags}
            entry_tags = {_normalize(tag) for tag in entry.tags}
            tag_overlap = len(normalized_tags & entry_tags) / max(1, len(normalized_tags))
        keyword_overlap = self._semantic_overlap(task_text, f"{entry.lesson}{entry.action}")
        relevance = 0.5 * role_match + 0.3 * tag_overlap + 0.2 * min(1.0, keyword_overlap * 3)
        usage = min(1.0, entry.hits / 5.0)
        recency = 0.5
        try:
            created = datetime.fromisoformat(entry.created_at)
            days = max(0.0, (datetime.now(timezone.utc) - created.astimezone(timezone.utc)).total_seconds() / 86400)
            recency = math.exp(-days / 30.0)
        except (ValueError, TypeError):
            pass
        score = (
            w_relevance * relevance
            + w_confidence * entry.confidence
            + w_usage * usage
            + w_recency * recency
        )
        detail = {
            "role_match": round(role_match, 2),
            "tag_overlap": round(tag_overlap, 2),
            "keyword_overlap": round(keyword_overlap, 3),
            "confidence": entry.confidence,
            "hits": entry.hits,
        }
        return score, detail

    def _semantic_overlap(self, task_text: str, entry_text: str) -> float:
        """语义软匹配（零依赖）：中文 bigram + 同义词扩展后的重叠系数。

        替代原来的「关键词子串精确包含」判断——同义改写（运费计算↔物流计价）、
        词序变化都不再导致召回失败。词典来自 learning.yaml 的 synonyms 段。
        """
        if not task_text or not entry_text:
            return 0.0
        query = _expand_with_synonyms(_tokenize_for_match(task_text), self._synonyms)
        haystack = _tokenize_for_match(entry_text)
        if not query or not haystack:
            return 0.0
        # 重叠系数：交集 / 较小集合大小（对长短文本不对称的场景比 Jaccard 稳健）
        return len(query & haystack) / max(1, min(len(query), len(haystack)))

    def _format_entry(self, entry: Experience, score: float) -> str:
        lines = [f"- (置信度 {entry.confidence:.2f} / 命中 {entry.hits} / 触发 {entry.trigger or '-'}) {entry.lesson}"]
        if entry.action:
            lines.append(f"  动作: {entry.action}")
        if entry.action_key:
            lines.append(f"  标识: {entry.action_key}")
        return "\n".join(lines)

    @staticmethod
    def _tier_of_scope(scope: str) -> str:
        return {"run": "L1", "project": "L2", "global": "L3"}.get(scope, "L3")

    # ---------------------------------------------------------------- 反馈
    def reinforce(
        self,
        ids: Iterable[str],
        *,
        outcome: str = "positive",
        reward: Optional[float] = None,
        evidence: str = "",
    ) -> int:
        """经验被实际消费后的置信度反馈（自学习闭环保真）。

        由调度器在两种时点调用（见 Leader）：
        - 回退归因：注入的经验未能预防其声称能防的失败 → outcome="negative"，置信度下调；
        - 运行终态：验收通过 → outcome="positive"，置信度上调；未通过但无法归因 →
          outcome="neutral"，仅留痕不改动。

        outcome:
          positive — confidence += feedback.reward_on_effect（默认 0.05），hits_success += 1
          negative — confidence -= feedback.penalize_on_rollback（默认 0.1，下限 0.05），
                     hits_failed += 1，并发出 experience_demoted 事件留痕
          neutral  — 不改置信度与计数（无法归因于经验，不奖不罚）

        reward 参数保留直接指定奖惩值的能力（正数上调、负数下调），默认按 outcome 取配置值。
        返回被更新的经验条数。
        """
        wanted = {str(item) for item in ([ids] if isinstance(ids, str) else ids)}
        if not wanted:
            return 0
        outcome = str(outcome or "neutral").lower()
        if outcome not in ("positive", "negative", "neutral"):
            outcome = "neutral"
        feedback = self.config.feedback or {}
        if reward is None:
            if outcome == "positive":
                reward = float(feedback.get("reward_on_effect", 0.05))
            elif outcome == "negative":
                reward = -abs(float(feedback.get("penalize_on_rollback", 0.1)))
            else:
                reward = 0.0
        reward = float(reward)
        with self._locked():
            # 锁内强制重载：同一工作区被多个运行并发反馈时，避免后写者用陈旧内存
            # 覆盖先写者的计数（与 add() 的锁内强制 load 模式一致）。
            self.load()
            touched = 0
            for entry in self._entries:
                if entry.id not in wanted:
                    continue
                touched += 1
                if outcome == "neutral":
                    continue      # 仅留痕：不改置信度、计数与时间戳
                entry.confidence = min(1.0, max(0.05, round(entry.confidence + reward, 3)))
                if outcome == "positive":
                    entry.hits_success += 1
                else:
                    entry.hits_failed += 1
                entry.last_used_at = _now()
                if self.events is not None:
                    self.events.emit(
                        "experience_reinforced" if outcome == "positive" else "experience_demoted",
                        agent="experience_library",
                        payload={
                            "experience_id": entry.id,
                            "tier": entry.tier,
                            "outcome": outcome,
                            "reward": reward,
                            "confidence": entry.confidence,
                            "hits_success": entry.hits_success,
                            "hits_failed": entry.hits_failed,
                            "evidence": str(evidence)[:200],
                            "lesson": entry.lesson,
                        },
                    )
            if touched and outcome != "neutral":
                self.save()
        return touched

    # ---------------------------------------------------------------- 晋级
    def promote(self) -> List[Dict[str, Any]]:
        """L1→L2→L3 晋级：被反复**有效**复用的经验提升层级与复用范围。

        晋升依据是「有效成功次数 = hits × success_rate」而非纯 hits：
        纯 hits 会把「反复注入但每次都导致返工」的经验推上 L3（使用次数多 ≠ 效果好，
        是自学习的反模式）。success_rate 来自 reinforce() 的 positive/negative 反馈，
        无反馈数据时取中性值 0.5（见 Experience.success_rate）。
        """
        with self._locked():
            self.load() if not self._loaded else None
            thresholds = {
                "L1": int(self.config.promotion.get("L1_to_L2", 1)),
                "L2": int(self.config.promotion.get("L2_to_L3", 2)),
            }
            promoted: List[Dict[str, Any]] = []
            for entry in self._entries:
                current_tier = entry.tier or self._tier_of_scope(entry.scope)
                need = thresholds.get(current_tier)
                if need is None or entry.hits < need:
                    continue
                effective = entry.hits * entry.success_rate
                if effective < need:
                    continue
                if current_tier == "L1":
                    entry.tier, entry.scope = "L2", "project"
                    entry.confidence = min(1.0, round(entry.confidence + 0.05, 3))
                elif current_tier == "L2":
                    entry.tier, entry.scope = "L3", "global"
                    entry.confidence = min(1.0, round(entry.confidence + 0.05, 3))
                promoted.append({"id": entry.id, "tier": entry.tier, "scope": entry.scope, "lesson": entry.lesson, "hits": entry.hits})
            if promoted:
                self.save()
                if self.events is not None:
                    for item in promoted:
                        self.events.emit("experience_promoted", agent="experience_library", payload=item)
        return promoted

    # ---------------------------------------------------------------- 提炼
    def extract_from_events(self, facts: Dict[str, Any]) -> List[Experience]:
        """规则化经验提炼器：从事件日志事实中抽取可复用经验（L1 即时经验）。"""
        entries: List[Experience] = []
        run_id = facts.get("run_id", self.run_id)
        domain_tags = list(facts.get("tags", []))

        # 规则 1：审查拦截 -> 提交前自检经验
        for item in facts.get("review_blocking", []):
            rule = str(item.get("rule", ""))
            count = int(item.get("count", 1))
            if not rule:
                continue
            label = RULE_LABELS.get(rule, rule)
            action = RULE_ACTIONS.get(rule, f"修复审查规则 {rule} 对应问题")
            entries.append(Experience(
                id="",
                tier="L1",
                scope="run",
                roles=["developer"],
                tags=sorted(set(domain_tags + ["代码审查", rule])),
                trigger="review_rejected",
                lesson=f"代码审查拦截「{label}」共 {count} 次（文件示例：{item.get('sample_file', '-')}）",
                action=action,
                action_key=f"avoid_{rule}",
                confidence=min(1.0, 0.7 + 0.05 * max(0, count - 1)),
                evidence_count=count,
                source_refs=list(item.get("refs", [])),
            ))

        # 规则 2：缺陷模式 -> 实现守护经验
        for item in facts.get("bug_patterns", []):
            fp_id = str(item.get("fp_id", ""))
            category = str(item.get("category", "functional"))
            category_label = CATEGORY_LABELS.get(category, category)
            symptom = str(item.get("symptom", ""))
            strategy = str(item.get("fix_strategy", ""))
            # action_key 用算子键（op）而非功能点 id：离线 Provider 的缺陷守护按
            # guard_<op>__<category> 匹配（见 providers/offline.py guarded_ops），
            # 文档（README / docs 01/02/09）声明的也是 guard_calc_shipping__boundary
            # 这一形式。缺 op 时回退到 fp_id，保证键非空。
            guard_key = str(item.get("op", "")) or fp_id
            entries.append(Experience(
                id="",
                tier="L1",
                scope="run",
                roles=["developer", "tester"],
                tags=sorted(set(domain_tags + ["缺陷修复", category, fp_id])),
                trigger="bug_found",
                lesson=(f"功能点 {fp_id} 出现{category_label}缺陷：{symptom}"
                        + (f"；有效修复策略：{strategy}" if strategy else "")),
                action=f"实现 {fp_id} 时显式覆盖{category_label}输入，并保留回归用例",
                action_key=f"guard_{guard_key}__{category}",
                confidence=0.8,
                evidence_count=int(item.get("count", 1)),
                source_refs=list(item.get("refs", [])),
            ))
            if category == "boundary":
                entries.append(Experience(
                    id="",
                    tier="L1",
                    scope="run",
                    roles=["developer", "tester"],
                    tags=sorted(set(domain_tags + ["边界条件"])),
                    trigger="bug_found",
                    lesson=f"{fp_id} 的缺陷全部落在阈值边界（{symptom}），阈值比较必须显式处理相等情形",
                    action="阈值边界按 t / t-ε / t+ε 三态设计与实现，禁止用截断替代向上取整",
                    action_key="boundary_tri_state",
                    confidence=0.85,
                    evidence_count=int(item.get("count", 1)),
                    source_refs=list(item.get("refs", [])),
                ))

        # 规则 3：验收未通过 -> 前置校验经验
        for item in facts.get("acceptance_failed", []):
            criterion = str(item.get("id", ""))
            reason = str(item.get("reason", ""))
            entries.append(Experience(
                id="",
                tier="L1",
                scope="run",
                roles=["all"],
                tags=sorted(set(domain_tags + ["验收门禁", criterion])),
                trigger="acceptance_rejected",
                lesson=f"验收门禁项「{criterion}」未满足：{reason}",
                action=f"在对应阶段提前校验「{criterion}」，避免带着不满足项进入验收",
                action_key=f"precheck_{_key_of(criterion)}",
                confidence=0.7,
                evidence_count=1,
                source_refs=list(item.get("refs", [])),
            ))

        # 规则 4：需求歧义协商闭环 -> 前置澄清经验
        for item in facts.get("negotiations", []):
            entries.append(Experience(
                id="",
                tier="L1",
                scope="run",
                roles=["product", "tester", "developer"],
                tags=sorted(set(domain_tags + ["需求歧义", str(item.get("fp_id", ""))])),
                trigger="negotiation_closed",
                lesson=f"需求歧义「{item.get('question', '')}」经协商结论为：{item.get('answer', '')}",
                action="编码前完成歧义澄清并把结论写入技术方案，测试按结论构造用例",
                action_key="clarify_before_implement",
                dedupe=str(item.get("question", "")),
                confidence=0.65,
                evidence_count=1,
                source_refs=list(item.get("refs", [])),
            ))

        # 规则 5：运行效率与成功路径
        metrics = facts.get("metrics", {})
        iterations = int(metrics.get("node_executions", 0))
        bugs = int(metrics.get("bugs_total", 0))
        if metrics.get("acceptance") and bugs == 0:
            entries.append(Experience(
                id="",
                tier="L1",
                scope="run",
                roles=["all"],
                tags=sorted(set(domain_tags + ["成功路径"])),
                trigger="run_end",
                lesson=f"存在一条首轮即通过验收的实现路径（步数 {iterations}、缺陷 0、审查驳回 0），技术选型与实现策略可复用",
                action="同类需求优先复用该技术选型与实现顺序，保持零依赖标准库实现",
                action_key="reuse_success_path",
                confidence=0.6,
                evidence_count=1,
            ))
        for flag in facts.get("efficiency_flags", []):
            entries.append(Experience(
                id="",
                tier="L1",
                scope="run",
                roles=["all"],
                tags=sorted(set(domain_tags + ["流程效率"])),
                trigger="run_end",
                lesson=str(flag.get("lesson", "")),
                action=str(flag.get("action", "")),
                action_key=str(flag.get("action_key", "")),
                confidence=0.5,
                evidence_count=1,
            ))
        return entries

    # ---------------------------------------------------------------- 指标
    def record_metrics(self, metrics: Dict[str, Any]) -> None:
        self.metrics_path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(self.metrics_path, timeout_s=self._lock_timeout_s):
            with self.metrics_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(metrics, ensure_ascii=False, default=str) + "\n")

    def metrics_history(self) -> List[Dict[str, Any]]:
        if not self.metrics_path.exists():
            return []
        history: List[Dict[str, Any]] = []
        with self.metrics_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    try:
                        history.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return history

    def improvement_summary(self) -> Dict[str, Any]:
        """对比最近两次运行，量化自学习收益。"""
        history = self.metrics_history()
        if len(history) < 2:
            return {"available": False, "reason": "历史运行不足 2 次，暂无可比数据"}
        previous, current = history[-2], history[-1]
        keys = ["node_executions", "rollbacks", "review_rejections", "bugs_total", "iterations_to_accept", "duration_s"]
        deltas = {key: (current.get(key, 0) or 0) - (previous.get(key, 0) or 0) for key in keys}
        return {
            "available": True,
            "previous_run": previous.get("run_id"),
            "current_run": current.get("run_id"),
            "previous": {key: previous.get(key) for key in keys},
            "current": {key: current.get(key) for key in keys},
            "deltas": deltas,
            "injected_experiences": current.get("injected_experiences", 0),
            "learned_experiences": current.get("experience_learned", 0),
        }

    def stats(self) -> Dict[str, Any]:
        self.load() if not self._loaded else None
        by_tier: Dict[str, int] = {}
        by_role: Dict[str, int] = {}
        for entry in self._entries:
            by_tier[entry.tier] = by_tier.get(entry.tier, 0) + 1
            for role in entry.roles or ["all"]:
                by_role[role] = by_role.get(role, 0) + 1
        top = sorted(self._entries, key=lambda item: (item.hits, item.confidence), reverse=True)[:5]
        return {
            "total": len(self._entries),
            "by_tier": by_tier,
            "by_role": by_role,
            "avg_confidence": round(sum(e.confidence for e in self._entries) / len(self._entries), 3) if self._entries else 0.0,
            "total_hits": sum(entry.hits for entry in self._entries),
            "top": [
                {"id": entry.id, "tier": entry.tier, "lesson": entry.lesson, "hits": entry.hits,
                 "confidence": entry.confidence, "action_key": entry.action_key}
                for entry in top
            ],
            "metrics_runs": len(self.metrics_history()),
        }

    def prune(self, *, min_confidence: float = 0.2) -> int:
        with self._locked():
            self.load() if not self._loaded else None
            before = len(self._entries)
            self._entries = [
                entry for entry in self._entries
                if entry.confidence >= min_confidence or entry.hits > 0
            ]
            removed = before - len(self._entries)
            if removed:
                self.save()
        return removed

    def learning_report_markdown(self) -> str:
        """自学习效果报告：多次运行的指标对比 + 经验库现状。"""
        history = self.metrics_history()
        stats = self.stats()
        lines = ["# 自学习闭环效果报告", ""]
        lines.append(f"- 经验库条目：{stats['total']}（分层分布 {stats['by_tier']}，累计命中 {stats['total_hits']} 次）")
        lines.append(f"- 平均置信度：{stats['avg_confidence']}")
        lines.append("")
        if history:
            lines.append("## 运行指标对比")
            lines.append("")
            lines.append("| 运行 | 需求 | 验收 | 步数 | 回退 | 审查驳回 | 缺陷数 | 注入经验 | 新增经验 | 耗时(s) |")
            lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
            for item in history:
                lines.append(
                    f"| {item.get('run_id')} | {str(item.get('requirement_title', ''))[:18]} "
                    f"| {'通过' if item.get('acceptance') else '未通过'} "
                    f"| {item.get('node_executions')} | {item.get('rollbacks')} | {item.get('review_rejections')} "
                    f"| {item.get('bugs_total')} | {item.get('injected_experiences')} "
                    f"| {item.get('experience_learned')} | {item.get('duration_s')} |"
                )
            lines.append("")
            summary = self.improvement_summary()
            if summary.get("available"):
                deltas = summary["deltas"]
                lines.append("## 学习收益（最近两次运行对比）")
                lines.append("")
                lines.append(f"- 运行步数变化：{deltas.get('node_executions'):+d}（{summary['previous'].get('node_executions')} → {summary['current'].get('node_executions')}）")
                lines.append(f"- 缺陷数量变化：{deltas.get('bugs_total'):+d}（{summary['previous'].get('bugs_total')} → {summary['current'].get('bugs_total')}）")
                lines.append(f"- 审查驳回变化：{deltas.get('review_rejections'):+d}（{summary['previous'].get('review_rejections')} → {summary['current'].get('review_rejections')}）")
                lines.append(f"- 本轮注入历史经验 {summary.get('injected_experiences')} 条，新增经验 {summary.get('learned_experiences')} 条")
                improved = sum(1 for value in deltas.values() if value < 0)
                lines.append(f"- 结论：{improved} 项指标下降（越少越好），自学习闭环{'已生效' if improved else '暂未体现'}")
        else:
            lines.append("(暂未记录运行指标)")
        if stats["top"]:
            lines.append("")
            lines.append("## 高价值经验（按命中次数）")
            lines.append("")
            for item in stats["top"]:
                lines.append(f"- `{item['id']}` [{item['tier']}] {item['lesson']}（命中 {item['hits']}，置信度 {item['confidence']}）")
        return "\n".join(lines)
