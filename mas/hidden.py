"""独立隐藏用例：只依据冻结契约生成，存放在 run 工作区之外。

与可见测试的区别不是「内容更神秘」，而是**信息与写权限的隔离**：

- 生成上下文只有冻结后的需求契约（功能点/规则/样例/验收要点/条款），看不到
  技术方案、实现代码、代码清单和可见测试；
- 文件写在 ``<workspace>/hidden/<run_id>/``（run 目录之外），由框架直接落盘；
- 框架用 ``TestVerifier`` 冻结并签名清单，门禁执行时只读挂载冻结副本；
- 隐藏套件在 solution 之前生成，因此不可能「照着实现写测试」。
"""

from __future__ import annotations

import ast
import json
import re
import shutil
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .contract import hidden_suite_dir
from .verifier import TestVerifier, VerificationError, sign_payload

__all__ = [
    "HiddenSuiteError",
    "build_generation_prompt",
    "validate_generated_suite",
    "create_hidden_suite",
]

_CATEGORIES = ("functional", "exception", "boundary")
_COMPONENT = re.compile(r"[A-Za-z0-9_]+\Z")

_SYSTEM_PROMPT = """你是一名独立隐藏测试工程师。你只能看到冻结的需求契约，看不到技术方案、\
实现代码、代码清单或已有的可见测试。你的任务是仅依据契约推导可执行的功能/异常/边界用例，\
用来在验收时独立检验交付物是否满足需求。"""

_PROTOCOL = """## 输出契约（必须只输出一个 JSON 对象）

```json
{
  "files": [{"path": "tests/test_hidden_xxx.py", "content": "完整 Python 源码"}],
  "cases": [{"id": "TH-FP-01-functional-xxx", "fp_id": "FP-01",
             "op": "被测函数名(ASCII)", "name": "用例短名(ASCII)",
             "category": "functional|exception|boundary",
             "call": "调用表达式", "expect": "期望值",
             "expect_error": "异常类名或空串", "assert_kind": "eq|truthy|falsy|empty|raises"}],
  "derived_from": "frozen-requirement-contract"
}
```

规则：
1. 只写 ``tests/`` 下的 ``.py`` 文件；这是框架落盘点，写入 run 工作区之外。
2. 只能使用 Python 标准库与契约声明的交付包（``from {package} import ...``）；测试文件只定义
   unittest 用例类与 ``test_*`` 方法，禁止 ``main``、结果标记、结果文件或读取实现源码。
3. 方法名必须是 ``test_{category}_{op}_{name}``，其中 ``op``/``name`` 只含 ASCII
   字母、数字、下划线，且全局唯一；``cases`` 元数据必须与方法名一一对应。
4. 每个功能点至少 1 条 functional 用例；契约里的异常与边界规则必须各自落到
   exception / boundary 用例（阈值按 t、t-ε、t+ε 三态设计）。
5. 期望值只能来自契约的规则与样例，禁止读取或猜测实现细节；不得把实现代码里可能存在的
   缺陷当成期望行为。
6. 测试套件必须在沙箱超时内完成；不得启动常驻进程。若契约声明的是 HTTP 服务，在测试进程内
   用端口 0 启动服务并在 finally 中关闭。
7. ``files`` 里的用例必须与 ``cases`` 完全对应：多定义或漏定义都会让验收失败。
"""


class HiddenSuiteError(RuntimeError):
    """隐藏套件生成/冻结失败。"""


def build_generation_prompt(
    *,
    spec: Dict[str, Any],
    clauses: List[Dict[str, Any]],
    package: str,
    delivery_shape: str,
) -> Tuple[str, str]:
    """构造隐藏生成上下文：输入只有冻结契约，不含任何下游产物。"""
    contract_view = {
        "title": spec.get("title", ""),
        "package": package,
        "delivery_shape": delivery_shape,
        "constraints": spec.get("constraints", []),
        "acceptance_criteria": spec.get("acceptance_criteria", []),
        "function_points": spec.get("function_points", []),
        "out_of_scope": spec.get("out_of_scope", []),
    }
    user = (
        "以下是已冻结的需求契约（唯一事实来源）。请据此独立生成隐藏测试套件。\n\n"
        "## 契约条款索引\n"
        + json.dumps(clauses, ensure_ascii=False, indent=2)
        + "\n\n## 冻结需求规格\n"
        + json.dumps(contract_view, ensure_ascii=False, indent=2)
        + "\n\n"
        + _PROTOCOL.replace("{package}", package or "交付包")
    )
    return _SYSTEM_PROMPT, user


