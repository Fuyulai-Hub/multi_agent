"""执行沙箱与静态分析。

- 沙箱：在 run 的工作目录内执行命令（Python 脚本 / 测试），采集真实证据（stdout/stderr/exit code/耗时）
- 静态分析：基于 AST 的真实代码检查（文档字符串、类型注解、异常处理、危险调用、复杂度等）
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import SystemConfig
from .errors import SandboxError

__all__ = ["ExecResult", "AnalysisResult", "Sandbox", "Finding"]


@dataclass
class ExecResult:
    cmd: List[str]
    cwd: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    ok: bool = False
    parsed: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cmd": self.cmd,
            "cwd": self.cwd,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "ok": self.ok,
            "stdout_tail": self.stdout[-2000:],
            "stderr_tail": self.stderr[-2000:],
            "parsed": self.parsed,
        }

    def tail(self, text: str = "", limit: int = 1200) -> str:
        body = (self.stdout + "\n" + self.stderr).strip()
        return body[-limit:] if body else text


@dataclass
class Finding:
    """静态分析/审查发现。"""

    rule: str
    severity: str
    file: str
    line: int = 0
    symbol: str = ""
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "file": self.file,
            "line": self.line,
            "symbol": self.symbol,
            "message": self.message,
        }


@dataclass
class AnalysisResult:
    findings: List[Finding] = field(default_factory=list)
    files: List[str] = field(default_factory=list)
    loc: int = 0
    functions: List[Dict[str, Any]] = field(default_factory=list)
    classes: int = 0
    syntax_errors: List[Dict[str, Any]] = field(default_factory=list)

    def rules_count(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for finding in self.findings:
            counts[finding.rule] = counts.get(finding.rule, 0) + 1
        return counts


class Sandbox:
    """工作目录受限的命令执行器。"""

    def __init__(self, root: Path, config: SystemConfig, *, events: Any = None) -> None:
        self.root = Path(root).resolve()
        self.config = config
        self.events = events
        self.root.mkdir(parents=True, exist_ok=True)
        self.python = str(config.runtime.get("python") or sys.executable or "python")
        self.timeout = int(config.runtime.get("sandbox_timeout", 180))

    # ---------------------------------------------------------------- 文件
    def resolve(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if self.root not in path.parents and path != self.root:
            raise SandboxError(f"路径越界（沙箱只允许写入 {self.root} 内）: {relative}")
        return path

    def write_file(self, relative: str, content: str) -> Path:
        path = self.resolve(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def read_file(self, relative: str) -> str:
        path = self.resolve(relative)
        if not path.exists():
            raise SandboxError(f"文件不存在: {relative}")
        return path.read_text(encoding="utf-8")

    def exists(self, relative: str) -> bool:
        return self.resolve(relative).exists()

    def list_files(self, sub: str = ".", pattern: str = "**/*") -> List[str]:
        base = self.resolve(sub)
        if not base.exists():
            return []
        return sorted(
            str(path.relative_to(self.root)).replace("\\", "/")
            for path in base.glob(pattern)
            if path.is_file()
        )

    def sha256(self, relative: str) -> str:
        path = self.resolve(relative)
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16] if path.exists() else ""

    def code_stats(self, files: List[str]) -> Dict[str, Any]:
        total_loc = 0
        stats: List[Dict[str, Any]] = []
        for relative in files:
            path = self.resolve(relative)
            if not path.exists() or path.suffix != ".py":
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
            code_lines = [line for line in lines if line.strip() and not line.strip().startswith("#")]
            total_loc += len(code_lines)
            stats.append({
                "file": relative,
                "loc": len(code_lines),
                "sha256": self.sha256(relative),
                "size": path.stat().st_size,
            })
        return {"files": stats, "total_loc": total_loc, "file_count": len(stats)}

    # ---------------------------------------------------------------- 执行
    def run(
        self,
        cmd: List[str],
        *,
        cwd: Optional[str] = None,
        timeout: Optional[int] = None,
        env_extra: Optional[Dict[str, str]] = None,
        result_marker: Optional[str] = None,
    ) -> ExecResult:
        workdir = self.resolve(cwd) if cwd else self.root
        env = dict(os.environ)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
        if env_extra:
            env.update(env_extra)
        started = time.time()
        try:
            completed = subprocess.run(
                cmd,
                cwd=str(workdir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout or self.timeout,
                env=env,
                shell=False,
            )
            exit_code, stdout, stderr = completed.returncode, completed.stdout or "", completed.stderr or ""
        except subprocess.TimeoutExpired as exc:
            exit_code = 124
            stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            stderr = f"命令超时（>{timeout or self.timeout}s）: {' '.join(cmd)}"
        except FileNotFoundError as exc:
            raise SandboxError(f"命令不可用: {cmd[0]}（{exc}）") from exc
        duration_ms = int((time.time() - started) * 1000)
        result = ExecResult(
            cmd=list(cmd),
            cwd=str(workdir),
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            ok=exit_code == 0,
        )
        if result_marker:
            result.parsed = self._extract_marker(stdout, result_marker)
        if self.events is not None:
            self.events.emit(
                "sandbox_exec",
                agent="sandbox",
                payload={
                    "cmd": " ".join(cmd),
                    "exit_code": exit_code,
                    "duration_ms": duration_ms,
                    "ok": result.ok,
                },
            )
        return result

    def run_python(self, script: str, *, args: Optional[List[str]] = None, cwd: Optional[str] = None,
                   result_marker: Optional[str] = None, timeout: Optional[int] = None) -> ExecResult:
        cmd = [self.python, script] + list(args or [])
        return self.run(cmd, cwd=cwd, result_marker=result_marker, timeout=timeout)

    @staticmethod
    def _extract_marker(stdout: str, marker: str) -> Optional[Dict[str, Any]]:
        if marker not in stdout:
            return None
        tail = stdout.split(marker, 1)[1].strip()
        for end in (tail.find("\n"), len(tail)):
            chunk = tail[:end].strip()
            if not chunk:
                continue
            try:
                return json.loads(chunk)
            except json.JSONDecodeError:
                # 结果可能是多行 JSON，尝试逐行累积
                buffer: List[str] = []
                for line in tail.splitlines():
                    buffer.append(line)
                    try:
                        return json.loads("\n".join(buffer))
                    except json.JSONDecodeError:
                        continue
        return None

    # ---------------------------------------------------------------- 静态分析
    def analyze(self, files: List[str], policies: Dict[str, Any]) -> AnalysisResult:
        severity_map: Dict[str, str] = dict(policies.get("severity_map") or {})
        max_lines = int(policies.get("max_function_lines", 60))
        max_line_length = int(policies.get("max_line_length", 120))
        require_module_doc = bool(policies.get("require_module_docstring", True))
        require_function_doc = bool(policies.get("require_function_docstring", True))
        require_hints = bool(policies.get("require_type_hints", True))
        result = AnalysisResult(files=list(files))

        def severity(rule: str, default: str = "minor") -> str:
            return severity_map.get(rule, default)

        for relative in files:
            path = self.resolve(relative)
            if not path.exists() or path.suffix != ".py":
                continue
            source = path.read_text(encoding="utf-8")
            lines = source.splitlines()
            try:
                tree = ast.parse(source)
            except SyntaxError as exc:
                result.syntax_errors.append({"file": relative, "line": exc.lineno, "message": exc.msg})
                result.findings.append(Finding(
                    rule="syntax_error", severity=severity("syntax_error", "critical"),
                    file=relative, line=exc.lineno or 0, message=f"语法错误: {exc.msg}",
                ))
                continue

            code_lines = [line for line in lines if line.strip() and not line.strip().startswith("#")]
            result.loc += len(code_lines)
            result.classes += sum(1 for node in ast.walk(tree) if isinstance(node, ast.ClassDef))

            if require_module_doc and not ast.get_docstring(tree):
                result.findings.append(Finding(
                    rule="missing_module_docstring", severity=severity("missing_module_docstring"),
                    file=relative, line=1, message="模块缺少文档字符串",
                ))

            imported_names: Dict[str, int] = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imported_names[(alias.asname or alias.name).split(".")[0]] = node.lineno
                elif isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        imported_names[alias.asname or alias.name] = node.lineno

            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                args = [arg for arg in node.args.args if arg.arg not in ("self", "cls")]
                arg_names = [arg.arg for arg in args]
                length = (getattr(node, "end_lineno", node.lineno) or node.lineno) - node.lineno + 1
                result.functions.append({
                    "name": node.name,
                    "file": relative,
                    "line": node.lineno,
                    "lines": length,
                    "args": arg_names,
                    "has_docstring": bool(ast.get_docstring(node)),
                })
                if require_function_doc and not ast.get_docstring(node):
                    result.findings.append(Finding(
                        rule="missing_function_docstring", severity=severity("missing_function_docstring"),
                        file=relative, line=node.lineno, symbol=node.name, message="公开函数缺少文档字符串",
                    ))
                missing_hints = [name for name, arg in zip(arg_names, args) if arg.annotation is None]
                if node.returns is None and not node.name.startswith("_"):
                    missing_hints.append("return")
                if require_hints and missing_hints:
                    result.findings.append(Finding(
                        rule="missing_type_hints", severity=severity("missing_type_hints"),
                        file=relative, line=node.lineno, symbol=node.name,
                        message=f"缺少类型注解: {', '.join(missing_hints)}",
                    ))
                if length > max_lines:
                    result.findings.append(Finding(
                        rule="function_too_long", severity=severity("function_too_long"),
                        file=relative, line=node.lineno, symbol=node.name,
                        message=f"函数长度 {length} 行，超过阈值 {max_lines} 行",
                    ))
                defaults = [d for d in list(node.args.defaults) + [d for d in node.args.kw_defaults if d is not None]]
                for default in defaults:
                    if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                        result.findings.append(Finding(
                            rule="mutable_default", severity=severity("mutable_default"),
                            file=relative, line=getattr(default, "lineno", node.lineno), symbol=node.name,
                            message="使用了可变对象作为默认参数",
                        ))
                for sub in ast.walk(node):
                    if isinstance(sub, ast.ExceptHandler) and sub.type is None:
                        result.findings.append(Finding(
                            rule="bare_except", severity=severity("bare_except"),
                            file=relative, line=getattr(sub, "lineno", node.lineno), symbol=node.name,
                            message="裸 except 会吞掉所有异常",
                        ))
                    if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id in {"eval", "exec"}:
                        result.findings.append(Finding(
                            rule="eval_usage", severity=severity("eval_usage", "critical"),
                            file=relative, line=getattr(sub, "lineno", node.lineno), symbol=node.name,
                            message=f"使用了危险调用 {sub.func.id}()",
                        ))

            for name, lineno in imported_names.items():
                occurrences = len(re.findall(rf"\b{re.escape(name)}\b", source))
                if occurrences <= 1:
                    result.findings.append(Finding(
                        rule="unused_import", severity=severity("unused_import"),
                        file=relative, line=lineno, symbol=name, message=f"导入 {name} 未被使用",
                    ))

            for index, line in enumerate(lines, start=1):
                if len(line) > max_line_length:
                    result.findings.append(Finding(
                        rule="line_too_long", severity=severity("line_too_long", "info"),
                        file=relative, line=index, message=f"单行长度 {len(line)} 超过 {max_line_length}",
                    ))
                if re.search(r"#\s*(TODO|FIXME)", line, re.IGNORECASE):
                    result.findings.append(Finding(
                        rule="todo_comment", severity=severity("todo_comment", "info"),
                        file=relative, line=index, message="存在 TODO/FIXME 注释",
                    ))
        return result

    # ---------------------------------------------------------------- 便捷
    def python_version(self) -> str:
        try:
            result = self.run([self.python, "-c", "import sys;print(sys.version.split()[0])"])
            return result.stdout.strip() or "unknown"
        except SandboxError:
            return "unknown"
