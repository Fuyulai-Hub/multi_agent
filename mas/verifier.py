"""独立测试验证器：冻结测试套件，并用框架侧证据重算测试结果。

解决的问题（docs/37 P0-2）：原实现每轮都由被测方重新生成 ``tests/*``，
覆盖率靠模型在用例元数据里填 ``fp_id`` 自报，门禁直接采信。这里把
「生成测试」与「认定证据有效」拆开：

1. **冻结**：首次执行前对 ``tests/**/*.py`` 计算 SHA-256，写入
   ``<run_dir>/test_manifest.json``，并把文件副本存到 ``<run_dir>/frozen_tests/``
   （两者都在沙箱根目录之外）。后续轮次不采信新生成的测试：检测到变更即从
   冻结副本恢复，并落 ``test_frozen_restored`` 事件。
2. **独立重算**：只采信框架 runner（``mas/verify/runner.py``）写到工作区之外的
   逐用例记录（cases），由框架重新计算 total / failed / 通过率 / 分类覆盖；
   测试进程 stdout 里的任何 marker 一律不作为证据。冻结清单同时记录
   ``test_ids`` 与 fp_id 元数据，runner 只执行清单内的用例。
3. **确定性反作弊**：执行前后测试文件 hash 必须一致；用例 id 不得重复；
   ``fp_id`` 必须属于需求功能点；分类必须合法；测试文件里必须至少有一条断言；
   exit code 与失败数必须一致。
4. **签名**：manifest 与 attestation 都用 HMAC-SHA256 签名（密钥取
   ``MAS_VERIFIER_SECRET``，未配置时用进程内随机密钥），artifact 被改动可检出。
   release 模式（``MAS_ENV=release/prod/production`` 或
   ``runtime.verifier.release_mode=true`` 或 ``MAS_VERIFIER_RELEASE=1``）必须配置
   稳定密钥（>=32 字符），缺失/过短时启动即拒绝，绝不退回进程随机密钥。
5. **证据绑定**：attestation 除 test 摘要外，还签入 ``requirement_hash`` /
   ``code_hash`` / ``visible_suite_hash`` / ``hidden_suite_hash`` / ``env_hash`` /
   ``runner_version`` / ``per_case_digest`` / ``coverage`` / ``run_id``。
   门禁会用 ``verify_evidence()`` 重新计算这些 hash、重验签名与逐用例/覆盖率，
   任何一项不一致直接判失败；其中 ``code_hash`` 在测试取证时记录、门禁时重算，
   专门防止「测试通过后代码又被改」的 TOCTOU。

字段口径（避免误读）：

- ``visible_suite_hash``：取证时沙箱 ``tests/**/*.py`` 的树摘要；
- ``hidden_suite_hash``：``run_dir/frozen_tests/`` 冻结副本的树摘要。本系统没有
  另外编写一套隐藏用例，这里把「生成方不可写的冻结副本」作为 hidden 侧；
- ``code_hash``：沙箱内交付代码的树摘要（排除 tests/.mas_verify/缓存目录）；
- ``env_hash``：框架 Python/平台/沙箱后端的稳定指纹。容器模式暂用镜像 tag，
  生产应固定到镜像 digest（当前实现如实标注为 tag，不假装是 digest）。

边界（如实说明）：这里没有实现行覆盖率/变异测试；``coverage.rate`` 仍是
「该分类覆盖了多少个需求功能点」，但功能点集合已由验证器按需求规格重算，
不再是模型自报的数字。真实行覆盖需要 coverage.py / c8 / gcov 等独立工具链。

密钥演进（生产建议）：当前为对称 HMAC-SHA256，签名方与验证方共享同一密钥；
生产环境建议换 Ed25519（私钥仅签名方持有、公钥分发验证）或云 KMS 签名，
并把算法标识写入 attestation 的 ``signature_alg``，便于平滑迁移。
"""

from __future__ import annotations

import ast
import hashlib
import hmac
import json
import os
import platform
import secrets
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .errors import ConfigError
from .verify.runner import RUNNER_NAME, RUNNER_VERSION

