"""Reviewer Agent：代码审查 / 技术方案评审 / 验收签署。"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ..intent import resolve_delivery_shape, shape_family
from ..llm import Generation
from ..schemas import AgentResult, NodeTask
from ..verifier import FROZEN_DIR, code_tree_hash
from .base import BaseAgent, Perception

__all__ = ["ReviewerAgent"]

# 功能点映射的声明格式：value 必须严格是「沙箱内相对路径::符号」。
# 不再接受裸函数名（旧格式），也不接受一个 FP 对多个符号的拼接值。
_MAPPING_SEPARATOR = "::"


def _normalize_rel(path: Any) -> str:
    value = str(path or "").strip().replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    return value


def _mapping_parts(value: Any) -> Tuple[str, str, str]:
    """解析 file::symbol；返回 (file, symbol, error)。"""
    if not isinstance(value, str):
        return "", "", "映射值必须是字符串 file::symbol"
    parts = value.split(_MAPPING_SEPARATOR)
    if len(parts) != 2:
        return "", "", "映射值必须恰好是一个 file::symbol（禁止裸函数名或多值拼接）"
    file, symbol = parts[0].strip(), parts[1].strip()
    if not file or not symbol:
        return "", "", "file 或 symbol 为空"
    file = _normalize_rel(file)
    if not file or file.startswith("/") or ":" in file:
        return "", "", "file 必须是沙箱内相对路径"
    if any(segment in ("", ".", "..") for segment in file.split("/")):
        return "", "", "file 必须是沙箱内相对路径（禁止 . / .. / 空段）"
    if not all(part.isidentifier() for part in symbol.split(".")):
        return "", "", "symbol 必须是以点分隔的合法标识符（如 func 或 Class.method）"
    return file, symbol, ""


def _module_name(file: str) -> str:
    parts = file[:-3].split("/") if file.endswith(".py") else file.split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(part for part in parts if part)


def _defined_symbols(tree: ast.AST) -> Set[str]:
    """模块级函数/类，以及类的方法（Class.method）。"""
    names: Set[str] = set()

    def visit(body: List[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{node.name}"
                names.add(name)
                if isinstance(node, ast.ClassDef):
                    visit(node.body, f"{name}.")

    visit(tree.body, "")
    return names


class ReviewerAgent(BaseAgent):
    role_id = "reviewer"
    task_kinds = {
        "default": "code_review",
        "review": "code_review",
        "solution_review": "design_review",
        "acceptance": "acceptance_signoff",
    }
    contracts = {
        "review": {
            "reviewed_files": [],
            "findings": [{"rule": "", "severity": "critical|major|minor|info", "file": "", "line": 0,
                          "symbol": "", "message": ""}],
            "blocking": 0,
            "stats": {"by_severity": {}, "by_rule": {}},
            "coverage": {"function_points": 0, "implemented": 0, "missing": []},
            "code_hash": "（框架按沙箱最终代码写入，模型留空）",
            "mapping_audit": {"format": "file::symbol", "total": 0, "valid": 0, "invalid": []},
            "verdict": "pass|reject",
            "summary": "",
            "recommendations": [],
        },
        "solution_review": {"findings": [], "blocking": 0, "verdict": "pass|reject", "summary": ""},
        "acceptance": {"verdict": "pass|reject", "opinion": "", "blocking_reasons": []},
        "default": {},
    }

    #: tests/ 目录的风格规则豁免集（可经 reviewer.yaml 的 test_style_rules_exempt 覆盖）。
    #: 测试文件在首轮测试后被框架冻结（verifier.restore_if_changed 会还原开发者的改动，
    #: tester 复测时也忽略新生成的 tests/），风格类阻塞项对开发者在后续迭代中不可修复，
    #: 计入 blocking 会与回退上限构成死锁（task09-miniledger-04 实测：54 条 docstring
    #: 阻塞项连续驳回，开发者两次修复都被冻结副本还原）。危险类规则不在豁免集内。
    _TEST_STYLE_RULES = (
        "missing_function_docstring", "missing_module_docstring",
        "missing_type_hints", "function_too_long", "line_too_long",
    )

    # 只有非纯后端形态才开启的规则：Web 服务生命周期（docs/15 补丁 3/4）与前端产物静态证据
    # （补丁 5）。backend_only 下 severity_map 不含这些键 → sandbox 不检出，行为零变更。
    _WEB_ONLY_SEVERITY = {
        "web_service_lifecycle": "major",
        "web_port_literal": "major",
        "web_service_not_daemon": "major",
        "html_placeholder_unresolved": "major",
        "html_asset_missing": "major",
        "html_tag_unclosed": "minor",
        "js_syntax_error": "info",
    }

    def _analysis_policies(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        """按交付形态调整审查策略（docs/15 补丁 1 / 3 / 4 / 5）。

        ``delivery_shape != backend_only`` 时：
        - ``network_import`` 降为 info：HTTP 服务 import http/socket 是需求本身，不是高危外连；
        - 开启 Web 服务生命周期与前端产物规则，补齐这两类此前完全空白的静态证据。

        豁免与新增都只覆盖上表这些规则，``eval_usage`` / ``sandbox_command_bypass`` 等不动。
        backend_only（默认）任务会把上表这些键从 severity_map **剔除**：sandbox 以「规则键是否在
        severity_map 里」作为开关，留着默认值会让这类 finding 在全形态下都产出，破坏验收标准 #1。
        """
        policies = dict(self.role.policies)
        shape, _, _ = resolve_delivery_shape(spec)
        severity_map = dict(policies.get("severity_map") or {})
        if shape_family(shape) == "full_stack":
            severity_map["network_import"] = "info"
            for rule, default in self._WEB_ONLY_SEVERITY.items():
                severity_map.setdefault(rule, default)   # role YAML 已声明时以其为准
        else:
            for rule in self._WEB_ONLY_SEVERITY:
                severity_map.pop(rule, None)
        policies["severity_map"] = severity_map
        return policies

    def _exempt_test_style(self, findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """过滤 tests/ 路径上的风格类 findings（见 _TEST_STYLE_RULES 注释）。"""
        exempt = self.role.policies.get("test_style_rules_exempt")
        if exempt is None:
            exempt = self._TEST_STYLE_RULES      # 未配置才用默认；显式空列表=关闭豁免
        exempt_set = {str(rule) for rule in exempt}
        kept: List[Dict[str, Any]] = []
        for item in findings:
            path = _normalize_rel(item.get("file"))
            if path.startswith("tests/") and str(item.get("rule", "")) in exempt_set:
                continue
            kept.append(item)
        return kept

    def _emit_test_style_exemption(self, task: NodeTask, dropped: int) -> None:
        if dropped <= 0:
            return
        self.ctx.events.emit(
            "review_test_style_exempt", agent=self.role_id, node=task.node_id,
            iteration=task.iteration,
            payload={
                "dropped": dropped,
                "note": "tests/ 冻结测试文件的风格类问题不计入审查证据与阻塞（开发者不可修复）",
            },
        )

    def _emit_exemption_trace(
        self, task: NodeTask, spec: Dict[str, Any], findings: List[Dict[str, Any]]
    ) -> None:
        """豁免留痕（docs/15 补丁 1）：降级了哪几条规则必须能在事件里查到。

        否则线上只能看到"这次 import socket 没被拦"，却查不出是需求形态触发的豁免、
        还是配置被人改过——这两者的处置方式完全不同。
        """
        shape, source, _ = resolve_delivery_shape(spec)
        if shape_family(shape) != "full_stack":
            return
        relaxed = [item for item in findings if item.get("rule") == "network_import"]
        self.ctx.events.emit(
            "network_finding_relaxed", agent=self.role_id, node=task.node_id,
            iteration=task.iteration,
            payload={"delivery_shape": shape, "delivery_shape_source": source,
                     "rule": "network_import", "from": "major", "to": "info",
                     "count": len(relaxed),
                     "note": "前后端交付形态下 HTTP 依赖属需求本身，已降为提示级，不计入 blocking"},
        )

    def _web_asset_findings(self, manifest: Dict[str, Any], policies: Dict[str, Any]) -> List[Dict[str, Any]]:
        """前端产物静态证据（docs/15 补丁 5）：Python AST 分析器只认 .py，这里单独分析。"""
        files = [str(item) for item in (manifest.get("files") or [])]
        if not files:
            return []
        try:
            findings = self.ctx.sandbox.analyze_web_assets(files, policies)
        except Exception:                       # noqa: BLE001 - 证据采集失败不应拖垮审查
            return []
        return [item.to_dict() for item in findings]

    def build_context(self, task: NodeTask, perception: Perception) -> Dict[str, Any]:
        manifest = perception.found.get("code_manifest") or {}
        if task.node_id == "review":
            policies = self._analysis_policies(perception.spec)
            analysis = self.ctx.sandbox.analyze(list(manifest.get("files", [])), policies)
            findings = [finding.to_dict() for finding in analysis.findings]
            findings = self._exempt_test_style(findings)
            self._emit_test_style_exemption(task, len(analysis.findings) - len(findings))
            self._emit_exemption_trace(task, perception.spec, findings)
            web_findings = self._web_asset_findings(manifest, policies)
            if web_findings:
                findings.extend(web_findings)
                self.ctx.events.emit(
                    "web_static_evidence", agent=self.role_id, node=task.node_id,
                    iteration=task.iteration,
                    payload={"delivery_shape": resolve_delivery_shape(perception.spec)[0],
                             "rules": sorted({item["rule"] for item in web_findings}),
                             "count": len(web_findings)},
                )
            return {
                "spec": perception.spec,
                "code_manifest": manifest,
                "test_report": perception.found.get("test_report") or {},
                "static_analysis": {
                    "findings": findings,
                    "loc": analysis.loc,
                    "functions": analysis.functions,
                    "classes": analysis.classes,
                    "syntax_errors": analysis.syntax_errors,
                },
                "policies": policies,
            }
        if task.node_id == "solution_review":
            return {"spec": perception.spec, "solution_design": perception.found.get("solution_design") or {}}
        return {
            "spec": perception.spec,
            "test_report": perception.found.get("test_report") or {},
            "review_report": perception.found.get("review_report") or {},
            "bug_reports": perception.found.get("bug_reports") or {},
        }

    # ---------------------------------------------------------------- Prompt 增强
    def build_prompt(self, task: NodeTask, perception: Perception, experience: Any) -> Any:
        """审查节点把「AST 静态证据 + 交付源码」补进 Prompt。

        原实现只在 build_context 里计算 static_analysis，而 openai_compatible 路径的
        _generate_openai 不消费 context——reviewer 只能看到黑板上的 manifest 清单，
        拿不到任何源码正文，只能以「未提供可读取的源文件内容」驳回
        （attempt03/04/05 实测，三轮同一措辞；attempt04 曾靠二次重做偶然过关）。
        离线 Provider 读 context，不受本改动影响。
        """
        bundle = super().build_prompt(task, perception, experience)
        if task.node_id != "review":
            return bundle
        manifest = perception.found.get("code_manifest") or {}
        files = [str(item) for item in (manifest.get("files") or [])]
        if not files:
            return bundle
        # 第二处 analyze（docs/15 补丁 1 强调两处都要改）：只改 build_context 的话，
        # build_prompt 仍会把 major 级别的 network_import 塞进 Prompt，LLM 照样判阻塞。
        policies = self._analysis_policies(perception.spec)
        analysis = self.ctx.sandbox.analyze(files, policies)
        evidence = {
            "loc": analysis.loc,
            "classes": analysis.classes,
            "functions": analysis.functions,
            "syntax_errors": analysis.syntax_errors,
            "findings": self._exempt_test_style(
                [finding.to_dict() for finding in analysis.findings]),
        }
        web_findings = self._web_asset_findings(manifest, policies)
        if web_findings:
            evidence["findings"].extend(web_findings)
        sections = [
            "## 静态分析证据（框架对沙箱真实文件执行的 AST 分析，确定性结果）",
            json.dumps(evidence, ensure_ascii=False, indent=1)[:12000],
        ]
        sources: List[str] = []
        budget = 40000
        used = 0
        for relative in files:
            try:
                content = self.ctx.sandbox.read_file(relative)
            except Exception:  # noqa: BLE001 - 单文件读取失败不阻塞审查
                continue
            if used + len(content) > budget:
                content = content[: max(0, budget - used)] + "\n...(源码超长，截断)"
            sources.append(f"### {relative}\n{content}")
            used += len(content)
            if used >= budget:
                break
        if sources:
            sections.append("## 交付源码（沙箱中的真实文件内容）\n" + "\n\n".join(sources))
        bundle.user += "\n\n" + "\n\n".join(sections)
        if bundle.path:
            try:
                with bundle.path.open("a", encoding="utf-8") as fh:
                    fh.write("\n\n" + "\n\n".join(sections))
            except OSError:
                pass  # 留痕失败不影响审查
        return bundle

    def apply(
        self,
        task: NodeTask,
        generation: Generation,
        perception: Perception,
        context: Dict[str, Any],
        result: AgentResult,
    ) -> None:
        data = generation.data or {}
        if task.node_id == "acceptance":
            self._apply_signoff(task, data, result)
            return
        if task.node_id == "review":
            data = self._recompute_blocking(task, data, perception, context)
        verdict = str(data.get("verdict", "reject"))
        artifact_name = "review_report" if task.node_id == "review" else "design_review"
        self.publish(
            artifact_name, data, task,
            summary=f"verdict={verdict}，阻塞 {data.get('blocking', 0)} 项，"
                    f"发现 {len(data.get('findings', []))} 项",
        )
        result.artifacts[artifact_name] = data
        result.status = "pass" if verdict == "pass" else "fail"
        result.summary = str(data.get("summary", "")) or f"{task.title}结论：{verdict}"
        # 摘要与判决对账（20261024-233917-897d 实测）：模型自报 verdict=pass/阻塞 0 时
        # summary 也写「通过，阻塞 0」，而框架重算 reject/blocking=N 才是节点真实状态——
        # node_end 里「status=fail + 摘要说通过」自相矛盾，误导时间线与前端。
        if (task.node_id == "review" and verdict != "pass") \
                or int(data.get("blocking", 0) or 0) > 0:
            result.summary += (f"（框架重算：verdict={verdict}，"
                               f"阻塞 {data.get('blocking', 0)} 项）")
        result.events = ["review_passed" if verdict == "pass" else "review_rejected"]
        result.broadcast_payload = {
            "verdict": verdict,
            "blocking": data.get("blocking", 0),
            "summary": result.summary,
        }
        self.ctx.events.emit(
            "review_completed", agent=self.role_id, node=task.node_id, iteration=task.iteration,
            payload={
                "verdict": verdict,
                "blocking": data.get("blocking", 0),
                "total": len(data.get("findings", [])),
                "findings": data.get("findings", []),
                "artifact": artifact_name,
            },
            refs=[f"artifacts/{artifact_name}"],
        )

    # ---------------------------------------------------------------- 功能点映射审计
    def _read_code_text(self, relative: str) -> Optional[str]:
        """读沙箱交付代码；不存在/越界均返回 None（由调用方记具体 finding）。"""
        sandbox = getattr(self.ctx, "sandbox", None)
        reader = getattr(sandbox, "read_file", None)
        if callable(reader):
            try:
                return reader(relative)
            except Exception:  # noqa: BLE001 - 缺失/越界都按不可读处理
                return None
        root = getattr(sandbox, "root", None)
        if root is None:
            return None
        path = Path(root) / relative
        try:
            return path.read_text(encoding="utf-8") if path.is_file() else None
        except OSError:
            return None

    def _frozen_test_sources(self) -> Dict[str, str]:
        """冻结测试副本（生成方不可写）是「通过」结论的原始证据来源。"""
        run_dir = getattr(self.ctx, "run_dir", None)
        if run_dir is None:
            return {}
        root = Path(run_dir) / FROZEN_DIR
        if not root.is_dir():
            return {}
        sources: Dict[str, str] = {}
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            try:
                sources[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8")
            except OSError:
                continue
        return sources

    def _code_hash(self) -> str:
        sandbox = getattr(self.ctx, "sandbox", None)
        root = getattr(sandbox, "root", None)
        if root is None:
            run_dir = getattr(self.ctx, "run_dir", None)
            if run_dir is None:
                return ""
            root = Path(run_dir) / "code"
        return code_tree_hash(Path(root))

    def _export_modules(self, file: str, base: str) -> Set[str]:
        """符号可被 import 的模块集合：定义模块本身 + 逐层 __init__.py 的再导出。"""
        module = _module_name(file)
        exports: Set[str] = {module} if module else set()
        directories = file.split("/")[:-1]
        for depth in range(len(directories), 0, -1):
            package = ".".join(directories[:depth])
            if package in exports:
                continue
            init_file = "/".join(directories[:depth] + ["__init__.py"])
            source = self._read_code_text(init_file)
            if source is None:
                continue
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            for node in tree.body:
                if not isinstance(node, ast.ImportFrom):
                    continue
                imported = node.module or ""
                if node.level:
                    parts = package.split(".")
                    keep = len(parts) - (node.level - 1)
                    if keep < 0:
                        continue
                    imported = ".".join(parts[:keep] + ([imported] if imported else []))
                names = {alias.name for alias in node.names}
                reexports_target = any(
                    imported == export or imported.startswith(f"{export}.")
                    for export in exports
                )
                if (base in names and reexports_target) or ("*" in names and imported in exports):
                    exports.add(package)
                    break
        return exports

    def _covered_by_tests(
        self, sources: Dict[str, str], exports: Set[str], symbol: str
    ) -> str:
        """返回覆盖该符号的测试文件；覆盖 = import 后确有使用（Name/Attribute）。"""
        base, _, member = symbol.partition(".")
        targets = {f"{export}.{base}" for export in exports if export}
        if not targets:
            return ""
        for path, source in sorted(sources.items()):
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            bindings: Dict[str, str] = {}
            stars: Set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.asname:
                            bindings[alias.asname] = alias.name
                        else:
                            root = alias.name.split(".")[0]
                            bindings[root] = root
                elif isinstance(node, ast.ImportFrom):
                    if node.level:      # 测试套件使用绝对导入；相对导入不做推断
                        continue
                    module = node.module or ""
                    for alias in node.names:
                        if alias.name == "*":
                            stars.add(module)
                        else:
                            bound = alias.asname or alias.name
                            bindings[bound] = f"{module}.{alias.name}" if module else alias.name

            def resolve(node: ast.AST) -> Optional[str]:
                if isinstance(node, ast.Name):
                    return bindings.get(node.id)
                if isinstance(node, ast.Attribute):
                    prefix = resolve(node.value)
                    return f"{prefix}.{node.attr}" if prefix else None
                return None

            base_used = False
            member_used = False
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute):
                    if member and node.attr == member:
                        member_used = True
                    resolved = resolve(node)
                    if not resolved:
                        continue
                    if resolved in targets:
                        base_used = True
                    if member and resolved in {f"{target}.{member}" for target in targets}:
                        return path
                elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                    if node.id == base and stars & exports:
                        base_used = True
                    if bindings.get(node.id) in targets:
                        base_used = True
            if base_used and (not member or member_used):
                return path
        return ""

    def _audit_fp_mapping(
        self, perception: Perception
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any], str]:
        """把 fp_mapping 从「自报 key 列表」升级为可核验的符号级证据。

        每个功能点必须：value 严格为 ``file::symbol`` → 文件在交付清单且真实存在
        → 符号在 AST 中被定义 → 该符号被至少一个**通过的冻结测试** import 并使用。
        任何一步失败都生成 critical finding，由 ``_recompute_blocking`` 计为阻塞。
        """
        manifest = perception.found.get("code_manifest") or {}
        mapping = manifest.get("fp_mapping") or {}
        manifest_files = {_normalize_rel(item) for item in (manifest.get("files") or [])}
        fps = [fp for fp in (perception.spec.get("function_points") or []) if isinstance(fp, dict)]
        first_file = _normalize_rel((manifest.get("files") or ["-"])[0])

        test_report = perception.found.get("test_report") or {}
        summary = test_report.get("summary") or {}
        attestation = test_report.get("verification_attestation") or {}
        try:
            total = int(summary.get("total") or 0)
            failed = int(summary.get("failed") or 0)
        except (TypeError, ValueError):
            total, failed = 0, 1
        tests_ok = total > 0 and failed == 0 and attestation.get("valid") is True
        tests = self._frozen_test_sources() if tests_ok else {}

        findings: List[Dict[str, Any]] = []
        invalid: List[Dict[str, str]] = []
        valid = 0

        def add(rule: str, fp_id: str, file: str, message: str) -> None:
            findings.append({
                "rule": rule, "severity": "critical", "file": file or first_file,
                "line": 0, "symbol": fp_id, "message": message,
            })
            invalid.append({"fp_id": fp_id, "value": str(mapping.get(fp_id, "")),
                            "reason": rule})

        if not tests_ok:
            findings.append({
                "rule": "review_without_passing_tests", "severity": "critical",
                "file": first_file, "line": 0, "symbol": "-",
                "message": ("review 没有可采信的通过测试证据（需 total>0、failed=0 且 "
                            "verification_attestation.valid=true），功能点 import/覆盖不可验证"),
            })

        for fp in fps:
            fp_id = str(fp.get("id") or "")
            value = mapping.get(fp_id)
            if value in (None, ""):
                add("unimplemented_function_point", fp_id, first_file,
                    f"需求功能点 {fp_id} 未映射到任何实现函数")
                continue
            file, symbol, error = _mapping_parts(value)
            if error:
                add("invalid_fp_mapping_format", fp_id, first_file,
                    f"功能点 {fp_id} 的映射值 {value!r} 非法：{error}")
                continue
            if file not in manifest_files:
                add("fp_mapping_file_not_in_manifest", fp_id, file,
                    f"功能点 {fp_id} 映射的文件 {file} 不在交付清单中")
                continue
            if not file.endswith(".py"):
                add("fp_mapping_symbol_unverifiable", fp_id, file,
                    f"功能点 {fp_id} 映射到非 Python 文件 {file}，AST 无法核验符号 {symbol}")
                continue
            source = self._read_code_text(file)
            if source is None:
                add("fp_mapping_file_missing", fp_id, file,
                    f"功能点 {fp_id} 映射的文件 {file} 在沙箱中不存在或不可读")
                continue
            try:
                tree = ast.parse(source)
            except SyntaxError as exc:
                add("fp_mapping_file_unparsable", fp_id, file,
                    f"功能点 {fp_id} 映射的文件 {file} 无法解析：{exc}")
                continue
            if symbol not in _defined_symbols(tree):
                add("fp_mapping_symbol_missing", fp_id, file,
                    f"功能点 {fp_id} 映射的符号 {symbol} 未在 {file} 中定义")
                continue
            if tests_ok:
                covered = self._covered_by_tests(tests, self._export_modules(file, symbol.split(".")[0]), symbol)
                if not covered:
                    add("fp_mapping_not_covered_by_passing_test", fp_id, file,
                        f"功能点 {fp_id} 的符号 {symbol} 未被任何通过的冻结测试 import/使用")
                    continue
            valid += 1

        audit = {
            "format": "file::symbol",
            "total": len(fps),
            "valid": valid,
            "invalid": invalid,
            "test_evidence": {
                "passed": tests_ok,
                "source": FROZEN_DIR if tests else "",
                "files": len(tests),
                "total": total,
                "failed": failed,
            },
        }
        return findings, audit, self._code_hash()

    def _recompute_blocking(
        self,
        task: NodeTask,
        data: Dict[str, Any],
        perception: Perception,
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """阻塞数由框架按确定性证据重算，不再采信模型 JSON（docs/37 P0-2）。

        确定性来源：build_context 已算好的 AST/Web 静态 findings + 功能点映射的
        符号级审计（文件存在 / 符号定义 / 通过的冻结测试 import 或使用）。
        LLM 的自报 findings 保留为 ``advisory_findings``，但 ``blocking``/
        ``blocking_items`` 只由框架写入——模型说 0 不能把 ``eval_usage``/语法错误/
        未实现功能点洗掉；模型虚报阻塞也无法改变计数。
        """
        policies = dict(context.get("policies") or self.role.policies)
        blocking_severities = set(policies.get("blocking_severities") or ("critical", "major"))
        static = (context.get("static_analysis") or {}).get("findings") or []
        deterministic = self._exempt_test_style(
            [item for item in static if isinstance(item, dict)])

        mapping_findings, mapping_audit, code_hash = self._audit_fp_mapping(perception)
        deterministic.extend(mapping_findings)
        data["code_hash"] = code_hash
        data["mapping_audit"] = mapping_audit
        data["coverage"] = {
            "function_points": mapping_audit["total"],
            "implemented": mapping_audit["valid"],
            "missing": [item["fp_id"] for item in mapping_audit["invalid"]],
        }
        self.ctx.events.emit(
            "review_mapping_checked", agent=self.role_id, node=task.node_id,
            iteration=task.iteration,
            severity="info" if not mapping_findings else "error",
            payload={
                "format": mapping_audit["format"],
                "total": mapping_audit["total"],
                "valid": mapping_audit["valid"],
                "invalid": mapping_audit["invalid"],
                "test_evidence": mapping_audit["test_evidence"],
                "code_hash": code_hash,
            },
        )

        llm_findings = [item for item in (data.get("findings") or []) if isinstance(item, dict)]
        merged: List[Dict[str, Any]] = []
        seen: set = set()
        for item in list(llm_findings) + deterministic:
            try:
                line = int(item.get("line", 0) or 0)
            except (TypeError, ValueError):
                line = 0
            key = (str(item.get("rule", "")), str(item.get("file", "")),
                   line, str(item.get("symbol", "")))
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
        blocking_items = [item for item in deterministic
                          if str(item.get("severity", "")) in blocking_severities]
        original = data.get("blocking")
        data["llm_verdict"] = data.get("verdict", "reject")
        data["llm_findings"] = llm_findings
        data["findings"] = merged
        data["blocking"] = len(blocking_items)
        data["blocking_items"] = blocking_items
        data["deterministic_blocking"] = len(blocking_items)
        if blocking_items:
            data["verdict"] = "reject"
        if original != len(blocking_items):
            self.ctx.events.emit(
                "review_blocking_recomputed", agent=self.role_id, node=task.node_id,
                iteration=task.iteration, severity="warning",
                payload={"model_reported": original, "framework_blocking": len(blocking_items),
                         "rules": sorted({str(item.get("rule", "")) for item in blocking_items}),
                         "note": "阻塞数以框架确定性证据为准，模型自报值仅作审计留痕"},
            )
        return data

    def _apply_signoff(self, task: NodeTask, data: Dict[str, Any], result: AgentResult) -> None:
        verdict = str(data.get("verdict", "reject"))
        self.publish("acceptance_opinion", data, task, summary=f"签署意见：{verdict}")
        result.artifacts["acceptance_opinion"] = data
        result.status = "pass"   # 门禁结论由 Leader 依据 criteria 表达式与签署意见综合判定
        result.summary = f"验收签署：{data.get('opinion', '')}"
        result.events = []
        result.metrics["signoff_verdict"] = verdict
        self.evidence(result, "signoff", verdict=verdict, reasons=data.get("blocking_reasons", []))
