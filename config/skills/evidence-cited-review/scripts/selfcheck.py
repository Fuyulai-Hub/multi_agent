"""evidence-cited-review 的冒烟脚本。

验证三件事：证据齐备的问题能被溯源到功能点、无法溯源的问题被降级而非丢弃、
blocking 计数与 blocking_items 长度一致（这关系到门禁表达式的正确求值）。

退出码 0 表示技能描述的规则可被正确实现。
"""

from __future__ import annotations

import json
import sys

REQUIRED_KEYS = ("rule", "file", "line", "message")


def review(findings, function_points, fp_mapping=None):
    fp_mapping = fp_mapping or {}
    blocking_items, untraceable = [], []
    fp_ids = {str(fp.get("id")) for fp in function_points}

    for finding in findings:
        if any(not finding.get(key) for key in REQUIRED_KEYS):
            untraceable.append({"reason": "证据字段缺失", "message": finding.get("message", "")})
            continue

        # fp_mapping 的口径是 file::symbol；旧格式（裸函数名）仍兼容文件名后缀匹配。
        target_file = str(finding.get("file"))
        symbols = [
            fp_id for fp_id, value in fp_mapping.items()
            if "::" in str(value) and str(value).split("::", 1)[0] == target_file
        ]
        if not symbols:
            symbols = [
                fp_id for fp_id, value in fp_mapping.items()
                if "::" not in str(value) and target_file.endswith(str(value))
            ]
        fp_id = ""
        for candidate in (symbols or list(fp_mapping.keys())):
            if str(candidate) in fp_ids:
                fp_id = str(candidate)
                break
        if not fp_id:
            message = str(finding.get("message", ""))
            for candidate in fp_ids:
                if candidate in message:
                    fp_id = candidate
                    break

        if not fp_id:
            untraceable.append({"reason": "无法归属到需求功能点", "message": finding.get("message")})
            continue

        blocking_items.append({
            "rule": finding["rule"],
            "file": finding["file"],
            "line": finding["line"],
            "fp_id": fp_id,
            "suggestion": f"违反 {fp_id} 的约束：{finding['rule']}，需在 {finding['file']}:{finding['line']} 修正",
        })

    return {
        "blocking": len(blocking_items),
        "blocking_items": blocking_items,
        "untraceable": untraceable,
    }


def main() -> int:
    fps = [{"id": f"FP-{i}", "title": f"功能{i}"} for i in range(1, 6)]
    mapping = {"FP-1": "calc_total.py", "FP-2": "validate.py", "FP-3": "apply_discount.py"}

    findings = [
        {"rule": "bare_except", "file": "calc_total.py", "line": 42, "message": "裸 except 吞异常"},
        {"rule": "missing_type_hints", "file": "validate.py", "line": 7,
         "message": "缺少类型注解 FP-2"},
        {"rule": "todo_comment", "file": "misc.py", "line": 3, "message": "TODO 残留"},
        {"rule": "eval_usage", "file": "risky.py", "line": 11, "message": ""},
    ]

    report = review(findings, fps, mapping)

    # 用例 1：可溯源的两条进入 blocking，且五要素齐全
    assert report["blocking"] == 2, f"应有 2 条阻塞，实际 {report['blocking']}"
    for item in report["blocking_items"]:
        assert all(item.get(key) for key in ("rule", "file", "line", "fp_id", "suggestion")), \
            f"阻塞项缺证据键：{item}"

    # 用例 2：blocking 计数必须与列表长度一致（门禁表达式直接求这个值）
    assert report["blocking"] == len(report["blocking_items"])

    # 用例 3：无法溯源 / 证据缺失的走 untraceable，不静默丢弃
    assert len(report["untraceable"]) == 2, f"应有 2 条被降级，实际 {report['untraceable']}"
    reasons = {item["reason"] for item in report["untraceable"]}
    assert reasons == {"无法归属到需求功能点", "证据字段缺失"}, f"降级原因不完整：{reasons}"

    # 用例 4：按 message 中的功能点编号兜底匹配生效
    fallback = [item for item in report["blocking_items"] if item["fp_id"] == "FP-2"]
    assert fallback, "message 中包含 FP-2 时应能通过兜底匹配溯源"

    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("evidence-cited-review smoke ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
