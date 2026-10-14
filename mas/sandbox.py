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

from .concurrency import parallel_map
from .config import SystemConfig
from .errors import SandboxError

__all__ = ["ExecResult", "AnalysisResult", "Sandbox", "Finding"]

# 网络相关模块：沙箱无网络隔离（标准库无跨平台网络命名空间方案），
# 对 LLM 生成的交付代码做静态检测，让 reviewer 能显式拦截高危外连依赖。
_NETWORK_MODULES = {
    "socket", "ssl", "urllib", "urllib2", "urllib3", "http", "httplib", "requests",
    "aiohttp", "ftplib", "telnetlib", "smtplib", "asyncio", "paramiko", "websocket",
}

# 命令白名单默认值：本系统所有沙箱命令都是 Python 解释器调用（跑实现/跑测试），
# 默认不放行任何 shell 解释器（cmd/sh/bash -c 可绕过一切路径级约束）。
DEFAULT_ALLOWED_COMMANDS = ("python", "python3", "py")
# 环境变量敏感词：命中即从沙箱子进程环境剔除，防止交付代码/测试窃取 LLM 凭据
_SENSITIVE_ENV_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")
# POSIX 资源限制默认值（秒/字节），防止失控脚本耗尽机器资源
DEFAULT_RLIMITS = {
    "cpu_seconds": 300,        # RLIMIT_CPU：单命令 CPU 时间上限（超时 kill 之外的硬闸
    "address_space_bytes": 1 << 30,   # RLIMIT_AS：1GB 虚拟内存上限
    "file_size_bytes": 64 << 20,      # RLIMIT_FSIZE：单文件 64MB，防日志打满磁盘
    "max_processes": 128,       # RLIMIT_NPROC：防 fork 炸弹
}


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
    """工作目录受限的命令执行器。

    隔离层次（由弱到强）：
    1. 路径级：resolve() 拒绝越界读写（既有能力）；
    2. 命令级：白名单只放行 Python 解释器，拒绝 shell 解释器；
    3. 环境级：剔除含 KEY/TOKEN/SECRET 等敏感词的环境变量，防凭据泄露给被测代码；
    4. 资源级（仅 POSIX 单线程进程）：rlimit 限制 CPU/内存/文件大小/进程数；
    5. 检测级：静态分析 network_import 规则标记外连依赖（沙箱无网络命名空间隔离，
       这一步是"让高危依赖可见"，不是隔离，已在 finding 消息中如实标注）。
    """

    def __init__(self, root: Path, config: SystemConfig, *, events: Any = None) -> None:
        self.root = Path(root).resolve()
        self.config = config
        self.events = events
        self.root.mkdir(parents=True, exist_ok=True)
        self.python = str(config.runtime.get("python") or sys.executable or "python")
        self.timeout = int(config.runtime.get("sandbox_timeout", 180))
        # 命令白名单（runtime.sandbox_command_policy 可覆盖；enabled=false 可关闭）
        policy = dict(config.runtime.get("sandbox_command_policy") or {})
        self.command_policy_enabled = bool(policy.get("enabled", True))
        self.allowed_commands = {
            str(item).lower() for item in (policy.get("allowed") or DEFAULT_ALLOWED_COMMANDS)
        }
        self.strip_sensitive_env = bool(config.runtime.get("sandbox_strip_sensitive_env", True))
        limits = dict(config.runtime.get("sandbox_limits") or DEFAULT_RLIMITS)
        self.rlimits = {key: int(value) for key, value in limits.items() if int(value) > 0}

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
        """交付文件行数统计。

        total_loc 统计全部文本交付文件（含 .hpp/.cpp 等非 Python），python_loc 只统计 .py；
        原实现只认 .py，非 Python 交付物的 manifest 会谎报「2 个文件，0 行」（实测 task04）。
        """
        total_loc = 0
        python_loc = 0
        stats: List[Dict[str, Any]] = []
        for relative in files:
            path = self.resolve(relative)
            if not path.exists():
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                continue          # 二进制/不可读文件不计行
            code_lines = [
                line for line in lines
                if line.strip() and not line.strip().startswith(("#", "//"))
            ]
            language = "python" if path.suffix == ".py" else (path.suffix.lstrip(".") or "text")
            total_loc += len(code_lines)
            if language == "python":
                python_loc += len(code_lines)
            stats.append({
                "file": relative,
                "loc": len(code_lines),
                "language": language,
                "sha256": self.sha256(relative),
                "size": path.stat().st_size,
            })
        return {"files": stats, "total_loc": total_loc, "python_loc": python_loc,
                "file_count": len(stats)}

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
        self._check_command(cmd)
        env = self._build_env(env_extra)
        started = time.time()
        spawn_kwargs = self._spawn_kwargs()
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
                **spawn_kwargs,
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
                    "isolation": self._isolation_label(),
                },
            )
        return result

    # ---------------------------------------------------------------- 隔离策略
    def _check_command(self, cmd: List[str]) -> None:
        """命令白名单：只放行 Python 解释器；显式拒绝 shell 解释器（-c 可绕过路径约束）。

        匹配规则：取可执行文件 basename，去 .exe 后缀后——
        ① 与白名单条目全等（python / python3 / py）；
        ② 或匹配白名单条目 + 版本号后缀（python3.13 / python3.13.exe，兼容绝对路径调用）。
        """
        if not cmd:
            raise SandboxError("空命令")
        raw_name = Path(str(cmd[0])).name.lower()
        executable = raw_name[:-4] if raw_name.endswith(".exe") else raw_name
        if executable in {"cmd", "sh", "bash", "zsh", "powershell", "pwsh"}:
            raise SandboxError(f"沙箱禁止执行 shell 解释器: {cmd[0]}（请改用 python 直接执行脚本）")
        if self.command_policy_enabled and not self._executable_allowed(executable):
            raise SandboxError(
                f"命令不在沙箱白名单: {cmd[0]}（允许: {sorted(self.allowed_commands)}；"
                f"可在 system.yaml runtime.sandbox_command_policy 中调整）"
            )

    def _executable_allowed(self, executable: str) -> bool:
        if executable in self.allowed_commands:
            return True
        # python3.13 / python3.13.exe 这类带版本号的解释器名
        return any(
            re.fullmatch(re.escape(entry) + r"[\d.]*", executable)
            for entry in self.allowed_commands
        )

    def _build_env(self, env_extra: Optional[Dict[str, str]]) -> Dict[str, str]:
        """构造子进程环境：默认剔除敏感变量（LLM 凭据等），显式 env_extra 仍可注入。"""
        env = dict(os.environ)
        if self.strip_sensitive_env:
            for key in [name for name in env if any(marker in name.upper() for marker in _SENSITIVE_ENV_MARKERS)]:
                env.pop(key, None)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
        if env_extra:
            env.update(env_extra)
        return env

    def _spawn_kwargs(self) -> Dict[str, Any]:
        """POSIX 单线程场景下注入 rlimit preexec_fn。

        取舍说明：preexec_fn 在 fork→exec 窗口内运行 Python 代码，多线程进程中有
        极小概率死锁（子进程继承父进程的锁状态）。因此仅当进程单线程
        （CLI 直跑）时启用；多线程场景（前端面板）跳过并在 sandbox_exec 事件中
        标注降级，保证可用性优先。
        """
        if os.name == "nt" or not self.rlimits:
            return {}
        import threading

        if threading.active_count() > 1:
            return {}
        return {"preexec_fn": self._apply_rlimits}

    def _apply_rlimits(self) -> None:  # pragma: no cover - 仅在 POSIX 子进程上下文中运行
        import resource

        mapping = {
            "cpu_seconds": resource.RLIMIT_CPU,
            "address_space_bytes": resource.RLIMIT_AS,
            "file_size_bytes": resource.RLIMIT_FSIZE,
            "max_processes": resource.RLIMIT_NPROC,
        }
        for key, value in self.rlimits.items():
            limit = mapping.get(key)
            if limit is not None:
                try:
                    resource.setrlimit(limit, (value, value))
                except (ValueError, OSError):
                    continue  # 平台不支持的限制项跳过，不阻断执行

    def _isolation_label(self) -> str:
        """本次执行的隔离等级标签（写入事件，便于审计实际生效的隔离措施）。"""
        if os.name == "nt":
            limits = "windows-no-rlimit"
        elif not self.rlimits:
            limits = "no-rlimit"
        elif __import__("threading").active_count() > 1:
            limits = "rlimit-skipped-multithread"
        else:
            limits = "rlimit-active"
        return f"path+cmd{' +env-strip' if self.strip_sensitive_env else ''}+{limits}"

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
        """按文件并行做 AST 静态分析（各文件分析相互独立，无共享状态，可安全并行）。"""
        severity_map: Dict[str, str] = dict(policies.get("severity_map") or {})
        max_lines = int(policies.get("max_function_lines", 60))
        max_line_length = int(policies.get("max_line_length", 120))
        require_module_doc = bool(policies.get("require_module_docstring", True))
        require_function_doc = bool(policies.get("require_function_docstring", True))
        require_hints = bool(policies.get("require_type_hints", True))
        result = AnalysisResult(files=list(files))

        def analyze_one(relative: str) -> Optional[Dict[str, Any]]:
            return self._analyze_file(
                relative,
                severity_map=severity_map,
                max_lines=max_lines,
                max_line_length=max_line_length,
                require_module_doc=require_module_doc,
                require_function_doc=require_function_doc,
                require_hints=require_hints,
            )

        # 并行度可被 runtime.sandbox_analyze_workers 覆盖（默认 IO 等待型负载，见 concurrency 模块）
        workers = self.config.runtime.get("sandbox_analyze_workers")
        per_file = parallel_map(
            analyze_one, [name for name in files if self._analyzable(name)],
            max_workers=int(workers) if workers else None,
        )
        for item in per_file:
            if item is None:
                continue
            result.findings.extend(item["findings"])
            result.loc += item["loc"]
            result.classes += item["classes"]
            result.functions.extend(item["functions"])
            result.syntax_errors.extend(item["syntax_errors"])
        return result

    def _analyzable(self, relative: str) -> bool:
        try:
            path = self.resolve(relative)
        except SandboxError:
            return False
        return path.exists() and path.suffix == ".py"

    def _analyze_file(
        self,
        relative: str,
        *,
        severity_map: Dict[str, str],
        max_lines: int,
        max_line_length: int,
        require_module_doc: bool,
        require_function_doc: bool,
        require_hints: bool,
    ) -> Optional[Dict[str, Any]]:
        """单文件 AST 分析：返回可合并的中间结果（findings/loc/classes/functions/syntax_errors）。"""
        def severity(rule: str, default: str = "minor") -> str:
            return severity_map.get(rule, default)

        path = self.resolve(relative)
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        findings: List[Finding] = []
        functions: List[Dict[str, Any]] = []
        syntax_errors: List[Dict[str, Any]] = []
        loc = 0
        classes = 0
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            syntax_errors.append({"file": relative, "line": exc.lineno, "message": exc.msg})
            findings.append(Finding(
                rule="syntax_error", severity=severity("syntax_error", "critical"),
                file=relative, line=exc.lineno or 0, message=f"语法错误: {exc.msg}",
            ))
            return {"findings": findings, "loc": 0, "classes": 0,
                    "functions": functions, "syntax_errors": syntax_errors}

        code_lines = [line for line in lines if line.strip() and not line.strip().startswith("#")]
        loc = len(code_lines)
        classes = sum(1 for node in ast.walk(tree) if isinstance(node, ast.ClassDef))

        if require_module_doc and not ast.get_docstring(tree):
            findings.append(Finding(
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
            functions.append({
                "name": node.name,
                "file": relative,
                "line": node.lineno,
                "lines": length,
                "args": arg_names,
                "has_docstring": bool(ast.get_docstring(node)),
            })
            if require_function_doc and not ast.get_docstring(node):
                findings.append(Finding(
                    rule="missing_function_docstring", severity=severity("missing_function_docstring"),
                    file=relative, line=node.lineno, symbol=node.name, message="公开函数缺少文档字符串",
                ))
            missing_hints = [name for name, arg in zip(arg_names, args) if arg.annotation is None]
            if node.returns is None and not node.name.startswith("_"):
                missing_hints.append("return")
            if require_hints and missing_hints:
                findings.append(Finding(
                    rule="missing_type_hints", severity=severity("missing_type_hints"),
                    file=relative, line=node.lineno, symbol=node.name,
                    message=f"缺少类型注解: {', '.join(missing_hints)}",
                ))
            if length > max_lines:
                findings.append(Finding(
                    rule="function_too_long", severity=severity("function_too_long"),
                    file=relative, line=node.lineno, symbol=node.name,
                    message=f"函数长度 {length} 行，超过阈值 {max_lines} 行",
                ))
            defaults = [d for d in list(node.args.defaults) + [d for d in node.args.kw_defaults if d is not None]]
            for default in defaults:
                if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                    findings.append(Finding(
                        rule="mutable_default", severity=severity("mutable_default"),
                        file=relative, line=getattr(default, "lineno", node.lineno), symbol=node.name,
                        message="使用了可变对象作为默认参数",
                    ))
            for sub in ast.walk(node):
                if isinstance(sub, ast.ExceptHandler) and sub.type is None:
                    findings.append(Finding(
                        rule="bare_except", severity=severity("bare_except"),
                        file=relative, line=getattr(sub, "lineno", node.lineno), symbol=node.name,
                        message="裸 except 会吞掉所有异常",
                    ))
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id in {"eval", "exec"}:
                    findings.append(Finding(
                        rule="eval_usage", severity=severity("eval_usage", "critical"),
                        file=relative, line=getattr(sub, "lineno", node.lineno), symbol=node.name,
                        message=f"使用了危险调用 {sub.func.id}()",
                    ))

        # 网络导入检测：对整棵树做一遍（覆盖模块级与函数内 import），避免逐函数重复计数
        for sub in ast.walk(tree):
            roots: List[str] = []
            if isinstance(sub, ast.Import):
                roots = [(alias.asname or alias.name).split(".")[0] for alias in sub.names]
            elif isinstance(sub, ast.ImportFrom):
                roots = [(sub.module or "").split(".")[0]]
            for root in roots:
                if root in _NETWORK_MODULES:
                    findings.append(Finding(
                        rule="network_import", severity=severity("network_import", "major"),
                        file=relative, line=getattr(sub, "lineno", 0),
                        message=f"导入网络模块 {root}（沙箱无网络隔离，属高危依赖，需审查确认）",
                    ))

        # 命令白名单绕过检测：runtime.sandbox_command_policy 只约束框架自己发起的命令，
        # 沙箱子进程内部再 fork 出的程序不经过 _check_command。实测 task04（C++ 需求）：
        # LLM 生成的测试 harness 里直接 subprocess.run(["g++", ...])，绕过了白名单，
        # 且失败只表现为 19 条用例全败（FileNotFoundError），看不出是"框架无法验证非 Python 交付物"。
        findings.extend(self._command_bypass_findings(tree, relative, severity))

        # 结果回传协议依赖 json.dumps，但 LLM 常漏写 import json（实测两次），
        # 文件跑完即 NameError、一条用例都不回传，框架只能看到"用例数为 0"。
        findings.extend(self._unbound_json_findings(tree, relative, severity))

        for name, lineno in imported_names.items():
            occurrences = len(re.findall(rf"\b{re.escape(name)}\b", source))
            if occurrences <= 1:
                findings.append(Finding(
                    rule="unused_import", severity=severity("unused_import"),
                    file=relative, line=lineno, symbol=name, message=f"导入 {name} 未被使用",
                ))

        for index, line in enumerate(lines, start=1):
            if len(line) > max_line_length:
                findings.append(Finding(
                    rule="line_too_long", severity=severity("line_too_long", "info"),
                    file=relative, line=index, message=f"单行长度 {len(line)} 超过 {max_line_length}",
                ))
            if re.search(r"#\s*(TODO|FIXME)", line, re.IGNORECASE):
                findings.append(Finding(
                    rule="todo_comment", severity=severity("todo_comment", "info"),
                    file=relative, line=index, message="存在 TODO/FIXME 注释",
                ))
        return {
            "findings": findings, "loc": loc, "classes": classes,
            "functions": functions, "syntax_errors": syntax_errors,
        }

    # ---------------------------------------------------------------- 便捷
    _BYPASS_CALLS = {
        "subprocess": {"run", "Popen", "call", "check_output", "check_call"},
        "os": {"system", "popen", "execv", "execve", "execvp", "spawnv", "spawnvp"},
    }

    def _command_bypass_findings(
        self, tree: ast.AST, relative: str, severity: Any
    ) -> List["Finding"]:
        """检出"测试/交付代码内联启动外部程序"的子进程调用。

        字面量目标（`["g++", ...]` / `os.system("cl xx.cpp")`）报 `sandbox_command_bypass`；
        非字面量目标报 `sandbox_command_unresolved`（无法证明它在白名单内）；
        `[sys.executable, "-m", "pkg.cli"]` 可证明就是解释器自身，不报——这是测试 CLI 的合法写法。
        """
        findings: List[Finding] = []
        if not self.command_policy_enabled:
            return findings
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if not isinstance(node.func.value, ast.Name):
                continue
            module = node.func.value.id
            method = node.func.attr
            if method not in self._BYPASS_CALLS.get(module, set()):
                continue
            program = self._literal_program(node.args)
            if program and self._executable_allowed(program):
                continue
            if program:
                findings.append(Finding(
                    rule="sandbox_command_bypass", severity=severity("sandbox_command_bypass", "major"),
                    file=relative, line=getattr(node, "lineno", 0), symbol=f"{module}.{method}",
                    message=(f"{module}.{method}() 直接调用未在沙箱白名单内的程序 `{program}`"
                             "（沙箱命令白名单只校验框架发起的命令，子进程内部再起的程序会绕过它）"),
                ))
                continue
            # 程序名不是字面量（`CXX = os.environ.get("CXX", "g++")` 后 `subprocess.run([CXX, ...])`）：
            # 无法静态确认它在白名单内。`sys.executable` 是唯一可证明合法的动态目标（它就是 Python 自身）。
            if self._is_interpreter_target(node.args):
                continue
            findings.append(Finding(
                rule="sandbox_command_unresolved", severity=severity("sandbox_command_unresolved", "minor"),
                file=relative, line=getattr(node, "lineno", 0), symbol=f"{module}.{method}",
                message=(f"{module}.{method}() 启动的外部程序名不是字面量，静态分析无法确认它在沙箱白名单内"
                         "（若该程序在本机不存在，测试失败属于环境问题而非交付物缺陷）"),
            ))
        return findings

    # 测试 harness / 结果回传协议里最常被 LLM 忘记 import 的标准库模块名。
    # 每个名字都走同一条保守判定（见 _unbound_json_findings）。
    _UNBOUND_IMPORT_CHECKS = ("json", "sys", "os", "re", "time", "math", "subprocess",
                              "itertools", "collections", "datetime", "hashlib", "uuid")

    def _unbound_json_findings(
        self, tree: ast.AST, relative: str, severity: Any
    ) -> List["Finding"]:
        """文件里用了某个标准库模块名，但整个模块从未绑定该名字 → 运行必抛 NameError。

        实测三次：`json`（task04-cpp-probe-02 iter02、task04-cpp-probe-03 iter01）、
        `sys`（task02-eshop-attempt04 iter01 的 `sys.exit(main())`）。LLM 按
        execution_protocol 写 `print(MARKER + json.dumps(summary))` 却漏了 import，
        测试文件跑完不回传结果，框架只看到"0 用例"、覆盖率 0，根因要人工翻 stderr 才知道。
        判定保守：出现 `from x import *`、任何形式的该名字绑定（import/赋值/形参/
        with-as/except-as/推导式目标）都视为已绑定、不报。
        """
        findings = []
        for name in self._UNBOUND_IMPORT_CHECKS:
            used_line = self._first_unbound_use(tree, name)
            if not used_line or self._name_bound(tree, name):
                continue
            rule = f"{name}_not_imported"
            findings.append(Finding(
                rule=rule, severity=severity(rule, "major"),
                file=relative, line=used_line, symbol=name,
                message=f"使用 {name} 但整个文件没有任何 `import {name}`（或等价的 {name} 绑定），"
                        "运行到该行必抛 NameError；测试文件里这会让结果回传协议静默失效、用例统计为 0",
            ))
        return findings

    @staticmethod
    def _first_unbound_use(tree: ast.AST, name: str) -> int:
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                    and node.value.id == name:
                return node.lineno
            if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load):
                return node.lineno
        return 0

    @staticmethod
    def _name_bound(tree: ast.AST, name: str) -> bool:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any((a.asname or a.name).split(".")[0] == name for a in node.names):
                    return True
            elif isinstance(node, ast.ImportFrom):
                if node.module == name or any((a.asname or a.name) == name for a in node.names):
                    return True
                if any(a.name == "*" for a in node.names):
                    return True
            elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.For,
                                   ast.comprehension, ast.NamedExpr)):
                targets = node.targets if isinstance(node, ast.Assign) else [getattr(node, "target", None)]
                for target in targets:
                    if isinstance(target, ast.Name) and target.id == name:
                        return True
                    if isinstance(target, (ast.Tuple, ast.List)):
                        if any(isinstance(e, ast.Name) and e.id == name for e in target.elts):
                            return True
            elif isinstance(node, ast.arg) and node.arg == name:
                return True
            elif isinstance(node, ast.withitem) and isinstance(node.optional_vars, ast.Name) \
                    and node.optional_vars.id == name:
                return True
            elif isinstance(node, ast.ExceptHandler) and node.name == name:
                return True
            elif isinstance(node, (ast.Global, ast.Nonlocal)) and name in node.names:
                return True
        return False

    @staticmethod
    def _is_interpreter_target(args: List[ast.expr]) -> bool:
        """第一个实参是否可证明就是当前 Python 解释器（`[sys.executable, "-m", ...]`）。"""
        if not args:
            return False
        first = args[0]
        head = first.elts[0] if isinstance(first, (ast.List, ast.Tuple)) and first.elts else first
        while isinstance(head, ast.BinOp):        # `[sys.executable, ...] + extra`
            head = head.left
            while isinstance(head, ast.List) and head.elts:
                head = head.elts[0]
        return (isinstance(head, ast.Attribute) and head.attr == "executable"
                and isinstance(head.value, ast.Name) and head.value.id == "sys")

    @staticmethod
    def _literal_program(args: List[ast.expr]) -> str:
        """取调用第一个实参里的可执行程序名；非字面量返回空串。"""
        if not args:
            return ""
        first = args[0]
        target: Optional[ast.expr] = None
        if isinstance(first, (ast.List, ast.Tuple)) and first.elts:
            target = first.elts[0]
        elif isinstance(first, ast.Constant):
            target = first
        if not isinstance(target, ast.Constant) or not isinstance(target.value, str):
            return ""
        text = target.value.strip()
        if not text:
            return ""
        head = text.split()[0]                    # os.system("g++ -std=c++17 a.cpp")
        name = Path(head.replace("\\", "/")).name  # 绝对路径取 basename
        if name.lower().endswith(".exe"):
            name = name[:-4]
        return name.lower()

    def python_version(self) -> str:
        try:
            result = self.run([self.python, "-c", "import sys;print(sys.version.split()[0])"])
            return result.stdout.strip() or "unknown"
        except SandboxError:
            return "unknown"