def _normalize_files(raw: Any) -> Tuple[List[Dict[str, str]], List[str]]:
    errors: List[str] = []
    files: List[Dict[str, str]] = []
    if not isinstance(raw, list) or not raw:
        return [], ["hidden_files_missing"]
    for item in raw:
        if not isinstance(item, dict):
            errors.append("hidden_file_not_object")
            continue
        path = str(item.get("path") or "").replace("\\", "/").strip()
        content = str(item.get("content") or "")
        relative = PurePosixPath(path)
        if (not path or relative.is_absolute() or ".." in relative.parts
                or len(relative.parts) < 2 or relative.parts[0] != "tests"
                or relative.suffix != ".py"):
            errors.append(f"hidden_file_path_invalid:{path or 'empty'}")
            continue
        if not content.strip():
            errors.append(f"hidden_file_empty:{path}")
            continue
        files.append({"path": relative.as_posix(), "content": content})
    return files, errors


def _method_for(case: Dict[str, Any]) -> str:
    return "test_{}_{}_{}".format(case.get("category", ""), case.get("op", ""), case.get("name", ""))


def _assertion_count(files: Iterable[Dict[str, str]]) -> int:
    count = 0
    for item in files:
        try:
            tree = ast.parse(item["content"])
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Assert):
                count += 1
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr.startswith("assert") or node.func.attr in {"raises", "fail"}:
                    count += 1
    return count


def validate_generated_suite(
    data: Dict[str, Any],
    spec: Dict[str, Any],
) -> Dict[str, Any]:
    """在写盘/冻结前校验隐藏套件：结构、FP 覆盖、方法名唯一与断言存在。"""
    errors: List[str] = []
    data = data if isinstance(data, dict) else {}
    files, file_errors = _normalize_files(data.get("files"))
    errors.extend(file_errors)

    fps = {
        str(item.get("id")): item
        for item in (spec.get("function_points") or [])
        if isinstance(item, dict) and item.get("id")
    }
    raw_cases = data.get("cases")
    cases: List[Dict[str, str]] = []
    methods: set = set()
    covered: Dict[str, set] = {}
    if not isinstance(raw_cases, list) or not raw_cases:
        errors.append("hidden_cases_missing")
    else:
        for item in raw_cases:
            if not isinstance(item, dict):
                errors.append("hidden_case_not_object")
                continue
            fp_id = str(item.get("fp_id") or "").strip()
            category = str(item.get("category") or "").strip()
            op = str(item.get("op") or "").strip()
            name = str(item.get("name") or "").strip()
            if fp_id not in fps:
                errors.append(f"hidden_case_unknown_fp:{fp_id or 'empty'}")
                continue
            if category not in _CATEGORIES:
                errors.append(f"hidden_case_invalid_category:{category or 'empty'}")
                continue
            if not op or not name or not _COMPONENT.match(op) or not _COMPONENT.match(name):
                errors.append(f"hidden_case_name_not_ascii:{fp_id}:{op or '?'}/{name or '?'}")
                continue
            method = _method_for({"category": category, "op": op, "name": name})
            if method in methods:
                errors.append(f"hidden_case_duplicate_method:{method}")
                continue
            methods.add(method)
            covered.setdefault(fp_id, set()).add(category)
            cases.append({
                "id": str(item.get("id") or method),
                "fp_id": fp_id, "category": category, "op": op, "name": name,
                "call": str(item.get("call") or ""),
                "expect": str(item.get("expect") or ""),
                "expect_error": str(item.get("expect_error") or ""),
                "assert_kind": str(item.get("assert_kind") or "eq"),
            })

    for fp_id in fps:
        if "functional" not in covered.get(fp_id, set()):
            errors.append(f"hidden_functional_case_missing:{fp_id}")
    if files and _assertion_count(files) == 0:
        errors.append("hidden_suite_has_no_assertions")

    return {"errors": list(dict.fromkeys(errors)), "files": files, "cases": cases}


