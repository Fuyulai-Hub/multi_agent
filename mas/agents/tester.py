"""测试 Agent：依据需求契约生成并真实执行功能/异常/边界用例，产出缺陷报告。"""

from __future__ import annotations

import ast
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from ..errors import SandboxError
from ..intent import SHAPE_STRICTNESS, probe_delivery_shape, resolve_delivery_shape, shape_family
from ..llm import Generation
from ..prompts import PromptBundle
from ..schemas import AgentResult, NodeTask
from ..verifier import TestVerifier, VerificationError
from ..verify.exec import execute_frozen_suite
from .base import BaseAgent, Perception

__all__ = ["TesterAgent"]

_SEVERITY = {"functional": "critical", "exception": "major", "boundary": "major"}

#: 沙箱唯一可执行的验证语言（证据通道固定为框架 runner 执行 Python 用例）
_EXECUTABLE_LANGUAGE = "python"

# Web 交付执行协议（A 档）：delivery_shape == full_stack_ssr 时由 build_prompt 注入。
# 设计依据 docs/15 补丁 4/A3：证据通道保持"单命令 python 测试文件 → exit_code"不变，
# 服务在测试进程内起停（端口 0 由 OS 分配），UI 功能点用响应体断言映射进覆盖率门禁。
_WEB_PROTOCOL = '''## Web 交付执行协议（delivery_shape={shape}，框架按需求规格注入）

被测交付物是 HTTP 服务。测试必须采用「同进程服务生命周期」模板：在测试进程内启动服务、
发真实请求、断言响应、finally 关停——禁止假设服务已在运行，禁止留下常驻进程。

```python
import threading
import http.client
from http.server import ThreadingHTTPServer
from {package_hint} import build_handler   # 被测包公开的处理器工厂（返回 BaseHTTPRequestHandler 子类）

srv = ThreadingHTTPServer(("127.0.0.1", 0), build_handler())
threading.Thread(target=srv.serve_forever, daemon=True).start()
try:
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    conn.request("GET", "/")
    resp = conn.getresponse()
    body = resp.read().decode("utf-8")
    assert resp.status == 200 and "期望片段" in body
finally:
    srv.shutdown()
    srv.server_close()
```

规则（与 execution_protocol 的既有要求叠加，冲突时以本段为准）：
1. 端口一律绑定 0（OS 分配），避免与并行 run 冲突；每个测试类自建自拆服务，或用
   setUpClass/tearDownClass 共享一个服务实例。
2. 页面/UI 功能点的用例 = 对 HTTP 响应的断言：状态码 + SSR 响应体片段
   （如 `assert "购物车: 2 件" in body`）。需求样例中「GET /path → 200 且 body 含 …」
   即是这类用例的期望值来源；禁止读取实现代码反推 HTML 结构。
3. 本模式下测试文件 import http/socket/threading 属需求本身（框架已按需求声明对该类
   依赖做审查豁免）；其余仍仅用标准库。
4. test_{{category}}_{{op}}_{{name}} 命名不变；执行与取证由框架 runner 负责——
   测试文件只定义用例，不要写 main()、结果标记或结果文件；fp_id 由契约 cases
   元数据绑定。整个套件须在沙箱超时（默认 180 秒）内完成。
5. files 只写 tests/ 下的测试文件：本角色无写实现代码的权限，写 serve.py/app.py 这类文件
   会让整个测试节点因权限被拒而失败；服务必须在测试进程内起停，不落成独立文件。
6. **全部功能点的用例必须写在同一个测试文件里**（如 tests/test_all.py），且每个功能点都要有
   functional / exception / boundary 三类用例。框架只执行一个测试文件，分散到多个文件会让
   覆盖率按单文件统计，其余功能点计为未覆盖，覆盖率门禁必然不达标。
'''

# 浏览器级检查（B 档）：仅 full_stack_spa 形态注入。
# 不能无条件下发——ui_service_cmd 的默认值是 `python serve.py`，会诱导模型在 files 里
# 输出 `serve.py`，而 tester 的 can_write_code=false，写入即抛 SandboxError 让整个节点崩溃
# （web_todo_app 真实运行实测：test 节点 iter1/iter2 都栽在这里）。
# 本串由 `section += _BROWSER_PROTOCOL` 直接拼接，**不经过 .format()**：花括号按字面下发，
# 因此 {{base_url}} 必须保持两个花括号（uiprober._subst 的替换契约），写成四个会让模型
# 照抄出非法 URL，浏览器导航必然失败。
_BROWSER_PROTOCOL = '''
浏览器级检查（full_stack_spa 形态必需，否则节点按"缺浏览器级证据"判失败）：
6. ui_service_cmd：单条命令启动被测服务（默认 ["python", "serve.py"]）。服务绑定端口 0，
   监听成功后必须在 stdout 打印一行 `MAS_UI_PORT=<实际端口>`，随后持续服务直至被外部终止。
   注意：本角色没有写实现代码的权限，不要试图新建这个文件——它应由研发交付；
   只在这里声明启动命令。
7. ui_checks：列表，每项形如
   {"name": "新增任务出现在列表页",
     "steps": [{"tool": "browser_navigate", "arguments": {"url": "{{base_url}}/"}},
               {"tool": "browser_evaluate",
                "arguments": {"function": "() => document.body.textContent"}}],
     "expect": {"contains": "买牛奶"}}
   tool 为浏览器 MCP 服务注册的工具名；arguments 中 {{base_url}} 占位符由框架替换为真实
   服务地址；expect.contains 对最后一个 step 的文本结果做子串断言（唯一允许的断言方式）。
   已实测（Playwright MCP）的三条硬约束：
   - browser_navigate 只返回页面 URL / 标题等元信息，browser_snapshot 只返回 snapshot
     文件链接，两者都不含页面正文——只写 navigate 加 expect 必然断言不中；
   - 断言必须落在 browser_evaluate 的返回值上（它把 JS 求值结果以文本内联返回），
     因此 steps 的最后一步须是 browser_evaluate 或其它内联返回正文的工具；
   - 取正文用 textContent，不要用 innerText：无头浏览器里 innerText 依赖布局，实测
     返回空串，断言必然不中。
'''


