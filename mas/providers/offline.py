"""离线确定性 Provider：无 API Key 时的端到端可运行实现。

职责与真实 LLM 完全一致——接收 PromptBundle 与结构化上下文，返回结构化 JSON：
    requirement_spec   产品 Agent   需求文档/文字描述 -> 需求规格（功能点/验收标准/歧义）
    solution_design    研发 Agent   技术选型 + 方案评估 + 实现计划
    implementation     研发 Agent   生成真实可运行代码（可注入首轮缺陷以演示缺陷闭环）
    code_review        评审 Agent   依据静态分析证据输出审查结论
    design_review      评审 Agent   技术方案评审（strict 流程）
    test_suite         测试 Agent   依据需求契约生成功能/异常/边界用例（真实可执行）
    acceptance_signoff 评审 Agent   验收签署意见
    consultation       研发 Agent   响应测试 Agent 的歧义澄清请求
    acceptance_report  报告 Agent   汇总证据生成验收报告
    skill_distill      报告 Agent   把一次成功运行提炼为可复用技能（SKILL.md）

自学习闭环在本 Provider 中的体现：PromptBundle 注入的经验若携带 action_key
（如 avoid_missing_function_docstring / guard_calc_shipping__boundary），
Provider 会消费这些动作（首轮即补文档字符串、不再注入缺陷），从而让"经验注入"
产生可测量的行为变化（更少的审查驳回与缺陷）。
"""

from __future__ import annotations

import ast as _ast
import hashlib
import re
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from ..config import SystemConfig
from ..prompts import PromptBundle
from .ops import OPS, HELPERS, match_op, ops_for_spec

__all__ = ["OfflineProvider"]

_FIELD_KEYS = {
    "描述": "description",
    "输入": "inputs",
    "输出": "outputs",
    "规则": "rules",
    "样例": "samples",
    "类别": "category",
    "验收": "acceptance",
}

_DOMAIN_KEYWORDS = [
    "订单", "金额", "支付", "电商", "结算", "校验", "折扣", "运费", "优惠券", "分摊", "金额计算",
]


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def _split_kv(line: str) -> Tuple[str, str]:
    for sep in ("：", ":"):
        if sep in line:
            key, _, value = line.partition(sep)
            return key.strip(), value.strip()
    return "", line.strip()


