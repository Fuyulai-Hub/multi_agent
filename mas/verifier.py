"""独立测试验证器：冻结测试套件，并用框架侧证据重算测试结果。

解决的问题（docs/37 P0-2）：原实现每轮都由被测方重新生成 ``tests/*``，
覆盖率靠模型在用例元数据里填 ``fp_id`` 自报，门禁直接采信。这里把
「生成测试」与「认定证据有效」拆开：

1. **冻结**：首次执行前对 ``tests/**/*.py`` 计算 SHA-256，写入
   ``<run_dir>/test_manifest.json``，并把文件副本存到 ``<run_dir>/frozen_tests/``
   （两者都在沙箱根目录之外）。后续轮次不采信新生成的测试：检测到变更即从
   冻结副本恢复，并落 ``test_frozen_restored`` 事件。
2. **独立重算**：只采信测试进程回传的逐用例记录（cases），由框架重新计算
   total / failed / 通过率 / 分类覆盖；与模型自报的 summary 不一致即判证据无效。
3. **确定性反作弊**：执行前后测试文件 hash 必须一致；用例 id 不得重复；
   ``fp_id`` 必须属于需求功能点；分类必须合法；测试文件里必须至少有一条断言；
   exit code 与失败数必须一致。
4. **签名**：manifest 与 attestation 都用 HMAC-SHA256 签名（密钥取
   ``MAS_VERIFIER_SECRET``，未配置时用进程内随机密钥），artifact 被改动可检出。

边界（如实说明）：这里没有实现行覆盖率/变异测试；``coverage.rate`` 仍是
「该分类覆盖了多少个需求功能点」，但功能点集合已由验证器按需求规格重算，
不再是模型自报的数字。真实行覆盖需要 coverage.py / c8 / gcov 等独立工具链。
"""

from __future__ import annotations

import ast
import hashlib
import hmac
import json
import os
import secrets
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

__all__ = ["VerificationError", "TestVerifier", "verify_attestation"]

_CATEGORIES = ("functional", "exception", "boundary")
_VALID_STATUS = {"passed", "failed", "error"}
_PROCESS_SECRET = secrets.token_hex(32)
MANIFEST_NAME = "test_manifest.json"
FROZEN_DIR = "frozen_tests"


class VerificationError(RuntimeError):
    """测试证据不可信或冻结状态损坏。"""


def _secret() -> bytes:
    return (os.environ.get("MAS_VERIFIER_SECRET") or _PROCESS_SECRET).encode("utf-8")


