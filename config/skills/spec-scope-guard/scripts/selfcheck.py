"""spec-scope-guard 的冒烟脚本。

验证三件事：规模估算、超阈值时生成分批建议、不可量化的验收标准被识别出来。
阈值刻意采用本项目历史运行的经验值（功能点 > 10、估算行数 > 300）。

退出码 0 表示技能描述的规则可被正确实现。
"""

from __future__ import annotations

import json
import sys

DEFAULT_THRESHOLDS = {"max_function_points": 10, "max_estimated_loc": 300}
LOC_PER_FP = 40


def assess(function_points, acceptance_criteria, module_count=None,
           thresholds=None):
    thresholds = dict(DEFAULT_THRESHOLDS if thresholds is None else thresholds)
    fp_count = len(function_points)

    if module_count:
        estimated_loc = module_count * 60
        estimation = "按模块数估算"
    else:
        estimated_loc = fp_count * LOC_PER_FP
        estimation = "退化估算"

    fp_exceeded = fp_count > thresholds["max_function_points"]
    loc_exceeded = estimated_loc > thresholds["max_estimated_loc"]

    batching_suggestion = ""
    if fp_exceeded or loc_exceeded:
        reasons = []
        if fp_exceeded:
            reasons.append(f"功能点 {fp_count} 个超过阈值 {thresholds['max_function_points']}")
        if loc_exceeded:
            reasons.append(f"估算行数约 {estimated_loc} 行超过阈值 {thresholds['max_estimated_loc']}")
        groups = sorted({str(fp.get("group", "未分组")) for fp in function_points})
        batching_suggestion = (
            "；".join(reasons)
            + f"。建议分批：先交付「{groups[0]}」组建立核心闭环，其余组 "
            + f"（{', '.join(groups[1:]) or '无'}）作为后续批次"
        )

    vague_words = ("尽量", "大概", "适当", "合理", "快速", "尽可能")
    unmeasurable = []
    for item in acceptance_criteria:
        text = str(item.get("description", ""))
        if not any(token in text for token in ("=", ">", "<", ">=", "<=", "==", "校验通过", "覆盖")) \
                or any(word in text for word in vague_words):
            unmeasurable.append({"id": item.get("id"), "description": text})

    return {
        "function_point_count": fp_count,
        "estimated_loc": estimated_loc,
        "estimation": estimation,
        "scope_risk": bool(batching_suggestion),
        "batching_suggestion": batching_suggestion,
        "unmeasurable_criteria": unmeasurable,
    }


def main() -> int:
    # 用例 1：小规模需求不应触发分批建议（对照「离线 7 功能点通过」那条运行）
    small = [{"id": f"FP-{i}", "title": f"功能{i}", "group": "核心"} for i in range(1, 8)]
    small_criteria = [{"id": "AC-1", "description": "边界用例通过率 >= 1.0"}]
    report = assess(small, small_criteria)
    assert report["function_point_count"] == 7
    assert report["scope_risk"] is False, f"小规模需求不应触发分批：{report}"
    assert report["unmeasurable_criteria"] == []

    # 用例 2：复现 13 功能点的失败运行规模，应触发分批建议
    large = [{"id": f"FP-{i}", "title": f"功能{i}",
              "group": "核心" if i <= 5 else "扩展"} for i in range(1, 14)]
    large_criteria = [
        {"id": "AC-1", "description": "所有接口响应应尽量快"},
        {"id": "AC-2", "description": "失败用例数 == 0"},
    ]
    report2 = assess(large, large_criteria)
    assert report2["function_point_count"] == 13
    assert report2["scope_risk"] is True, f"13 功能点应触发分批：{report2}"
    assert "核心" in report2["batching_suggestion"] and "扩展" in report2["batching_suggestion"]
    assert [c["id"] for c in report2["unmeasurable_criteria"]] == ["AC-1"], \
        f"「应尽量快」应被判为不可量化：{report2['unmeasurable_criteria']}"

    # 用例 3：无模块数时必须退化为按功能点估算，不得跳过
    report3 = assess(small, small_criteria, module_count=None)
    assert report3["estimation"] == "退化估算"
    assert report3["estimated_loc"] == 7 * LOC_PER_FP

    print(json.dumps(report2, ensure_ascii=False, indent=2))
    print("spec-scope-guard smoke ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
