"""contract-schema-guard 的可调用脚本（function calling 入口）。

框架调用协议（见 SKILL.md 的 tool 段与 mas/skills.py 的 run_skill_script）：
- 入参经 `sys.argv[1]` 传入单个 JSON 对象，字段与 SKILL.md 的 inputs 一一对应：
  `contract_spec`（字段清单）/ `payload`（待校验数据）/ `node_id`（定位用）；
- 结果 JSON 打到 stdout（框架原样回灌给模型）；退出码 0 表示脚本自身执行成功，
  校验结论（fatal 等）体现在 stdout 的报告里，不用退出码表达业务结论。

本脚本与 scripts/selfcheck.py 共享同一套校验规则：selfcheck 用内置用例做冒烟断言，
本脚本把同一规则暴露为可被模型真实调用的一次性校验。
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, List


def check(contract_spec: List[Dict[str, Any]], payload: Any, node_id: str = "") -> Dict[str, Any]:
    """按交付契约做字段级结构校验，返回固定六键报告。

    六键：checked / missing / type_mismatch / skipped / repaired / fatal。
    """
    report: Dict[str, Any] = {
        "checked": 0, "missing": [], "type_mismatch": [],
        "skipped": 0, "repaired": [], "fatal": [],
        "node_id": node_id,
    }
    expected_types = {"str": str, "dict": dict, "list": list, "int": int}

    for field in contract_spec:
        if not isinstance(field, dict):
            report["fatal"].append(f"契约项不是对象: {field!r}")
            continue
        field_path = str(field.get("field_path") or "")
        expected_name = str(field.get("expected_type") or "")
        expected = expected_types.get(expected_name)
        if not field_path or expected is None:
            report["fatal"].append(
                f"契约定义不完整: field_path={field_path!r} expected_type={expected_name!r}"
                "（expected_type 仅支持 str/dict/list/int）"
            )
            continue
        required = bool(field.get("required"))
        default = field.get("default")

        cursor = payload
        present = True
        for part in field_path.split("."):
            if isinstance(cursor, dict) and part in cursor:
                cursor = cursor[part]
            else:
                present = False
                break

        report["checked"] += 1
        if not present or cursor is None:
            if required and default is None:
                report["fatal"].append(f"{field_path} 缺失且无默认值")
            elif default is not None:
                report["repaired"].append(field_path)
            else:
                report["missing"].append(field_path)
            continue

        if not isinstance(cursor, expected):
            message = f"{field_path} 期望 {expected_name} 实为 {type(cursor).__name__}"
            report["type_mismatch"].append(message)
            if required:
                report["fatal"].append(message)
            continue

        # 容器元素级校验：列表里混入非法元素时记 skipped，不中断整体处理
        element_schema = field.get("element_type")
        if expected is list and element_schema:
            allowed = expected_types.get(str(element_schema))
            if allowed is None:
                report["fatal"].append(
                    f"{field_path} 的 element_type={element_schema!r} 不支持（仅 str/dict/list/int）"
                )
                continue
            for index, item in enumerate(cursor):
                if not isinstance(item, allowed):
                    report["skipped"] += 1
                    report["type_mismatch"].append(
                        f"{field_path}[{index}] 期望 {element_schema} 实为 {type(item).__name__}"
                    )
    return report


def _load_fields(contract_spec: Any) -> List[Dict[str, Any]]:
    """兼容常见的两种契约写法：字段清单列表本身，或含 fields 列表的对象。"""
    if isinstance(contract_spec, list):
        return contract_spec
    if isinstance(contract_spec, dict):
        for key in ("fields", "items", "contract"):
            value = contract_spec.get(key)
            if isinstance(value, list):
                return value
    raise ValueError(
        "contract_spec 必须是「字段清单列表」，或含 fields 列表的对象；"
        "每项含 field_path / expected_type / required / default"
    )


def main() -> int:
    raw = sys.argv[1] if len(sys.argv) > 1 else "{}"
    try:
        params = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "error": f"入参不是合法 JSON: {exc}"}, ensure_ascii=False))
        return 1
    if not isinstance(params, dict):
        print(json.dumps({"ok": False, "error": "入参必须是 JSON 对象"}, ensure_ascii=False))
        return 1

    try:
        contract = _load_fields(params.get("contract_spec"))
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1

    payload = params.get("payload")
    if payload is None:
        payload = {}
    report = check(contract, payload, str(params.get("node_id") or ""))
    print(json.dumps(
        {"ok": not report["fatal"], "skill": "contract-schema-guard", "report": report},
        ensure_ascii=False,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