def _canonical(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _sign(payload: Dict[str, Any]) -> str:
    return hmac.new(_secret(), _canonical(payload), hashlib.sha256).hexdigest()


def _signed(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(payload)
    data.pop("signature", None)
    data["signature"] = _sign(data)
    return data


def verify_attestation(attestation: Dict[str, Any]) -> bool:
    """校验 attestation 的签名（不校验 valid 的业务含义）。"""
    if not isinstance(attestation, dict) or not attestation.get("signature"):
        return False
    data = {key: value for key, value in attestation.items() if key != "signature"}
    try:
        expected = _sign(data)
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(str(attestation["signature"]), expected)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestVerifier:
    """某个 run 的冻结测试与证据验证器。"""

    def __init__(self, run_dir: Path, sandbox_root: Path, *, events: Any = None) -> None:
        self.run_dir = Path(run_dir)
        self.sandbox_root = Path(sandbox_root).resolve()
        self.events = events
        self.manifest_path = self.run_dir / MANIFEST_NAME
        self.frozen_dir = self.run_dir / FROZEN_DIR

    # ---------------------------------------------------------------- manifest
    def load_manifest(self) -> Optional[Dict[str, Any]]:
        if not self.manifest_path.exists():
            return None
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VerificationError(f"测试冻结清单不可读: {exc}") from exc
        if not verify_attestation(data):
            raise VerificationError("测试冻结清单签名无效（文件被篡改或来自其他验证器实例）")
        if data.get("invalidated"):
            return None
        return data

    def has_frozen_suite(self) -> bool:
        return self.load_manifest() is not None

    def freeze(self, test_file: str, iteration: int) -> Dict[str, Any]:
        """把当前 ``tests/**/*.py`` 冻结为权威套件。"""
        hashes = self._test_hashes()
        if not hashes:
            raise VerificationError("沙箱 tests/ 下没有任何 .py 测试文件，无法冻结")
        if test_file not in hashes:
            raise VerificationError(f"入口测试文件不在 tests/ 冻结集合内: {test_file}")
        if self.frozen_dir.exists():
            shutil.rmtree(self.frozen_dir)
        self.frozen_dir.mkdir(parents=True, exist_ok=True)
        for relative, digest in hashes.items():
            target = self.frozen_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.sandbox_root / relative, target)
        manifest = _signed({
            "version": 1,
            "test_file": test_file,
            "files": hashes,
            "digest": self._tree_digest(hashes),
            "iteration": int(iteration),
            "frozen_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "invalidated": False,
            "invalidation_reason": "",
        })
        self._write_json(self.manifest_path, manifest)
        if self.events is not None:
            self.events.emit(
                "test_suite_frozen", agent="verifier",
                payload={"test_file": test_file, "files": len(hashes),
                         "test_hash": manifest["digest"], "iteration": int(iteration)},
            )
        return manifest

    def restore_if_changed(self) -> List[Dict[str, str]]:
        """把被生成方改动的 tests/ 恢复成冻结副本；返回变更清单。"""
        manifest = self.load_manifest()
        if manifest is None:
            return []
        frozen = dict(manifest.get("files") or {})
        current = self._test_hashes()
        changes: List[Dict[str, str]] = []
        for relative, digest in sorted(frozen.items()):
            if relative not in current:
                changes.append({"path": relative, "reason": "missing"})
            elif current[relative] != digest:
                changes.append({"path": relative, "reason": "modified"})
        for relative in sorted(set(current) - set(frozen)):
            changes.append({"path": relative, "reason": "added"})
        if not changes:
            return []
        self._restore(manifest)
        if self.events is not None:
            self.events.emit(
                "test_frozen_restored", agent="verifier", severity="warning",
                payload={"test_file": manifest.get("test_file", ""), "changes": changes,
                         "test_hash": manifest.get("digest", ""),
                         "note": "冻结后测试文件发生变更，已用冻结副本覆盖；变更未被执行"},
            )
        return changes

    def invalidate(self, reason: str) -> None:
        """测试套件自身失效（无结果/协议崩溃/测试代码缺陷）时，允许下一轮重新生成。"""
        manifest = self.load_manifest()
        if manifest is None:
            return
        manifest["invalidated"] = True
        manifest["invalidation_reason"] = str(reason)[:500]
        manifest["invalidated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        self._write_json(self.manifest_path, _signed(manifest))
        if self.events is not None:
            self.events.emit(
                "test_freeze_invalidated", agent="verifier", severity="warning",
                payload={"reason": str(reason)[:500], "test_hash": manifest.get("digest", "")},
            )

    # ---------------------------------------------------------------- 取证
    def attest(
        self,
        *,
        parsed: Optional[Dict[str, Any]],
        exit_code: int,
        duration_ms: int,
        test_file: str,
        iteration: int,
        spec_fps: Iterable[str] = (),
        verification_gap: bool = False,
        root_cause_category: str = "",
    ) -> Dict[str, Any]:
        """用框架侧规则重算测试证据并签名。"""
        manifest = self.load_manifest()
        if manifest is None:
            raise VerificationError("没有有效的冻结测试清单，拒绝签发验证凭证")
        reasons: List[str] = []
        frozen_files = dict(manifest.get("files") or {})
        current = self._test_hashes()
        if current != frozen_files:
            reasons.append("test_files_changed_during_execution")

        cases_raw = parsed.get("cases") if isinstance(parsed, dict) else None
        if not isinstance(cases_raw, list):
            cases_raw = []
            reasons.append("missing_case_records")
        cases: List[Dict[str, Any]] = []
        seen: set = set()
        for item in cases_raw:
            if not isinstance(item, dict):
                reasons.append("malformed_case_record")
                continue
            name = str(item.get("test") or "").strip()
            class_name = str(item.get("class") or "").strip()
            if not name:
                reasons.append("case_missing_name")
                continue
            key = (class_name, name)
            if key in seen:
                reasons.append("duplicate_case_id")
                continue
            seen.add(key)
            status = str(item.get("status") or "")
            if status not in _VALID_STATUS:
                reasons.append(f"invalid_case_status:{status or 'empty'}")
                continue
            category = str(item.get("category") or "")
            if category not in _CATEGORIES:
                reasons.append(f"invalid_case_category:{category or 'empty'}")
            cases.append({
                "test": name,
                "class": class_name,
                "fp_id": str(item.get("fp_id") or ""),
                "category": category,
                "status": status,
                "message": str(item.get("message") or "")[:500],
            })

        total = len(cases)
        failed = sum(1 for case in cases if case["status"] != "passed")
        passed = total - failed
        if total == 0:
            reasons.append("no_cases")
        if isinstance(parsed, dict):
            if self._as_int(parsed.get("total")) != total:
                reasons.append("summary_total_mismatch")
            if self._as_int(parsed.get("failed")) != failed:
                reasons.append("summary_failed_mismatch")
            if self._as_int(parsed.get("passed")) != passed:
                reasons.append("summary_passed_mismatch")
        else:
            reasons.append("missing_result_marker")
        if exit_code == 0 and failed > 0:
            reasons.append("exit_code_zero_with_failures")
        if exit_code not in (0, 1):
            reasons.append(f"nonzero_infrastructure_exit:{exit_code}")
        if exit_code == 1 and failed == 0 and total > 0:
            reasons.append("exit_code_one_without_failures")

        spec_ids = {str(item) for item in spec_fps if str(item)}
        for case in cases:
            if spec_ids and not case["fp_id"]:
                reasons.append("case_missing_fp_id")
                break
        unknown = sorted({case["fp_id"] for case in cases
                          if case["fp_id"] and spec_ids and case["fp_id"] not in spec_ids})
        if unknown:
            reasons.append("unknown_fp_id:" + ",".join(unknown[:5]))

        assertions = self._assertion_count()
        if assertions == 0:
            reasons.append("no_assertions_in_frozen_tests")
        if verification_gap:
            reasons.append("verification_gap")

        coverage = self._coverage(cases, len(spec_ids))
        unique_reasons = list(dict.fromkeys(reasons))
        attestation = _signed({
            "version": 1,
            "valid": not unique_reasons,
            "reasons": unique_reasons,
            "test_file": test_file,
            "test_hash": manifest.get("digest", ""),
            "files": frozen_files,
            "iteration": int(iteration),
            "total": total,
            "passed": passed,
            "failed": failed,
            "assertions": assertions,
            "coverage": coverage,
            "exec": {"exit_code": int(exit_code), "duration_ms": int(duration_ms)},
            "verification_gap": bool(verification_gap),
            "root_cause_category": str(root_cause_category or ""),
            "verified_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "limitations": "功能点覆盖由框架按冻结用例重算；未接入行覆盖/变异测试",
        })
        self._write_json(self.run_dir / "verification_attestation.json", attestation)
        result = {**attestation, "cases": cases}   # 仅供本次进程内复用，不参与签名
        if self.events is not None:
            self.events.emit(
                "test_verified", agent="verifier", iteration=int(iteration),
                severity="info" if attestation["valid"] else "error",
                payload={"valid": attestation["valid"], "reasons": attestation["reasons"],
                         "test_hash": attestation["test_hash"], "total": total,
                         "failed": failed, "assertions": assertions},
            )
        return result

    # ---------------------------------------------------------------- 内部
    @staticmethod
    def _as_int(value: Any, default: int = -1) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    def _test_hashes(self) -> Dict[str, str]:
        tests_dir = self.sandbox_root / "tests"
        if not tests_dir.exists():
            return {}
        hashes: Dict[str, str] = {}
        for path in sorted(tests_dir.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            relative = path.relative_to(self.sandbox_root).as_posix()
            hashes[relative] = _sha256(path)
        return hashes

    @staticmethod
    def _tree_digest(hashes: Dict[str, str]) -> str:
        payload = "\n".join(f"{path}:{digest}" for path, digest in sorted(hashes.items()))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _assertion_count(self) -> int:
        count = 0
        if not self.frozen_dir.exists():
            return 0
        for path in sorted(self.frozen_dir.rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Assert):
                    count += 1
                elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr.startswith("assert") or node.func.attr in {"raises", "fail"}:
                        count += 1
        return count

    @staticmethod
    def _coverage(cases: List[Dict[str, Any]], fp_total: int) -> Dict[str, Dict[str, Any]]:
        coverage: Dict[str, Dict[str, Any]] = {}
        for category in _CATEGORIES:
            category_cases = [case for case in cases if case["category"] == category]
            fps = {case["fp_id"] for case in category_cases if case["fp_id"]}
            coverage[category] = {
                "total": len(category_cases),
                "fps": len(fps),
                "fps_total": fp_total,
                "rate": round(len(fps) / fp_total, 4) if fp_total else 0.0,
            }
        return coverage

    def _restore(self, manifest: Dict[str, Any]) -> None:
        if not self.frozen_dir.exists():
            raise VerificationError("冻结副本目录不存在，无法恢复冻结测试")
        frozen_hashes = dict(manifest.get("files") or {})
        for relative, digest in frozen_hashes.items():
            source = self.frozen_dir / relative
            if not source.exists() or _sha256(source) != digest:
                raise VerificationError(f"冻结副本损坏: {relative}")
        tests_dir = self.sandbox_root / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        current = self._test_hashes()
        for relative in sorted(set(current) - set(frozen_hashes)):
            try:
                (self.sandbox_root / relative).unlink()
            except OSError:
                pass
        for relative in frozen_hashes:
            target = self.sandbox_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.frozen_dir / relative, target)

    @staticmethod
    def _write_json(path: Path, payload: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)
