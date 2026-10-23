"""把冻结套件交给框架侧 runner 执行，并读回工作区之外的逐用例记录。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import runner as _runner

__all__ = [
    "RUNNER_RELATIVE", "SuiteExecution",
    "execute_frozen_suite", "execute_hidden_suite", "install_runner",
]

#: runner 在沙箱工作区内的固定位置。每次执行前由框架覆盖写入，模型无法靠
#: 预置同名文件取得执行权；它不在 tests/ 内，不参与测试冻结 hash。
RUNNER_RELATIVE = ".mas_verify/runner.py"
#: 容器后端下证据目录的挂载点（宿主 run_dir 在容器内不可见）。
CONTAINER_EVIDENCE_DIR = "/mas-verify"
#: 容器后端下隐藏套件的只读挂载点。
CONTAINER_HIDDEN_DIR = "/mas-hidden"


@dataclass
class SuiteExecution:
    """一次 runner 执行的原始结果与落盘位置。"""

    exec: Any
    result: Optional[Dict[str, Any]]
    io_dir: Path
    manifest_path: Path
    result_path: Path
    cmd: List[str] = field(default_factory=list)


def install_runner(sandbox: Any) -> str:
    """把仓库内的 runner 源码覆盖写入沙箱（框架动作，绕过角色写权限）。"""
    source = Path(_runner.__file__).read_text(encoding="utf-8")
    sandbox.write_file(RUNNER_RELATIVE, source)
    return RUNNER_RELATIVE


def execute_frozen_suite(
    sandbox: Any,
    run_dir: Path,
    manifest: Dict[str, Any],
    *,
    iteration: int,
    only: Optional[str] = None,
    tag: str = "tester",
) -> SuiteExecution:
    """执行冻结清单中的用例。

    - ``manifest`` 必须是已通过 ``TestVerifier.load_manifest()`` 校验的清单；
      这里再落一份到证据目录，供容器内 runner 读取。
    - 清单与结果文件都在 ``run_dir/verify_io/`` 下（沙箱工作区之外）；容器后端
      把该目录额外挂载到 ``/mas-verify``，本地后端直接使用宿主绝对路径。
    - stdout/stderr 只作为诊断日志返回，结果只从 ``runner_result.json`` 读取。
    """
    io_dir = Path(run_dir) / "verify_io" / f"iter{int(iteration):02d}-{tag}"
    io_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = io_dir / "test_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    result_path = io_dir / "runner_result.json"
    try:
        result_path.unlink()
    except FileNotFoundError:
        pass

    install_runner(sandbox)
    container = str(getattr(sandbox, "backend", "local") or "local") == "container"
    if container:
        manifest_arg = f"{CONTAINER_EVIDENCE_DIR}/test_manifest.json"
        out_arg = f"{CONTAINER_EVIDENCE_DIR}/runner_result.json"
        root_arg = "/workspace"
        mounts = [(str(io_dir.resolve()), CONTAINER_EVIDENCE_DIR)]
    else:
        manifest_arg = str(manifest_path.resolve())
        out_arg = str(result_path.resolve())
        root_arg = str(Path(sandbox.root).resolve())
        mounts = None

    cmd = [
        sandbox.python, RUNNER_RELATIVE,
        "--manifest", manifest_arg,
        "--root", root_arg,
        "--out", out_arg,
    ]
    if only:
        cmd += ["--only", str(only)]
    exec_result = sandbox.run(
        cmd,
        env_extra={"PYTHONPATH": str(sandbox.root)},
        extra_mounts=mounts,
    )

    parsed: Optional[Dict[str, Any]] = None
    if result_path.exists():
        try:
            parsed = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            parsed = None
    return SuiteExecution(
        exec=exec_result,
        result=parsed,
        io_dir=io_dir,
        manifest_path=manifest_path,
        result_path=result_path,
        cmd=list(cmd),
    )


def execute_hidden_suite(
    sandbox: Any,
    run_dir: Path,
    hidden_root: Path,
    manifest: Dict[str, Any],
    *,
    iteration: int,
    tag: str = "hidden",
) -> SuiteExecution:
    """执行 run 工作区之外的冻结隐藏套件（门禁专用）。

    与 :func:`execute_frozen_suite` 的差异：
    - 测试根固定为 ``hidden_root/frozen_tests``（隐藏套件的冻结副本），而不是沙箱根；
    - 容器后端把冻结目录**只读**挂载到 ``/mas-hidden``，工作区仍挂在 ``/workspace``，
      ``PYTHONPATH`` 同时包含两者——测试可导入交付包，但不能改写自己的用例；
    - 本地后端没有文件系统只读语义，执行后由 ``TestVerifier.attest`` 对比冻结 hash
      检测篡改（证据链失败关闭），此处如实标注这一限制。
    """
    hidden_root = Path(hidden_root).resolve()
    frozen_dir = hidden_root / "frozen_tests"
    io_dir = Path(run_dir) / "verify_io" / f"iter{int(iteration):02d}-{tag}"
    io_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = io_dir / "test_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    result_path = io_dir / "runner_result.json"
    try:
        result_path.unlink()
    except FileNotFoundError:
        pass

    install_runner(sandbox)
    container = str(getattr(sandbox, "backend", "local") or "local") == "container"
    if container:
        manifest_arg = f"{CONTAINER_EVIDENCE_DIR}/test_manifest.json"
        out_arg = f"{CONTAINER_EVIDENCE_DIR}/runner_result.json"
        root_arg = CONTAINER_HIDDEN_DIR
        mounts: Optional[List[tuple]] = [
            (str(io_dir.resolve()), CONTAINER_EVIDENCE_DIR, "rw"),
            (str(frozen_dir), CONTAINER_HIDDEN_DIR, "ro"),
        ]
        pythonpath = os.pathsep.join([CONTAINER_HIDDEN_DIR, "/workspace"])
    else:
        manifest_arg = str(manifest_path.resolve())
        out_arg = str(result_path.resolve())
        root_arg = str(frozen_dir)
        mounts = None
        pythonpath = os.pathsep.join([str(frozen_dir), str(Path(sandbox.root).resolve())])

    cmd = [
        sandbox.python, RUNNER_RELATIVE,
        "--manifest", manifest_arg,
        "--root", root_arg,
        "--out", out_arg,
    ]
    exec_result = sandbox.run(cmd, env_extra={"PYTHONPATH": pythonpath}, extra_mounts=mounts)

    parsed: Optional[Dict[str, Any]] = None
    if result_path.exists():
        try:
            parsed = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            parsed = None
    return SuiteExecution(
        exec=exec_result,
        result=parsed,
        io_dir=io_dir,
        manifest_path=manifest_path,
        result_path=result_path,
        cmd=list(cmd),
    )
