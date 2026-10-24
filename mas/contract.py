"""需求契约：条款抽取、映射校验、冻结与签名。

门禁加固（docs/39 续篇）要解决的问题：产品 Agent 只要「功能点非空 + 每点有样例」
就能通过，规格漏掉原文里的一条需求在门禁里完全合法。这里把原始需求切成可引用的
**规范性条款**（C-01…），并要求：

1. 每个功能点带 ``source_spans``（引用 clause_id，冻结时补全原文/行号）与
   ``acceptance_criteria``；
2. 每个条款要么被某个功能点引用，要么进入 ``out_of_scope`` 并给出理由；
3. 契约在 solution 之前冻结到 ``<run_dir>/contract/requirement_contract.json``，
   用与测试证据同一套 HMAC 密钥签名；门禁重算 spec/需求 hash，后续节点改规格即失败。

冻结文件位于 run 工作区（沙箱根 ``run/code``）之外；隐藏用例目录由
``hidden_suite_dir()`` 给出，同样在 run 目录之外。
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .verifier import SIGNATURE_ALG, canonical_hash, sign_payload, verify_signed

__all__ = [
    "ContractError",
    "CONTRACT_VERSION",
    "extract_clauses",
    "derive_package",
    "validate_contract",
    "freeze_contract",
    "load_frozen_contract",
    "verify_frozen_contract",
    "hidden_suite_dir",
]

CONTRACT_VERSION = 1
CONTRACT_RELATIVE = Path("contract") / "requirement_contract.json"
HIDDEN_DIRNAME = "hidden"

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_IMPORT_PATTERN = re.compile(r"\bimport\s+([A-Za-z_][A-Za-z0-9_]*)")
_BULLET = re.compile(r"^(?:[-*+]|\d+[.)、])\s+")
_FENCE = re.compile(r"^\s*```")
_OPEN_QUESTION_HINTS = ("待澄清", "开放问题", "澄清问题", "open question", "clarification")
_NON_NORMATIVE_HINTS = ("背景", "目标", "概述", "范围说明")


class ContractError(RuntimeError):
    """需求契约缺失、被篡改或不可冻结。"""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _norm_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


#: 引文比对用的"实义字符"口径：NFKC 归一全角标点/字母后，只保留字母数字与汉字。
#: 背景（task09-miniledger-01 实测）：契约条款里带 Markdown code span（反引号）、
#: 中文分号与 `->`，模型引用时只要去掉反引号/换半角标点/多一个空格，精确子串
#: 匹配就判 source_span_quote_mismatch，一条格式差异直接让整轮 intake 失败。
#: 引文校验的用途是证明"没有编造条款"，不是逐字复刻排版，所以按实义字符比对。
_QUOTE_NOISE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")


def _quote_key(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).lower()
    return _QUOTE_NOISE.sub("", normalized)


def _is_open_question_section(section: str) -> bool:
    lowered = section.lower()
    return any(hint in lowered for hint in _OPEN_QUESTION_HINTS)


def _is_non_normative_section(section: str) -> bool:
    lowered = section.lower()
    return any(hint in lowered for hint in _NON_NORMATIVE_HINTS)


def extract_clauses(text: str) -> List[Dict[str, Any]]:
    """把需求正文切成确定性编号的条款索引（行号 + 所在章节 + 原文）。

    规则（有意保守，避免把说明性文字当成需求）：
    - 跳过标题行、空行、代码围栏内容；
    - 跳过「背景/目标/概述」与「待澄清/开放问题」章节；
    - 其余每个非空行都是一条条款（列表符号被去掉，正文原样保留）。
    同一份文本在任何进程里都得到同样的 C-01… 编号，模型与校验器因此可对齐。
    """
    clauses: List[Dict[str, Any]] = []
    section = ""
    in_fence = False
    for lineno, raw in enumerate(str(text or "").splitlines(), start=1):
        line = raw.strip()
        if _FENCE.match(raw):
            in_fence = not in_fence
            continue
        if in_fence or not line:
            continue
        if line.startswith("#"):
            section = line.lstrip("#").strip()
            continue
        if _is_open_question_section(section) or _is_non_normative_section(section):
            continue
        if line in {"---", "***", "___"} or re.fullmatch(r"\|?[\s:|-]+\|?", line):
            continue
        body = _BULLET.sub("", line).strip()
        if not body:
            continue
        clauses.append({
            "id": f"C-{len(clauses) + 1:02d}",
            "line": lineno,
            "section": section,
            "text": body,
        })
    return clauses


def derive_package(spec: Dict[str, Any], text: str) -> Tuple[str, str]:
    """交付包名：优先取契约 ``package``，否则从需求里的 ``import xxx`` 推导。"""
    declared = str((spec or {}).get("package") or "").strip()
    if _IDENTIFIER.match(declared):
        return declared, "contract"
    match = _IMPORT_PATTERN.search(str(text or ""))
    if match and _IDENTIFIER.match(match.group(1)):
        return match.group(1), "requirement"
    return "", ""


def _clause_map(clauses: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {str(item.get("id")): item for item in clauses if isinstance(item, dict) and item.get("id")}


def _span_clause_id(span: Any) -> str:
    if isinstance(span, str):
        return span.strip()
    if isinstance(span, dict):
        for key in ("clause_id", "clause", "id"):
            value = str(span.get(key) or "").strip()
            if value:
                return value
    return ""


def _acceptance_texts(value: Any) -> List[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    texts: List[str] = []
    for item in value:
        if isinstance(item, dict):
            text = _norm_text(item.get("description") or item.get("text") or item.get("criteria"))
        else:
            text = _norm_text(item)
        if text:
            texts.append(text)
    return texts


def validate_contract(
    spec: Dict[str, Any],
    requirement_text: str,
    *,
    clauses: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """校验需求契约并就地补全 source_spans 的原文/行号。

    返回 ``{"valid", "errors", "clauses", "out_of_scope", "coverage", "package"}``。
    在原始需求为空（单元测试直接调用 Agent）时退化为「只校验样例」，但门禁端
    硬性要求存在已签名契约，因此生产运行无法利用这个退化分支。
    """
    spec = spec if isinstance(spec, dict) else {}
    text = str(requirement_text or "")
    clauses = list(clauses if clauses is not None else extract_clauses(text))
    by_id = _clause_map(clauses)
    errors: List[str] = []

    function_points = spec.get("function_points")
    if not isinstance(function_points, list) or not function_points:
        return {
            "valid": False, "errors": ["function_points_missing"], "clauses": clauses,
            "out_of_scope": [], "coverage": {"total": len(clauses), "mapped": 0,
                                              "out_of_scope": 0, "uncovered": [c["id"] for c in clauses]},
            "package": "",
        }

    seen_fps: set = set()
    mapped: set = set()
    for fp in function_points:
        if not isinstance(fp, dict):
            errors.append("function_point_not_object")
            continue
        fp_id = str(fp.get("id") or "").strip()
        if not fp_id:
            errors.append("function_point_missing_id")
            continue
        if fp_id in seen_fps:
            errors.append(f"duplicate_function_point:{fp_id}")
        seen_fps.add(fp_id)
        if not fp.get("samples"):
            errors.append(f"function_point_missing_samples:{fp_id}")
        criteria = _acceptance_texts(fp.get("acceptance_criteria"))
        if not criteria:
            errors.append(f"function_point_missing_acceptance_criteria:{fp_id}")
        else:
            fp["acceptance_criteria"] = criteria

        if not clauses:
            continue
        spans = fp.get("source_spans")
        if isinstance(spans, str):
            spans = [spans]
        if not isinstance(spans, list) or not spans:
            errors.append(f"function_point_missing_source_spans:{fp_id}")
            continue
        normalized: List[Dict[str, Any]] = []
        for span in spans:
            clause_id = _span_clause_id(span)
            if not clause_id:
                errors.append(f"source_span_missing_clause_id:{fp_id}")
                continue
            clause = by_id.get(clause_id)
            if clause is None:
                errors.append(f"source_span_unknown_clause:{fp_id}:{clause_id}")
                continue
            quote = _norm_text(span.get("quote")) if isinstance(span, dict) else ""
            if quote:
                probe = quote.strip("` ")
                if probe.endswith(("…", "...")):
                    probe = re.sub(r"(?:…|\.\.\.)$", "", probe).strip()
                # 实义字符子串比对：忽略反引号/全半角标点/空白差异，但改写用词仍判失败。
                probe_key = _quote_key(probe)
                matched = (len(probe_key) >= 4
                           and probe_key in _quote_key(clause["text"]))
                if not matched:
                    errors.append(f"source_span_quote_mismatch:{fp_id}:{clause_id}")
                    continue
            normalized.append({
                "clause_id": clause_id,
                "quote": clause["text"],
                "line": int(clause["line"]),
            })
            mapped.add(clause_id)
        if normalized:
            fp["source_spans"] = normalized
            fp["source_lines"] = [item["line"] for item in normalized]

    out_of_scope: List[Dict[str, Any]] = []
    seen_out: set = set()
    raw_out = spec.get("out_of_scope")
    if isinstance(raw_out, list):
        for item in raw_out:
            if isinstance(item, str):
                errors.append("out_of_scope_requires_reason")
                continue
            if not isinstance(item, dict):
                errors.append("out_of_scope_not_object")
                continue
            clause_id = _span_clause_id(item)
            reason = _norm_text(item.get("reason"))
            if not clause_id:
                errors.append("out_of_scope_missing_clause_id")
                continue
            if clause_id not in by_id:
                errors.append(f"out_of_scope_unknown_clause:{clause_id}")
                continue
            if not reason:
                errors.append(f"out_of_scope_missing_reason:{clause_id}")
                continue
            if clause_id in seen_out:
                errors.append(f"out_of_scope_duplicate:{clause_id}")
                continue
            seen_out.add(clause_id)
            out_of_scope.append({
                "clause_id": clause_id, "line": int(by_id[clause_id]["line"]),
                "text": by_id[clause_id]["text"], "reason": reason,
            })
    elif raw_out is not None:
        errors.append("out_of_scope_not_list")

    both = sorted(mapped & seen_out)
    for clause_id in both:
        errors.append(f"clause_both_mapped_and_out_of_scope:{clause_id}")
    uncovered = [item["id"] for item in clauses if item["id"] not in mapped
                 and item["id"] not in seen_out]
    for clause_id in uncovered:
        errors.append(f"clause_unmapped:{clause_id}")

    package, package_source = derive_package(spec, text)
    if clauses and not package:
        errors.append("package_missing")
    if package:
        spec["package"] = package
        spec["package_source"] = package_source

    return {
        "valid": not errors,
        "errors": list(dict.fromkeys(errors)),
        "clauses": clauses,
        "out_of_scope": out_of_scope,
        "coverage": {
            "total": len(clauses),
            "mapped": len(mapped),
            "out_of_scope": len(out_of_scope),
            "uncovered": uncovered,
        },
        "package": package,
    }


def freeze_contract(
    run_dir: Path,
    spec: Dict[str, Any],
    requirement_text: str,
    *,
    run_id: str,
    config: Any = None,
    clauses: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """冻结并签名需求契约（幂等）。

    同 run 内重复调用只有内容完全一致才复用；任何差异都抛 ``ContractError``，
    保证 solution 之后的节点无法「重新定义需求」。
    """
    run_dir = Path(run_dir)
    path = run_dir / CONTRACT_RELATIVE
    requirement_text = str(requirement_text or "")
    clauses = list(clauses if clauses is not None else extract_clauses(requirement_text))
    spec_copy = copy.deepcopy(spec)
    contract_hash = canonical_hash(spec_copy)
    requirement_hash = _sha256_text(requirement_text)

    if path.exists():
        existing = load_frozen_contract(path, config=config)
        if (str(existing.get("run_id") or "") == str(run_id)
                and str(existing.get("contract_hash") or "") == contract_hash
                and str(existing.get("requirement_hash") or "") == requirement_hash):
            existing["reused"] = True
            return existing
        raise ContractError(
            "需求契约已冻结且内容不同：拒绝覆盖（需求/规格在 solution 之前必须保持不变）"
        )

    payload = {
        "version": CONTRACT_VERSION,
        "signature_alg": SIGNATURE_ALG,
        "run_id": str(run_id),
        "frozen_at": _now(),
        "requirement_hash": requirement_hash,
        "contract_hash": contract_hash,
        "clauses": clauses,
        "out_of_scope": list(spec_copy.get("out_of_scope") or []),
        "spec": spec_copy,
    }
    # signature_alg 必须在签名**之前**写入——签名覆盖除 signature 外的全部字段，
    # 事后补写会让刚冻结的契约验签失败。
    frozen = sign_payload(payload, config=config)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(frozen, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)
    frozen["reused"] = False
    return frozen


def load_frozen_contract(path: Path, *, config: Any = None) -> Dict[str, Any]:
    """读取并验签冻结契约；签名/内部 hash 任一不符即抛 ``ContractError``。"""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"需求契约不可读: {exc}") from exc
    if not isinstance(data, dict):
        raise ContractError("需求契约不是 JSON 对象")
    if int(data.get("version") or 0) != CONTRACT_VERSION:
        raise ContractError(f"不支持的需求契约版本: {data.get('version')}")
    if not verify_signed(data, config=config):
        raise ContractError("需求契约签名无效（文件被篡改或来自其他签名密钥）")
    spec = data.get("spec")
    if not isinstance(spec, dict) or canonical_hash(spec) != str(data.get("contract_hash") or ""):
        raise ContractError("需求契约内部 hash 不一致（spec 被改动）")
    return data


def verify_frozen_contract(
    frozen: Dict[str, Any],
    spec: Dict[str, Any],
    requirement_text: str,
    *,
    run_id: str = "",
    config: Any = None,
    require_requirement_text: bool = True,
) -> List[str]:
    """门禁侧重验：签名、run_id、需求原文与当前规格都必须与冻结时一致。"""
    reasons: List[str] = []
    if not isinstance(frozen, dict) or not frozen:
        return ["requirement_contract_missing"]
    if not verify_signed(frozen, config=config):
        reasons.append("requirement_contract_signature_invalid")
    expected_run_id = str(run_id or "")
    if expected_run_id and str(frozen.get("run_id") or "") != expected_run_id:
        reasons.append("requirement_contract_run_id_mismatch")
    frozen_spec = frozen.get("spec") if isinstance(frozen.get("spec"), dict) else {}
    if canonical_hash(frozen_spec) != str(frozen.get("contract_hash") or ""):
        reasons.append("requirement_contract_internal_hash_mismatch")
    if canonical_hash(spec if isinstance(spec, dict) else {}) != str(frozen.get("contract_hash") or ""):
        reasons.append("requirement_spec_modified_after_freeze")

    text = str(requirement_text or "")
    if text:
        if _sha256_text(text) != str(frozen.get("requirement_hash") or ""):
            reasons.append("requirement_text_modified_after_freeze")
    elif require_requirement_text:
        reasons.append("requirement_source_missing")

    clauses = frozen.get("clauses") if isinstance(frozen.get("clauses"), list) else []
    outcome = validate_contract(copy.deepcopy(spec), text, clauses=clauses)
    for error in outcome.get("errors") or []:
        reasons.append(f"contract_invalid:{error}")
    return list(dict.fromkeys(reasons))


def hidden_suite_dir(workspace_dir: Path, run_id: str) -> Path:
    """隐藏套件的固定位置：workspace/hidden/<run_id>（在 run 目录之外）。"""
    return Path(workspace_dir) / HIDDEN_DIRNAME / str(run_id)