__all__ = [
    "VerificationError", "TestVerifier", "verify_attestation",
    "release_mode", "require_signing_secret",
    "canonical_hash", "sign_payload", "verify_signed",
    "code_tree_hash",
]

_CATEGORIES = ("functional", "exception", "boundary")
_VALID_STATUS = {"passed", "failed", "error"}
_PROCESS_SECRET = secrets.token_hex(32)
MANIFEST_NAME = "test_manifest.json"
FROZEN_DIR = "frozen_tests"
SIGNATURE_ALG = "hmac-sha256"
_MIN_RELEASE_SECRET = 32
_RELEASE_ENV_VALUES = {"release", "prod", "production"}
_TRUTHY_VALUES = {"1", "true", "yes", "on"}
# code_hash 的证据范围：交付代码，不含测试/框架 runner/缓存。tests 目录无论嵌套在哪层
# 都排除（测试套件由 suite hash 单独绑定）。
_CODE_EXCLUDED_DIRS = {
    "tests", ".mas_verify", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".git",
}
_CODE_EXCLUDED_SUFFIXES = {".pyc", ".pyo"}


class VerificationError(RuntimeError):
    """测试证据不可信或冻结状态损坏。"""


def _configured_secret() -> str:
    return str(os.environ.get("MAS_VERIFIER_SECRET") or "").strip()


def release_mode(config: Any = None) -> bool:
    """是否处于 release 模式：环境变量优先，其次 runtime.verifier / system.environment。"""
    env_value = str(os.environ.get("MAS_ENV") or "").strip().lower()
    if env_value in _RELEASE_ENV_VALUES:
        return True
    flag = str(os.environ.get("MAS_VERIFIER_RELEASE") or "").strip().lower()
    if flag in _TRUTHY_VALUES:
        return True
    runtime = getattr(config, "runtime", None) if config is not None else None
    if isinstance(runtime, dict):
        verifier_cfg = runtime.get("verifier") or {}
        if isinstance(verifier_cfg, dict) and \
                str(verifier_cfg.get("release_mode") or "").strip().lower() in _TRUTHY_VALUES:
            return True
    system = getattr(config, "system", None) if config is not None else None
    if isinstance(system, dict) and \
            str(system.get("environment") or "").strip().lower() in _RELEASE_ENV_VALUES:
        return True
    return False


def require_signing_secret(config: Any = None) -> None:
    """release 模式启动前校验稳定密钥；dev 模式保留进程随机密钥（重启后旧凭据失效）。

    在 Leader 构造时调用，缺失即抛 ConfigError 拒绝启动，而不是运行到一半才发现
    无法验签或退回进程随机密钥。
    """
    if not release_mode(config):
        return
    secret = _configured_secret()
    if not secret:
        raise ConfigError(
            "release 模式缺少稳定的验证签名密钥：必须设置 MAS_VERIFIER_SECRET"
            "（>=32 字符，推荐 openssl rand -hex 32；生产建议 Ed25519/KMS）。"
            "拒绝退回进程随机密钥。"
        )
    if len(secret) < _MIN_RELEASE_SECRET:
        raise ConfigError(
            f"release 模式的 MAS_VERIFIER_SECRET 过短（{len(secret)} 字符，至少 "
            f"{_MIN_RELEASE_SECRET}）：请使用 openssl rand -hex 32 生成。"
        )


def _resolve_secret(config: Any = None) -> bytes:
    """解析签名密钥：有环境密钥用之；release 缺失/过短抛错，dev 才回落进程随机。"""
    secret = _configured_secret()
    is_release = release_mode(config)
    if secret:
        if is_release and len(secret) < _MIN_RELEASE_SECRET:
            raise VerificationError(
                f"release 模式的 MAS_VERIFIER_SECRET 过短（{len(secret)} 字符，至少 "
                f"{_MIN_RELEASE_SECRET}）"
            )
        return secret.encode("utf-8")
    if is_release:
        raise VerificationError(
            "release 模式必须设置稳定的 MAS_VERIFIER_SECRET，拒绝使用进程随机密钥"
        )
    return _PROCESS_SECRET.encode("utf-8")