def _artifact(
    *,
    root: Path,
    manifest: Dict[str, Any],
    contract_hash: str,
    package: str,
    generation: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "root": str(root),
        "workspace_relative": f"{root.parent.name}/{root.name}",
        "test_file": str(manifest.get("test_file") or ""),
        "files": dict(manifest.get("files") or {}),
        "manifest_digest": str(manifest.get("digest") or ""),
        "suite_hash": str(manifest.get("digest") or ""),
        "signature": str(manifest.get("signature") or ""),
        "test_ids": list(manifest.get("test_ids") or []),
        "case_count": len(manifest.get("case_meta") or {}),
        "frozen_at": str(manifest.get("frozen_at") or ""),
        "contract_hash": contract_hash,
        "package": package,
        "derived_from": "frozen-requirement-contract",
        "generation": dict(generation or {}),
        "limitations": (
            "隐藏套件由独立上下文仅依据冻结契约生成；生成与执行证据保留，"
            "但未做行覆盖率/变异测试，覆盖口径是功能点绑定。"
        ),
    }


def create_hidden_suite(
    *,
    config: Any,
    run_dir: Path,
    sandbox: Any,
    run_id: str,
    spec: Dict[str, Any],
    contract_hash: str,
    data: Dict[str, Any],
    iteration: int,
    package: str,
    generation: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """把生成结果写进 run 目录之外并冻结；同 run 重复调用复用已签名清单。"""
    root = hidden_suite_dir(config.workspace_dir, run_id)
    sandbox_root = Path(getattr(sandbox, "root", run_dir / "code"))
    verifier = TestVerifier(
        root, root, events=getattr(sandbox, "events", None), config=config, sandbox=sandbox,
        run_id=run_id, code_root=sandbox_root,
        attestation_path=Path(run_dir) / "hidden_attestation.json",
    )

    try:
        existing = verifier.load_manifest()
    except VerificationError as exc:
        raise HiddenSuiteError(f"已存在的隐藏套件冻结清单不可信: {exc}") from exc
    if existing is not None:
        meta_path = root / "hidden_meta.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
        if str(meta.get("contract_hash") or "") != str(contract_hash):
            raise HiddenSuiteError("隐藏套件与当前需求契约不一致（契约冻结后不得更换隐藏套件）")
        return _artifact(root=root, manifest=existing, contract_hash=contract_hash,
                         package=package, generation=meta.get("generation") or generation or {})

    outcome = validate_generated_suite(data, spec)
    if outcome["errors"]:
        raise HiddenSuiteError("隐藏套件结构校验失败: " + "; ".join(outcome["errors"][:8]))

    if root.exists():
        shutil.rmtree(root)
    for item in outcome["files"]:
        target = root / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(item["content"], encoding="utf-8")

    test_file = outcome["files"][0]["path"]
    try:
        manifest = verifier.freeze(test_file, iteration, case_meta=outcome["cases"])
    except VerificationError as exc:
        shutil.rmtree(root, ignore_errors=True)
        raise HiddenSuiteError(f"隐藏套件冻结失败: {exc}") from exc

    expected_methods = {_method_for(case) for case in outcome["cases"]}
    bound = {
        str(test_id).rsplit(".", 1)[-1]
        for test_id in (manifest.get("case_meta") or {})
    }
    missing = sorted(expected_methods - bound)
    unbound = sorted(
        str(test_id) for test_id in (manifest.get("test_ids") or [])
        if str(test_id) not in (manifest.get("case_meta") or {})
    )
    if missing or unbound:
        shutil.rmtree(root, ignore_errors=True)
        raise HiddenSuiteError(
            "隐藏套件方法绑定不完整: 缺 "
            + (", ".join(missing[:5]) or "无")
            + "；未绑定元数据的用例 "
            + (", ".join(unbound[:5]) or "无")
        )

    meta = sign_payload({
        "version": 1,
        "run_id": str(run_id),
        "contract_hash": str(contract_hash),
        "manifest_digest": str(manifest.get("digest") or ""),
        "package": package,
        "frozen_at": str(manifest.get("frozen_at") or ""),
        "generation": dict(generation or {}),
    }, config=config)
    (root / "hidden_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return _artifact(root=root, manifest=manifest, contract_hash=contract_hash,
                     package=package, generation=generation or {})
