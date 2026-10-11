"""contract-schema-guard 的冒烟脚本。

复现并验证一项核心能力：模型把 cases 返回成字符串列表（而非 dict 列表）时，
能否产出带 field_path 的结构化报告，而不是抛出裸 AttributeError。

退出码 0 表示技能描述的规则可被正确实现。
"""

from __future__ import annotations

import json
import sys


def check(contract_spec, payload, node_id="test"):
    """按交付契约做字段级结构校验，返回固定六键报告。"""
    report = {
        "checked": 0, "missing": [], "type_mismatch": [],
        "skipped": 0, "repaired": [], "fatal": [],
    }
    expected_types = {"str": str, "dict": dict, "list": list, "int": int}

    for field in contract_spec:
        field_path = field["field_path"]
        expected = expected_types[field["expected_type"]]
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
            report["type_mismatch"].append(
                f"{field_path} 期望 {field['expected_type']} 实为 {type(cursor).__name__}"
            )
            if required:
                report["fatal"].append(
                    f"{field_path} 期望 {field['expected_type']} 实为 {type(cursor).__name__}"
                )
            continue

        # 容器元素级校验：列表里混入非法元素时记 skipped，不中断整体处理
        element_schema = field.get("element_type")
        if expected is list and element_schema:
            allowed = expected_types[element_schema]
            for index, item in enumerate(cursor):
                if not isinstance(item, allowed):
                    report["skipped"] += 1
                    report["type_mismatch"].append(
                        f"{field_path}[{index}] 期望 {element_schema} 实为 {type(item).__name__}"
                    )
    return report


def main() -> int:
    contract = [
        {"field_path": "cases", "expected_type": "list", "required": True, "element_type": "dict"},
        {"field_path": "summary.failed", "expected_type": "int", "required": True},
        {"field_path": "summary.total", "expected_type": "int", "required": True, "default": 0},
        {"field_path": "derived_from", "expected_type": "str", "required": False},
    ]

    # 用例 1：复现历史故障 —— cases 是字符串列表
    broken = {"cases": ["用例A执行失败", "用例B执行失败"], "summary": {"failed": 1}}
    report = check(contract, broken, "test")
    assert report["skipped"] == 2, f"应跳过 2 个畸形元素，实际 {report['skipped']}"
    assert any("cases[0] 期望 dict 实为 str" in item for item in report["type_mismatch"]), \
        f"未生成带下标的字段路径：{report['type_mismatch']}"

    # 用例 2：缺失且有默认值 → repaired；缺失且无默认值 → fatal
    partial = {"cases": [{"status": "passed"}], "summary": {"failed": 0}}
    report2 = check(contract, partial, "test")
    assert "summary.total" in report2["repaired"], f"应修复 summary.total：{report2}"
    assert "derived_from" not in report2["fatal"], "非必填字段缺失不得计入 fatal"

    # 用例 3：结构完好 → fatal 为空，可放行
    healthy = {
        "cases": [{"status": "passed"}, {"status": "failed"}],
        "summary": {"failed": 1, "total": 2},
        "derived_from": "requirement_spec",
    }
    report3 = check(contract, healthy, "test")
    assert report3["fatal"] == [], f"正常结构不应产生 fatal：{report3['fatal']}"
    assert report3["skipped"] == 0

    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("contract-schema-guard smoke ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
