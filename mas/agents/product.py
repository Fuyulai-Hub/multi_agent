"""产品 Agent：需求评估与拆解。"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..contract import (
    CONTRACT_RELATIVE,
    ContractError,
    extract_clauses,
    freeze_contract,
    validate_contract,
)
from ..hidden import HiddenSuiteError, build_generation_prompt, create_hidden_suite
from ..intent import (
    DEFAULT_DELIVERY_SHAPE,
    DELIVERY_SHAPES,
    classify_delivery_shape,
    resolve_delivery_shape,
)
from ..llm import Generation
from ..projectdocs import directive_shape
from ..prompts import PromptBundle
from ..schemas import AgentResult, NodeTask
from .base import BaseAgent, Perception

__all__ = ["ProductAgent"]


def _clause_index_section(clauses: List[Dict[str, Any]]) -> str:
    """把确定性条款索引注入 Prompt（模型可引用的唯一编号来源）。"""
    lines = [
        "## 原始需求条款索引（框架按原文确定性抽取，编号不可改写）",
        "",
        "每个条款要么被某个功能点 ``source_spans`` 引用，要么写入 ``out_of_scope`` 并给出理由；"
        "两份清单合起来必须覆盖下表全部 clause_id。",
        "",
        "| clause_id | 行号 | 章节 | 原文 |",
        "| --- | --- | --- | --- |",
    ]
    for clause in clauses:
        text = str(clause.get("text", "")).replace("|", "\\|")
        section = str(clause.get("section", "")).replace("|", "\\|")
        lines.append(
            f"| {clause.get('id', '')} | {clause.get('line', '')} | {section} | {text} |"
        )
    return "\n".join(lines)


class ProductAgent(BaseAgent):
    role_id = "product"
    task_kinds = {"default": "requirement_spec"}
    contracts = {
        "default": {
            "id": "需求编号",
            "title": "需求标题",
            "delivery_shape": " | ".join(DELIVERY_SHAPES) + "（交付形态：纯后端（库/CLI/服务）="
                              "backend_only；前后端一起且页面由服务端渲染=full_stack_ssr；"
                              "前后端一起且前端在浏览器里运行=full_stack_spa；无法判定填 backend_only）",
            "delivery_shape_reason": "一句话说明为什么归到该形态（形态无法判定时必须写成待澄清问题）",
            "background": ["业务背景"],
            "constraints": ["技术约束"],
            "tags": ["领域标签"],
            "package": "交付包名（Python import 名，如 order_pricing）",
            "function_points": [
                {
                    "id": "FP-01",
                    "title": "功能点标题",
                    "description": "描述",
                    "parameters": [{"name": "参数名", "type": "类型"}],
                    "inputs": ["输入契约"],
                    "outputs": ["输出契约"],
                    "rules": ["业务规则"],
                    "samples": [{"input": "样例输入", "expect": "期望输出"}],
                    "category": "功能/异常/边界",
                    "acceptance": ["功能点级验收要点"],
                    "acceptance_criteria": ["该功能点可验证的验收要点（非空）"],
                    "source_spans": [{"clause_id": "C-01", "quote": "原始需求条款的原文片段"}],
                    "ambiguous": False,
                }
            ],
            "out_of_scope": [{"clause_id": "C-xx", "reason": "明确不做的原因（无则空数组）"}],
            "acceptance_criteria": [{"id": "AC-01", "description": "验收标准"}],
            "open_questions": [{"id": "Q-1", "question": "待澄清问题"}],
            "metrics": {"function_points": 0},
        }
    }

    def build_context(self, task: NodeTask, perception: Perception) -> Dict[str, Any]:
        source = perception.found.get("requirement_source") or {}
        text = str(source.get("text", ""))
        # L1 规则预分类（docs/15 §3.2）：确定性提示，随需求原文一起进 Prompt 供 L2 参考。
        # 只产提示不产结论——最终形态仍由模型按契约判定，本层不覆盖它。
        hint = classify_delivery_shape(text)
        self.ctx.events.emit(
            "intent_classified", agent=self.role_id, node=task.node_id, iteration=task.iteration,
            payload={"hint": hint["hint"], "explicit": hint["explicit"], "keywords": hint["keywords"]},
        )
        # 附图（docs/16 的 01 文档）：artifact 只存相对 run_dir 路径，这里还原成绝对路径供 payload 读取
        images = [
            {
                "name": item.get("name", ""),
                "path": str(self.ctx.run_dir / str(item.get("path", ""))),
                "mime": item.get("mime", ""),
                "bytes": item.get("bytes", 0),
            }
            for item in (source.get("images") or [])
            if item.get("path")
        ]
        return {"requirement_text": text, "source": source, "intent_hint": hint, "images": images}

    def build_prompt(self, task: NodeTask, perception: Perception, experience: Any) -> PromptBundle:
        """在通用 Prompt 后追加确定性条款索引（需求原文为空时不追加，Prompt 不变）。"""
        bundle = super().build_prompt(task, perception, experience)
        text = str((perception.found.get("requirement_source") or {}).get("text", ""))
        clauses = extract_clauses(text) if text.strip() else []
        if not clauses:
            return bundle
        section = _clause_index_section(clauses)
        bundle.user += "\n\n" + section
        if bundle.path:
            try:
                with bundle.path.open("a", encoding="utf-8") as fh:
                    fh.write("\n\n" + section)
            except OSError:
                pass          # 留痕失败不影响需求分析
        return bundle

    def apply(
        self,
        task: NodeTask,
        generation: Generation,
        perception: Perception,
        context: Dict[str, Any],
        result: AgentResult,
    ) -> None:
        spec = generation.data or {}
        function_points = spec.get("function_points") or []
        if not function_points:
            result.status = "fail"
            result.error = "需求规格未解析出任何功能点"
            result.summary = result.error
            return

        # L3 框架校验（docs/15 §3.2）：delivery_shape 必须在发布前恒定存在且取值受控。
        # 门禁表达式会直接引用该字段，而 expr.py 的 BoolOp 不短路求值——字段缺失会让
        # 引用它的整条 criterion 判失败（历史任务全部被误伤），所以这里是硬要求不是防御性代码。
        # 兜底取 backend_only：现有已验证能力全在该形态下，不改变任何既有行为。
        raw_shape = spec.get("delivery_shape", spec.get("delivery_mode"))
        shape, source, fell_back = resolve_delivery_shape(spec)

        # L0 项目指令声明（docs/36，对标 Codex AGENTS.md）：需求方在 AGENTS.md 的
        # front-matter 里写明的形态**覆盖**模型判定。判错的代价很高——形态决定用哪条
        # 证据通道验收，而 full_stack_spa 在本机无浏览器通道时必然不通过；此前除了改
        # intent.py 的分类规则没有任何纠正入口，现在纠正权回到需求方手里。
        declared = directive_shape(self.ctx.blackboard.get("project_docs"))
        if declared:
            # 与模型判定不一致时单独留痕：这就是「用户纠正了框架」的证据，复盘时可直接查。
            if declared != shape:
                self.ctx.events.emit(
                    "intent_declared_by_project_doc", agent=self.role_id, node=task.node_id,
                    iteration=task.iteration,
                    payload={"model_shape": shape, "declared_shape": declared,
                             "note": "项目指令文件 front-matter 声明的交付形态覆盖模型判定"},
                )
            shape, source = declared, "project_doc"

        spec["delivery_shape"] = shape
        spec["delivery_shape_source"] = source
        if fell_back:
            self.ctx.events.emit(
                "intent_fallback", agent=self.role_id, node=task.node_id, iteration=task.iteration,
                severity="warning",
                payload={
                    "raw": str(raw_shape if raw_shape is not None else ""),
                    "fallback": DEFAULT_DELIVERY_SHAPE,
                    "note": "需求规格未声明合法的 delivery_shape，按安全默认兜底为纯后端形态",
                },
            )

        # 结构完备性检查（产品策略：require_samples_per_fp）
        if self.role.policy("require_samples_per_fp", True):
            no_samples = [fp.get("id") for fp in function_points if not fp.get("samples")]
            if no_samples:
                result.status = "fail"
                result.error = f"以下功能点缺少样例: {', '.join(map(str, no_samples))}"
                result.summary = result.error
                return

        # 需求契约（门禁加固）：原文条款 → FP 映射 → 冻结签名 → 独立隐藏用例。
        # 需求原文为空只可能来自直接调用 Agent 的单元测试；生产 run 的 intake 必发原文，
        # 且门禁硬性要求已签名契约，因此退化分支无法绕过门禁。
        requirement_text = str(context.get("requirement_text") or "")
        if requirement_text.strip():
            outcome = validate_contract(spec, requirement_text)
            if not outcome["valid"]:
                result.status = "fail"
                result.error = "需求契约校验失败：" + "；".join(outcome["errors"][:8])
                result.summary = result.error
                result.metrics["contract_errors"] = outcome["errors"]
                # 被拒规格必须完整落盘：llm_traces 的 output 有 12000 字符上限，
                # 实测被拒时 trace 只剩半份 JSON，事后无法复核是哪条引文不匹配。
                rejected_path = (self.ctx.run_dir / "rejected"
                                 / f"requirement_spec_iter{task.iteration:02d}.json")
                try:
                    rejected_path.parent.mkdir(parents=True, exist_ok=True)
                    rejected_path.write_text(json.dumps(
                        {"spec": spec, "errors": outcome["errors"],
                         "coverage": outcome["coverage"]},
                        ensure_ascii=False, indent=2), encoding="utf-8")
                    rejected_rel = str(rejected_path.relative_to(self.ctx.run_dir)).replace("\\", "/")
                except OSError:
                    rejected_rel = ""
                self.ctx.events.emit(
                    "requirement_contract_rejected", agent=self.role_id, node=task.node_id,
                    iteration=task.iteration, severity="error",
                    payload={"errors": outcome["errors"][:20],
                             "coverage": outcome["coverage"],
                             "rejected_spec": rejected_rel},
                )
                return
            try:
                frozen = freeze_contract(
                    self.ctx.run_dir, spec, requirement_text,
                    run_id=str(self.ctx.run_id), config=self.ctx.config,
                    clauses=outcome["clauses"],
                )
            except ContractError as exc:
                result.status, result.error = "fail", str(exc)
                result.summary = result.error
                return
            contract_artifact = {
                "path": CONTRACT_RELATIVE.as_posix(),
                "run_id": str(frozen.get("run_id") or ""),
                "frozen_at": str(frozen.get("frozen_at") or ""),
                "requirement_hash": str(frozen.get("requirement_hash") or ""),
                "contract_hash": str(frozen.get("contract_hash") or ""),
                "signature": str(frozen.get("signature") or ""),
                "signature_alg": str(frozen.get("signature_alg") or ""),
                "clauses": frozen.get("clauses") or [],
                "out_of_scope": frozen.get("out_of_scope") or [],
                "coverage": outcome["coverage"],
                "package": outcome["package"],
                "reused": bool(frozen.get("reused")),
                "frozen": True,
            }
            self.publish(
                "requirement_contract", contract_artifact, task,
                summary=(f"条款 {outcome['coverage']['total']} 条：映射 {outcome['coverage']['mapped']}"
                         f" / 范围外 {outcome['coverage']['out_of_scope']}，已签名冻结"),
            )
            result.artifacts["requirement_contract"] = contract_artifact
            result.metrics["contract_coverage"] = outcome["coverage"]
            self.evidence(result, "contract", path=contract_artifact["path"],
                          contract_hash=contract_artifact["contract_hash"],
                          reused=contract_artifact["reused"])
            self.ctx.events.emit(
                "requirement_contract_frozen", agent=self.role_id, node=task.node_id,
                iteration=task.iteration,
                payload={"contract_hash": contract_artifact["contract_hash"],
                         "requirement_hash": contract_artifact["requirement_hash"],
                         "coverage": outcome["coverage"], "reused": contract_artifact["reused"]},
            )
            hidden = self._generate_hidden_suite(task, frozen, outcome, result)
            if hidden is None and result.status == "fail":
                return
            if hidden is not None:
                self.publish(
                    "hidden_suite", hidden, task,
                    summary=(f"隐藏用例 {hidden.get('case_count', 0)} 条（契约 "
                             f"{str(hidden.get('manifest_digest', ''))[:12]}…，"
                             f"存放在 run 工作区之外）"),
                )
                result.artifacts["hidden_suite"] = hidden
                result.metrics["hidden_cases"] = int(hidden.get("case_count") or 0)

        self.publish(
            "requirement_spec", spec, task,
            summary=(f"功能点 {len(function_points)} 个，验收标准 {len(spec.get('acceptance_criteria', []))} 条，"
                     f"待澄清 {len(spec.get('open_questions', []))} 项；交付形态 {shape}"
                     f"（{source}）"),
        )
        result.status = "pass"
        result.artifacts["requirement_spec"] = spec
        result.metrics["delivery_shape"] = shape
        result.summary = (
            f"完成需求评估：{spec.get('title', '')} → 功能点 {len(function_points)} 个"
            f"（含异常/边界要求 {sum(1 for fp in function_points if fp.get('category') != '功能')}），"
            f"验收标准 {len(spec.get('acceptance_criteria', []))} 条；交付形态 {shape}"
        )
        result.events = ["requirement_ready"]
        result.broadcast_payload = {
            "summary": result.summary,
            "function_points": len(function_points),
            "open_questions": len(spec.get("open_questions", [])),
            "delivery_shape": shape,
        }
        self.evidence(result, "artifact", name="requirement_spec", note="需求规格已发布至黑板")
        self.evidence(result, "source", note="输入来源 requirement_source", chars=len(context.get("requirement_text", "")))
        self.ctx.events.emit(
            "requirement_ready", agent=self.role_id, node=task.node_id, iteration=task.iteration,
            payload=result.broadcast_payload,
        )

    def _dump_rejected_hidden(
        self, task: NodeTask, attempt: int, error: str, generation: Generation,
    ) -> str:
        """被拒的隐藏套件全量落盘（对齐 requirement_contract_rejected 的做法）。

        llm_traces 对 output 有 12000 字符上限，且隐藏生成不经过 _trace_llm；
        不落盘的话失败现场零留痕，事后无法复核模型到底输出了什么。
        """
        rejected = (self.ctx.run_dir / "rejected"
                    / f"hidden_suite_iter{task.iteration:02d}_attempt{attempt:02d}.json")
        try:
            rejected.parent.mkdir(parents=True, exist_ok=True)
            rejected.write_text(json.dumps({
                "error": error,
                "data": generation.data,
                "raw": generation.text or "",
                "finish_reason": getattr(generation, "finish_reason", ""),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            return str(rejected.relative_to(self.ctx.run_dir)).replace("\\", "/")
        except OSError:
            return ""

    # ---------------------------------------------------------------- 隐藏用例
    def _generate_hidden_suite(
        self,
        task: NodeTask,
        frozen: Dict[str, Any],
        validation: Dict[str, Any],
        result: AgentResult,
    ) -> Optional[Dict[str, Any]]:
        """用独立上下文（只含冻结契约）生成隐藏用例并冻结到 run 工作区之外。

        返回 None 表示失败（``result.status == "fail"``）或跳过（单元测试无 LLM 客户端）。
        """
        if self.ctx.llm is None:
            self.ctx.events.emit(
                "hidden_suite_skipped", agent=self.role_id, node=task.node_id,
                iteration=task.iteration, severity="warning",
                payload={"reason": "当前上下文没有 LLM 客户端（直接调用 Agent 的单元测试），跳过隐藏用例生成"},
            )
            return None
        frozen_spec = frozen.get("spec") if isinstance(frozen.get("spec"), dict) else {}
        package = str(validation.get("package") or frozen_spec.get("package") or "")
        system, user = build_generation_prompt(
            spec=frozen_spec,
            clauses=list(frozen.get("clauses") or []),
            package=package,
            delivery_shape=str(frozen_spec.get("delivery_shape") or ""),
        )
        prompt_dir = self.ctx.run_dir / "prompts"
        prompt_dir.mkdir(parents=True, exist_ok=True)
        base_prompt_path = prompt_dir / f"{task.node_id}_hidden_iter{task.iteration:02d}.md"
        context = {
            "spec": frozen_spec,
            "clauses": list(frozen.get("clauses") or []),
            "package": package,
            "policies": dict(self.role.policies),
        }
        # 结构校验失败是有界可修复的：把错误清单回灌给模型重生成整份 JSON。
        # task09-miniledger-03 实测：模型漏写 FP-14 一条用例，一次性生成直接
        # hidden_case_missing:FP-14 判 fail（整轮 1690s 作废）；同类错误还包括
        # 方法名非法、断言为空——这些都是「输出没对齐协议」，不是需求不可实现。
        max_attempts = max(1, int(self.ctx.config.runtime.get("hidden_suite_max_attempts", 2) or 2))
        attempt = 1
        repaired: List[str] = []
        while True:
            prompt_user = user
            prompt_path = base_prompt_path
            if attempt > 1:
                prompt_user = user + "\n\n" + self._hidden_repair_block(repaired, attempt - 1)
                prompt_path = base_prompt_path.with_name(
                    f"{base_prompt_path.stem}_repair{attempt - 1:02d}.md")
            prompt_path.write_text(
                f"<<<SYSTEM>>>\n{system}\n<<<USER>>>\n{prompt_user}", encoding="utf-8")
            bundle = PromptBundle(
                role=self.role_id, node=task.node_id, task_kind="hidden_test_suite",
                iteration=task.iteration, system=system, user=prompt_user, context=context,
                token_estimate=(len(system) + len(prompt_user)) // 3,
                path=prompt_path,
            )
            try:
                generation = self.ctx.llm.generate("hidden_test_suite", bundle, context)
            except Exception as exc:  # noqa: BLE001 - 生成失败必须让节点明确失败
                result.status = "fail"
                result.error = f"隐藏用例生成失败: {type(exc).__name__}: {exc}"
                result.summary = result.error
                return None
            result.metrics["hidden_generation"] = generation.to_dict()
            result.metrics["hidden_generation_attempts"] = attempt
            if generation.error or generation.data is None:
                result.status = "fail"
                result.error = generation.error or "隐藏用例生成未返回可解析的 JSON"
                result.summary = result.error
                self.ctx.events.emit(
                    "hidden_suite_generation_failed", agent=self.role_id, node=task.node_id,
                    iteration=task.iteration, severity="error",
                    payload={"error": result.error, "prompt": str(prompt_path),
                             "attempt": attempt,
                             "rejected": self._dump_rejected_hidden(
                                 task, attempt, result.error, generation)},
                )
                return None
            try:
                artifact = create_hidden_suite(
                    config=self.ctx.config,
                    run_dir=self.ctx.run_dir,
                    sandbox=self.ctx.sandbox,
                    run_id=str(self.ctx.run_id),
                    spec=frozen_spec,
                    contract_hash=str(frozen.get("contract_hash") or ""),
                    data=generation.data,
                    iteration=task.iteration,
                    package=package,
                    generation=generation.to_dict(),
                )
                break
            except HiddenSuiteError as exc:
                if attempt >= max_attempts:
                    result.status = "fail"
                    result.error = f"隐藏用例冻结失败: {exc}"
                    result.summary = result.error
                    self.ctx.events.emit(
                        "hidden_suite_generation_failed", agent=self.role_id, node=task.node_id,
                        iteration=task.iteration, severity="error",
                        payload={"error": result.error, "prompt": str(prompt_path),
                                 "attempt": attempt, "max_attempts": max_attempts,
                                 "rejected": self._dump_rejected_hidden(
                                     task, attempt, str(exc), generation)},
                    )
                    return None
                repaired.append(str(exc))
                self.ctx.events.emit(
                    "hidden_suite_repair_requested", agent=self.role_id, node=task.node_id,
                    iteration=task.iteration, severity="warning",
                    payload={"attempt": attempt + 1, "errors": str(exc)[:500],
                             "prompt": str(prompt_path)},
                )
                attempt += 1
                continue
        self.evidence(
            result, "hidden_suite", path=str(artifact.get("workspace_relative") or ""),
            test_file=str(artifact.get("test_file") or ""),
            manifest_digest=str(artifact.get("manifest_digest") or ""),
            test_ids=len(artifact.get("test_ids") or []),
            contract_hash=str(artifact.get("contract_hash") or ""),
        )
        self.ctx.events.emit(
            "hidden_suite_generated", agent=self.role_id, node=task.node_id,
            iteration=task.iteration,
            payload={
                "root": str(artifact.get("root") or ""),
                "test_file": str(artifact.get("test_file") or ""),
                "manifest_digest": str(artifact.get("manifest_digest") or ""),
                "test_ids": len(artifact.get("test_ids") or []),
                "contract_hash": str(artifact.get("contract_hash") or ""),
                "prompt": str(prompt_path),
                "attempts": attempt,
                "note": "隐藏用例仅依据冻结契约生成，存放在 run 工作区之外",
            },
        )
        return artifact

    # 结构校验错误键 → 针对性修复指引。只回灌裸错误码时模型不知道「怎么改」，
    # task09-miniledger-04 实测：第 1 轮修好了类结构却引入方法名重复，两轮耗尽整轮作废。
    _REPAIR_HINTS = (
        ("hidden_suite_no_test_methods",
         "测试文件必须是 unittest 风格：定义继承 unittest.TestCase 的类，用例写成类方法 "
         "def test_xxx(self)；pytest 风格的模块级 test_ 函数不会被框架枚举"),
        ("hidden_file_syntax_error",
         "files 里每份 content 都必须是可被 Python 解析的完整源码（不要转义/折行/省略）"),
        ("hidden_case_method_missing",
         "cases 声明的每个用例都必须在 TestCase 类里有同名方法 test_{category}_{op}_{name}，一一对应"),
        ("hidden_method_without_case",
         "文件里每个 test_* 类方法都必须在 cases 中有对应条目；多余方法删除或补齐元数据"),
        ("hidden_case_duplicate_method",
         "同一 (category, op, name) 只能用一次；不同功能点的相似场景给 name 加区分后缀（如 _fp06/_fp10）"),
        ("hidden_case_unknown_fp", "fp_id 只能引用契约 function_points 里真实存在的 id"),
        ("hidden_case_missing", "cases 必须逐个覆盖契约的全部功能点（fp_id 逐个核对）"),
        ("hidden_functional_case_missing", "每个功能点都必须有 functional 用例（不限 FP 类别，验收门禁按 functional 覆盖率 100% 核对）"),
        ("hidden_case_invalid_category", "category 只能是 functional / exception / boundary"),
        ("hidden_case_name_not_ascii", "op 与 name 只含 ASCII 字母、数字、下划线"),
    )

    @staticmethod
    def _hidden_repair_block(errors: List[str], repair_index: int) -> str:
        listed = "\n".join(f"- {item}" for item in errors) or "- （无错误详情）"
        hints = [
            hint for key, hint in ProductAgent._REPAIR_HINTS
            if any(key in item for item in errors)
        ]
        hint_block = ("\n\n针对性修复指引：\n" + "\n".join(f"- {h}" for h in hints)) if hints else ""
        return (
            f"## 上一次输出未通过框架校验（第 {repair_index} 次修复）\n\n"
            f"{listed}\n"
            f"{hint_block}\n\n"
            "请输出**修正后的完整 JSON**（files 与 cases 全量重写，不要输出差异或省略内容），"
            "逐条消除上述错误；cases 必须覆盖冻结契约中的每一个功能点。"
        )