class TesterAgent(BaseAgent):
    role_id = "tester"
    task_kinds = {"default": "test_suite"}
    contracts = {
        "default": {
            "package": "",
            "files": [{"path": "tests/test_xxx.py", "content": ""}],
            "cases": [{"id": "TC-FP-01-functional-xxx", "fp_id": "FP-01", "op": "被测试函数名(ASCII)",
                       "name": "用例短名(ASCII)", "category": "functional|exception|boundary",
                       "call": "调用表达式", "expect": "期望值", "expect_error": "异常类名或空串",
                       "assert_kind": "eq|truthy|falsy|empty|raises"}],
            "case_count": {"total": 0, "functional": 0, "exception": 0, "boundary": 0},
            "negotiation": {"questions": [], "answers": [], "open": 0, "status": "pending"},
            "derived_from": "",
            # ui_service_cmd / ui_checks 不写进静态契约：它们只在 full_stack_spa（B 档）
            # 形态下才有意义，无条件下发会诱导模型输出 `serve.py`，而 tester 无写实现代码权限
            # （实测导致测试节点直接崩溃）。改为在 _BROWSER_PROTOCOL 里按形态注入。
            "execution_protocol": {
                "note": (
                    "测试文件只定义 unittest 用例；框架用固定执行器 mas/verify/runner.py "
                    "按冻结清单逐条执行，并直接读取 runner 写到沙箱工作区之外的逐用例记录。"
                    "不要在测试文件里实现 main()/argparse、打印结果标记或写结果文件——"
                    "stdout 不作为验收证据，测试文件也没有选择执行哪些用例的权利。"
                ),
                "runner": "mas/verify/runner.py",
                "case_discovery": "框架从冻结清单 test_ids 枚举用例：文件里额外定义/动态生成的用例不会执行，清单内缺失的用例记 error",
                "result_file": "由框架通过 --out 指定（位于模型工作区之外），测试文件禁止读写该路径",
                "requirements": [
                    "文件只定义测试类与 test_* 方法（可定义纯辅助函数/常量）；禁止文件级结果回传逻辑：不要写 main、MARKER、sys.exit 或 result.json",
                    "测试方法命名约定 test_{category}_{op}_{name}（category 为 functional/exception/boundary 之一），cases 元数据的 category/op/name 必须与方法名一致",
                    "fp_id 由契约 cases 元数据提供并在冻结时绑定到 test_id，测试文件无需（也不应）自报 fp_id",
                    "期望值只能来自需求契约（规则/样例），禁止读取研发实现代码反推",
                    "仅用标准库；import 被测包（package 字段指定的包名）下的公开接口",
                    "测试套件首次执行后由框架冻结（test_manifest.json 记录文件 hash、test_ids 与 fp_id 元数据）："
                    "后续轮次提交的 tests/ 修改不会生效，执行的是冻结副本；测试自身崩溃/无法枚举用例时框架才会解冻重新生成"
                ]
            },
        }
    }

    def build_context(self, task: NodeTask, perception: Perception) -> Dict[str, Any]:
        return {
            "spec": perception.spec,
            "solution_design": perception.found.get("solution_design") or {},
            "code_manifest": perception.found.get("code_manifest") or {},
            "previous_test_report": perception.found.get("test_report") or {},
            "previous_bugs": perception.found.get("bug_reports") or {},
            "policies": dict(self.role.policies),
        }

    def build_prompt(self, task: NodeTask, perception: Perception, experience: Any) -> Any:
        """full_stack_* 需求把「Web 交付执行协议」补进 Prompt（docs/15 补丁 4）。

        与 reviewer 的源码增强同一模式：契约是静态类属性，按需求规格的 delivery_shape
        动态追加协议段并同步落盘留痕；backend_only 任务 Prompt 不变。
        """
        bundle = super().build_prompt(task, perception, experience)
        shape, _, _ = resolve_delivery_shape(perception.spec)
        if shape_family(shape) != "full_stack":
            return bundle
        manifest = perception.found.get("code_manifest") or {}
        section = _WEB_PROTOCOL.format(
            shape=shape,
            package_hint=str(manifest.get("package") or "被测包"),
        )
        if shape == "full_stack_spa":
            section += _BROWSER_PROTOCOL
        bundle.user += "\n\n" + section
        if bundle.path:
            try:
                with bundle.path.open("a", encoding="utf-8") as fh:
                    fh.write("\n\n" + section)
            except OSError:
                pass  # 留痕失败不影响测试生成
        return bundle

    # ---------------------------------------------------------------- 执行
    def apply(
        self,
        task: NodeTask,
        generation: Generation,
        perception: Perception,
        context: Dict[str, Any],
        result: AgentResult,
    ) -> None:
        data = generation.data or {}
        spec = perception.spec
        fps = spec.get("function_points", [])
        fp_total = len(fps)

        # 1) 测试冻结（docs/37 P0-2）：一旦冻结，生成方提交的 tests/ 文件不再生效，
        # 执行前从 run_dir/frozen_tests/ 恢复权威副本。首次执行前冻结当前套件。
        verifier = TestVerifier(
            self.ctx.run_dir, self.ctx.sandbox.root, events=self.ctx.events,
            config=self.ctx.config, sandbox=self.ctx.sandbox,
            run_id=getattr(self.ctx, "run_id", ""),
        )
        try:
            frozen = verifier.load_manifest()
        except VerificationError as exc:
            result.status, result.error = "fail", f"测试冻结清单校验失败: {exc}"
            result.summary = result.error
            return

        # 0.5) 冻结前覆盖率校验（20261024-233917-897d 实测）：套件一旦冻结，覆盖率就被
        # 冻结清单的 fp_id 元数据永久固定——复测忽略新生成、套件只在自身失效时解冻，
        # 而验收门禁要求三类覆盖率都 >= 1.0。首轮生成少覆盖（实测 10/9/7 of 12）时，
        # 冻结 = 把缺口写进死档，门禁从此永远无法满足。必须在冻结前校验并给修复机会。
        if frozen is None and fp_total:
            data, generation = self._ensure_full_coverage(
                task, generation, data, fps, result)
            if result.status == "fail":
                return

        generated_tests = [
            (str(item["path"]), str(item["content"]))
            for item in (data.get("files") or [])
            if isinstance(item, dict) and str(item.get("path", "")).startswith("tests/")
        ]
        test_file = ""
        suite_manifest: Optional[Dict[str, Any]] = frozen
        if frozen is not None:
            test_file = str(frozen.get("test_file") or "")
            changes = verifier.restore_if_changed()
            self.ctx.events.emit(
                "test_generation_ignored", agent=self.role_id, node=task.node_id,
                iteration=task.iteration, severity="warning",
                payload={"test_hash": frozen.get("digest", ""), "files": len(generated_tests),
                         "restored_changes": changes,
                         "note": "测试套件已冻结：本轮新生成的 tests/ 文件被忽略，执行冻结副本"},
            )
            self.evidence(result, "frozen_tests", test_file=test_file,
                          test_hash=frozen.get("digest", ""), restored=len(changes),
                          test_ids=len(frozen.get("test_ids") or []))
        else:
            # 首次（或冻结已失效）时写入测试文件（真实可执行）
            self._clear_foreign_tests(task, [
                str(item["path"]) for item in (data.get("files") or [])
                if isinstance(item, dict) and str(item.get("path", "")).startswith("tests/")
            ])
            for file_item in data.get("files", []):
                if not isinstance(file_item, dict) or not file_item.get("path"):
                    continue          # 契约外防御：跳过结构非法的文件项，不让整个节点崩溃
                path = str(file_item["path"])
                try:
                    self.sandbox_write(path, str(file_item["content"]))
                except SandboxError as exc:
                    # 权限守卫拒绝（如契约诱导模型输出 tests/ 之外的 serve.py）：单文件写不进去
                    # 不该让整个测试节点崩掉——跳过后继续写其余测试文件，并在事件里如实记账。
                    self.ctx.events.emit(
                        "test_file_write_denied", agent=self.role_id, node=task.node_id,
                        iteration=task.iteration, severity="error",
                        payload={"path": path, "error": str(exc),
                                 "note": "测试角色只能写 tests/ 下的文件；该文件被跳过，未写入沙箱"},
                    )
                    continue
                if path.startswith("tests/"):
                    test_file = path
                    self.evidence(result, "file", path=test_file, chars=len(str(file_item["content"])))
            if test_file:
                try:
                    suite_manifest = verifier.freeze(
                        test_file, task.iteration, case_meta=data.get("cases") or [])
                except VerificationError as exc:
                    result.status, result.error = "fail", f"测试套件冻结失败: {exc}"
                    result.summary = result.error
                    return
        if not test_file:
            result.status, result.error = "fail", "测试套件未生成测试文件"
            result.summary = result.error
            return
        if suite_manifest is None:
            result.status, result.error = "fail", "没有可用的冻结测试清单，拒绝执行"
            result.summary = result.error
            return

        # 1.5) 执行前静态体检：测试文件里若内联调用沙箱白名单之外的可执行程序（g++/cl 等），
        # 说明它想绕过白名单去验证非 Python 交付物；若用了 json.* 却没 import json，
        # 则结果回传协议必然崩在 NameError。两类都必须在执行前就写明，而不是事后翻 stderr。
        # 实测 task04（C++ 需求）：19 条用例全部败于 FileNotFoundError，报告里看不出根因。
        preexec_findings = self._preexec_diagnosis(test_file)
        if preexec_findings:
            self.ctx.events.emit(
                "test_environment_gap", agent=self.role_id, node=task.node_id,
                iteration=task.iteration, severity="error",
                payload={"note": "测试文件执行前体检发现外部程序依赖或协议缺陷",
                         "findings": preexec_findings},
            )

        # 2) 需求歧义协商（Agent 间请求-应答通信）
        negotiation = self._negotiate(data, task, result)

        # 2.5) 验证口径与选型语言的一致性：非 Python 选型下，沙箱执行的 Python 测试文件
        # 不构成对交付物的证据（实测 probe-02 因此拿到 8 项门禁全绿）。
        # 交付形态以**沙箱真实文件探测**为准收紧（不由模型自报）：探测只升不降——
        # 文件里有 package.json/index.html/框架配置等 SPA 特征时，即使 spec 申报
        # backend_only 也按 full_stack_spa 走浏览器证据通道（fail-closed 方向）。
        shape, shape_source, _ = resolve_delivery_shape(spec)
        shape_probe = probe_delivery_shape(self.ctx.sandbox.root, self.ctx.sandbox.list_files())
        if (shape_probe.get("shape")
                and SHAPE_STRICTNESS.get(shape_probe["shape"], 0) > SHAPE_STRICTNESS.get(shape, 0)):
            self.ctx.events.emit(
                "intent_probed_from_files", agent=self.role_id, node=task.node_id,
                iteration=task.iteration, severity="warning",
                payload={"declared_shape": shape, "probed_shape": shape_probe["shape"],
                         "evidence": shape_probe["evidence"][:8],
                         "note": "沙箱交付文件探测到更强的前端形态，覆盖模型/规格自报的交付形态"},
            )
            shape, shape_source = shape_probe["shape"], "file_probe"
        env_probe = perception.found.get("env_probe") or {}
        verification = self._verification_gap(
            perception.found.get("solution_design") or {}, env_probe, spec, shape=shape)
        if verification["gap"]:
            self.ctx.events.emit(
                "test_verification_gap", agent=self.role_id, node=task.node_id,
                iteration=task.iteration, severity="error",
                payload={"note": verification["note"],
                         "design_language": verification["design_language"],
                         "executable_languages": verification["executable_languages"]},
            )

        # 3) 沙箱真实执行：执行权交给框架 runner（只跑冻结清单 test_ids），
        #    结果从工作区外的 runner_result.json 读取；stdout 只留作诊断日志。
        execution = execute_frozen_suite(
            self.ctx.sandbox, self.ctx.run_dir, suite_manifest,
            iteration=task.iteration, tag="tester",
        )
        exec_result = execution.exec
        run_dir = self.ctx.run_dir / "test_runs" / f"iter{task.iteration:02d}"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "stdout.txt").write_text(exec_result.stdout, encoding="utf-8")
        (run_dir / "stderr.txt").write_text(exec_result.stderr, encoding="utf-8")
        parsed = execution.result
        protocol_note = ""
        if parsed is None:
            # runner 没有产出逐用例记录：文件崩溃、解释器起不来或证据目录不可写。
            # 不再从 stdout 兜底解析，缺少 runner 记录就是无证据。
            protocol_note = ("框架执行器未产出逐用例记录（runner_result.json 缺失或不可解析）："
                             "用例统计为 0，stdout 不再作为证据")
            parsed = {"total": 0, "passed": 0, "failed": 0, "cases": [], "duration_ms": 0,
                      "runner": {"name": "", "version": 0, "manifest_digest": ""}}
            self.ctx.events.emit(
                "test_protocol_violation", agent=self.role_id, node=task.node_id,
                iteration=task.iteration, severity="error",
                payload={"note": protocol_note,
                         "runner_cmd": execution.cmd,
                         "stdout_tail": exec_result.stdout.strip()[-300:],
                         "stderr_tail": exec_result.stderr.strip()[-300:]},
            )
        elif parsed.get("error"):
            protocol_note = f"框架执行器报告错误：{parsed['error']}"
            self.ctx.events.emit(
                "test_protocol_violation", agent=self.role_id, node=task.node_id,
                iteration=task.iteration, severity="error",
                payload={"note": protocol_note,
                         "unexpected_test_ids": parsed.get("unexpected_test_ids") or [],
                         "stderr_tail": exec_result.stderr.strip()[-300:]},
            )
        (run_dir / "result.json").write_text(
            json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.evidence(
            result, "execution", cmd=" ".join(exec_result.cmd), exit_code=exec_result.exit_code,
            log=str((run_dir / "stdout.txt").relative_to(self.ctx.run_dir)).replace("\\", "/"),
            runner=str(execution.result_path.relative_to(self.ctx.run_dir)).replace("\\", "/"),
        )

        # 4) 独立验证（docs/37 P0-2）：只用逐用例记录重算结果，并核对冻结 hash。
        root_cause = self._root_cause(preexec_findings, parsed.get("cases", []))
        spec_fps = [str(fp.get("id")) for fp in fps if fp.get("id")]
        try:
            attestation = verifier.attest(
                parsed=parsed, exit_code=exec_result.exit_code,
                duration_ms=int(parsed.get("duration_ms") or exec_result.duration_ms),
                test_file=test_file,
                iteration=task.iteration, spec_fps=spec_fps,
                verification_gap=bool(verification["gap"]),
                root_cause_category=root_cause.get("category", ""),
                requirement_spec=spec, run_id=getattr(self.ctx, "run_id", ""),
            )
        except VerificationError as exc:
            attestation = {
                "version": 2, "signature_alg": "", "valid": False,
                "reasons": [f"verifier_error:{exc}"],
                "test_file": test_file, "test_hash": "", "files": {}, "total": 0,
                "passed": 0, "failed": 0, "assertions": 0,
                "requirement_hash": "", "code_hash": "",
                "visible_suite_hash": "", "hidden_suite_hash": "", "env_hash": "",
                "runner_version": 0, "run_id": getattr(self.ctx, "run_id", ""),
                "per_case_digest": "",
                "coverage": self._coverage(parsed.get("cases", []), fp_total, data),
                "exec": {"exit_code": exec_result.exit_code,
                         "duration_ms": exec_result.duration_ms},
                "verification_gap": bool(verification["gap"]),
                "root_cause_category": root_cause.get("category", ""),
                "signature": "", "cases": parsed.get("cases", []),
            }
        effective_cases = attestation.get("cases") or parsed.get("cases", [])
        coverage = (attestation.get("coverage") if attestation.get("valid")
                    else self._coverage(effective_cases, fp_total, data))

        # 5) 缺陷跟踪（含上一轮缺陷的闭环判定）
        bugs = self._track_bugs(effective_cases, data.get("cases", []), task, perception)

        # 6) 测试报告（summary 以验证器重算值为准）
        summary = {
            "total": int(attestation.get("total", parsed.get("total", 0))),
            "passed": int(attestation.get("passed", parsed.get("passed", 0))),
            "failed": int(attestation.get("failed", parsed.get("failed", 0))),
            "pass_rate": 0.0,
            "duration_ms": int(parsed.get("duration_ms", exec_result.duration_ms)),
        }
        if summary["total"]:
            summary["pass_rate"] = round(summary["passed"] / summary["total"], 4)
        attestation_report = {key: value for key, value in attestation.items() if key != "cases"}
        if not attestation.get("valid"):
            # 测试套件自身失效（无记录/协议崩溃/自报不一致/弱断言）允许下轮重新生成；
            # 冻结文件被篡改或仅是验证口径缺口时不放开，避免“改测试”成为通过手段。
            reopen_reasons = set(attestation.get("reasons") or []) - {
                "test_files_changed_during_execution", "verification_gap"}
            if reopen_reasons:
                verifier.invalidate("; ".join(sorted(reopen_reasons)))
        test_report = {
            "title": f"《{spec.get('title', '')}》业务测试报告",
            "test_file": test_file,
            "runner": parsed.get("runner") or {},
            "test_ids": list(suite_manifest.get("test_ids") or []),
            "summary": summary,
            "coverage": coverage,
            "verification_attestation": attestation_report,
            "negotiation": negotiation,
            "executions": [{
                "cmd": " ".join(exec_result.cmd),
                "exit_code": exec_result.exit_code,
                "duration_ms": exec_result.duration_ms,
                "log": f"test_runs/iter{task.iteration:02d}/stdout.txt",
            }],
            "failures": [
                {
                    "test": f"{case.get('class', '')}.{case.get('test', '')}",
                    "fp_id": case.get("fp_id", ""),
                    "category": case.get("category", ""),
                    "message": case.get("message", ""),
                }
                for case in parsed.get("cases", []) if case.get("status") != "passed"
            ],
            "case_count": data.get("case_count", {}),
            "derived_from": data.get("derived_from", ""),
            "protocol_note": protocol_note,
            "preexec_findings": preexec_findings,
            "verification": verification,
            "delivery_shape": shape,
            "delivery_shape_source": shape_source,
            "shape_probe": {"probed": shape_probe.get("shape") or "",
                            "evidence": shape_probe.get("evidence") or []},
            "root_cause": root_cause,
            "root_cause_note": self._root_cause_note(preexec_findings, root_cause),
            "environment": {"python": self.ctx.sandbox.python_version()},
        }
        self.publish(
            "test_report", test_report, task,
            summary=f"用例 {summary['total']}（通过 {summary['passed']} / 失败 {summary['failed']}），"
                    f"缺陷开 {bugs['open']} / 闭环 {bugs['fixed']}",
        )
        self.publish("bug_reports", bugs, task, summary=f"缺陷总数 {bugs['total']}（open {bugs['open']}）")
        result.artifacts["test_report"] = test_report
        result.artifacts["bug_reports"] = bugs

        # 6.5) UI 证据（docs/15 前置 1 / B2）：**任何形态都发布**，值随形态变化。
        # 不发布会让门禁里引用 ui_evidence 的 criterion 因字段缺失判失败（expr BoolOp
        # 不短路），backend_only 的老任务被批量误伤——这是改动前 13 个用例失败的直接根因。
        ui_evidence = self._collect_ui_evidence(data, summary, env_probe, verification, shape)
        self.publish(
            "ui_evidence", ui_evidence, task,
            summary=(f"UI 证据（{ui_evidence.get('mode')}，形态 {shape}）：失败断言 "
                     f"{ui_evidence.get('failed_asserts', 0)}，"
                     f"断言 {len(ui_evidence.get('asserts') or [])} 条"),
        )
        result.artifacts["ui_evidence"] = ui_evidence
        if ui_evidence.get("error"):
            self.ctx.events.emit(
                "test_ui_evidence_incomplete", agent=self.role_id, node=task.node_id,
                iteration=task.iteration, severity="error",
                payload={"delivery_shape": shape, "mode": ui_evidence.get("mode"),
                         "failed_asserts": ui_evidence.get("failed_asserts", 0),
                         "error": ui_evidence["error"]},
            )

        # 无证据不得判通过：total==0（测试文件崩溃/未按协议回传）时 failed 也为 0，
        # 原实现据此判 pass 并把 32 个缺陷自动闭环（task04-cpp-probe-02 iter02 实测）。
        result.status = ("pass" if summary["total"] > 0 and summary["failed"] == 0
                         and bool(attestation.get("valid"))
                         and negotiation["open"] == 0 and not verification["gap"]
                         and int(ui_evidence.get("failed_asserts", 0)) == 0
                         else "fail")
        if verification["gap"]:
            result.error = verification["note"]
        elif not attestation.get("valid"):
            result.error = "测试证据未通过独立验证: " + ", ".join(attestation.get("reasons") or [])
        result.summary = (
            f"业务测试：功能 {coverage['functional']['total']} / 异常 {coverage['exception']['total']} / "
            f"边界 {coverage['boundary']['total']} 条用例，通过率 {summary['pass_rate']}，"
            f"缺陷 {bugs['total']} 个（新增 {len(bugs.get('new', []))}，闭环 {bugs['fixed']}）；"
            f"歧义协商 {negotiation['closed']}/{negotiation['total']} 闭环"
            + (f"；根因：{test_report['root_cause_note']}" if preexec_findings else "")
            + (f"；验证口径缺口：{verification['note']}" if verification["gap"] else "")
            + (f"；独立验证未通过：{', '.join(attestation.get('reasons') or [])}"
               if not attestation.get("valid") else
               f"；独立验证通过（test_hash {str(attestation.get('test_hash', ''))[:16]}，"
               f"断言 {attestation.get('assertions', 0)} 条）")
            + (f"；交付形态 {shape}"
               + (f"；UI 证据失败断言 {int(ui_evidence.get('failed_asserts', 0))} 条"
                  f"（{ui_evidence.get('error') or ui_evidence.get('mode')}）"
                  if int(ui_evidence.get("failed_asserts", 0)) else ""))
            + ("" if summary["total"] else "；无执行证据：本轮没有一条用例回传结果，判失败（见 protocol_note 与 test_runs/iter*/stderr.txt）")
        )
        result.events = ["test_passed" if result.status == "pass" else "test_failed"]
        if bugs.get("new"):
            result.events.append("bug_found")
        result.broadcast_payload = {
            "summary": result.summary,
            "failed": summary["failed"],
            "open_bugs": bugs["open"],
            "pass_rate": summary["pass_rate"],
        }
        self.ctx.events.emit(
            "test_run_completed", agent=self.role_id, node=task.node_id, iteration=task.iteration,
            payload={
                "passed": summary["passed"],
                "failed": summary["failed"],
                "total": summary["total"],
                "pass_rate": summary["pass_rate"],
                "coverage": {key: value["rate"] for key, value in coverage.items()},
                "verification_valid": bool(attestation.get("valid")),
                "verification_reasons": attestation.get("reasons") or [],
                "test_hash": attestation.get("test_hash", ""),
                "failures": test_report["failures"],
            },
            refs=[f"test_runs/iter{task.iteration:02d}/stdout.txt"],
        )
        for bug in bugs["items"]:
            if bug.get("status") == "open" and bug.get("id") in bugs.get("new", []):
                self.ctx.events.emit(
                    "bug_reported", agent=self.role_id, node=task.node_id, iteration=task.iteration,
                    payload={
                        "bug_id": bug["id"],
                        "fp_id": bug["fp_id"],
                        "op": bug.get("op", ""),
                        "category": bug["category"],
                        "severity": bug["severity"],
                        "symptom": bug["symptom"],
                        "test": bug["test"],
                    },
                )

    def _preexec_diagnosis(self, test_file: str) -> List[Dict[str, Any]]:
        """执行前对测试文件做 AST 体检，只挑"绕过/依赖沙箱命令白名单外的外部程序"这一类高危项。

        复用 reviewer 的审查策略（severity_map 可调），分析失败不影响主流程。
        """
        try:
            policies = dict(self.ctx.config.role("reviewer").policies)
            analysis = self.ctx.sandbox.analyze([test_file], policies)
        except Exception:                       # noqa: BLE001 - 体检是诊断，不能拖垮测试节点
            return []
        return [item.to_dict() for item in analysis.findings
                if getattr(item, "rule", "") in ("sandbox_command_bypass",
                                                 "sandbox_command_unresolved")
                or str(getattr(item, "rule", "")).endswith("_not_imported")]

    # 外部程序在本机不存在的报错特征：命中即判定为环境/构建问题，而不是交付物缺陷
    _MISSING_PROG_PATTERNS = ("filenotfounderror", "winerror 2", "no such file or directory",
                              "not found", "not recognized")
    # 测试套件自身的静态缺陷（可修，不该算环境也不该算交付物）：`<模块>_not_imported`
    _SUITE_DEFECT_SUFFIX = "_not_imported"

    def _root_cause(self, findings: List[Dict[str, Any]],
                    cases: List[Dict[str, Any]]) -> Dict[str, Any]:
        """把"测试全败/无结果"归到四类根因中的一类，避免环境问题被记成研发缺陷、
        也避免测试套件自身的缺陷被推给环境。

        task04（C++ 需求）实测：32 条用例全败于 FileNotFoundError（本机没有编译器），
        原实现只报"32 个缺陷"并回退给研发修复，回退预算耗尽后才以失败终止——
        根因其实是环境/构建问题，研发再怎么改也改不出一个不存在的 g++。
        """
        if not findings:
            return {"category": "", "evidence": []}
        evidence = []
        for case in cases:
            message = str(case.get("message", "")).lower()
            if any(pattern in message for pattern in self._MISSING_PROG_PATTERNS):
                evidence.append(f"{case.get('class', '')}.{case.get('test', '')}: "
                                f"{str(case.get('message', ''))[:120]}")
        rules = {str(item.get("rule", "")) for item in findings}
        if any(rule.endswith(self._SUITE_DEFECT_SUFFIX) for rule in rules):
            category = "test_suite_defect"
        elif evidence:
            category = "environment_or_build"
        else:
            category = "delivery_unverifiable"
        return {"category": category, "evidence": evidence[:5], "evidence_total": len(evidence)}

    def _root_cause_note(self, findings: List[Dict[str, Any]], root_cause: Dict[str, Any]) -> str:
        if not findings:
            return ""
        # 同一条规则的多个命中（如两处 subprocess.run）只保留一句说明，
        # 否则事件与节点 summary 会被重复文本淹没（task04-cpp-probe-03 iter02 实测）。
        detail = "；".join(dict.fromkeys(
            str(item.get("message", "")) for item in findings if item.get("message")))
        if root_cause.get("category") == "test_suite_defect":
            return (f"【测试套件缺陷】{detail}。失败发生在测试文件自身（执行前静态可判定），"
                    "不属于交付物缺陷，也不是环境缺失——需按 execution_protocol 重新生成测试文件")
        if root_cause.get("category") == "environment_or_build":
            return (f"【环境/构建问题】{detail}。本机缺少被测交付物所需的外部工具链，"
                    f"{root_cause.get('evidence_total', 0)} 条用例报"
                    f"`{root_cause['evidence'][0].split(': ',1)[-1] if root_cause.get('evidence') else ''}`；"
                    "这类失败无法靠修改交付物代码消除，需补齐环境或改用可验证的语言")
        return (f"【交付物无法验证】{detail}。沙箱命令白名单只校验框架发起的命令，"
                "测试文件内部再起的程序会绕过它，非 Python 交付物因此无法被真实验证")

    @staticmethod
    def _language_tokens(language: str) -> List[str]:
        """把选型语言串切成语言分片：``"React + Python"`` -> ``["react","python"]``。

        按语言分片而不是整串子串匹配，是为了让判定不依赖字符串巧合——文档 §4 补丁 2 指出，
        ``"python" in language`` 对 ``"React + Python"`` 侥幸通过、对 ``"TypeScript + React"``
        一律判失败，两种结果都不可靠。
        """
        return [token.lower() for token in re.split(r"[^0-9A-Za-z]+", str(language or "")) if token]

    def _verification_gap(self, solution_design: Dict[str, Any],
                          env_probe: Optional[Dict[str, Any]] = None,
                          spec: Optional[Dict[str, Any]] = None,
                          shape: str = "") -> Dict[str, Any]:
        """选型语言超出可执行范围（或缺少所需证据通道）时，本轮执行不构成验据。

        沙箱能用 `python <测试文件>`（以及白名单内的 node）执行证据。
        task04-cpp-probe-02 实测：C++ 需求在回退压力下由研发补一份 Python 复刻 +
        Python 测试套件，25 条用例全过、三类覆盖率 1.0，8 项门禁全绿并签署"验收通过"——
        而被验收的 `.hpp` 从未被编译过。此处把"验证口径与选型语言不一致"写成
        可判定的事实，交由测试节点如实报失败（与 F17/F19 一样修在证据源，不改门禁表达式）。

        docs/15 AB 档：判定按交付形态分叉——
        - ``backend_only``：维持原行为（语言串未声明或含 python 分片即通过）；
        - ``full_stack_ssr``：验证通道是 Python 测试，同样按语言分片判定；
        - ``full_stack_spa``：额外要求浏览器证据通道；通道未接入**如实报 gap=1**，
          绝不允许退化成"Python 测试全过就放行"（与教材/ui evidence 的诚实性底线一致）。
        """
        language = str(((solution_design or {}).get("tech_choice") or {}).get("language", "")).strip()
        probe = dict(env_probe or {})
        if not shape:
            shape, _, _ = resolve_delivery_shape(spec or {})
        tokens = self._language_tokens(language)
        has_python = any(token.startswith(_EXECUTABLE_LANGUAGE) for token in tokens)
        has_js = any(token in ("javascript", "typescript", "js", "ts") for token in tokens)
        executable = [_EXECUTABLE_LANGUAGE]
        if has_js and probe.get("javascript_ready"):
            executable.append("javascript")
        base = {"design_language": language, "executable_languages": executable,
                "delivery_shape": shape}

        if shape == "full_stack_spa":
            # 通道已接入（runtime.ui_probe 启用 且 浏览器 MCP 就绪）时，SPA 的两种验证手段
            # （Python 用例 + 浏览器断言）都在可执行集合内，验证口径完整 → gap=0；
            # 证据是否够用由门禁第 9/10 条对 ui_evidence 的真实断言数把关（docs/15 §3.3：
            # "要求浏览器通道已接入，否则 gap=1"——gap 只在通道未接入时报）。
            if probe.get("browser_ui_ready"):
                return {**base, "gap": 0, "note": ""}
            return {
                **base, "gap": 1,
                "note": ("交付形态为 full_stack_spa（浏览器前端），需要浏览器级证据通道才能验证；"
                         "当前通道未接入（config/runtime.ui_probe 未启用或浏览器 MCP 未就绪），"
                         "本轮 Python 用例不构成对该形态交付物的完整验证——如实判失败，不降级放行"),
            }
        if not tokens or has_python:
            if has_js and not probe.get("javascript_ready"):
                return {
                    **base, "gap": 1,
                    "note": (f"选型语言为 {language}，但环境探测显示 node 不可用"
                             f"（{probe.get('node', {}).get('reason', '未探测')}）："
                             "JS 部分无法被执行验证，属环境或构建问题，需补齐工具链后重跑"),
                }
            return {**base, "gap": 0, "note": ""}
        return {
            **base, "gap": 1,
            "note": (f"选型语言为 {language}，沙箱只能执行 Python：本轮用例全过验证的是 Python 测试"
                     "文件自身，不是该语言的交付物（补齐工具链或改用可验证的交付形态后才能验收）"),
        }

    def _ui_evidence_disabled(self, shape: str, note: str) -> Dict[str, Any]:
        """不需要浏览器通道的形态：发布「已明确不适用」而不是不发布。

        expr.py 的 BoolOp **不短路求值**（``values = [self.visit(v) for v in node.values]``
        先求全部值再 any/all）：门禁里写 ``delivery_shape != 'full_stack_spa' or
        ui_evidence.failed_asserts == 0`` 时，只要 ``ui_evidence`` 这个 artifact 不存在，
        ``_lookup`` 就抛 ``_MissingField``，整条 criterion 判失败——连 backend_only 的
        老任务都会被误伤（实测改动前 13 个用例失败的根因）。所以任何形态都必须发布。
        """
        return {"enabled": False, "mode": "disabled", "delivery_shape": shape,
                "channel": "", "asserts": [], "checks": [], "screenshots": [],
                "console_errors": [], "console_note": "", "probe_version": "",
                "http_status": 0, "http_probe": {},
                "failed_asserts": 0, "error": "", "note": note}

    def _collect_ui_evidence(self, data: Dict[str, Any], summary: Dict[str, Any],
                             env_probe: Dict[str, Any], verification: Dict[str, Any],
                             shape: str) -> Dict[str, Any]:
        """按交付形态产出 ui_evidence（docs/15 前置 1 / B2），四种结果都必须可判定。

        1. ``backend_only`` → disabled：无前端产物，UI 证据不适用；
        2. ``full_stack_ssr`` → ssr：UI 验证已由测试套件的 HTTP 响应体断言承担，
           failed_asserts 与用例失败数同源，不谎报采集过浏览器证据；
        3. ``full_stack_spa`` + 浏览器就绪 → browser：UiProber 真实采集，断言逐条入账；
        4. ``full_stack_spa`` + 浏览器不可得 → browser + failed_asserts=1：
           SSR 响应体断言验证不了客户端渲染，放行就是 F23 式假通过，必须判失败。
        """
        if shape_family(shape) != "full_stack":
            return self._ui_evidence_disabled(shape, "纯后端形态无前端产物，UI 证据不适用")

        probe_cfg = dict(self.ctx.config.runtime.get("ui_probe") or {})
        checks = [item for item in (data.get("ui_checks") or []) if isinstance(item, dict)]
        browser_ready = bool(probe_cfg.get("enabled")) and bool(env_probe.get("browser_ui_ready"))

        if shape == "full_stack_spa":
            if not browser_ready:
                return {
                    "enabled": True, "mode": "browser", "delivery_shape": shape, "channel": "",
                    "asserts": [], "checks": [], "screenshots": [], "console_errors": [],
                    "console_note": "", "probe_version": "", "http_status": 0, "http_probe": {},
                    "failed_asserts": 1,
                    "error": ("full_stack_spa 需要浏览器级 UI 证据，当前不可得（"
                              + ("runtime.ui_probe 未启用" if not probe_cfg.get("enabled")
                                 else f"浏览器 MCP 未就绪：{env_probe.get('ui_server', {}).get('reason', '')}")
                              + "）：断言数为 0 时按证据不足判失败，不允许空证据通过"),
                }
            if not checks:
                return {
                    "enabled": True, "mode": "browser", "delivery_shape": shape, "channel": "",
                    "asserts": [], "checks": [], "screenshots": [], "console_errors": [],
                    "console_note": "", "probe_version": "", "http_status": 0, "http_probe": {},
                    "failed_asserts": 1,
                    "error": "测试契约未提供 ui_checks：SPA 形态无可执行文件清单，按证据不足判失败",
                }
            return self._run_ui_prober(data, shape, checks)

        baseline = self._ui_evidence_disabled(
            shape, "SSR 形态的 UI 验证由测试套件的 HTTP 响应体断言承担，未启用浏览器级采集")
        baseline.update({
            "enabled": True, "mode": "ssr",
            "failed_asserts": int(summary.get("failed", 0)),
            "note": ("UI 验证由测试套件的 HTTP 响应体断言承担"
                     + ("" if probe_cfg.get("enabled")
                        else "（runtime.ui_probe 未启用，跳过浏览器级采集）")),
        })
        return baseline

    def _run_ui_prober(self, data: Dict[str, Any], shape: str,
                       checks: List[Dict[str, Any]]) -> Dict[str, Any]:
        """经 Playwright MCP 真实采集浏览器证据；采集器返回结构归一化为 ui_evidence 契约。"""
        from ..uiprober import UiProber

        prober = UiProber(self.ctx.config, self.ctx.sandbox, self.ctx.run_dir, self.ctx.events)
        service_cmd = data.get("ui_service_cmd") or [self.ctx.sandbox.python, "serve.py"]
        if not isinstance(service_cmd, list):
            service_cmd = [str(service_cmd)]
        raw = prober.collect(service_cmd=[str(part) for part in service_cmd], checks=checks)
        checks_done = [item for item in (raw.get("checks") or []) if isinstance(item, dict)]
        asserts = [
            {"fp_id": str(item.get("fp_id", "")), "category": str(item.get("category", "functional")),
             "name": str(item.get("name", "")),
             "status": "passed" if item.get("passed") else "failed",
             "message": str(item.get("detail", ""))[:300]}
            for item in checks_done
        ]
        failed = int(raw.get("failed_asserts", 0)) or sum(
            1 for item in asserts if item["status"] != "passed")
        # -browser 通道不可用/未产生任何断言时一律按失败计：这不构成"空证据的 pass"
        if not asserts and failed == 0:
            failed = 1
        return {
            "enabled": True, "mode": "browser", "delivery_shape": shape,
            "channel": f"mcp:{raw.get('server', '')}" if raw.get("server") else "mcp",
            "base_url": str(raw.get("base_url", "")),
            "asserts": asserts, "checks": asserts, "screenshots": list(raw.get("screenshots") or []),
            "console_errors": list(raw.get("console_errors") or []),
            "console_note": str(raw.get("console_note", "")),
            # probe 版本/HTTP 状态由框架采集器写入：门禁据此拒绝"agent 自填"的空壳证据
            "probe_version": str(raw.get("probe_version", "")),
            "http_status": int(raw.get("http_status", 0)),
            "http_probe": dict(raw.get("http_probe") or {}),
            "failed_asserts": failed,
            "error": str(raw.get("error", "")),
            "note": "浏览器级 UI 断言（UiProber 经 MCP 采集，证据落在 evidence/ui/probe_evidence.json）",
        }

    # ---------------------------------------------------------------- 协商
    def _negotiate(self, data: Dict[str, Any], task: NodeTask, result: AgentResult) -> Dict[str, Any]:
        negotiation_cfg = self.ctx.config.collaboration.negotiation
        # 契约外防御：LLM 可能把 negotiation 输出成字符串或其他非对象类型，
        # 原实现直接 .get("questions") 会抛 AttributeError 整个节点崩溃（实测）。
        raw = data.get("negotiation")
        if not isinstance(raw, dict):
            raw = {}
        questions = [item for item in (raw.get("questions") or []) if isinstance(item, dict)]
        answers: List[Dict[str, Any]] = []
        # 协商预算：每个入场问题一轮请求-应答。超出预算的问题不计入 open——
        # 原实现只协商前 max_rounds 个、其余永久 open，门禁 negotiation.open==0
        # 永远无法满足（task02 实测：LLM 提 5 问只协 2 问 → 3 问永久阻塞验收）。
        cap = max(1, int(negotiation_cfg.get("max_rounds", 6)))
        admitted = questions[:cap]
        over_cap = questions[cap:]
        if not negotiation_cfg.get("enabled", True) or not admitted:
            return {
                "questions": admitted,
                "answers": answers,
                "total": len(admitted),
                "closed": 0,
                "open": len(admitted),
                "over_cap": len(over_cap),
                "status": "open" if admitted else "closed",
            }
        for question in admitted:
            reply = (self.ctx.consult or (lambda **kwargs: None))(
                requester=self.role_id,
                target_role=str(negotiation_cfg.get("responder_role", "developer")),
                question=str(question.get("question", "")),
                fp_id=str(question.get("fp_id", "")),
                node=task.node_id,
            )
            if reply:
                answers.append(reply)
        # 闭环判定按"请求顺序"配对（每个问题一次请求-应答），并由框架覆写问题上的
        # status/resolution 字段：LLM 常在应答里把多个问题合并复述，原实现只数
        # 应答条数，导致问题项自带 status=open 而汇总写 4/4 闭环，证据自相矛盾。
        # 同时要求应答正文实质非空，空话/空串不算闭环。
        min_len = int(negotiation_cfg.get("min_answer_chars", 8))
        closed = 0
        for index, question in enumerate(admitted):
            reply = answers[index] if index < len(answers) else {}
            text = str(reply.get("answer", "")).strip()
            if len(text) >= min_len:
                question["status"] = "resolved"
                question["resolution"] = text
                closed += 1
            else:
                question["status"] = "open"
                question["resolution"] = ""
        return {
            "questions": admitted,
            "answers": answers,
            "total": len(admitted),
            "closed": closed,
            "open": len(admitted) - closed,
            "over_cap": len(over_cap),
            "status": "closed" if closed >= len(admitted) else "open",
        }

    # ---------------------------------------------------------------- 覆盖率
    def _coverage_gaps(self, cases: Any, fps: List[Dict[str, Any]]) -> List[str]:
        """返回「FP:类别」形式的覆盖缺口清单（门禁要求每个 FP 三类用例齐全）。"""
        cases = [c for c in (cases or []) if isinstance(c, dict)]
        covered: Dict[str, set] = {}
        for case in cases:
            if case.get("fp_id") and case.get("category"):
                covered.setdefault(str(case["fp_id"]), set()).add(str(case["category"]))
        gaps: List[str] = []
        for fp in fps:
            fp_id = str(fp.get("id") or "")
            if not fp_id:
                continue
            for category in ("functional", "exception", "boundary"):
                if category not in covered.get(fp_id, set()):
                    gaps.append(f"{fp_id}:{category}")
        return gaps

    def _binding_gaps(
        self, files: Any, cases: Any, fp_ids: Any
    ) -> List[str]:
        """文件 test_* 方法 ↔ cases 元数据的 1:1 绑定缺口（20261025-035830-af5d 实测）。

        绑定口径与 ``verifier._bind_case_meta`` 一致：按 ``test_{category}_{op}_{name}``
        方法名配对。文件方法无元数据（unbound_method）或元数据 fp_id 为空/未知
        （case_missing_fp / case_unknown_fp）、category 非法（case_bad_category）、
        元数据无对应方法（case_without_method）——前几类会让冻结清单带着 fp_id="" 的
        用例签名，执行后凭证必带 case_missing_fp_id → 套件解冻 → 测试节点失败回退，
        连续复现即撞回退上限（实测两轮 90 方法仅绑定 83）。
        """
        defined: set = set()
        for item in files or []:
            if not isinstance(item, dict):
                continue
            path, content = str(item.get("path") or ""), str(item.get("content") or "")
            if not path.startswith("tests/") or not path.endswith(".py") or not content:
                continue
            try:
                tree = ast.parse(content)
            except SyntaxError:
                continue          # 冻结阶段的 AST 解析会给出精确到文件的报错
            for node in tree.body:
                if not isinstance(node, ast.ClassDef) \
                        or not TestVerifier._is_test_case(node):
                    continue
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                            and child.name.startswith("test"):
                        defined.add(child.name)
        fp_set = {str(fp) for fp in (fp_ids or ()) if str(fp)}
        gaps: List[str] = []
        declared: set = set()
        for case in cases or []:
            if not isinstance(case, dict):
                continue
            category = str(case.get("category") or "")
            method = "test_" + "_".join(
                part for part in (category, str(case.get("op") or ""),
                                  str(case.get("name") or "")) if part)
            if method == "test_":
                continue
            declared.add(method)
            if method not in defined:
                gaps.append(f"case_without_method:{method}")
            if category not in ("functional", "exception", "boundary"):
                gaps.append(f"case_bad_category:{method}:{category or 'empty'}")
            fp_id = str(case.get("fp_id") or "")
            if not fp_id:
                gaps.append(f"case_missing_fp:{method}")
            elif fp_set and fp_id not in fp_set:
                gaps.append(f"case_unknown_fp:{method}:{fp_id}")
        gaps.extend(f"unbound_method:{method}" for method in sorted(defined - declared))
        return gaps

    def _clear_foreign_tests(self, task: NodeTask, declared: Any) -> List[str]:
        """首次冻结前清除 tests/ 下非本套件声明的外来 .py（研发自测等）。

        developer.can_write_tests=true，研发自测文件会留在 tests/；它们不遵循
        test_{category}_{op}_{name} 命名、没有 cases 元数据，冻结会把整个 tests/
        扫进清单，绑定缺口使执行凭证必带 case_missing_fp_id
        （20261025-053247-7c17 实测：unbound:test_lengths 等研发辅助方法被兜底拦截）。
        冻结后 restore_if_changed 本就会删除后加进 tests/ 的文件——首次冻结前先
        对齐「tests/ = 冻结套件」这一不变量。__init__.py 无测试方法、不参与
        绑定，保留以免破坏测试包导入。
        """
        declared_set = {str(path).replace("\\", "/") for path in (declared or ())}
        tests_dir = self.ctx.sandbox.root / "tests"
        if not tests_dir.is_dir():
            return []
        cleared: List[str] = []
        for path in sorted(tests_dir.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            relative = path.relative_to(self.ctx.sandbox.root).as_posix()
            if relative in declared_set or path.name == "__init__.py":
                continue
            try:
                path.unlink()
                cleared.append(relative)
            except OSError:
                pass
        if cleared:
            self.ctx.events.emit(
                "test_foreign_files_cleared", agent=self.role_id, node=task.node_id,
                iteration=task.iteration, severity="warning",
                payload={"files": cleared,
                         "note": "首次冻结前清除 tests/ 下非本套件声明的外来文件"
                                 "（研发自测等）；冻结清单只收可绑定元数据的用例"},
            )
        return cleared

    def _ensure_full_coverage(
        self,
        task: NodeTask,
        generation: Generation,
        data: Dict[str, Any],
        fps: List[Dict[str, Any]],
        result: AgentResult,
    ) -> Tuple[Dict[str, Any], Generation]:
        """冻结前校验覆盖与元数据绑定并给一次带缺口清单的重生成（镜像隐藏套件的修复闭环）。

        返回 (data, generation)；修复失败时置 result.status=fail（不冻结任何套件）。
        """
        fp_ids = [str(fp.get("id") or "") for fp in fps if fp.get("id")]
        gaps = self._coverage_gaps(data.get("cases"), fps) \
            + self._binding_gaps(data.get("files"), data.get("cases"), fp_ids)
        if not gaps:
            return data, generation
        if self.ctx.llm is None:
            # 直接调用 Agent 的单元测试没有 LLM 客户端（生产 Leader 必带）：
            # 与 hidden_suite_skipped 同一先例，跳过校验并留痕，保持既有行为。
            self.ctx.events.emit(
                "test_coverage_skipped", agent=self.role_id, node=task.node_id,
                iteration=task.iteration, severity="warning",
                payload={"gaps": gaps[:20],
                         "reason": "当前上下文没有 LLM 客户端（单元测试直连），跳过冻结前覆盖校验"},
            )
            return data, generation
        max_attempts = max(1, int(self.ctx.config.runtime.get("test_coverage_max_attempts", 2) or 2))
        fp_view = [
            {k: fp.get(k) for k in ("id", "title", "description", "category", "rules", "samples") if fp.get(k)}
            for fp in fps
        ]
        attempt = 1
        while gaps and attempt < max_attempts:
            self.ctx.events.emit(
                "test_coverage_repair_requested", agent=self.role_id, node=task.node_id,
                iteration=task.iteration, severity="warning",
                payload={"attempt": attempt + 1, "gaps": gaps[:30],
                         "gap_count": len(gaps)},
            )
            system = (
                "你是业务测试工程师。上一轮测试套件的功能点覆盖不完整，验收门禁要求"
                "每个功能点都有 functional / exception / boundary 三类用例。"
                "你只能依据需求契约与既有用例补齐覆盖，看不到实现源码，期望值只能来自契约。"
            )
            user = (
                "## 需求功能点（必须逐个、逐类覆盖）\n"
                + json.dumps(fp_view, ensure_ascii=False, indent=1)
                + "\n\n## 上一轮已生成的用例元数据（在此基础上补齐，不要删减）\n"
                + json.dumps(data.get("cases") or [], ensure_ascii=False, indent=1)
                + "\n\n## 上一轮已生成的测试文件（作为补齐基础全量重写）\n"
                + json.dumps(data.get("files") or [], ensure_ascii=False, indent=1)
                + "\n\n## 缺口清单（两类：FP:类别 = 覆盖缺口，每个至少补一条用例；"
                "unbound_method:/case_without_method:/case_missing_fp:/case_unknown_fp:/case_bad_category: = 绑定缺口，"
                "文件里的 test_* 方法与 cases 元数据必须一一对应——多余方法删除、缺元数据的补元数据、fp_id/category 修正）\n"
                + "\n".join(f"- {item}" for item in gaps)
                + "\n\n## 输出契约（必须只输出一个 JSON 对象）\n"
                + json.dumps({
                    "files": [{"path": "tests/test_all.py",
                               "content": "完整 Python 源码（全量重写，包含全部用例）"}],
                    "cases": [{"id": "TC-FP-01-functional-xxx", "fp_id": "FP-01",
                               "op": "被测函数名(ASCII)", "name": "用例短名(ASCII)",
                               "category": "functional|exception|boundary",
                               "call": "调用表达式", "expect": "期望值",
                               "expect_error": "异常类名或空串",
                               "assert_kind": "eq|truthy|falsy|empty|raises"}],
                    "case_count": {"total": 0, "functional": 0, "exception": 0, "boundary": 0},
                    "derived_from": "coverage-repair",
                }, ensure_ascii=False, indent=1)
                + "\n\n规则：方法名 test_{category}_{op}_{name}（ASCII、全局唯一）；"
                "所有用例写在同一个 tests/ 下的 .py 文件里；仅用标准库 import 被测包公开接口；"
                "禁止 main/结果标记/结果文件；cases 元数据与方法一一对应。"
            )
            bundle = PromptBundle(
                role=self.role_id, node=task.node_id, task_kind="test_suite_repair",
                iteration=task.iteration, system=system, user=user,
                context={"task_kind": "test_suite_repair"},
                token_estimate=(len(system) + len(user)) // 3, path=None,
            )
            try:
                generation = self.ctx.llm.generate("test_suite_repair", bundle, {})
            except Exception as exc:  # noqa: BLE001 - 修复失败按缺口如实判失败
                result.status = "fail"
                result.error = f"测试覆盖率修复生成失败: {type(exc).__name__}: {exc}"
                result.summary = result.error
                return data, generation
            if generation.error or not isinstance(generation.data, dict):
                result.status = "fail"
                result.error = "测试覆盖率修复未返回可解析的 JSON: " + str(generation.error or "")
                result.summary = result.error
                return data, generation
            data = generation.data
            attempt += 1
            gaps = self._coverage_gaps(data.get("cases"), fps) \
                + self._binding_gaps(data.get("files"), data.get("cases"), fp_ids)
        if gaps:
            result.status = "fail"
            result.error = ("测试套件覆盖/绑定不全（冻结前校验，套件一旦冻结缺口将无法弥补）: "
                            + ", ".join(gaps[:20]))
            result.summary = result.error
            self.ctx.events.emit(
                "test_coverage_insufficient", agent=self.role_id, node=task.node_id,
                iteration=task.iteration, severity="error",
                payload={"gaps": gaps[:30], "gap_count": len(gaps),
                         "attempts": attempt,
                         "note": "未冻结任何套件；回退后测试节点将重新生成"},
            )
        else:
            self.ctx.events.emit(
                "test_coverage_repaired", agent=self.role_id, node=task.node_id,
                iteration=task.iteration,
                payload={"attempts": attempt,
                         "note": "覆盖缺口已在冻结前补齐"},
            )
        return data, generation

    def _coverage(self, cases: List[Dict[str, Any]], fp_total: int, data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        coverage: Dict[str, Dict[str, Any]] = {}
        for category in ("functional", "exception", "boundary"):
            category_cases = [
                case for case in cases
                if isinstance(case, dict) and case.get("category") == category
            ]
            fps = {case.get("fp_id") for case in category_cases if case.get("fp_id")}
            coverage[category] = {
                "total": len(category_cases),
                "fps": len(fps),
                "fps_total": fp_total,
                "rate": round(len(fps) / fp_total, 4) if fp_total else 0.0,
            }
        return coverage

    # ---------------------------------------------------------------- 缺陷
    def _track_bugs(
        self,
        executed: List[Dict[str, Any]],
        generated: List[Dict[str, Any]],
        task: NodeTask,
        perception: Perception,
    ) -> Dict[str, Any]:
        method_to_case = {}
        for case in generated:
            if not isinstance(case, dict):
                continue          # 契约外防御：跳过结构非法的用例元数据
            method = f"test_{case.get('category')}_{case.get('op')}_{case.get('name')}"
            method_to_case[method] = case

        previous = {
            item.get("test"): item
            for item in ((perception.found.get("bug_reports") or {}).get("items") or [])
        }
        items: List[Dict[str, Any]] = []
        new_ids: List[str] = []
        for case in executed:
            if not isinstance(case, dict):
                continue          # 契约外防御：跳过结构非法的执行记录
            method = str(case.get("test", ""))
            test_ref = f"{case.get('class', '')}.{method}"
            status = case.get("status", "passed")
            prior = previous.get(test_ref)
            if status == "passed":
                if prior and prior.get("status") == "open":
                    prior.update({"status": "fixed", "fixed_iteration": task.iteration,
                                  "verified_by": "tester"})
                    items.append(prior)
                elif prior:
                    prior.update({"status": "fixed", "fixed_iteration": prior.get("fixed_iteration", task.iteration)})
                    items.append(prior)
                continue
            meta = method_to_case.get(method, {})
            if prior:
                prior.update({
                    "status": "open",
                    "symptom": str(case.get("message", ""))[:200] or prior.get("symptom", ""),
                    "detected_iteration": prior.get("detected_iteration", task.iteration),
                })
                items.append(prior)
                continue
            import hashlib

            bug = {
                "id": "BUG-" + hashlib.sha1(test_ref.encode("utf-8")).hexdigest()[:8],
                "test": test_ref,
                "fp_id": case.get("fp_id") or meta.get("fp_id", ""),
                "op": str(meta.get("op", "")),
                "category": case.get("category") or meta.get("category", "functional"),
                "severity": _SEVERITY.get(str(case.get("category", "")), "major"),
                "symptom": str(case.get("message", ""))[:200],
                "status": "open",
                "detected_iteration": task.iteration,
                "fixed_iteration": None,
                "evidence": {"log": f"test_runs/iter{task.iteration:02d}/stdout.txt"},
            }
            items.append(bug)
            new_ids.append(bug["id"])
        # 缺陷闭环判定只在本轮真实执行到用例时生效：一条都没跑成（协议违规/文件崩溃）
        # 不构成"缺陷已修复"的证据（task04-cpp-probe-02 iter02 实测：32 缺陷被自动闭环，
        # bug_reports.open 归 0，门禁第 6 项被虚假满足）。无证据时旧缺陷原样保留为 open。
        if executed:
            for prior in previous.values():
                if prior.get("status") == "open" and not any(item.get("id") == prior.get("id") for item in items):
                    if prior.get("source") == "hidden":
                        # 隐藏用例缺陷只在 acceptance 现场执行（20261025-063338-26c1）：
                        # 可见套件跑不到它 ≠ 已修复，闭环证据只能来自隐藏门禁复跑
                        # （Leader._merge_hidden_bugs 在隐藏套件通过时统一改 fixed）。
                        items.append(prior)
                        continue
                    prior.update({"status": "fixed", "fixed_iteration": task.iteration, "verified_by": "tester"})
                    items.append(prior)
        else:
            for prior in previous.values():
                if prior.get("status") == "open":
                    items.append(prior)
        open_count = sum(1 for item in items if item.get("status") == "open")
        fixed_count = sum(1 for item in items if item.get("status") == "fixed")
        return {
            "items": items,
            "total": len(items),
            "open": open_count,
            "fixed": fixed_count,
            "closed": fixed_count,
            "new": new_ids,
        }
