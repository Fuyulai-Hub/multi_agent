"""框架侧冻结测试执行器（测试执行权归框架，不归生成的测试文件）。

门禁加固要点：

1. **只执行冻结清单**：从 ``test_manifest.json`` 的 ``test_ids`` 取用例，
   用标准库 ``unittest.TestLoader`` 加载，再作为一个 ``unittest.TestSuite``
   统一执行（``setUpClass`` / ``tearDownClass`` / ``setUpModule`` 只在这里
   生效）。测试文件里额外定义/动态生成的用例不会被执行，并作为
   ``unexpected_test_ids`` 上报；清单内缺失的用例记为 ``error``。
2. **结果由 runner 自己汇总**：逐用例记录（``test_id / status / duration /
   failure``）直接写入 ``--out`` 指定的文件。该路径由框架分配，位于模型
   工作区之外；框架不再解析测试进程 stdout，任何结果标记都不作为证据。
3. **测试文件只定义用例**：模块以普通 import 方式加载，文件末尾的
   ``if __name__ == "__main__"`` 不会执行，测试文件也无法通过 ``--only``
   或自报 summary 改变执行集合。

边界（如实说明）：测试代码与 runner 同进程执行，理论上可以在 import/test
期间篡改 runner 的内存或结果文件；这里解决的是「执行集合与结果汇总权」，
不是恶意代码隔离。真正的强隔离需要独立进程 + 父进程侧签名/管道校验。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
import traceback
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

__all__ = ["RUNNER_NAME", "main", "run_manifest"]

RUNNER_NAME = "mas.verify.runner"
RUNNER_VERSION = 1
_FAILURE_LIMIT = 800

_STATUS_RANK = {"passed": 0, "failed": 1, "error": 2}


class _SuiteResult(unittest.TestResult):
    """执行 ``TestSuite`` 时逐用例收集状态/耗时/失败摘要。

    不能逐条调用 ``TestCase.run``（旧实现）：那会绕过 ``setUpClass`` /
    ``tearDownClass``，使用类级夹具的生成套件会整体 AttributeError
    （task11-slink-fullstack-01 实测 14/49 假失败）。这里交给 unittest
    的 ``TestSuite`` 管理类级夹具，只负责把每条用例的结果落到独立记录。
    """

    def __init__(self) -> None:
        super().__init__()
        self.records: Dict[str, Dict[str, Any]] = {}
        self._started = 0.0

    def _entry(self, test: unittest.TestCase) -> Dict[str, Any]:
        return self.records.setdefault(str(test.id()), {
            "status": "passed", "failure": "", "duration": 0.0})

    @staticmethod
    def _exc_text(err: Any) -> str:
        try:
            return "".join(traceback.format_exception_only(err[0], err[1])).strip()
        except Exception:  # noqa: BLE001 - 失败摘要绝不能反向拖垮 runner
            return repr(err)

    def _mark(self, test: unittest.TestCase, status: str, failure: str = "") -> None:
        entry = self._entry(test)
        if _STATUS_RANK.get(status, 0) > _STATUS_RANK.get(entry["status"], 0):
            entry["status"] = status
        text = str(failure or "").strip()
        if text:
            entry["failure"] = text[-_FAILURE_LIMIT:]

    def startTest(self, test: unittest.TestCase) -> None:
        super().startTest(test)
        self._entry(test)
        self._started = time.perf_counter()

    def stopTest(self, test: unittest.TestCase) -> None:
        self._entry(test)["duration"] = time.perf_counter() - self._started
        super().stopTest(test)

    def addError(self, test: unittest.TestCase, err: Any) -> None:
        super().addError(test, err)
        self._mark(test, "error", self._exc_text(err))

    def addFailure(self, test: unittest.TestCase, err: Any) -> None:
        super().addFailure(test, err)
        self._mark(test, "failed", self._exc_text(err))

    def addSkip(self, test: unittest.TestCase, reason: str) -> None:
        super().addSkip(test, reason)
        self._mark(test, "failed", f"skipped: {reason}")

    def addExpectedFailure(self, test: unittest.TestCase, err: Any) -> None:
        super().addExpectedFailure(test, err)
        self._mark(test, "failed", self._exc_text(err) or "expected failure（xfail 不作为通过证据）")

    def addUnexpectedSuccess(self, test: unittest.TestCase) -> None:
        super().addUnexpectedSuccess(test)
        self._mark(test, "failed", "unexpected success")

    def addSubTest(self, test: unittest.TestCase, subtest: Any, err: Any) -> None:
        super().addSubTest(test, subtest, err)
        if err is not None:
            status = ("failed" if isinstance(err[1], getattr(test, "failureException", AssertionError))
                      else "error")
            self._mark(test, status, self._exc_text(err))


def _flatten(suite: unittest.TestSuite) -> Iterable[unittest.TestCase]:
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


def _module_name(relative: str) -> str:
    """``tests/test_all.py`` -> ``tests.test_all``（冻结清单与 TestLoader 共用）。"""
    return relative[:-3].replace("\\", "/").replace("/", ".")


def _load_module(module_name: str, path: Path) -> Any:
    """按文件路径加载测试模块，避免被宿主同名包（如仓库自带 tests/）遮蔽。"""
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为测试文件创建模块: {path}")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous
        raise
    return module


def _module_for(test_id: str, module_paths: Dict[str, Path]) -> str:
    matches = [name for name in module_paths if test_id.startswith(name + ".")]
    return max(matches, key=len) if matches else ""


def _format_exc(exc: BaseException) -> str:
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return text[-_FAILURE_LIMIT:].strip() or type(exc).__name__


def _record(
    test_id: str,
    status: str,
    duration: float,
    failure: str,
    manifest: Dict[str, Any],
    *,
    fp_by_class: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    parts = test_id.rsplit(".", 2)
    method = parts[-1]
    class_name = parts[-2] if len(parts) >= 3 else ""
    meta = dict(manifest.get("case_meta") or {}).get(test_id) or {}
    category = str(meta.get("category") or "")
    if not category:
        chunks = method.split("_", 2)
        category = chunks[1] if len(chunks) > 1 else ""
    fp_id = str(meta.get("fp_id") or "")
    if not fp_id and fp_by_class:
        fp_id = str(fp_by_class.get(class_name, ""))
    message = str(failure or "")[:_FAILURE_LIMIT]
    return {
        "test_id": test_id,
        "test": method,
        "class": class_name,
        "fp_id": fp_id,
        "category": category,
        "status": status,
        "duration": round(float(duration), 6),
        "failure": message,
        "message": message,   # 兼容既有缺陷跟踪/报告字段
    }


def _finalize(result: Dict[str, Any], started: float, *, forced: Optional[int] = None) -> Tuple[Dict[str, Any], int]:
    cases = result["cases"]
    result["total"] = len(cases)
    result["failed"] = sum(1 for case in cases if case["status"] != "passed")
    result["passed"] = result["total"] - result["failed"]
    result["duration_ms"] = int((time.perf_counter() - started) * 1000)
    if forced is not None:
        return result, forced
    if result.get("error"):
        return result, 2
    if not cases:
        return result, 2
    if result["failed"]:
        return result, 1
    return result, 0


def run_manifest(
    manifest_path: str,
    root: str,
    *,
    only: str = "",
) -> Tuple[Dict[str, Any], int]:
    """执行冻结清单并返回 (结果, exit_code)。

    - ``only`` 仅用于研发的定向回归（按 test_id 子串过滤清单内用例），
      不能扩大执行集合；测试文件本身没有选择权。
    """
    started = time.perf_counter()
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("冻结清单不是 JSON 对象")
    all_ids = [str(item) for item in (manifest.get("test_ids") or []) if str(item)]
    selected = [test_id for test_id in all_ids if not only or only in test_id]
    result: Dict[str, Any] = {
        "version": RUNNER_VERSION,
        "runner": {
            "name": RUNNER_NAME,
            "version": RUNNER_VERSION,
            "manifest_digest": str(manifest.get("digest") or ""),
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        },
        "manifest_test_ids": all_ids,
        "unexpected_test_ids": [],
        "error": "",
        "cases": [],
        "total": 0,
        "passed": 0,
        "failed": 0,
        "duration_ms": 0,
    }
    if not all_ids:
        result["error"] = "manifest_has_no_test_ids"
        return _finalize(result, started, forced=2)
    if not selected:
        result["error"] = "no_test_id_matched"
        return _finalize(result, started, forced=2)

    root_path = Path(root).resolve()
    if not root_path.is_dir():
        result["error"] = f"sandbox_root_not_found: {root_path}"
        return _finalize(result, started, forced=2)
    os.chdir(root_path)
    if str(root_path) not in sys.path:
        sys.path.insert(0, str(root_path))

    module_paths: Dict[str, Path] = {}
    files = manifest.get("files")
    if isinstance(files, dict):
        for relative in files:
            if isinstance(relative, str) and relative.endswith(".py"):
                module_paths[_module_name(relative)] = root_path / relative
    if not module_paths:
        result["error"] = "manifest_has_no_test_files"
        return _finalize(result, started, forced=2)

    manifest_set = set(all_ids)
    by_module: Dict[str, List[str]] = {}
    for test_id in selected:
        name = _module_for(test_id, module_paths)
        if not name:
            result["cases"].append(_record(
                test_id, "error", 0.0,
                "test module not present in frozen manifest", manifest))
        else:
            by_module.setdefault(name, []).append(test_id)

    discovered: Dict[str, Dict[str, unittest.TestCase]] = {}
    fp_by_module: Dict[str, Dict[str, str]] = {}
    module_errors: Dict[str, str] = {}
    loader = unittest.TestLoader()
    for name in sorted(by_module):
        path = module_paths[name]
        if not path.exists():
            module_errors[name] = f"frozen test file missing: {path}"
            continue
        try:
            module = _load_module(name, path)
            tests = list(_flatten(loader.loadTestsFromModule(module)))
        except BaseException as exc:  # noqa: BLE001 - import/枚举失败要落到逐用例记录
            module_errors[name] = _format_exc(exc)
            continue
        discovered[name] = {test.id(): test for test in tests}
        fp_by_module[name] = dict(getattr(module, "FP_BY_CLASS", {}) or {})
        for test_id in discovered[name]:
            if test_id not in manifest_set:
                result["unexpected_test_ids"].append(test_id)

    suite = unittest.TestSuite()
    planned: List[Tuple[str, unittest.TestCase, str]] = []
    for test_id in selected:
        name = _module_for(test_id, module_paths)
        if name in module_errors:
            result["cases"].append(_record(
                test_id, "error", 0.0,
                f"test module import failed: {module_errors[name]}", manifest))
            continue
        test = (discovered.get(name) or {}).get(test_id)
        if test is None:
            result["cases"].append(_record(
                test_id, "error", 0.0,
                "test_id_not_found（冻结清单声明了该用例，但 TestLoader 未枚举到）", manifest))
            continue
        planned.append((test_id, test, name))
        suite.addTest(test)

    # 统一交给 TestSuite.run：setUpClass/tearDownClass、setUpModule/tearDownModule
    # 只在 TestSuite 里生效；逐条 test.run() 会静默跳过类级夹具（假失败）。
    suite_result = _SuiteResult()
    interrupted = ""
    if planned:
        try:
            suite.run(suite_result)
        except BaseException as exc:  # noqa: BLE001 - SystemExit/KeyboardInterrupt 也不能终止整轮取证
            interrupted = _format_exc(exc)

    planned_ids = {test_id for test_id, _, _ in planned}
    fixture_errors = {
        holder: str(record.get("failure") or "")
        for holder, record in suite_result.records.items()
        if holder not in planned_ids and record.get("status") != "passed"
    }
    attributed: set = set()
    built: List[Tuple[str, str, str, Dict[str, Any]]] = []
    for test_id, _test, name in planned:
        parts = test_id.rsplit(".", 2)
        class_name = parts[-2] if len(parts) >= 3 else ""
        record = suite_result.records.get(test_id)
        if record is None:
            # 类/模块级夹具失败时 TestSuite 会整组跳过（不调用 startTest）；
            # 用夹具报错原文回填，避免只剩一句「未执行」。
            owners = {name, f"{name}.{class_name}" if class_name else name}
            hint = ""
            for holder in sorted(fixture_errors):
                if _fixture_parent(holder) in owners:
                    attributed.add(holder)
                    hint = hint or f"{holder}: {fixture_errors[holder]}"
            failure = hint or interrupted or "test was not executed（执行器在到达该用例前中断）"
            case = _record(test_id, "error", 0.0, failure, manifest,
                           fp_by_class=fp_by_module.get(name))
            result["cases"].append(case)
            built.append((test_id, name, class_name, case))
            continue
        status = str(record.get("status") or "passed")
        failure = str(record.get("failure") or "")
        duration = float(record.get("duration") or 0.0)
        if status != "passed" and not failure:
            failure = "test failed without message"
        case = _record(test_id, status, duration, failure, manifest,
                       fp_by_class=fp_by_module.get(name))
        result["cases"].append(case)
        built.append((test_id, name, class_name, case))

    # 用例执行完之后才暴露的夹具错误（典型是 tearDownClass/tearDownModule）：
    # 归到该组最后一条用例上，不能让「全集 passed」掩盖未清理的失败。
    for holder in sorted(set(fixture_errors) - attributed):
        parent = _fixture_parent(holder)
        targets = [item for item in built
                   if parent in (item[1], f"{item[1]}.{item[2]}")]
        if not targets:
            continue
        _, _, _, case = targets[-1]
        text = f"{holder}: {fixture_errors[holder]}"[:_FAILURE_LIMIT]
        case["status"] = "error"
        case["failure"] = text
        case["message"] = text
    return _finalize(result, started)


def _fixture_parent(holder: str) -> str:
    """``setUpClass (tests.test_all.Case)`` -> ``tests.test_all.Case``。"""
    text = str(holder or "").strip()
    if text.endswith(")") and " (" in text:
        return text[text.rindex(" (") + 2:-1]
    return ""


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="框架侧冻结测试执行器（只信任冻结清单）")
    parser.add_argument("--manifest", required=True, help="冻结清单 test_manifest.json 路径")
    parser.add_argument("--root", default=".", help="沙箱工作区根目录（测试 import 根）")
    parser.add_argument("--out", required=True, help="逐用例结果文件（工作区之外）")
    parser.add_argument("--only", default="", help="可选：按 test_id 子串过滤（仅研发定向回归）")
    args = parser.parse_args(argv)

    out_path = Path(args.out)
    try:
        result, code = run_manifest(args.manifest, args.root, only=str(args.only or ""))
    except BaseException as exc:  # noqa: BLE001 - runner 自身失败也必须留下可诊断记录
        traceback.print_exc()
        result = {
            "version": RUNNER_VERSION,
            "runner": {"name": RUNNER_NAME, "version": RUNNER_VERSION,
                       "manifest_digest": "", "started_at": ""},
            "manifest_test_ids": [],
            "unexpected_test_ids": [],
            "error": f"runner_failed: {type(exc).__name__}: {exc}"[:_FAILURE_LIMIT],
            "cases": [],
            "total": 0, "passed": 0, "failed": 0, "duration_ms": 0,
        }
        code = 2
    try:
        _write_json(out_path, result)
    except OSError:
        traceback.print_exc()
        return 2
    return int(code)


if __name__ == "__main__":
    sys.exit(main())
