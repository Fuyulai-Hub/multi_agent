"""研发 Agent：技术方案设计 / 编码实现 / 缺陷修复（含回归自测报告）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from ..intent import probe_delivery_shape, resolve_delivery_shape
from ..llm import Generation
from ..schemas import AgentResult, NodeTask
from ..verifier import TestVerifier, VerificationError
from ..verify.exec import execute_frozen_suite
from .base import BaseAgent, Perception

__all__ = ["DeveloperAgent"]


class DeveloperAgent(BaseAgent):
    role_id = "developer"
    task_kinds = {
        "default": "implementation",
        "solution": "solution_design",
        "implement": "implementation",
        "optimize": "implementation",
    }
    contracts = {
        "solution": {
            "tech_choice": {"language": "", "dependencies": [], "rationale": ""},
            "candidates": [{"name": "", "scores": {}, "pros": [], "cons": []}],
            "evaluation": {"method": "", "selected": "", "reason": ""},
            "design": {"architecture": "", "modules": [], "error_strategy": "", "testing_strategy": "",
                       "ambiguity_resolutions": []},
            "risks": [{"risk": "", "mitigation": ""}],
            "implementation_plan": [],
            "requirement_coverage": [{"id": "FP-01", "target": "模块.函数"}],
        },
        "default": {
            "mode": "first|rework|fix",
            "package": "",
            "files": [{"path": "", "content": ""}],
            "fp_mapping": {"FP-01": "pkg/core.py::function_name"},
            "functions": [],
            "defects_injected": [],
            "fix_targets": [],
            "notes": "",
        },
    }

    # ---------------------------------------------------------------- 模式判定
    def _mode(self, perception: Perception, node_id: str = "implement") -> str:
        if node_id == "optimize":
            # 修复/优化节点永远走 fix 通道：绝不重新引入已修复的缺陷
            return "fix"
        bugs = perception.found.get("bug_reports") or {}
        if int(bugs.get("open", 0) or 0) > 0:
            return "fix"
        review = perception.found.get("review_report") or {}
        if review.get("verdict") == "reject":
            return "rework"
        return "rework" if perception.found.get("code_manifest") else "first"

    # ---------------------------------------------------------------- 上下文
    def build_context(self, task: NodeTask, perception: Perception) -> Dict[str, Any]:
        spec = perception.spec
        bugs = perception.found.get("bug_reports") or {}
        review = perception.found.get("review_report") or {}
        if task.node_id == "solution":
            return {"spec": spec}
        return {
            "spec": spec,
            "mode": self._mode(perception, task.node_id),
            "open_bugs": [item for item in (bugs.get("items") or []) if item.get("status") == "open"],
            "review_findings": review.get("blocking_items") or review.get("findings") or [],
            "test_report": perception.found.get("test_report") or {},
            "node": task.node_id,
            "iteration": task.iteration,
        }

    # ---------------------------------------------------------------- 执行
    def apply(
        self,
        task: NodeTask,
        generation: Generation,
        perception: Perception,
        context: Dict[str, Any],
        result: AgentResult,
    ) -> None:
        if task.node_id == "solution":
            self._apply_solution(task, generation, perception, result)
        elif task.node_id == "optimize":
            self._apply_fix(task, generation, perception, context, result)
        else:
            self._apply_implementation(task, generation, perception, context, result)

    # ---------------------------------------------------------------- 方案设计
    def _apply_solution(self, task: NodeTask, generation: Generation, perception: Perception,
                        result: AgentResult) -> None:
        design = generation.data or {}
        evaluation = design.get("evaluation") or {}
        if not evaluation.get("selected"):
            result.status, result.error = "fail", "技术方案缺少选型结论（evaluation.selected）"
            result.summary = result.error
            return
        coverage = design.get("requirement_coverage") or []
        spec_fps = [fp.get("id") for fp in perception.spec.get("function_points", [])]
        covered = {item.get("id") for item in coverage}
        if spec_fps and set(spec_fps) - covered:
            result.status = "fail"
            result.error = f"方案未覆盖功能点: {', '.join(sorted(set(spec_fps) - covered))}"
            result.summary = result.error
            return
        self.publish(
            "solution_design", design, task,
            summary=f"选型『{evaluation.get('selected')}』，候选 {len(design.get('candidates', []))} 个，"
                    f"风险 {len(design.get('risks', []))} 项",
        )
        result.status = "pass"
        result.artifacts["solution_design"] = design
        result.summary = (
            f"技术方案完成：选型『{evaluation.get('selected')}』（候选 {len(design.get('candidates', []))} 个、"
            f"模块 {len((design.get('design') or {}).get('modules', []))} 个、"
            f"风险 {len(design.get('risks', []))} 项），功能点覆盖 {len(covered)}/{len(spec_fps)}"
        )
        result.events = ["design_ready"]
        result.broadcast_payload = {"summary": result.summary, "selected": evaluation.get("selected")}
        self.ctx.events.emit(
            "design_ready", agent=self.role_id, node=task.node_id, iteration=task.iteration,
            payload=result.broadcast_payload,
        )

    # ---------------------------------------------------------------- 编码实现
    def _write_files(self, files: List[Dict[str, Any]], task: NodeTask, result: AgentResult) -> List[str]:
        written = []
        for file_item in files or []:
            path = self.sandbox_write(str(file_item["path"]), str(file_item["content"]))
            written.append(str(file_item["path"]))
            self.evidence(result, "file", path=str(file_item["path"]), chars=len(str(file_item["content"])))
        return written

    @staticmethod
    def _self_check_label(check: Dict[str, Any]) -> str:
        if check.get("unsupported"):
            return "非 Python 交付不支持（缺工具链，fail closed）"
        if check.get("static_assets"):
            return "静态资源交付（无编译型自检，验证通道为浏览器 probe）"
        return "导入自检通过" if check.get("ok") else "导入自检失败"

    def _package_importable_dir(self, package: str) -> bool:
        """沙箱工作区里是否已存在该 Python 包（上一轮交付的包目录或同名 .py）。

        目录必须真的含 .py 才算：JS/静态交付也可能建出与 package 同名的目录，
        Python 3 会把它当命名空间包 import 成功，让非 Python 交付借导入自检放行。
        """
        sandbox = getattr(self.ctx, "sandbox", None)
        root = getattr(sandbox, "root", None)
        if not root or not package:
            return False
        root = Path(root)
        pkg_dir = root / package
        if pkg_dir.is_dir():
            return any(pkg_dir.rglob("*.py"))
        return (root / f"{package}.py").exists()

    @staticmethod
    def _is_test_file(name: str) -> bool:
        """测试文件形态：tests/ 目录下或 test_*.py（它们不构成可导入的交付包）。"""
        path = str(name).replace("\\", "/")
        while path.startswith("./"):
            path = path[2:]
        return path.startswith("tests/") or path.rsplit("/", 1)[-1].startswith("test_")

    # 交付文件后缀 → 语言（只做非 Python 部分的工具链判定）
    _LANGUAGE_BY_SUFFIX = {
        ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
        ".ts": "typescript", ".tsx": "typescript",
        ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".hpp": "cpp",
        ".java": "java", ".go": "go", ".rs": "rust", ".rb": "ruby", ".php": "php", ".cs": "csharp",
    }
    # 语言 → (可执行文件候选, 参数模板)：命中即用真实工具链做语法级校验；
    # node 在沙箱白名单内（system.yaml sandbox_command_policy.allowed），g++/clang 需
    # 同时满足"本机存在 + 已加入白名单"才会执行，否则走 unsupported fail closed。
    _TOOLCHAINS = {
        "javascript": (["node"], ["--check"]),
        "c": (["gcc", "clang"], ["-fsyntax-only"]),
        "cpp": (["g++", "clang++"], ["-fsyntax-only"]),
    }
    _STATIC_ASSET_SUFFIXES = (".html", ".htm", ".css", ".json", ".md", ".txt", ".svg", ".yml", ".yaml")

    def _toolchain_command(self, language: str, file_name: str):
        """返回 (cmd, reason)：找到可用工具链给 cmd，否则 cmd=None + 不可用原因。"""
        entry = self._TOOLCHAINS.get(language)
        if entry is None:
            return None, f"没有 {language} 的编译/校验工具链映射"
        import shutil

        for binary in entry[0]:
            if not shutil.which(binary):
                continue
            sandbox = getattr(self.ctx, "sandbox", None)
            if (getattr(sandbox, "command_policy_enabled", False)
                    and binary not in set(getattr(sandbox, "allowed_commands", ()) or ())):
                return None, f"{binary} 在 PATH 中但不在沙箱命令白名单"
            return [binary, *entry[1], file_name], ""
        return None, f"PATH 中未找到 {'/'.join(entry[0])}"

    def _nonpython_check(self, deliverables: List[str]) -> Dict[str, Any]:
        """非 Python 交付：有工具链就真实执行校验，没有就 fail closed 标 unsupported。

        历史漏洞（task04 C++）：原来这里返回 not_applicable + ok=True，`.hpp` 从未被
        编译就让 implement 节点放行。现在：JS 走 `node --check`（白名单内、真实执行），
        C/C++ 走 g++/clang -fsyntax-only（需本机存在且入白名单），其余语言或工具链
        缺失一律 ok=False + unsupported=True——节点判 fail，绝不空证据放行。
        """
        per_file: List[Dict[str, Any]] = []
        unsupported: List[str] = []
        checked = 0
        all_ok = True
        for name in deliverables:
            suffix = Path(str(name)).suffix.lower()
            language = self._LANGUAGE_BY_SUFFIX.get(suffix, "")
            if not language:
                continue          # html/css/json 等静态资源：无编译型校验，见 static_assets
            cmd, reason = self._toolchain_command(language, str(name))
            if cmd is None:
                unsupported.append(f"{name}（{language}）：{reason}")
                all_ok = False
                per_file.append({"file": name, "language": language, "ok": False,
                                 "cmd": "", "reason": reason})
                continue
            try:
                outcome = self.sandbox_run(cmd)
            except Exception as exc:  # noqa: BLE001 - SandboxError/权限拒绝都按不可验证计
                unsupported.append(f"{name}（{language}）：{type(exc).__name__}: {exc}")
                all_ok = False
                per_file.append({"file": name, "language": language, "ok": False,
                                 "cmd": " ".join(cmd), "reason": str(exc)[:200]})
                continue
            checked += 1
            file_ok = bool(outcome.ok)
            all_ok = all_ok and file_ok
            per_file.append({
                "file": name, "language": language, "ok": file_ok,
                "cmd": " ".join(cmd), "exit_code": outcome.exit_code,
                "reason": (outcome.stderr.strip()[-200:] if not file_ok else ""),
            })
        if not per_file:
            # 纯静态资源（html/css/json…）：沙箱里没有可执行校验，但也不允许伪装成
            # "已验证"——如实标注 static_assets，真正的验证通道是 tester 的浏览器 probe
            # （文件探测会把这类交付收紧为 full_stack_spa，SPA 无浏览器证据必判失败）。
            return {
                "cmd": "", "exit_code": 0, "ok": True, "static_assets": True,
                "note": "纯静态资源交付：无编译型自检，验证由浏览器 probe 承担",
                "files": [], "duration_ms": 0,
            }
        return {
            "cmd": "; ".join(item["cmd"] for item in per_file if item["cmd"]),
            "exit_code": 0 if all_ok else 1, "ok": all_ok,
            "unsupported": bool(unsupported),
            "note": ("非 Python 交付工具链校验"
                     + (f"：{len(unsupported)} 个文件无工具链可验证，fail closed" if unsupported else "")),
            "files": per_file,
            "stdout_tail": "", "stderr_tail": "; ".join(unsupported)[:200], "duration_ms": 0,
        }

    def _self_check(self, package: str, task: NodeTask,
                    files: Optional[List[str]] = None) -> Dict[str, Any]:
        deliverables = [name for name in (files or []) if not self._is_test_file(name)]
        deliverable_py = [name for name in deliverables if str(name).endswith(".py")]
        if files is not None and not deliverable_py and not self._package_importable_dir(package):
            # 非 Python 交付物：`python -c import x` 必然 ModuleNotFoundError，跑它只会
            # 让失败点失真。改为工具链判定——能真实校验就校验，不能就 unsupported 判失败。
            # 只看"交付包自身文件"：optimize 轮次连带重写的 tests/test_*.py 不参与判定。
            return self._nonpython_check(deliverables)
        check = self.sandbox_run(
            [self.ctx.sandbox.python, "-c", f"import {package}; print('import-ok')"],
            env_extra={"PYTHONPATH": str(self.ctx.sandbox.root)},
        )
        outcome = {
            "cmd": " ".join(check.cmd),
            "exit_code": check.exit_code,
            "ok": check.ok,
            "stdout_tail": check.stdout.strip()[-200:],
            "stderr_tail": check.stderr.strip()[-200:],
            "duration_ms": check.duration_ms,
        }
        # 混合交付（Python + JS/C++ 等同轮出现）：导入自检之外，非 Python 代码文件
        # 同样要过工具链校验，任何一侧不可验证都整体 fail closed。
        code_non_py = [name for name in deliverables
                       if Path(str(name)).suffix.lower() in self._LANGUAGE_BY_SUFFIX]
        if code_non_py:
            extra = self._nonpython_check(code_non_py)
            outcome["toolchain_check"] = extra
            outcome["ok"] = bool(outcome["ok"] and extra["ok"])
            outcome["unsupported"] = bool(extra.get("unsupported"))
        return outcome

    def _shape_probe(self, spec: Dict[str, Any], written: List[str]) -> Dict[str, Any]:
        """交付形态文件探测（审计留痕）：声明形态 vs 沙箱真实文件探测形态。"""
        declared, _, _ = resolve_delivery_shape(spec or {})
        probe = probe_delivery_shape(getattr(self.ctx.sandbox, "root", None), written)
        return {"declared": declared, "probed": probe.get("shape") or "",
                "evidence": probe.get("evidence") or []}

    def _apply_implementation(
        self, task: NodeTask, generation: Generation, perception: Perception,
        context: Dict[str, Any], result: AgentResult,
    ) -> None:
        data = generation.data or {}
        files = data.get("files") or []
        if not files:
            result.status, result.error = "fail", "实现未产出任何文件"
            result.summary = result.error
            return
        written = self._write_files(files, task, result)
        package = data.get("package") or self.ctx.config.llm.get("offline", {}).get("package_name", "delivery")
        self_check = self._self_check(package, task, written)
        stats = self.ctx.sandbox.code_stats(written)
        manifest = {
            "package": package,
            "files": written,
            "loc": stats.get("total_loc", 0),
            "functions": data.get("functions", []),
            "fp_mapping": data.get("fp_mapping", {}),
            "self_check": self_check,
            "shape_probe": self._shape_probe(perception.spec, written),
            "mode": data.get("mode", context.get("mode")),
            "defects_injected": data.get("defects_injected", []),
            "notes": data.get("notes", ""),
            "file_stats": stats.get("files", []),
        }
        summary_data = {
            "mode": manifest["mode"],
            "files": written,
            "loc": manifest["loc"],
            "fp_mapping": manifest["fp_mapping"],
            "self_check": self_check,
            "notes": data.get("notes", ""),
        }
        self.publish("code_manifest", manifest, task, summary=f"{len(written)} 个文件，{manifest['loc']} 行")
        self.publish("implementation_summary", summary_data, task, summary=f"模式 {manifest['mode']}")
        result.artifacts["code_manifest"] = manifest
        result.artifacts["implementation_summary"] = summary_data
        result.status = "pass" if self_check.get("ok") else "fail"
        result.summary = (
            f"编码实现（{manifest['mode']}）：{len(written)} 个文件、{manifest['loc']} 行代码、"
            f"功能点映射 {len(manifest['fp_mapping'])} 个；{self._self_check_label(self_check)}"
        )
        result.events = ["implementation_ready"]
        result.broadcast_payload = {
            "summary": result.summary,
            "files": written,
            "self_check_ok": self_check.get("ok", False),
        }
        self.ctx.events.emit(
            "implementation_ready", agent=self.role_id, node=task.node_id, iteration=task.iteration,
            payload={"files": written, "loc": manifest["loc"], "mode": manifest["mode"]},
        )
        if not self_check.get("ok"):
            if self_check.get("unsupported"):
                result.error = ("交付语言不受支持（unsupported，fail closed）："
                                + (self_check.get("stderr_tail") or self_check.get("note", ""))[:200])
            else:
                result.error = f"导入自检失败: {self_check.get('stderr_tail', '')[:200]}"

    # ---------------------------------------------------------------- 缺陷修复
    def _apply_fix(
        self, task: NodeTask, generation: Generation, perception: Perception,
        context: Dict[str, Any], result: AgentResult,
    ) -> None:
        data = generation.data or {}
        files = data.get("files") or []
        if not files:
            result.status, result.error = "fail", "修复未产出任何文件"
            result.summary = result.error
            return
        written = self._write_files(files, task, result)
        package = data.get("package") or (perception.found.get("code_manifest") or {}).get("package", "delivery")
        self_check = self._self_check(package, task, written)

        open_bugs = context.get("open_bugs") or []
        test_report = perception.found.get("test_report") or {}
        test_file = test_report.get("test_file", "")
        try:
            frozen_suite = TestVerifier(
                self.ctx.run_dir, self.ctx.sandbox.root, events=self.ctx.events,
                config=self.ctx.config, sandbox=self.ctx.sandbox,
                run_id=getattr(self.ctx, "run_id", ""),
            ).load_manifest()
        except VerificationError:
            frozen_suite = None
        fix_items: List[Dict[str, Any]] = []
        all_ok = bool(self_check.get("ok"))
        for bug in open_bugs:
            item = {
                "bug_id": bug.get("id"),
                "fp_id": bug.get("fp_id"),
                "op": bug.get("op", ""),
                "category": bug.get("category"),
                "strategy": (data.get("fix_strategies") or {}).get(bug.get("op", ""), "按需求契约语义修正实现"),
                "regression": None,
                "regression_ok": None,
            }
            if frozen_suite is not None and bug.get("test"):
                # 定向回归同样走框架 runner：--only 只在冻结清单内过滤，
                # 测试文件没有 main/marker，不能再自行决定跑哪些 case。
                execution = execute_frozen_suite(
                    self.ctx.sandbox, self.ctx.run_dir, frozen_suite,
                    iteration=task.iteration, only=str(bug["test"]), tag="developer",
                )
                regression = execution.exec
                parsed = execution.result or {}
                item["regression"] = {
                    "cmd": " ".join(regression.cmd),
                    "exit_code": regression.exit_code,
                    "failed": parsed.get("failed", regression.exit_code),
                    "stdout_tail": regression.stdout.strip()[-400:],
                    # stderr 同样留痕：测试文件 import 崩溃（修复改坏公开接口）时
                    # traceback 在 stderr，只有 stdout_tail 会看到空输出无法诊断
                    "stderr_tail": regression.stderr.strip()[-400:],
                }
                regression_ok = bool(regression.ok) and int(parsed.get("total") or 0) > 0
                item["regression_ok"] = regression_ok
                all_ok = all_ok and regression_ok
            elif test_report:
                item["regression"] = {
                    "cmd": "", "exit_code": 2, "failed": None,
                    "error": "没有可用的冻结测试清单：拒绝用测试文件自行回传的结果做回归",
                }
                item["regression_ok"] = False
                all_ok = False
            fix_items.append(item)
            self.ctx.events.emit(
                "bug_fixed", agent=self.role_id, node=task.node_id, iteration=task.iteration,
                payload={
                    "bug_id": bug.get("id"),
                    "fp_id": bug.get("fp_id"),
                    "op": bug.get("op"),
                    "strategy": item["strategy"],
                    "regression_ok": item["regression_ok"],
                },
            )

        stats = self.ctx.sandbox.code_stats(written)
        manifest = {
            "package": package,
            "files": written,
            "loc": stats.get("total_loc", 0),
            "functions": data.get("functions", []),
            "fp_mapping": data.get("fp_mapping", {}),
            "self_check": self_check,
            "shape_probe": self._shape_probe(perception.spec, written),
            "mode": "fix",
            "defects_injected": [],
            "notes": data.get("notes", ""),
            "file_stats": stats.get("files", []),
        }
        fix_summary = {
            "fixed": len(fix_items),
            "items": fix_items,
            "self_check": self_check,
            "fix_targets": data.get("fix_targets", []),
        }
        fix_test_report = {
            "title": "缺陷修复回归自测报告（研发 Agent）",
            "scope": [bug.get("id") for bug in open_bugs],
            "results": [
                {
                    "bug_id": item["bug_id"],
                    "test": next((bug.get("test") for bug in open_bugs if bug.get("id") == item["bug_id"]), ""),
                    "regression_ok": item["regression_ok"],
                    "evidence": item["regression"],
                }
                for item in fix_items
            ],
            "all_pass": all_ok,
            "conclusion": ("全部缺陷的定向回归用例通过，等待测试 Agent 复测确认。"
                           if all_ok else "存在回归未通过的缺陷，需继续修复。"),
        }
        self.publish("code_manifest", manifest, task, summary=f"修复后 {len(written)} 个文件，{manifest['loc']} 行")
        self.publish("fix_summary", fix_summary, task, summary=f"修复 {len(fix_items)} 个缺陷")
        self.publish("fix_test_report", fix_test_report, task, summary="回归自测报告")
        result.artifacts.update({
            "code_manifest": manifest,
            "fix_summary": fix_summary,
            "fix_test_report": fix_test_report,
        })
        result.status = "pass" if all_ok else "fail"
        result.summary = (
            f"缺陷修复：处理 {len(fix_items)} 个缺陷（定向回归 "
            f"{sum(1 for item in fix_items if item['regression_ok'])}/{len(fix_items)} 通过），"
            f"{self._self_check_label(self_check)}；已产出回归自测报告"
        )
        result.events = ["bug_fixed"]
        result.broadcast_payload = {"summary": result.summary, "fixed": [item["bug_id"] for item in fix_items]}
        self.ctx.events.emit(
            "bug_fixed", agent=self.role_id, node=task.node_id, iteration=task.iteration,
            payload={"fixed": len(fix_items), "all_regression_ok": all_ok},
        )

    # ---------------------------------------------------------------- 协商应答
    def consult(self, question: str, fp_id: str = "") -> Dict[str, Any]:
        """响应其他 Agent 的澄清请求（Agent 间双向通信）。"""
        prompt = self.ctx.prompts.build(
            role=self.role,
            node=type("N", (), {"id": "consultation", "title": "需求歧义澄清", "role": "developer", "kind": "task"})(),
            task_kind="consultation",
            iteration=0,
            directive=f"请回答测试 Agent 的澄清请求（涉及 {fp_id or '需求整体'}）",
            artifacts_brief="",
            bus_digest=self.ctx.bus.digest(self.role_id),
            perception=["requirement_spec"],
            missing=[],
            experience=self.ctx.experience.retrieve(
                role=self.role_id, tags=list(self.role.learning_tags) + ["需求歧义"],
                task_text=question, tier_keys=["L2", "L3"], current_run=self.ctx.run_id,
            ),
            contract={"question": "原始问题", "answer": "结论", "impact": "对实现与测试的影响"},
            context={"question": question, "fp_id": fp_id},
        )
        generation = self.ctx.llm.generate("consultation", prompt, {"question": question, "fp_id": fp_id})
        answer = generation.data or {"answer": "（无法澄清）"}
        answer.setdefault("question", question)
        answer.setdefault("fp_id", fp_id)
        return answer
