"""容器沙箱后端与失败关闭（docs/37 P0-1）的单元测试。

不需要安装 Docker：运行时探测与 docker 命令都通过 mock 校验。
覆盖：
1. backend=container 时运行时缺失 / 镜像缺失 → 构造即失败，不降级到宿主；
2. 容器命令包含网络禁用、只读根、cap-drop、no-new-privileges、资源上限；
3. 宿主解释器与宿主绝对路径被映射到容器内的 python / /workspace；
4. docker run 返回 125（运行时故障）→ 抛 SandboxError，不当成测试失败。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mas.config import load_config
from mas.errors import SandboxError
from mas.sandbox import Sandbox

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _config(tmp: str):
    config = load_config(PROJECT_ROOT / "config", workspace=Path(tmp) / "ws")
    config.runtime = dict(config.runtime)
    config.runtime["sandbox_backend"] = "container"
    config.runtime["sandbox_container"] = {
        "runtime": "docker",
        "image": "python:3.12-slim",
        "python": "python",
        "network": "none",
        "memory": "1g",
        "cpus": "1.0",
        "pids_limit": 128,
        "read_only": True,
        "tmpfs": "/tmp:rw,noexec,nosuid,size=64m",
        "user": "65534:65534",
    }
    return config


class ContainerSandboxTests(unittest.TestCase):
    def test_fails_closed_when_runtime_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("mas.sandbox.shutil.which", return_value=None):
                with self.assertRaises(SandboxError) as ctx:
                    Sandbox(Path(tmp) / "code", _config(tmp))
            self.assertIn("拒绝降级", str(ctx.exception))

    def test_fails_closed_when_image_missing(self):
        failed = SimpleNamespace(returncode=1, stdout="", stderr="No such image")
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("mas.sandbox.shutil.which", return_value="/usr/bin/docker"), \
                    mock.patch("mas.sandbox.subprocess.run", return_value=failed):
                with self.assertRaises(SandboxError) as ctx:
                    Sandbox(Path(tmp) / "code", _config(tmp))
            self.assertIn("镜像不可用", str(ctx.exception))

    def _sandbox(self, tmp: str) -> Sandbox:
        ok = SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch("mas.sandbox.shutil.which", return_value="/usr/bin/docker"), \
                mock.patch("mas.sandbox.subprocess.run", return_value=ok):
            return Sandbox(Path(tmp) / "code", _config(tmp))

    def test_container_hardening_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = self._sandbox(tmp)
            sandbox.write_file("tests/run.py", "print('ok')\n")
            completed = SimpleNamespace(returncode=0, stdout="ok\n", stderr="")
            with mock.patch("mas.sandbox.subprocess.run", return_value=completed) as run:
                result = sandbox.run([sandbox.python, "tests/run.py"],
                                     env_extra={"PYTHONPATH": str(sandbox.root)})
            self.assertTrue(result.ok)
            argv = run.call_args.args[0]
            for flag, value in (
                ("--network", "none"), ("--cap-drop", "ALL"),
                ("--security-opt", "no-new-privileges"),
            ):
                self.assertIn(flag, argv)
                self.assertEqual(argv[argv.index(flag) + 1], value)
            self.assertIn("--read-only", argv)
            self.assertIn("--pids-limit", argv)
            self.assertIn("--memory", argv)
            self.assertIn(f"{sandbox.root}:/workspace:rw", argv)
            self.assertEqual(argv[argv.index("-w") + 1], "/workspace")
            self.assertIn("PYTHONPATH=/workspace", argv)
            self.assertEqual(argv[-2:], ["python", "tests/run.py"])

    def test_host_paths_mapped_into_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = self._sandbox(tmp)
            completed = SimpleNamespace(returncode=0, stdout="", stderr="")
            with mock.patch("mas.sandbox.subprocess.run", return_value=completed) as run:
                sandbox.run([sys.executable, str(sandbox.root / "tests" / "run.py")])
            argv = run.call_args.args[0]
            self.assertEqual(argv[-2:], ["python", "/workspace/tests/run.py"])

    def test_docker_exit_125_is_infrastructure_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = self._sandbox(tmp)
            completed = SimpleNamespace(returncode=125, stdout="", stderr="Cannot connect to Docker daemon")
            with mock.patch("mas.sandbox.subprocess.run", return_value=completed):
                with self.assertRaises(SandboxError) as ctx:
                    sandbox.run([sandbox.python, "-c", "print(1)"])
            self.assertIn("125", str(ctx.exception))

    def test_isolation_label_reports_container(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = self._sandbox(tmp)
            label = sandbox._isolation_label()
            self.assertIn("container:python:3.12-slim", label)
            self.assertIn("network:none", label)
            self.assertIn("cap-drop:ALL", label)


if __name__ == "__main__":
    unittest.main()