def _coerce_secret(secret: Any) -> bytes:
    if isinstance(secret, (bytes, bytearray)):
        return bytes(secret)
    return str(secret).encode("utf-8")


def _canonical(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _sign(payload: Dict[str, Any], secret: Any = None) -> str:
    key = _coerce_secret(secret) if secret is not None else _resolve_secret()
    return hmac.new(key, _canonical(payload), hashlib.sha256).hexdigest()


def _signed(payload: Dict[str, Any], secret: Any = None) -> Dict[str, Any]:
    data = dict(payload)
    data.pop("signature", None)
    data["signature"] = _sign(data, secret)
    return data


def _per_case_digest(cases: Iterable[Any]) -> str:
    """逐用例记录的摘要（与 cases 一起签入证据包，门禁可重算）。"""
    return hashlib.sha256(_canonical(list(cases))).hexdigest()


def verify_attestation(attestation: Dict[str, Any], *, secret: Any = None,
                       config: Any = None) -> bool:
    """校验 attestation 的签名（不校验 valid 的业务含义）。

    ``secret`` 显式传入时用于校验该验证器实例的密钥；否则按 ``config``/环境解析。
    release 模式缺少稳定密钥时返回 False（fail-closed），不退回进程随机。
    """
    if not isinstance(attestation, dict) or not attestation.get("signature"):
        return False
    data = {key: value for key, value in attestation.items() if key != "signature"}
    try:
        expected = _sign(data, secret if secret is not None else _resolve_secret(config))
    except (TypeError, ValueError, VerificationError):
        return False
    return hmac.compare_digest(str(attestation["signature"]), expected)


def canonical_hash(payload: Any) -> str:
    """任意 JSON 负载的 canonical SHA-256（与签名同口径，跨进程可复算）。"""
    return hashlib.sha256(_canonical(payload)).hexdigest()


def sign_payload(payload: Dict[str, Any], *, config: Any = None,
                 secret: Any = None) -> Dict[str, Any]:
    """给框架侧产物签名（需求契约等），复用测试证据的 HMAC 密钥体系。

    与 ``TestVerifier`` 一样：release 模式缺稳定密钥时抛 ``VerificationError``，
    dev 模式回落到进程随机密钥。
    """
    key = _coerce_secret(secret) if secret is not None else _resolve_secret(config)
    return _signed(payload, key)


def verify_signed(payload: Dict[str, Any], *, config: Any = None,
                  secret: Any = None) -> bool:
    """校验 ``sign_payload`` 生成的对象；密钥解析失败按不可信处理（fail-closed）。"""
    if secret is None:
        try:
            secret = _resolve_secret(config)
        except VerificationError:
            return False
    return verify_attestation(payload, secret=secret)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def code_hashes_at(root: Path) -> Dict[str, str]:
    """交付代码的树 hash：排除测试、框架 runner、缓存与版本库目录。

    模块级函数是唯一口径：TestVerifier 取证与门禁重算、Reviewer 的
    ``review_report.code_hash`` 都调用它，避免三处各写一份导致 hash 对不上。
    """
    root = Path(root)
    if not root.exists():
        return {}
    hashes: Dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        parts = relative.split("/")
        if any(part in _CODE_EXCLUDED_DIRS for part in parts[:-1]):
            continue
        if parts[0] in _CODE_EXCLUDED_DIRS:
            continue
        if path.suffix.lower() in _CODE_EXCLUDED_SUFFIXES:
            continue
        hashes[relative] = _sha256(path)
    return hashes


def tree_digest(hashes: Dict[str, str]) -> str:
    payload = "\n".join(f"{path}:{digest}" for path, digest in sorted(hashes.items()))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def code_tree_hash(root: Path) -> str:
    """交付代码树摘要；空目录返回空串（门禁据此判缺失）。"""
    hashes = code_hashes_at(root)
    return tree_digest(hashes) if hashes else ""


class TestVerifier:
    """某个 run 的冻结测试与证据验证器。"""

    def __init__(self, run_dir: Path, sandbox_root: Path, *, events: Any = None,
                 config: Any = None, sandbox: Any = None, run_id: str = "",
                 code_root: Optional[Path] = None,
                 attestation_path: Optional[Path] = None) -> None:
        self.run_dir = Path(run_dir)
        self.sandbox_root = Path(sandbox_root).resolve()
        # 交付代码的 hash 根：可见套件默认就是沙箱根；隐藏套件的测试文件在
        # run 工作区之外，但证据必须绑定的仍是沙箱里的交付代码。
        self.code_root = Path(code_root).resolve() if code_root is not None else self.sandbox_root
        self.events = events
        self.config = config
        self.sandbox = sandbox
        self.run_id = str(run_id or self.run_dir.name)
        self.release_mode = release_mode(config)
        # release 缺失稳定密钥时在构造期就失败（Leader 启动检查会更早给出 ConfigError）
        self._secret = _resolve_secret(config)
        self.manifest_path = self.run_dir / MANIFEST_NAME
        self.frozen_dir = self.run_dir / FROZEN_DIR
        self.attestation_path = (Path(attestation_path) if attestation_path is not None
                                 else self.run_dir / "verification_attestation.json")

    # ---------------------------------------------------------------- manifest
    def load_manifest(self) -> Optional[Dict[str, Any]]:
        if not self.manifest_path.exists():
            return None
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VerificationError(f"测试冻结清单不可读: {exc}") from exc
        if not verify_attestation(data, secret=self._secret):
            raise VerificationError("测试冻结清单签名无效（文件被篡改或来自其他验证器实例）")
        if data.get("invalidated"):
            return None
        return data

    def has_frozen_suite(self) -> bool:
        return self.load_manifest() is not None

    def freeze(
        self,
        test_file: str,
        iteration: int,
        *,
        case_meta: Optional[Iterable[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """把当前 ``tests/**/*.py`` 冻结为权威套件。

        ``case_meta`` 是生成方提交的用例元数据（fp_id/op/name/category）。
        框架用 AST 枚举冻结文件里的 ``test_id``，再把元数据按方法名绑定到
        ``test_id`` 上一起签名——runner 只执行这些 id，fp_id 也以冻结清单为准。
        """
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
        test_ids = self._collect_test_ids()
        if not test_ids:
            raise VerificationError("冻结测试文件中没有可枚举的 unittest 用例")
        manifest = _signed({
            "version": 2,
            "test_file": test_file,
            "files": hashes,
            "digest": self._tree_digest(hashes),
            "test_ids": test_ids,
            "case_meta": self._bind_case_meta(test_ids, case_meta),
            "iteration": int(iteration),
            "frozen_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "invalidated": False,
            "invalidation_reason": "",
        }, self._secret)
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
        self._write_json(self.manifest_path, _signed(manifest, self._secret))
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
        requirement_spec: Optional[Dict[str, Any]] = None,
        run_id: str = "",
    ) -> Dict[str, Any]:
        """用框架侧规则重算测试证据并签名。

        只认框架 runner 写出的结果：``parsed`` 必须带 ``runner.name ==
        'mas.verify.runner'`` 与匹配的 ``manifest_digest``；逐用例记录必须
        与冻结清单 ``test_ids`` 全量一致。任何来自测试进程 stdout marker 的
        JSON（缺 runner 元数据）都会被判 ``untrusted_result_source``。

        证据包同时绑定 requirement/code/visible-suite/hidden-suite/env 五个 hash、
        runner 版本、逐用例摘要与 run_id；门禁会用 ``verify_evidence()`` 重算并
        再次验签，任何一项不匹配都判失败。
        """
        manifest = self.load_manifest()
        if manifest is None:
            raise VerificationError("没有有效的冻结测试清单，拒绝签发验证凭证")
        reasons: List[str] = []
        frozen_files = dict(manifest.get("files") or {})
        current = self._test_hashes()
        if current != frozen_files:
            reasons.append("test_files_changed_during_execution")

        has_result = isinstance(parsed, dict)
        runner_meta = parsed.get("runner") if has_result else None
        if not has_result:
            reasons.append("missing_runner_result")
        elif not isinstance(runner_meta, dict) \
                or str(runner_meta.get("name") or "") != RUNNER_NAME:
            reasons.append("untrusted_result_source")
        else:
            if self._as_int(runner_meta.get("version")) != RUNNER_VERSION:
                reasons.append("unsupported_runner_version")
            if str(runner_meta.get("manifest_digest") or "") != str(manifest.get("digest") or ""):
                reasons.append("manifest_digest_mismatch")
            if str(parsed.get("error") or ""):
                reasons.append("runner_error:" + str(parsed.get("error"))[:200])

        cases_raw = parsed.get("cases") if has_result else None
        if not isinstance(cases_raw, list):
            cases_raw = []
            if has_result:
                reasons.append("missing_case_records")
        meta_all = dict(manifest.get("case_meta") or {})
        cases: List[Dict[str, Any]] = []
        seen: set = set()
        for item in cases_raw:
            if not isinstance(item, dict):
                reasons.append("malformed_case_record")
                continue
            test_id = str(item.get("test_id") or "").strip()
            if not test_id:
                reasons.append("case_missing_test_id")
                continue
            parts = test_id.rsplit(".", 2)
            name = parts[-1]
            class_name = parts[-2] if len(parts) >= 3 else ""
            if test_id in seen:
                reasons.append("duplicate_case_id")
                continue
            seen.add(test_id)
            status = str(item.get("status") or "")
            if status not in _VALID_STATUS:
                reasons.append(f"invalid_case_status:{status or 'empty'}")
                continue
            meta = meta_all.get(test_id) or {}
            category = str(meta.get("category") or item.get("category") or "")
            if category not in _CATEGORIES:
                reasons.append(f"invalid_case_category:{category or 'empty'}")
            cases.append({
                "test_id": test_id,
                "test": name,
                "class": class_name,
                "fp_id": str(meta.get("fp_id") or item.get("fp_id") or ""),
                "category": category,
                "status": status,
                "message": str(item.get("failure") or item.get("message") or "")[:500],
            })

        manifest_ids = [str(item) for item in (manifest.get("test_ids") or []) if str(item)]
        manifest_set = set(manifest_ids)
        executed_ids = {case["test_id"] for case in cases}
        if not manifest_set:
            reasons.append("manifest_missing_test_ids")
        missing = sorted(manifest_set - executed_ids)
        if missing:
            reasons.append("manifest_cases_not_executed:" + ",".join(missing[:5]))
        unexpected = sorted(executed_ids - manifest_set)
        if unexpected:
            reasons.append("unexpected_case_ids:" + ",".join(unexpected[:5]))
        if has_result:
            unexpected_discovered = [str(item) for item in (parsed.get("unexpected_test_ids") or [])]
            if unexpected_discovered:
                reasons.append("unexpected_test_ids:" + ",".join(unexpected_discovered[:5]))

        total = len(cases)
        failed = sum(1 for case in cases if case["status"] != "passed")
        passed = total - failed
        if total == 0:
            reasons.append("no_cases")
        if has_result:
            if self._as_int(parsed.get("total")) != total:
                reasons.append("summary_total_mismatch")
            if self._as_int(parsed.get("failed")) != failed:
                reasons.append("summary_failed_mismatch")
            if self._as_int(parsed.get("passed")) != passed:
                reasons.append("summary_passed_mismatch")
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
        # 证据 hash：测试时记录，门禁时由 verify_evidence() 用同一口径重算。
        try:
            evidence = self.evidence_hashes(requirement_spec=requirement_spec)
        except VerificationError as exc:
            evidence = {
                "requirement_hash": "", "code_hash": "",
                "visible_suite_hash": "", "hidden_suite_hash": "",
                "env_hash": "", "manifest_digest": str(manifest.get("digest") or ""),
                "test_files_changed": False, "frozen_suite_corrupted": False,
            }
            reasons.append(f"evidence_recompute_failed:{exc}")
        if evidence.get("test_files_changed"):
            reasons.append("test_files_changed_during_execution")
        if evidence.get("frozen_suite_corrupted"):
            reasons.append("frozen_suite_corrupted")

        unique_reasons = list(dict.fromkeys(reasons))
        attestation = _signed({
            "version": 2,
            "signature_alg": SIGNATURE_ALG,
            "valid": not unique_reasons,
            "reasons": unique_reasons,
            "test_file": test_file,
            "test_hash": manifest.get("digest", ""),
            "requirement_hash": evidence.get("requirement_hash", ""),
            "code_hash": evidence.get("code_hash", ""),
            "visible_suite_hash": evidence.get("visible_suite_hash", ""),
            "hidden_suite_hash": evidence.get("hidden_suite_hash", ""),
            "env_hash": evidence.get("env_hash", ""),
            "runner_version": RUNNER_VERSION,
            "runner_name": RUNNER_NAME,
            "run_id": str(run_id or self.run_id),
            "per_case_digest": _per_case_digest(cases),
            "files": frozen_files,
            "iteration": int(iteration),
            "total": total,
            "passed": passed,
            "failed": failed,
            "assertions": assertions,
            "coverage": coverage,
            "cases": cases,
            "exec": {"exit_code": int(exit_code), "duration_ms": int(duration_ms)},
            "verification_gap": bool(verification_gap),
            "root_cause_category": str(root_cause_category or ""),
            "verified_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "limitations": "功能点覆盖由框架按冻结用例重算；未接入行覆盖/变异测试",
        }, self._secret)
        self._write_json(self.attestation_path, attestation)
        if self.events is not None:
            self.events.emit(
                "test_verified", agent="verifier", iteration=int(iteration),
                severity="info" if attestation["valid"] else "error",
                payload={"valid": attestation["valid"], "reasons": attestation["reasons"],
                         "test_hash": attestation["test_hash"],
                         "code_hash": attestation["code_hash"],
                         "run_id": attestation["run_id"], "total": total,
                         "failed": failed, "assertions": assertions},
            )
        return attestation

    # ---------------------------------------------------------------- 门禁重验
    def evidence_hashes(self, *, requirement_spec: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """按取证时的同一口径重算全部 hash（门禁与 attest 共用）。

        返回 ``manifest_digest`` 以及两个一致性标记，便于调用方区分「hash 变了」
        与「冻结清单/副本损坏」。
        """
        manifest = self.load_manifest()
        if manifest is None:
            raise VerificationError("冻结测试清单缺失或已失效，无法重算证据 hash")
        frozen_files = dict(manifest.get("files") or {})
        current = self._test_hashes()
        frozen_actual = self._frozen_hashes()
        code_files = self._code_hashes()
        return {
            "requirement_hash": self._requirement_hash(requirement_spec),
            "code_hash": self._tree_digest(code_files) if code_files else "",
            "visible_suite_hash": self._tree_digest(current) if current else "",
            "hidden_suite_hash": self._tree_digest(frozen_actual) if frozen_actual else "",
            "env_hash": self._env_hash(),
            "manifest_digest": str(manifest.get("digest") or ""),
            "test_files_changed": current != frozen_files,
            "frozen_suite_corrupted": frozen_actual != frozen_files,
            "code_file_count": len(code_files),
        }

    def verify_evidence(self, attestation: Dict[str, Any], *,
                        requirement_spec: Optional[Dict[str, Any]] = None,
                        run_id: str = "") -> Dict[str, Any]:
        """门禁侧重验证据包：重算 hash → 验签 → 逐项比对。

        重算在验签之前完成（先看「当前工作区事实」，再用签名确认「取证时事实」），
        任何一项不匹配都返回 ``passed=False``。此方法不抛 VerificationError，
        所有异常都收敛成 reasons，保证门禁可以明确失败而不是崩溃。
        """
        reasons: List[str] = []
        recomputed: Dict[str, Any] = {}
        try:
            recomputed = self.evidence_hashes(requirement_spec=requirement_spec)
        except VerificationError as exc:
            reasons.append(f"recompute_failed:{exc}")
        if not isinstance(attestation, dict) or not attestation:
            return {"passed": False, "reasons": reasons + ["attestation_missing"],
                    "recomputed": recomputed}

        if not attestation.get("signature"):
            reasons.append("signature_missing")
        elif not verify_attestation(attestation, secret=self._secret):
            reasons.append("signature_invalid")
        if str(attestation.get("signature_alg") or "") != SIGNATURE_ALG:
            reasons.append("signature_alg_unsupported")

        expected_run_id = str(run_id or self.run_id)
        if expected_run_id and str(attestation.get("run_id") or "") != expected_run_id:
            reasons.append("run_id_mismatch")
        if attestation.get("valid") is not True:
            reasons.append("attestation_not_valid")
            for item in list(attestation.get("reasons") or [])[:5]:
                reasons.append(f"attested_reason:{item}")
        if self._as_int(attestation.get("runner_version")) != RUNNER_VERSION:
            reasons.append("runner_version_mismatch")
        if attestation.get("verification_gap"):
            reasons.append("verification_gap")

        for field in ("requirement_hash", "code_hash", "visible_suite_hash",
                      "hidden_suite_hash", "env_hash"):
            value = str(attestation.get(field) or "")
            expected = str((recomputed or {}).get(field) or "")
            if not value:
                reasons.append(f"{field}_missing")
            elif not hmac.compare_digest(value, expected):
                reasons.append(f"{field}_mismatch")

        if recomputed:
            manifest_digest = str(recomputed.get("manifest_digest") or "")
            hidden = str(attestation.get("hidden_suite_hash") or "")
            if not manifest_digest or not hmac.compare_digest(hidden, manifest_digest):
                reasons.append("frozen_manifest_digest_mismatch")
            if not hmac.compare_digest(str(attestation.get("test_hash") or ""), hidden):
                reasons.append("test_hash_not_equal_hidden_suite")
            if recomputed.get("visible_suite_hash") != recomputed.get("hidden_suite_hash"):
                reasons.append("visible_suite_differs_from_frozen")

        cases = attestation.get("cases")
        if not isinstance(cases, list) or not cases:
            reasons.append("cases_missing")
        else:
            if not hmac.compare_digest(_per_case_digest(cases),
                                       str(attestation.get("per_case_digest") or "")):
                reasons.append("per_case_digest_mismatch")
            total = len(cases)
            failed = sum(1 for case in cases
                         if isinstance(case, dict) and case.get("status") != "passed")
            passed = total - failed
            if self._as_int(attestation.get("total")) != total:
                reasons.append("cases_total_mismatch")
            if self._as_int(attestation.get("failed")) != failed:
                reasons.append("cases_failed_mismatch")
            if self._as_int(attestation.get("passed")) != passed:
                reasons.append("cases_passed_mismatch")
            spec_ids = {
                str(item.get("id"))
                for item in ((requirement_spec or {}).get("function_points") or [])
                if isinstance(item, dict) and item.get("id")
            }
            expected_coverage = self._coverage(
                [case for case in cases if isinstance(case, dict)], len(spec_ids))
            if expected_coverage != attestation.get("coverage"):
                reasons.append("coverage_mismatch")

        unique = list(dict.fromkeys(reasons))
        return {
            "passed": not unique,
            "reasons": unique,
            "recomputed": recomputed,
            "signature_valid": "signature_invalid" not in unique
                               and "signature_missing" not in unique,
            "run_id": expected_run_id,
        }

    # ---------------------------------------------------------------- 内部
    @staticmethod
    def _as_int(value: Any, default: int = -1) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _collect_test_ids(self) -> List[str]:
        """用 AST 枚举冻结副本中的 unittest 用例（不执行测试代码）。

        只收集直接继承 ``*TestCase`` 的类中 ``test*`` 方法：这正是
        ``unittest.TestLoader.loadTestsFromModule`` 会加载的集合（模块级
        ``test_*`` 函数不会被加载）。测试文件若有同名包遮蔽问题，runner 按
        文件路径加载模块，仍能得到这些 id。
        """
        ids: List[str] = []
        for path in sorted(self.frozen_dir.rglob("*.py")):
            if "__pycache__" in path.parts or path.name == "__init__.py":
                continue
            relative = path.relative_to(self.frozen_dir).as_posix()
            module = relative[:-3].replace("/", ".")
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, SyntaxError) as exc:
                raise VerificationError(f"测试文件无法解析: {relative}: {exc}") from exc
            for node in tree.body:
                if not isinstance(node, ast.ClassDef) or not self._is_test_case(node):
                    continue
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                            and child.name.startswith("test"):
                        ids.append(f"{module}.{node.name}.{child.name}")
        return sorted(dict.fromkeys(ids))

    @staticmethod
    def _is_test_case(node: ast.ClassDef) -> bool:
        for base in node.bases:
            name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
            if str(name).endswith("TestCase"):
                return True
        return False

    @staticmethod
    def _bind_case_meta(
        test_ids: Iterable[str],
        case_meta: Optional[Iterable[Dict[str, Any]]],
    ) -> Dict[str, Dict[str, str]]:
        """把生成方用例元数据按方法名绑定到 test_id（class 名由 AST 决定）。"""
        by_method: Dict[str, Dict[str, str]] = {}
        for item in case_meta or []:
            if not isinstance(item, dict):
                continue
            parts = [str(item.get(key) or "") for key in ("category", "op", "name")]
            method = "test_" + "_".join(part for part in parts if part)
            if method == "test_":
                continue
            by_method[method] = {
                "fp_id": str(item.get("fp_id") or ""),
                "category": str(item.get("category") or ""),
                "op": str(item.get("op") or ""),
            }
        bound: Dict[str, Dict[str, str]] = {}
        for test_id in test_ids:
            meta = by_method.get(test_id.rsplit(".", 1)[-1])
            if meta:
                bound[test_id] = dict(meta)
        return bound

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

    def _frozen_hashes(self) -> Dict[str, str]:
        """冻结副本（run_dir/frozen_tests，生成方不可写）的树 hash。"""
        if not self.frozen_dir.exists():
            return {}
        hashes: Dict[str, str] = {}
        for path in sorted(self.frozen_dir.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            relative = path.relative_to(self.frozen_dir).as_posix()
            hashes[relative] = _sha256(path)
        return hashes

    def _code_hashes(self) -> Dict[str, str]:
        """交付代码的树 hash（口径见模块级 ``code_hashes_at``）。"""
        return code_hashes_at(self.code_root)

    def _requirement_hash(self, requirement_spec: Optional[Dict[str, Any]]) -> str:
        """需求绑定：优先用本轮需求规格（canonical JSON），否则退回需求原文快照。"""
        if isinstance(requirement_spec, dict) and requirement_spec:
            return hashlib.sha256(_canonical(requirement_spec)).hexdigest()
        requirement_file = self.run_dir / "input" / "requirement.md"
        if requirement_file.exists():
            return _sha256(requirement_file)
        return ""

    def _env_hash(self) -> str:
        """稳定环境指纹：框架 Python/平台 + 沙箱后端/镜像配置 + runner 版本。

        不含时间戳、进程 id 等易变值；attest 与 gate 在同一进程/配置下必须一致。
        容器模式目前只记录镜像 tag（配置里也没有 digest），不假装它是镜像 digest。
        """
        runtime = getattr(self.config, "runtime", None) if self.config is not None else None
        if not isinstance(runtime, dict):
            runtime = {}
        backend = str(getattr(self.sandbox, "backend", "") or
                      runtime.get("sandbox_backend") or "local")
        container = runtime.get("sandbox_container") or {}
        image = ""
        if backend == "container" and isinstance(container, dict):
            image = str(container.get("image") or "")
        payload = {
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "sandbox_backend": backend,
            "sandbox_python": str(runtime.get("python") or ""),
            "container_image": image,
            "runner_version": RUNNER_VERSION,
        }
        return hashlib.sha256(_canonical(payload)).hexdigest()

    @staticmethod
    def _tree_digest(hashes: Dict[str, str]) -> str:
        return tree_digest(hashes)

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
