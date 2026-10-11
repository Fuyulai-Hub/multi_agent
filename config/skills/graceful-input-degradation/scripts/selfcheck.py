"""graceful-input-degradation 的冒烟脚本。

验证：缺失产物能被翻译成一段带「缺失什么 / 手上有什么 / 谁该产出」的非空摘要，
而不是崩溃或空字符串。

退出码 0 表示技能描述的规则可被正确实现。
"""

from __future__ import annotations

import json
import sys

# 与 config/flow.yaml 的产物声明保持一致（产物名 -> 产出角色）
PRODUCER_MAP = {
    "requirement_spec": "product@intake",
    "solution_design": "developer@solution",
    "code_manifest": "developer@implement",
    "implementation_summary": "developer@implement",
    "review_report": "reviewer@review",
    "test_report": "tester@test",
    "bug_reports": "tester@test",
    "acceptance_gate": "leader@acceptance",
}

REQUIRED_INPUTS = {
    "review": ["requirement_spec", "code_manifest", "implementation_summary"],
    "implement": ["requirement_spec", "solution_design"],
    "intake": ["requirement_source"],
}


def degrade(node_id, missing, found, duty="执行本节点"):
    if not missing and not found:
        raise ValueError("感知阶段异常：missing 与 found 同时为空")

    expected_producers = {}
    for name in missing:
        expected_producers[name] = PRODUCER_MAP.get(
            name, "未知产出方（需补 flow.yaml 的 outputs 声明）"
        )

    needs = REQUIRED_INPUTS.get(node_id, [])
    blocking = [name for name in missing if name in needs]
    can_continue = not blocking

    summary = (
        f"本轮不具备{duty}的输入条件：上游仅提供 {'、'.join(found) or '无'}，"
        f"缺失 {'、'.join(missing)}"
        + (f"（应由 {expected_producers[missing[0]]} 产出）" if missing else "")
    )
    if not can_continue:
        summary += f"；其中 {'、'.join(blocking)} 为必填输入，建议回退到对应产出节点"

    return {
        "missing": list(missing),
        "expected_producers": expected_producers,
        "suggestion": "继续按降级路径执行" if can_continue else "回退到产出节点补齐输入",
        "can_continue": can_continue,
        "summary": summary,
    }


def main() -> int:
    # 用例 1：复现 reviewer 的真实正确行为 —— 必填输入缺失，应建议回退
    report = degrade(
        "review",
        ["code_manifest", "implementation_summary"],
        ["requirement_spec", "solution_design"],
        duty="代码审查",
    )
    assert "输入条件" in report["summary"], "摘要必须含『输入条件』四字"
    assert report["can_continue"] is False, f"必填缺失不得继续：{report}"
    assert "回退" in report["summary"]
    assert report["expected_producers"]["code_manifest"] == "developer@implement"
    assert report["summary"].strip(), "摘要不得为空串"

    # 用例 2：仅可选输入缺失 → 可降级继续
    report2 = degrade("review", ["bug_reports"], ["code_manifest", "implementation_summary"],
                      duty="代码审查")
    assert report2["can_continue"] is True, f"可选项缺失应可继续：{report2}"

    # 用例 3：未知产物必须显式标注，不得猜角色名
    report3 = degrade("implement", ["unknown_artifact"], ["requirement_spec"])
    assert report3["expected_producers"]["unknown_artifact"].startswith("未知产出方")

    # 用例 4：感知异常应报错，而不是伪装成降级
    try:
        degrade("implement", [], [])
    except ValueError as exc:
        assert "感知阶段异常" in str(exc)
    else:
        raise AssertionError("missing 与 found 同时为空时应报错")

    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("graceful-input-degradation smoke ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