class OfflineProvider:
    """确定性生成器（offline provider）。"""

    def __init__(self, config: SystemConfig) -> None:
        self.config = config
        self.offline = dict(config.llm.get("offline") or {})
        self.package = str(self.offline.get("package_name", "order_pricing"))
        self._oracle: Optional[Dict[str, Any]] = None

    # ================================================================= 入口
    def provide(self, task_kind: str, *, bundle: PromptBundle, context: Dict[str, Any]) -> Dict[str, Any]:
        handlers = {
            "requirement_spec": self._requirement_spec,
            "solution_design": self._solution_design,
            "implementation": self._implementation,
            "code_review": self._code_review,
            "design_review": self._design_review,
            "test_suite": self._test_suite,
            "acceptance_signoff": self._acceptance_signoff,
            "consultation": self._consultation,
            "acceptance_report": self._acceptance_report,
            "skill_distill": self._skill_distill,
        }
        handler = handlers.get(task_kind)
        if handler is None:
            raise ValueError(f"offline provider 不支持的任务类型: {task_kind}")
        return handler(bundle, context)

    # ================================================================ 经验消费
    def _experience_actions(self, bundle: PromptBundle) -> set:
        return {str(item.get("action_key", "")) for item in bundle.experiences if item.get("action_key")}

    # ================================================================= 需求规格
    def _requirement_spec(self, bundle: PromptBundle, context: Dict[str, Any]) -> Dict[str, Any]:
        text = str(context.get("requirement_text", ""))
        return self.parse_requirement(text)

    def parse_requirement(self, text: str) -> Dict[str, Any]:
        lines = [line.rstrip() for line in text.splitlines()]
        title = next((line[2:].strip() for line in lines if line.startswith("# ")), "未命名需求")
        background: List[str] = []
        constraints: List[str] = []
        quality_requirements: List[str] = []
        criteria: List[str] = []
        questions: List[str] = []
        function_points: List[Dict[str, Any]] = []
        current_section = ""
        current_fp: Optional[Dict[str, Any]] = None

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("### "):
                if current_fp is not None:
                    function_points.append(current_fp)
                heading = stripped[4:].strip()
                fp_id = ""
                match = re.match(r"(FP[-–]?\d+)", heading)
                if match:
                    fp_id = re.sub(r"[-–]", "-", match.group(1))
                current_fp = {
                    "id": fp_id or f"FP-{len(function_points) + 1:02d}",
                    "title": heading,
                    "description": "",
                    "parameters": [],
                    "inputs": [],
                    "outputs": [],
                    "rules": [],
                    "samples": [],
                    "category": "功能",
                    "acceptance": [],
                }
                current_section = "fp"
                continue
            if stripped.startswith("## "):
                if current_fp is not None:
                    function_points.append(current_fp)
                    current_fp = None
                current_section = stripped[3:].strip()
                continue
            if not stripped or current_fp is None and current_section == "":
                continue
            if current_fp is not None and current_section == "fp":
                if stripped.startswith("- ") or stripped.startswith("* "):
                    body = stripped[2:].strip()
                    key, value = _split_kv(body)
                    field = _FIELD_KEYS.get(key, "")
                    if field == "description":
                        current_fp["description"] = value
                    elif field in ("inputs", "outputs", "rules", "samples", "acceptance"):
                        if field == "samples":
                            parts = [chunk.strip() for chunk in value.split("→")]
                            current_fp["samples"].append(
                                {"input": parts[0].strip("`"), "expect": (parts[1].strip("`") if len(parts) > 1 else "")}
                            )
                        else:
                            current_fp[field].append(value)
                    elif field == "category":
                        current_fp["category"] = value
                continue
            # 章节正文
            if stripped.startswith("- ") or stripped.startswith("* "):
                item = stripped[2:].strip()
            else:
                item = stripped
            if not item:
                continue
            if "背景" in current_section or "目标" in current_section:
                background.append(item)
            elif "异常" in current_section or "边界" in current_section:
                quality_requirements.append(item)
            elif "技术约束" in current_section or "约束" in current_section:
                constraints.append(item)
            elif "验收标准" in current_section or "验收" in current_section:
                criteria.append(item)
            elif "澄清" in current_section or "开放问题" in current_section:
                questions.append(item)
        if current_fp is not None:
            function_points.append(current_fp)

        # 参数解析（输入：text: str）
        for fp in function_points:
            for raw in fp["inputs"]:
                for token in re.split(r"[,，;；]", raw):
                    token = token.strip()
                    if not token:
                        continue
                    name, _, type_hint = token.partition(":")
                    fp["parameters"].append(
                        {"name": name.strip(), "type": (type_hint or "Any").strip()}
                    )

        # 歧义标记：开放问题显式引用的功能点（并保留问题原文，供协商闭环使用）
        ambiguities: List[Dict[str, Any]] = []
        for fp in function_points:
            related = [
                question for question in questions
                if fp["id"] in question or fp["title"] in question
            ]
            fp["ambiguous"] = bool(related)
            for question in related:
                ambiguities.append({"fp_id": fp["id"], "title": fp["title"], "question": question})
            fp["op_key"] = match_op(fp)

        keywords = " ".join([title] + background + constraints)
        tags = ["需求规格"] + [keyword for keyword in _DOMAIN_KEYWORDS if keyword in keywords]
        if re.search(r"python", keywords, re.IGNORECASE):
            tags.append("python")

        return {
            "id": f"REQ-{_sha(title)}",
            "title": title,
            "background": background,
            "constraints": constraints,
            "quality_requirements": quality_requirements,
            "tags": sorted(set(tags)),
            "function_points": function_points,
            "acceptance_criteria": [
                {"id": f"AC-{index + 1:02d}", "description": item}
                for index, item in enumerate(criteria)
            ],
            "open_questions": [
                {"id": f"Q-{index + 1}", "question": item} for index, item in enumerate(questions)
            ],
            "ambiguities": ambiguities,
            "metrics": {
                "function_points": len(function_points),
                "constraints": len(constraints),
                "acceptance_criteria": len(criteria),
                "open_questions": len(questions),
                "samples": sum(len(fp["samples"]) for fp in function_points),
            },
            "parser": "offline-markdown-rules",
        }

    # ================================================================= 技术方案
    def _solution_design(self, bundle: PromptBundle, context: Dict[str, Any]) -> Dict[str, Any]:
        spec = context.get("spec") or {}
        matched = ops_for_spec(spec)
        fps = spec.get("function_points", [])
        package = self.package

        candidates = [
            {
                "name": "标准库 Decimal + 正则（零第三方依赖）",
                "scores": {"可维护性": 9, "依赖风险": 10, "精度安全": 10, "性能": 8},
                "pros": ["零依赖、可移植", "Decimal 保证金额精度", "便于沙箱内验证"],
                "cons": ["格式解析需自行维护正则"],
            },
            {
                "name": "第三方金额/货币库（如 moneyed）",
                "scores": {"可维护性": 7, "依赖风险": 5, "精度安全": 9, "性能": 7},
                "pros": ["现成的货币类型"],
                "cons": ["引入外部依赖与升级风险", "超出需求约束（要求零依赖）"],
            },
            {
                "name": "float 浮点运算实现",
                "scores": {"可维护性": 8, "依赖风险": 9, "精度安全": 3, "性能": 9},
                "pros": ["实现最简单"],
                "cons": ["0.1+0.2 类精度问题不可接受", "金额计算风险高"],
            },
        ]
        for candidate in candidates:
            candidate["total"] = sum(candidate["scores"].values())
        selected = candidates[0]
        selected_name = selected["name"]

        coverage = []
        for fp in fps:
            op_key = fp.get("op_key") or match_op(fp)
            coverage.append(
                {
                    "id": fp.get("id", ""),
                    "title": fp.get("title", ""),
                    "target": f"{package}.core.{op_key}" if op_key else f"{package}.core（待定）",
                    "op": op_key or "fallback",
                }
            )

        resolutions: List[Dict[str, Any]] = []
        for question in spec.get("open_questions", []):
            resolutions.append(
                {
                    "question": question.get("question", ""),
                    "resolution": "先按会员等级折扣计算，再抵扣优惠券固定金额，最后叠加运费（叠加顺序以澄清结论为准）",
                    "source": "negotiation:tester<->developer",
                    "status": "closed",
                }
            )

        return {
            "title": f"《{spec.get('title', '')}》技术方案",
            "tech_choice": {
                "language": "Python 3.9+（标准库）",
                "runtime": "CPython（沙箱内执行）",
                "dependencies": [],
                "stdlib": ["decimal", "re", "typing", "unittest"],
                "rationale": "需求约束要求纯标准库零依赖；金额计算必须避免浮点误差",
            },
            "candidates": candidates,
            "evaluation": {
                "method": "四维加权打分（可维护性/依赖风险/精度安全/性能）",
                "criteria": ["可维护性", "依赖风险", "精度安全", "性能"],
                "selected": selected_name,
                "reason": "零依赖且精度安全满分，符合技术约束『纯标准库实现、Decimal 保证精度』",
            },
            "design": {
                "architecture": f"单包 {package} 纯函数库：core.py 承载全部业务规则，__init__.py 对外导出",
                "modules": [
                    {
                        "path": f"{package}/__init__.py",
                        "responsibility": "对外 API 导出",
                        "function_points": [fp.get("id", "") for fp in fps],
                    },
                    {
                        "path": f"{package}/core.py",
                        "responsibility": "全部功能点的业务实现（金额解析/折扣/运费/优惠券/分摊/总价/校验）",
                        "function_points": [fp.get("id", "") for fp in fps],
                        "functions": sorted(matched.keys()),
                    },
                ],
                "error_strategy": "统一抛出 ValueError 并携带中文错误描述；禁止裸 except 与静默吞错",
                "testing_strategy": "测试 Agent 独立依据需求契约生成功能/异常/边界三类用例，沙箱真实执行 unittest",
                "ambiguity_resolutions": resolutions,
            },
            "risks": [
                {"risk": "金额精度误差", "mitigation": "全链路 Decimal，统一两位小数量化（ROUND_HALF_UP）"},
                {"risk": "阈值边界计算错误", "mitigation": "续重步数向上取整，边界用例按 t/t-ε/t+ε 覆盖"},
                {"risk": "需求歧义导致实现与测试口径不一致", "mitigation": "编码前通过协商澄清并把结论固化进方案"},
            ],
            "implementation_plan": [
                "1) 依据需求规格建立功能点→实现函数映射",
                "2) 实现 core.py（含异常与边界处理）与 __init__.py",
                "3) 编译与导入自检（可 import、无语法错误）",
                "4) 提交代码审查（静态规则 + 需求一致性）",
                "5) 测试 Agent 生成三类用例并真实执行",
                "6) 缺陷修复后回归，直至验收门禁通过",
            ],
            "requirement_coverage": coverage,
        }

    # ================================================================= 编码实现
    def _implementation(self, bundle: PromptBundle, context: Dict[str, Any]) -> Dict[str, Any]:
        spec = context.get("spec") or {}
        mode = str(context.get("mode", "first"))           # first | rework | fix
        open_bugs = list(context.get("open_bugs") or [])
        findings = list(context.get("review_findings") or [])
        actions = self._experience_actions(bundle)
        matched = ops_for_spec(spec)
        fps = spec.get("function_points", [])

        inject_defect = bool(self.offline.get("inject_first_pass_defect", True))
        defect_ops = list(self.offline.get("defect_ops") or [])
        strip_docstrings = bool(self.offline.get("strip_first_pass_docstrings", True))

        # ---- 经验驱动的行为修正（自学习闭环）
        guarded_ops = {
            op_key for op_key in matched
            if f"guard_{op_key}__boundary" in actions or f"guard_{op_key}__exception" in actions
            or f"guard_{op_key}__functional" in actions or "boundary_tri_state" in actions
        }
        know_docstring_rule = any(
            key in actions for key in ("avoid_missing_module_docstring", "avoid_missing_function_docstring")
        )

        # ---- 缺陷注入决策：首轮与审查返工轮均保留未知缺陷，直到缺陷单出现或经验守护生效
        buggy_ops = set()
        if inject_defect and mode in ("first", "rework"):
            for op_key in defect_ops:
                op = matched.get(op_key)
                if not op or not op.get("bug_impl"):
                    continue
                if op_key in guarded_ops:
                    continue
                if any(str(bug.get("op", "")) == op_key for bug in open_bugs):
                    continue
                buggy_ops.add(op_key)

        with_docstrings = True
        if mode == "first" and strip_docstrings and not know_docstring_rule:
            with_docstrings = False
        if mode == "rework":
            with_docstrings = True   # 针对审查驳回（缺文档字符串）的返工

        fp_mapping: Dict[str, str] = {}
        op_by_fp: Dict[str, str] = {}
        for fp in fps:
            op_key = fp.get("op_key") or match_op(fp)
            if op_key and op_key in matched:
                fp_mapping[fp["id"]] = op_key
                op_by_fp[op_key] = fp["id"]

        core_source = self._build_core(
            spec, matched, with_docstrings=with_docstrings, buggy_ops=buggy_ops
        )
        init_source = self._build_init(spec, matched, with_docstrings=with_docstrings)

        defects = [
            {
                "op": op_key,
                "fp_id": op_by_fp.get(op_key, ""),
                "note": matched[op_key].get("defect_note", ""),
            }
            for op_key in sorted(buggy_ops)
        ]
        fix_targets = sorted({str(bug.get("op", "")) for bug in open_bugs if bug.get("op")})

        summary = {
            "mode": mode,
            "package": self.package,
            "files": [
                {"path": f"{self.package}/__init__.py", "content": init_source},
                {"path": f"{self.package}/core.py", "content": core_source},
            ],
            "fp_mapping": fp_mapping,
            "functions": sorted(matched.keys()),
            "defects_injected": defects,
            "fix_targets": fix_targets,
            "fix_strategies": {
                op_key: "修正为需求契约语义（阈值向上取整 + 偏远地区加收）"
                for op_key in fix_targets
            },
            "notes": (
                f"实现 {len(fp_mapping)}/{len(fps)} 个功能点；"
                + (f"针对审查问题修复 {len(findings)} 项；" if mode == "rework" and findings else "")
                + (f"针对缺陷单修复 {len(fix_targets)} 个算子；" if mode == "fix" else "")
                + ("首轮草稿（待审查）。" if mode == "first" else "")
            ),
            "design_basis": f"{self.package}.core 承载全部业务规则，异常统一 ValueError",
        }
        return summary

    def _build_core(
        self,
        spec: Dict[str, Any],
        matched: Dict[str, Dict[str, Any]],
        *,
        with_docstrings: bool,
        buggy_ops: set,
    ) -> str:
        fps = spec.get("function_points", [])
        fp_labels = ", ".join(f"{fp.get('id')}" for fp in fps) or "全部功能点"
        selected = "标准库 Decimal + 正则（零第三方依赖）"
        if with_docstrings:
            header = (
                f'"""{spec.get("title", "交付物")} 核心实现。\n\n'
                f"由研发 Agent 依据需求规格与技术方案生成：\n"
                f"- 方案选型：{selected}\n"
                f"- 覆盖功能点：{fp_labels}\n"
                f"- 金额统一使用 Decimal（ROUND_HALF_UP，两位小数）\n"
                f"- 非法输入统一抛出 ValueError\n""\n\"\"\"\n"
            )
        else:
            header = ""
        parts: List[str] = [header, HELPERS.strip(), ""]
        for op_key, op in matched.items():
            source = op["bug_impl"] if op_key in buggy_ops and op.get("bug_impl") else op["impl"]
            parts.append(source.strip())
            parts.append("")
        module = "\n".join(parts).rstrip() + "\n"
        if not with_docstrings:
            module = strip_docstrings(module)
        return module

    def _build_init(self, spec: Dict[str, Any], matched: Dict[str, Dict[str, Any]], *, with_docstrings: bool) -> str:
        exports = sorted(matched.keys())
        if with_docstrings:
            header = (
                f'"""{spec.get("title", "交付包")}（{self.package}）。\n\n'
                f"对外导出 {len(exports)} 个功能点实现，均来自 core 模块。\n""\n\"\"\"\n"
            )
        else:
            header = ""
        import_block = "from .core import (\n" + "".join(f"    {name},\n" for name in exports) + ")\n"
        all_block = "__all__ = [\n" + "".join(f'    "{name}",\n' for name in exports) + "]\n"
        return header + import_block + all_block

    # ================================================================= 代码审查
    def _code_review(self, bundle: PromptBundle, context: Dict[str, Any]) -> Dict[str, Any]:
        spec = context.get("spec") or {}
        manifest = context.get("code_manifest") or {}
        analysis = context.get("static_analysis") or {}
        policies = context.get("policies") or {}
        findings = list(analysis.get("findings") or [])
        blocking_severities = set(policies.get("blocking_severities") or ["critical", "major"])

        # 需求一致性：功能点必须全部映射到实现
        missing_fps = [
            fp.get("id", "")
            for fp in spec.get("function_points", [])
            if fp.get("id") not in (manifest.get("fp_mapping") or {})
        ]
        for fp_id in missing_fps:
            findings.append({
                "rule": "unimplemented_function_point",
                "severity": "critical",
                "file": (manifest.get("files") or ["-"])[0] if manifest.get("files") else "-",
                "line": 0,
                "symbol": fp_id,
                "message": f"需求功能点 {fp_id} 未映射到任何实现函数",
            })

        blocking = [item for item in findings if item.get("severity") in blocking_severities]
        by_severity: Dict[str, int] = {}
        by_rule: Dict[str, int] = {}
        for item in findings:
            by_severity[item.get("severity", "info")] = by_severity.get(item.get("severity", "info"), 0) + 1
            by_rule[item.get("rule", "")] = by_rule.get(item.get("rule", ""), 0) + 1
        return {
            "reviewed_files": manifest.get("files", []),
            "findings": findings,
            "blocking": len(blocking),
            "blocking_items": blocking,
            "stats": {"by_severity": by_severity, "by_rule": by_rule, "total": len(findings)},
            "coverage": {
                "function_points": len(spec.get("function_points", [])),
                "implemented": len(manifest.get("fp_mapping") or {}),
                "missing": missing_fps,
            },
            "verdict": "pass" if not blocking else "reject",
            "summary": (
                f"审查 {len(manifest.get('files', []))} 个文件、{analysis.get('loc', 0)} 行代码，"
                f"发现 {len(findings)} 项问题（阻塞 {len(blocking)}），"
                + ("无阻塞问题，同意进入测试。" if not blocking else "存在阻塞问题，退回研发修复。")
            ),
            "recommendations": [
                "补充文档字符串与类型注解" if by_rule.get("missing_function_docstring") else "",
                "消除裸 except，改用具体异常" if by_rule.get("bare_except") else "",
            ],
        }

    def _design_review(self, bundle: PromptBundle, context: Dict[str, Any]) -> Dict[str, Any]:
        spec = context.get("spec") or {}
        design = context.get("solution_design") or {}
        findings: List[Dict[str, Any]] = []

        candidates = design.get("candidates") or []
        evaluation = design.get("evaluation") or {}
        if not candidates:
            findings.append({"rule": "missing_candidates", "severity": "major", "message": "缺少候选方案对比"})
        if not evaluation.get("selected"):
            findings.append({"rule": "missing_selection", "severity": "major", "message": "缺少选型结论"})
        covered = {
            item.get("id")
            for item in (design.get("requirement_coverage") or [])
        }
        missing = [fp.get("id", "") for fp in spec.get("function_points", []) if fp.get("id") not in covered]
        if missing:
            findings.append({
                "rule": "incomplete_coverage",
                "severity": "major",
                "message": f"方案未覆盖功能点: {', '.join(missing)}",
            })
        open_questions = spec.get("open_questions") or []
        resolutions = design.get("design", {}).get("ambiguity_resolutions") or []
        if open_questions and len(resolutions) < len(open_questions):
            findings.append({
                "rule": "unresolved_ambiguity",
                "severity": "major",
                "message": f"{len(open_questions) - len(resolutions)} 个待澄清问题未在方案中闭环",
            })
        blocking = [item for item in findings if item.get("severity") in ("critical", "major")]
        return {
            "findings": findings,
            "blocking": len(blocking),
            "verdict": "pass" if not blocking else "reject",
            "summary": (
                f"方案评审：候选 {len(candidates)} 个、选中『{evaluation.get('selected', '-')}』、"
                f"功能点覆盖 {len(covered)}/{len(spec.get('function_points', []))}、"
                f"歧义闭环 {len(resolutions)}/{len(open_questions)}。"
                + ("方案可行。" if not blocking else "存在阻塞问题，需完善方案。")
            ),
        }

    # ================================================================= 测试用例
    def _test_suite(self, bundle: PromptBundle, context: Dict[str, Any]) -> Dict[str, Any]:
        spec = context.get("spec") or {}
        policies = context.get("policies") or {}
        epsilon = float(policies.get("boundary_epsilon", 0.01))
        matched = ops_for_spec(spec)
        fps = spec.get("function_points", [])

        cases: List[Dict[str, Any]] = []
        fp_by_op: Dict[str, str] = {}
        for fp in fps:
            op_key = fp.get("op_key") or match_op(fp)
            if op_key and op_key in matched:
                fp_by_op.setdefault(op_key, fp.get("id", ""))

        for op_key, op in matched.items():
            fp_id = fp_by_op.get(op_key, "")
            for case in op.get("cases", []):
                cases.append({
                    "id": f"TC-{fp_id}-{case['category']}-{_sha(case['name'])}",
                    "fp_id": fp_id,
                    "op": op_key,
                    "name": case["name"],
                    "category": case["category"],
                    "call": case["call"],
                    "expect": case.get("expect", ""),
                    "assert_kind": case.get("assert_kind", "eq"),
                    "expect_error": case.get("expect_error", ""),
                    "source": "requirement-contract",
                })
            derived = self._derive_threshold_cases(op, fp_id, epsilon)
            if derived:
                cases.extend(derived)

        # 未匹配算子的兜底用例（依据样例）
        for fp in fps:
            op_key = fp.get("op_key") or match_op(fp)
            if op_key in matched:
                continue
            for sample in fp.get("samples", [])[:3]:
                cases.append({
                    "id": f"TC-{fp['id']}-functional-{_sha(sample.get('input', ''))}",
                    "fp_id": fp["id"],
                    "op": "",
                    "name": f"sample_{_sha(sample.get('input', ''))}",
                    "category": "functional",
                    "call": "",
                    "expect": sample.get("expect", ""),
                    "assert_kind": "manual",
                    "source": "requirement-sample",
                })

        test_file = self._build_test_file(spec, matched, cases, fp_by_op)
        by_category: Dict[str, int] = {}
        for case in cases:
            by_category[case["category"]] = by_category.get(case["category"], 0) + 1

        negotiation = {
            "questions": [
                {"fp_id": item.get("fp_id", ""), "question": item.get("question", "")}
                for item in spec.get("ambiguities", [])
            ],
            "answers": [],
            "open": len(spec.get("ambiguities", [])),
            "status": "pending",
        }
        return {
            "package": self.package,
            "files": [{"path": f"tests/test_{self.package}.py", "content": test_file}],
            "cases": cases,
            "case_count": {
                "total": len(cases),
                "functional": by_category.get("functional", 0),
                "exception": by_category.get("exception", 0),
                "boundary": by_category.get("boundary", 0),
            },
            "coverage_plan": {
                "function_points": len(fps),
                "categories": policies.get("categories", ["functional", "exception", "boundary"]),
            },
            "negotiation": negotiation,
            "derived_from": "需求功能点规则/样例 + 阈值三态派生（不读取研发实现代码）",
        }

    def _derive_threshold_cases(self, op: Dict[str, Any], fp_id: str, epsilon: float) -> List[Dict[str, Any]]:
        if not op.get("threshold_builder"):
            return []
        unit = str(op.get("threshold_unit", "kg"))
        key = op["key"]
        if key != "calc_shipping":   # 阈值派生模板按算子注册
            return []
        # 从算子契约说明中提取阈值（首重 1kg）
        rules_text = " ".join(str(case) for case in op.get("cases", [])) + op["summary"]
        numbers = sorted({Decimal(match) for match in re.findall(r"(\d+(?:\.\d+)?)\s*" + unit, op["summary"])})
        if not numbers:
            numbers = [Decimal("1")]
        oracle = self._oracle_call()
        cases: List[Dict[str, Any]] = []
        for threshold in numbers:
            variants = [
                ("t", threshold),
                ("t_minus_eps", threshold - Decimal(str(epsilon))),
                ("t_plus_eps", threshold + Decimal(str(epsilon))),
            ]
            for label, value in variants:
                amount = f"{value.normalize()}"
                call = f'calc_shipping("{amount}", "上海")'
                expected = oracle("calc_shipping", str(value), "上海")
                cases.append({
                    "id": f"TC-{fp_id}-boundary-threshold-{_sha(call)}",
                    "fp_id": fp_id,
                    "op": key,
                    "name": f"threshold_{amount.replace('.', '_')}_{label}",
                    "category": "boundary",
                    "call": call,
                    "expect": expected,
                    "assert_kind": "eq",
                    "source": "threshold-derived",
                })
        _ = rules_text
        return cases

    def _oracle_namespace(self) -> Dict[str, Any]:
        if self._oracle is None:
            namespace: Dict[str, Any] = {}
            exec(HELPERS + "\n" + "\n".join(op["impl"] for op in OPS.values()), namespace)  # noqa: S102
            self._oracle = namespace
        return self._oracle

    def _oracle_call(self) -> Any:
        namespace = self._oracle_namespace()

        def call(function_name: str, *args: Any) -> str:
            result = namespace[function_name](*args)
            return repr(result)

        return call

    def _build_test_file(
        self,
        spec: Dict[str, Any],
        matched: Dict[str, Dict[str, Any]],
        cases: List[Dict[str, Any]],
        fp_by_op: Dict[str, str],
    ) -> str:
        title = spec.get("title", "交付物")
        import_statement = (
            f"from {self.package} import {', '.join(sorted(matched.keys()))}"
            if matched else "# （无可自动化的功能点）"
        )
        lines: List[str] = [
            f'"""{title} 业务测试用例。',
            "",
            "由测试 Agent 依据需求契约（功能点规则/样例/阈值三态）生成，覆盖功能 / 异常 / 边界三类。",
            '执行：python tests/test_....py [--only 名称子串,名称子串]',
            '"""',
            "from __future__ import annotations",
            "",
            "import json",
            "import sys",
            "import time",
            "import traceback",
            "import unittest",
            "from decimal import Decimal",
            "",
            import_statement,
            "",
            'MARKER = "<<<MASS_RESULT>>>"',
            "",
            "",
            "def _norm(value):",
            "    if isinstance(value, Decimal):",
            '        return value.quantize(Decimal("0.01"))',
            "    if isinstance(value, (list, tuple)):",
            "        return [_norm(item) for item in value]",
            "    if isinstance(value, float):",
            '        return Decimal(str(value)).quantize(Decimal("0.01"))',
            "    return value",
            "",
            "",
        ]
        # 按功能点分组
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for case in cases:
            if not case.get("call"):
                continue
            grouped.setdefault(case["fp_id"], []).append(case)

        class_names: Dict[str, str] = {}
        for fp_id, fp_cases in grouped.items():
            op_key = fp_cases[0].get("op", "")
            class_name = f"Test{fp_id.replace('-', '')}{_camel(op_key)}"
            class_names[class_name] = fp_id
            lines.append(f"class {class_name}(unittest.TestCase):")
            lines.append(f'    """{fp_id} 用例组。"""')
            lines.append("")
            for case in fp_cases:
                method = f"test_{case['category']}_{case['op']}_{case['name']}"
                lines.append(f"    def {method}(self):")
                if case.get("expect_error"):
                    lines.append(f"        with self.assertRaises({case['expect_error']}):")
                    lines.append(f"            {case['call']}")
                elif case.get("assert_kind") == "truthy":
                    lines.append(f"        self.assertTrue(bool({case['call']}))")
                elif case.get("assert_kind") == "falsy":
                    lines.append(f"        self.assertFalse(bool({case['call']}))")
                elif case.get("assert_kind") == "empty":
                    lines.append(f"        self.assertEqual(list({case['call']}), [])")
                elif case.get("assert_kind") == "len_ge":
                    lines.append(f"        self.assertGreaterEqual(len({case['call']}), {case.get('expect', '1')})")
                elif case.get("assert_kind") == "contains":
                    lines.append(
                        f'        self.assertIn("{case.get("expect", "")}", " ".join({case["call"]}))'
                    )
                else:
                    lines.append(f"        self.assertEqual(_norm({case['call']}), _norm({case['expect']}))")
                lines.append("")
        fp_map = "{" + ", ".join(f'"{name}": "{fp}"' for name, fp in class_names.items()) + "}"
        runner = '''
class _CollectResult(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.records = []
        self.started = time.time()

    def startTest(self, test):
        super().startTest(test)
        self.records.append({"id": test.id(), "status": "passed", "message": ""})

    def _mark(self, test, status, err):
        for record in self.records:
            if record["id"] == test.id():
                record["status"] = status
                record["message"] = "".join(
                    traceback.format_exception_only(err[0], err[1])
                )[-300:].strip()

    def addError(self, test, err):
        super().addError(test, err)
        self._mark(test, "error", err)

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._mark(test, "failed", err)


def _filter(suite, keywords):
    filtered = unittest.TestSuite()
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            filtered.addTests(_filter(item, keywords))
        elif any(keyword in item.id() for keyword in keywords):
            filtered.addTest(item)
    return filtered


def main():
    keywords = []
    if "--only" in sys.argv:
        keywords = [part for part in sys.argv[sys.argv.index("--only") + 1].split(",") if part]
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    if keywords:
        suite = _filter(suite, keywords)
    result = _CollectResult()
    suite.run(result)
    cases = []
    for record in result.records:
        _, class_name, method = record["id"].rsplit(".", 2)[-3:]
        category = method.split("_", 2)
        cases.append({
            "test": method,
            "class": class_name,
            "fp_id": FP_BY_CLASS.get(class_name, ""),
            "category": category[1] if len(category) > 1 else "",
            "status": record["status"],
            "message": record["message"],
        })
    failed = sum(1 for case in cases if case["status"] != "passed")
    summary = {
        "total": result.testsRun,
        "passed": result.testsRun - failed,
        "failed": failed,
        "duration_ms": int((time.time() - result.started) * 1000),
        "cases": cases,
    }
    print(MARKER + json.dumps(summary, ensure_ascii=False))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
'''
        lines.append(f"FP_BY_CLASS = {fp_map}")
        lines.append(runner.strip("\n"))
        lines.append("")
        return "\n".join(lines)

    # ================================================================= 验收签署
    def _acceptance_signoff(self, bundle: PromptBundle, context: Dict[str, Any]) -> Dict[str, Any]:
        test_report = context.get("test_report") or {}
        review_report = context.get("review_report") or {}
        bug_reports = context.get("bug_reports") or {}
        spec = context.get("spec") or {}
        summary = test_report.get("summary", {})
        coverage = test_report.get("coverage", {})
        reasons: List[str] = []
        if int(summary.get("failed", 0)) > 0:
            reasons.append(f"仍有 {summary.get('failed')} 个用例未通过")
        if int(bug_reports.get("open", 0)) > 0:
            reasons.append(f"仍有 {bug_reports.get('open')} 个缺陷未闭环")
        if int(review_report.get("blocking", 0)) > 0:
            reasons.append(f"仍有 {review_report.get('blocking')} 个阻塞性审查问题")
        for name in ("functional", "exception", "boundary"):
            rate = (coverage.get(name) or {}).get("rate", 0)
            if float(rate or 0) < 1.0:
                reasons.append(f"{name} 用例覆盖不完整（{rate}）")
        if (test_report.get("negotiation") or {}).get("open", 0):
            reasons.append("存在未闭环的需求歧义")
        verdict = "reject" if reasons else "pass"
        fp_total = len(spec.get("function_points", []))
        opinion = (
            f"证据核查：功能点 {fp_total} 个、用例 {summary.get('total', 0)} 条"
            f"（功能 {coverage.get('functional', {}).get('total', 0)} / 异常 {coverage.get('exception', {}).get('total', 0)}"
            f" / 边界 {coverage.get('boundary', {}).get('total', 0)}）、"
            f"通过率 {summary.get('pass_rate', 0)}、缺陷闭环 {bug_reports.get('closed', 0)}"
            f"/{bug_reports.get('total', 0)}。"
            + ("同意验收。" if verdict == "pass" else "不同意验收：" + "；".join(reasons))
        )
        return {"verdict": verdict, "opinion": opinion, "blocking_reasons": reasons}

    # ================================================================= 协商应答
    def _consultation(self, bundle: PromptBundle, context: Dict[str, Any]) -> Dict[str, Any]:
        question = str(context.get("question", ""))
        fp_id = str(context.get("fp_id", ""))
        answer = str(context.get("default_answer", "")) or (
            "结论：先按会员等级折扣计算，再抵扣优惠券固定金额，最后叠加运费；"
            "实现与测试均按此口径，并回写技术方案的歧义决议。"
        )
        return {
            "question": question,
            "answer": answer,
            "fp_id": fp_id,
            "topic": "ambiguity",
            "references": ["requirement_spec.open_questions", "solution_design.design.ambiguity_resolutions"],
            "impact": "实现与测试用例的期望值按该结论对齐",
        }

    # ================================================================= 验收报告
    def _acceptance_report(self, bundle: PromptBundle, context: Dict[str, Any]) -> Dict[str, Any]:
        facts = context.get("facts") or {}
        spec = facts.get("spec") or {}
        design = facts.get("solution") or {}
        manifest = facts.get("manifest") or {}
        review = facts.get("review") or {}
        test = facts.get("test") or {}
        bugs = facts.get("bugs") or {}
        gate = facts.get("gate") or {}
        learning = facts.get("learning") or {}
        stats = facts.get("event_stats") or {}
        run_id = facts.get("run_id", "")
        title = spec.get("title", "")

        md: List[str] = []
        md.append(f"# 验收报告：《{title}》")
        md.append("")
        md.append(f"- 运行 ID：`{run_id}`")
        md.append(f"- 生成时间：{facts.get('finished_at', '')}")
        md.append(f"- 验收结论：**{'通过' if gate.get('passed') else '未通过'}**")
        md.append(f"- 需求功能点：{len(spec.get('function_points', []))} 个｜测试用例：{(test.get('summary') or {}).get('total', 0)} 条｜缺陷：{bugs.get('total', 0)} 个（闭环 {bugs.get('closed', 0)}）")
        md.append(f"- 回退次数：{facts.get('rollbacks', 0)}｜审查驳回：{facts.get('review_rejections', 0)}｜流程步数：{facts.get('node_executions', 0)}")
        md.append("")

        md.append("## 1. 需求评估（产品 Agent）")
        md.append("")
        md.append(f"背景：{' '.join(spec.get('background', [])[:2]) or '-'}")
        md.append("")
        md.append("| 功能点 | 标题 | 类别 | 样例数 | 实现映射 |")
        md.append("| --- | --- | --- | --- | --- |")
        for fp in spec.get("function_points", []):
            md.append(
                f"| {fp.get('id')} | {fp.get('title')} | {fp.get('category')} "
                f"| {len(fp.get('samples', []))} | `{(manifest.get('fp_mapping') or {}).get(fp.get('id'), '-')}` |"
            )
        criteria = spec.get("acceptance_criteria", [])
        if criteria:
            md.append("")
            md.append("验收标准：")
            md.extend(f"- {item.get('id')}：{item.get('description')}" for item in criteria)

        md.append("")
        md.append("## 2. 技术方案（研发 Agent）")
        md.append("")
        evaluation = design.get("evaluation") or {}
        md.append(f"- 选型结论：**{evaluation.get('selected', '-')}**")
        md.append(f"- 决策理由：{evaluation.get('reason', '-')}")
        md.append("")
        md.append("| 候选方案 | 可维护性 | 依赖风险 | 精度安全 | 性能 | 总分 |")
        md.append("| --- | --- | --- | --- | --- | --- |")
        for candidate in design.get("candidates", []):
            scores = candidate.get("scores", {})
            md.append(
                f"| {candidate.get('name')} | {scores.get('可维护性', '-')} | {scores.get('依赖风险', '-')} "
                f"| {scores.get('精度安全', '-')} | {scores.get('性能', '-')} | **{candidate.get('total', '-')}** |"
            )
        for module in (design.get("design") or {}).get("modules", []):
            md.append(f"- 模块 `{module.get('path')}`：{module.get('responsibility')}")
        resolutions = (design.get("design") or {}).get("ambiguity_resolutions", [])
        if resolutions:
            md.append("")
            md.append("歧义决议：")
            for item in resolutions:
                md.append(f"- Q：{item.get('question')} → {item.get('resolution')}（来源：{item.get('source')}）")

        md.append("")
        md.append("## 3. 编码实现（研发 Agent）")
        md.append("")
        md.append(f"- 交付包：`{manifest.get('package', '-')}`")
        md.append(f"- 文件数：{len(manifest.get('files', []))}｜代码行数：{manifest.get('loc', 0)}｜函数数：{len(manifest.get('functions', []))}")
        for path in manifest.get("files", []):
            md.append(f"  - `{path}`")
        fixes = facts.get("fixes") or []
        if fixes:
            md.append("")
            md.append("缺陷修复记录：")
            for fix in fixes:
                md.append(f"- `{fix.get('bug_id', '')}`（{fix.get('op', '')}）：{fix.get('strategy', '')}｜回归自测：{'通过' if fix.get('regression_ok') else '未通过'}")

        md.append("")
        md.append("## 4. 代码审查（Reviewer Agent）")
        md.append("")
        review_stats = review.get("stats") or {}
        md.append(f"- 结论：**{review.get('verdict', '-')}**｜阻塞问题：{review.get('blocking', 0)}｜问题总数：{review_stats.get('total', 0)}")
        by_rule = review_stats.get("by_rule", {})
        if by_rule:
            md.append("| 规则 | 次数 |")
            md.append("| --- | --- |")
            for rule, count in by_rule.items():
                md.append(f"| `{rule}` | {count} |")
        coverage = review.get("coverage") or {}
        md.append("")
        md.append(f"- 需求一致性：功能点覆盖 {coverage.get('implemented', 0)}/{coverage.get('function_points', 0)}"
                  + (f"，缺失：{', '.join(coverage.get('missing', []))}" if coverage.get("missing") else ""))

        md.append("")
        md.append("## 5. 业务测试（测试 Agent）")
        md.append("")
        summary = test.get("summary") or {}
        md.append(f"- 用例总数：{summary.get('total', 0)}｜通过：{summary.get('passed', 0)}｜失败：{summary.get('failed', 0)}｜通过率：{summary.get('pass_rate', 0)}")
        coverage = test.get("coverage") or {}
        md.append("| 用例类别 | 覆盖功能点 | 用例数 | 覆盖率 |")
        md.append("| --- | --- | --- | --- |")
        for name in ("functional", "exception", "boundary"):
            item = coverage.get(name) or {}
            md.append(f"| {name} | {item.get('fps', 0)}/{item.get('fps_total', 0)} | {item.get('total', 0)} | {item.get('rate', 0)} |")
        md.append(f"- 执行方式：沙箱真实执行 `python tests/test_{manifest.get('package', '')}.py`（exit code / stdout 均有留档）")
        negotiation = test.get("negotiation") or {}
        if negotiation.get("total", 0):
            md.append("")
            md.append(f"- 需求歧义协商：{negotiation.get('closed', 0)}/{negotiation.get('total', 0)} 已闭环")

        md.append("")
        md.append("## 6. 验收判定（门禁）")
        md.append("")
        md.append("| 门禁项 | 判定 | 依据 |")
        md.append("| --- | --- | --- |")
        for item in gate.get("criteria", []):
            md.append(f"| {item.get('id')} | {'✅' if item.get('passed') else '❌'} | `{item.get('expr', '')}` → {item.get('value', '')}{'（' + item.get('reason', '') + '）' if item.get('reason') else ''} |")
        signoff = gate.get("signoff") or {}
        md.append("")
        md.append(f"- Reviewer 签署意见：{signoff.get('opinion', '-')}")

        md.append("")
        md.append("## 7. 缺陷与修复闭环")
        md.append("")
        items = bugs.get("items", [])
        if items:
            md.append("| 缺陷 ID | 功能点 | 类别 | 严重 | 状态 | 修复轮次 | 症状 |")
            md.append("| --- | --- | --- | --- | --- | --- | --- |")
            for bug in items:
                md.append(
                    f"| `{bug.get('id')}` | {bug.get('fp_id')} | {bug.get('category')} | {bug.get('severity')} "
                    f"| {bug.get('status')} | {bug.get('fixed_iteration', '-')} | {str(bug.get('symptom', ''))[:60]} |"
                )
        else:
            md.append("本轮交付未发现缺陷。")

        md.append("")
        md.append("## 8. 证据索引（可追溯）")
        md.append("")
        for name, path in (facts.get("evidence_paths") or {}).items():
            md.append(f"- {name}：`{path}`")
        md.append(f"- 事件日志：`{len(stats.get('by_type', {}))} 类事件共 {stats.get('total', 0)} 条`（events.jsonl）")

        md.append("")
        md.append("## 9. 自学习沉淀")
        md.append("")
        md.append(
            f"- 本轮注入历史经验：{learning.get('injected', 0)} 条不同经验"
            f"（累计注入 {learning.get('injected_total', 0)} 次，分层：{learning.get('injected_tiers', {})}）"
        )
        md.append(f"- 本轮沉淀新经验：{learning.get('learned', 0)} 条")
        for item in learning.get("lessons", [])[:8]:
            md.append(f"  - [{item.get('tier')}] {item.get('lesson')}")
        improvement = learning.get("improvement") or {}
        if improvement.get("available"):
            deltas = improvement.get("deltas", {})
            md.append("")
            md.append(
                f"- 学习收益（对比 {improvement.get('previous_run')}）：步数 {deltas.get('node_executions', 0):+d}，"
                f"缺陷 {deltas.get('bugs_total', 0):+d}，审查驳回 {deltas.get('review_rejections', 0):+d}"
            )

        timeline = facts.get("timeline") or []
        if timeline:
            md.append("")
            md.append("## 10. 时间线")
            md.append("")
            md.append("```")
            md.extend(timeline[-30:])
            md.append("```")
        md.append("")

        json_summary = {
            "run_id": run_id,
            "requirement": title,
            "verdict": "pass" if gate.get("passed") else "fail",
            "function_points": len(spec.get("function_points", [])),
            "test_summary": summary,
            "bugs": {"total": bugs.get("total", 0), "open": bugs.get("open", 0), "closed": bugs.get("closed", 0)},
            "review": {"verdict": review.get("verdict"), "blocking": review.get("blocking", 0)},
            "gate": gate,
            "learning": learning,
            "metrics": facts.get("metrics") or {},
        }
        return {"markdown": "\n".join(md), "json": json_summary}

    # ================================================================ 流程沉淀
    def _skill_distill(self, bundle: PromptBundle, context: Dict[str, Any]) -> Dict[str, Any]:
        """规则化沉淀：从「最终通过路径」生成 SKILL.md（质量上限低于真实 LLM）。"""
        from ..skills import distill_meta

        facts = context.get("facts") or {}
        meta = distill_meta(facts)
        node_path = facts.get("node_path") or []

        md: List[str] = []
        md.append(f"# 技能：{facts.get('title') or meta['name']}")
        md.append("")
        md.append(f"沉淀自运行 `{facts.get('run_id', '')}`，该运行已通过验收。")
        md.append("")
        md.append("## 执行步骤")
        md.append("")
        md.extend(f"{index}. {step}" for index, step in enumerate(meta["steps"], start=1))
        md.append("")
        if meta["checkpoints"]:
            md.append("## 结果校验点")
            md.append("")
            md.extend(f"- {item}" for item in meta["checkpoints"])
            md.append("")
        if meta["error_handling"]:
            md.append("## 异常处理")
            md.append("")
            md.extend(f"- {item}" for item in meta["error_handling"])
            md.append("")
        md.append(f"## 流程路径\n\n{' → '.join(node_path)}")
        md.append("")
        return {"markdown": "\n".join(md), "json": meta}


def _camel(name: str) -> str:
    return "".join(part.capitalize() for part in str(name).split("_"))


def strip_docstrings(source: str) -> str:
    """生成首轮草稿时去掉文档字符串（用于演示审查驳回与经验纠偏）。"""
    tree = _ast.parse(source)
    if tree.body and isinstance(tree.body[0], _ast.Expr) and isinstance(tree.body[0].value, _ast.Constant) \
            and isinstance(tree.body[0].value.value, str):
        tree.body.pop(0)
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
            if node.body and isinstance(node.body[0], _ast.Expr) and isinstance(node.body[0].value, _ast.Constant) \
                    and isinstance(node.body[0].value.value, str):
                node.body.pop(0)
                if not node.body:
                    node.body.append(_ast.Pass())
    return _ast.unparse(tree) + "\n"
