"""需求契约与隐藏用例回归测试（docs/39 门禁优化）。

覆盖：
- 条款抽取：确定性编号、跳过背景/待澄清章节与代码围栏；
- 契约校验：source_spans 映射、quote 必须命中原文、acceptance_criteria 必填、
  out_of_scope 必须有理由、漏条/一条两用直接报错；
- 冻结与签名：冻结后可验签、幂等复用、改规格/改需求/改 run_id 均被门禁重验拒绝；
- 隐藏套件：只基于冻结契约构造提示、结构校验、落盘在 run 目录之外并签名冻结，
  已冻结套件不能换绑另一份契约。
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mas.config import load_config
from mas.contract import (
    ContractError,
    extract_clauses,
    freeze_contract,
    hidden_suite_dir,
    load_frozen_contract,
    validate_contract,
    verify_frozen_contract,
)
from mas.hidden import (
    HiddenSuiteError,
    build_generation_prompt,
    create_hidden_suite,
    validate_generated_suite,
)
from mas.verifier import TestVerifier, canonical_hash, verify_signed

PROJECT_ROOT = Path(__file__).resolve().parent.parent

REQUIREMENT_TEXT = """# 加法服务

## 背景
这是一个用于演示的加法服务。

## 功能要求
- add(a, b) 必须返回两数之和
- 输入不是数字时必须抛出 TypeError

## 待澄清
- 是否需要支持浮点数？

```
add(1, 2) == 3
```
"""

HIDDEN_SUITE = '''"""隐藏用例（冻结契约派生）。"""
import unittest

from pkg.core import add


class TestHiddenAdd(unittest.TestCase):
    def test_functional_add_result(self):
        self.assertEqual(add(1, 2), 3)
'''

HIDDEN_CASES = [{
    "id": "TH-FP-01-functional-add-result",
    "fp_id": "FP-01",
    "op": "add",
    "name": "result",
    "category": "functional",
    "call": "add(1, 2)",
    "expect": "3",
}]


def _spec() -> dict:
    """一份满足契约要求的规格：C-01 映射到 FP-01，C-02 显式排除。"""
    return {
        "title": "加法服务",
        "package": "pkg",
        "delivery_shape": "backend_only",
        "function_points": [{
            "id": "FP-01",
            "title": "两数相加",
            "samples": [{"input": "add(1, 2)", "expect": "3"}],
            "acceptance_criteria": ["add(1, 2) 返回 3"],
            "source_spans": [{"clause_id": "C-01", "quote": "add(a, b) 必须返回两数之和"}],
        }],
        "out_of_scope": [{"clause_id": "C-02", "reason": "异常语义由交付代码的既有行为约定，本期不验收"}],
    }


class ClauseExtractionTests(unittest.TestCase):
    def test_clauses_are_deterministic_and_skip_non_normative(self):
        clauses = extract_clauses(REQUIREMENT_TEXT)
        self.assertEqual([item["id"] for item in clauses], ["C-01", "C-02"])
        self.assertEqual(clauses[0]["line"], 7)
        self.assertEqual(clauses[0]["section"], "功能要求")
        self.assertIn("两数之和", clauses[0]["text"])
        # 背景/待澄清/代码围栏都不应成为条款
        joined = " ".join(item["text"] for item in clauses)
        self.assertNotIn("演示", joined)
        self.assertNotIn("浮点数", joined)
        self.assertNotIn("add(1, 2) ==", joined)
        self.assertEqual(extract_clauses(REQUIREMENT_TEXT), clauses)


class ContractValidationTests(unittest.TestCase):
    def test_valid_contract_maps_clauses_and_fills_quotes(self):
        spec = _spec()
        outcome = validate_contract(spec, REQUIREMENT_TEXT)
        self.assertTrue(outcome["valid"], outcome["errors"])
        self.assertEqual(outcome["coverage"], {
            "total": 2, "mapped": 1, "out_of_scope": 1, "uncovered": [],
        })
        span = spec["function_points"][0]["source_spans"][0]
        self.assertEqual(span["quote"], "add(a, b) 必须返回两数之和")
        self.assertEqual(span["line"], 7)
        self.assertEqual(spec["package"], "pkg")

    def test_unmapped_clause_is_rejected(self):
        spec = _spec()
        spec["out_of_scope"] = []
        outcome = validate_contract(spec, REQUIREMENT_TEXT)
        self.assertFalse(outcome["valid"])
        self.assertIn("clause_unmapped:C-02", outcome["errors"])

    def test_out_of_scope_requires_reason(self):
        spec = _spec()
        spec["out_of_scope"] = [{"clause_id": "C-02", "reason": "  "}]
        outcome = validate_contract(spec, REQUIREMENT_TEXT)
        self.assertIn("out_of_scope_missing_reason:C-02", outcome["errors"])

    def test_quote_must_match_clause_text(self):
        spec = _spec()
        spec["function_points"][0]["source_spans"] = [
            {"clause_id": "C-01", "quote": "add(a, b) 必须返回两数之差"}]
        spec["out_of_scope"] = [{"clause_id": "C-02", "reason": "本期不验收"}]
        outcome = validate_contract(spec, REQUIREMENT_TEXT)
        self.assertIn("source_span_quote_mismatch:FP-01:C-01", outcome["errors"])

    def test_quote_formatting_variants_are_accepted(self):
        """引文只差排版（反引号/全半角标点/空格/箭头）不应判编造。

        背景（2026-10-24 task09-miniledger-01 真实运行）：条款原文是
        「`save(path) -> None`；`load(path) -> Book`」，模型引用时去掉反引号或
        换半角分号即触发 source_span_quote_mismatch，整轮 intake 直接 halt。
        """
        text = ("# 接口\n\n## 公共接口\n"
                "- `save(path) -> None`；`load(path) -> Book`\n")
        variants = [
            "`save(path) -> None`；`load(path) -> Book`",   # 逐字
            "save(path) -> None; load(path) -> Book",       # 去反引号 + 半角标点
            "save(path) → None；load(path) → Book",          # 箭头变体
            "  save(path)  ->  None ； load(path)  ->  Book ",  # 多余空白
        ]
        for quote in variants:
            with self.subTest(quote=quote):
                spec = {
                    "package": "pkg",
                    "function_points": [{
                        "id": "FP-01",
                        "samples": [{"input": "save", "expect": "None"}],
                        "acceptance_criteria": ["save/load 可用"],
                        "source_spans": [{"clause_id": "C-01", "quote": quote}],
                    }],
                    "out_of_scope": [],
                }
                outcome = validate_contract(spec, text)
                self.assertTrue(outcome["valid"], outcome["errors"])

    def test_paraphrased_quote_is_still_rejected(self):
        """规范化只放过排版差异：改写用词的引文仍判 mismatch（门禁不被放松）。"""
        text = ("# 接口\n\n## 公共接口\n"
                "- `save(path) -> None`；`load(path) -> Book`\n")
        spec = {
            "package": "pkg",
            "function_points": [{
                "id": "FP-01",
                "samples": [{"input": "save", "expect": "None"}],
                "acceptance_criteria": ["save/load 可用"],
                "source_spans": [{"clause_id": "C-01",
                                  "quote": "save 保存账本，load 读取账本"}],
            }],
            "out_of_scope": [],
        }
        outcome = validate_contract(spec, text)
        self.assertIn("source_span_quote_mismatch:FP-01:C-01", outcome["errors"])

    def test_unknown_clause_and_clause_used_twice_are_rejected(self):
        spec = _spec()
        spec["function_points"][0]["source_spans"] = [{"clause_id": "C-99"}]
        spec["out_of_scope"] = [
            {"clause_id": "C-99", "reason": "x"},
            {"clause_id": "C-02", "reason": "本期不验收"},
        ]
        outcome = validate_contract(spec, REQUIREMENT_TEXT)
        self.assertIn("source_span_unknown_clause:FP-01:C-99", outcome["errors"])
        self.assertIn("out_of_scope_unknown_clause:C-99", outcome["errors"])

        spec = _spec()
        spec["function_points"][0]["source_spans"] = [{"clause_id": "C-01"}]
        spec["out_of_scope"] = [
            {"clause_id": "C-01", "reason": "重复"},
            {"clause_id": "C-02", "reason": "本期不验收"},
        ]
        outcome = validate_contract(spec, REQUIREMENT_TEXT)
        self.assertIn("clause_both_mapped_and_out_of_scope:C-01", outcome["errors"])

    def test_acceptance_criteria_required(self):
        spec = _spec()
        spec["function_points"][0]["acceptance_criteria"] = []
        outcome = validate_contract(spec, REQUIREMENT_TEXT)
        self.assertIn("function_point_missing_acceptance_criteria:FP-01", outcome["errors"])


class ContractFreezeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.run_dir = self.root / "run"
        self.config = load_config(PROJECT_ROOT / "config", workspace=self.root / "ws")
        self.spec = _spec()
        outcome = validate_contract(self.spec, REQUIREMENT_TEXT)
        self.assertTrue(outcome["valid"], outcome["errors"])

    def tearDown(self):
        self.tmp.cleanup()

    def _freeze(self, run_id="run-contract"):
        return freeze_contract(
            self.run_dir, copy.deepcopy(self.spec), REQUIREMENT_TEXT,
            run_id=run_id, config=self.config,
        )

    def test_freeze_signs_and_verifies(self):
        frozen = self._freeze()
        path = self.run_dir / "contract" / "requirement_contract.json"
        self.assertTrue(path.exists())
        self.assertFalse(frozen["reused"])
        self.assertEqual(frozen["signature_alg"], "hmac-sha256")
        self.assertEqual(frozen["contract_hash"], canonical_hash(frozen["spec"]))
        loaded = load_frozen_contract(path, config=self.config)
        self.assertTrue(verify_signed(loaded, config=self.config))
        reasons = verify_frozen_contract(
            loaded, self.spec, REQUIREMENT_TEXT, run_id="run-contract", config=self.config)
        self.assertEqual(reasons, [])

    def test_freeze_is_idempotent_but_rejects_changed_spec(self):
        first = self._freeze()
        again = self._freeze()
        self.assertTrue(again["reused"])
        self.assertEqual(first["contract_hash"], again["contract_hash"])

        changed = copy.deepcopy(self.spec)
        changed["title"] = "另一个需求"
        with self.assertRaises(ContractError):
            freeze_contract(
                self.run_dir, changed, REQUIREMENT_TEXT,
                run_id="run-contract", config=self.config,
            )

    def test_tampered_contract_is_rejected_on_load(self):
        self._freeze()
        path = self.run_dir / "contract" / "requirement_contract.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["spec"]["title"] = "被篡改的需求"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ContractError):
            load_frozen_contract(path, config=self.config)

    def test_verify_detects_spec_requirement_and_run_id_changes(self):
        frozen = self._freeze()

        changed_spec = copy.deepcopy(self.spec)
        changed_spec["title"] = "改过的规格"
        reasons = verify_frozen_contract(
            frozen, changed_spec, REQUIREMENT_TEXT,
            run_id="run-contract", config=self.config)
        self.assertIn("requirement_spec_modified_after_freeze", reasons)

        reasons = verify_frozen_contract(
            frozen, self.spec, REQUIREMENT_TEXT + "\n- 新增一条需求\n",
            run_id="run-contract", config=self.config)
        self.assertIn("requirement_text_modified_after_freeze", reasons)

        reasons = verify_frozen_contract(
            frozen, self.spec, REQUIREMENT_TEXT, run_id="run-other", config=self.config)
        self.assertIn("requirement_contract_run_id_mismatch", reasons)


class HiddenSuiteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.run_dir = self.root / "run"
        self.sandbox_root = self.run_dir / "code"
        (self.sandbox_root / "pkg").mkdir(parents=True)
        (self.sandbox_root / "pkg" / "core.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8")
        self.config = load_config(PROJECT_ROOT / "config", workspace=self.root / "ws")
        self.spec = _spec()
        outcome = validate_contract(self.spec, REQUIREMENT_TEXT)
        self.assertTrue(outcome["valid"], outcome["errors"])
        self.clauses = outcome["clauses"]
        self.contract = freeze_contract(
            self.run_dir, copy.deepcopy(self.spec), REQUIREMENT_TEXT,
            run_id="run-hidden", config=self.config,
        )
        self.sandbox = SimpleNamespace(root=self.sandbox_root, events=None)

    def tearDown(self):
        self.tmp.cleanup()

    def _data(self, *, cases=None, content=HIDDEN_SUITE):
        return {
            "files": [{"path": "tests/test_hidden_add.py", "content": content}],
            "cases": list(HIDDEN_CASES if cases is None else cases),
            "derived_from": "frozen-requirement-contract",
        }

    def test_prompt_only_contains_frozen_contract(self):
        noisy = {**copy.deepcopy(self.spec), "solution_design": "SENTINEL-DESIGN",
                 "code_files": ["SENTINEL-CODE"]}
        system, user = build_generation_prompt(
            spec=noisy, clauses=self.clauses, package="pkg",
            delivery_shape="backend_only",
        )
        self.assertIn("冻结", system)
        self.assertIn("function_points", user)
        self.assertIn("C-01", user)
        self.assertNotIn("SENTINEL-DESIGN", user)
        self.assertNotIn("SENTINEL-CODE", user)

    def test_validation_rejects_unknown_fp_and_missing_functional(self):
        unknown = self._data(cases=[{**HIDDEN_CASES[0], "fp_id": "FP-99"}])
        outcome = validate_generated_suite(unknown, self.spec)
        self.assertIn("hidden_case_unknown_fp:FP-99", outcome["errors"])

        boundary = self._data(cases=[{**HIDDEN_CASES[0], "category": "boundary",
                                      "name": "zero"}])
        boundary["files"][0]["content"] = HIDDEN_SUITE.replace(
            "test_functional_add_result", "test_boundary_add_zero")
        outcome = validate_generated_suite(boundary, self.spec)
        self.assertIn("hidden_functional_case_missing:FP-01", outcome["errors"])

    def test_boundary_fp_accepts_boundary_case(self):
        """边界类功能点：exception/boundary 用例照常接受，但 functional 用例不可缺。

        2026-10-25 口径对齐（20261024-233917-897d 实测）：隐藏门禁要求
        hidden_functional_coverage rate >= 1.0（覆盖全部 FP，不限类别）。
        旧协议只要求「功能类 FP 写 functional」，异常/边界 FP 只写 boundary 用例
        会在验收阶段才暴露 hidden_functional_coverage_incomplete——整条流水线
        跑完后才发现，代价最大。校验器必须与门禁同口径，把缺口拦在 intake。
        """
        spec = {"function_points": [
            {"id": "FP-01", "title": "两数相加", "category": "功能"},
            {"id": "FP-02", "title": "上限截断", "category": "边界"},
        ]}
        # FP-02 只有 boundary 用例：旧口径放行，新口径（对齐门禁）必须拦截
        only_boundary = self._data(cases=[
            dict(HIDDEN_CASES[0]),
            {**HIDDEN_CASES[0], "fp_id": "FP-02", "category": "boundary",
             "op": "add", "name": "cap", "id": "TH-FP-02-boundary-add-cap"},
        ], content=HIDDEN_SUITE)
        outcome = validate_generated_suite(only_boundary, spec)
        self.assertIn("hidden_functional_case_missing:FP-02", outcome["errors"])

        # FP-02 同时有 functional + boundary 用例：通过（boundary 用例仍然被接受）
        content = HIDDEN_SUITE + '''
    def test_functional_add_cap(self):
        self.assertEqual(add(99, 1), 100)

    def test_boundary_add_cap(self):
        self.assertEqual(add(99, 1), 100)
'''
        both = self._data(cases=[
            dict(HIDDEN_CASES[0]),
            {**HIDDEN_CASES[0], "fp_id": "FP-02", "category": "functional",
             "op": "add", "name": "cap", "id": "TH-FP-02-functional-add-cap"},
            {**HIDDEN_CASES[0], "fp_id": "FP-02", "category": "boundary",
             "op": "add", "name": "cap", "id": "TH-FP-02-boundary-add-cap"},
        ], content=content)
        outcome = validate_generated_suite(both, spec)
        self.assertEqual(outcome["errors"], [])

    def test_functional_fp_still_requires_functional_case(self):
        """类别为「功能」的 FP 只有 boundary 用例 → 仍旧报错（门禁没有被放松）。"""
        spec = {"function_points": [{"id": "FP-01", "title": "两数相加", "category": "功能"}]}
        data = self._data(cases=[{**HIDDEN_CASES[0], "category": "boundary", "name": "zero"}])
        outcome = validate_generated_suite(data, spec)
        self.assertIn("hidden_functional_case_missing:FP-01", outcome["errors"])

    def test_fp_without_any_case_is_rejected(self):
        """任何 FP 一条隐藏用例都没有 → hidden_case_missing（覆盖要求仍在）。"""
        spec = {"function_points": [
            {"id": "FP-01", "title": "两数相加", "category": "功能"},
            {"id": "FP-02", "title": "上限截断", "category": "边界"},
        ]}
        outcome = validate_generated_suite(self._data(), spec)
        self.assertIn("hidden_case_missing:FP-02", outcome["errors"])
        self.assertNotIn("hidden_case_missing:FP-01", outcome["errors"])

    def test_validation_requires_assertions(self):
        empty = self._data(content=(
            "import unittest\n\n"
            "class TestHiddenAdd(unittest.TestCase):\n"
            "    def test_functional_add_result(self):\n"
            "        pass\n"
        ))
        outcome = validate_generated_suite(empty, self.spec)
        self.assertIn("hidden_suite_has_no_assertions", outcome["errors"])

    def test_hidden_suite_is_frozen_outside_run_dir(self):
        artifact = create_hidden_suite(
            config=self.config, run_dir=self.run_dir, sandbox=self.sandbox,
            run_id="run-hidden", spec=self.spec,
            contract_hash=self.contract["contract_hash"], data=self._data(),
            iteration=1, package="pkg", generation={"provider": "offline"},
        )
        root = hidden_suite_dir(self.config.workspace_dir, "run-hidden")
        self.assertEqual(Path(artifact["root"]), root)
        self.assertNotIn(self.run_dir.resolve(), root.resolve().parents)
        self.assertTrue((root / "tests" / "test_hidden_add.py").exists())
        self.assertTrue((root / "frozen_tests" / "tests" / "test_hidden_add.py").exists())
        self.assertEqual(artifact["case_count"], 1)
        self.assertEqual(artifact["contract_hash"], self.contract["contract_hash"])

        meta = json.loads((root / "hidden_meta.json").read_text(encoding="utf-8"))
        self.assertTrue(verify_signed(meta, config=self.config))
        verifier = TestVerifier(
            root, root, config=self.config, sandbox=self.sandbox,
            run_id="run-hidden", code_root=self.sandbox_root,
            attestation_path=self.run_dir / "hidden_attestation.json",
        )
        manifest = verifier.load_manifest()
        self.assertIsNotNone(manifest)
        self.assertEqual(manifest["digest"], artifact["manifest_digest"])

    def test_existing_hidden_suite_cannot_be_rebound_to_another_contract(self):
        create_hidden_suite(
            config=self.config, run_dir=self.run_dir, sandbox=self.sandbox,
            run_id="run-hidden", spec=self.spec,
            contract_hash=self.contract["contract_hash"], data=self._data(),
            iteration=1, package="pkg",
        )
        with self.assertRaises(HiddenSuiteError):
            create_hidden_suite(
                config=self.config, run_dir=self.run_dir, sandbox=self.sandbox,
                run_id="run-hidden", spec=self.spec,
                contract_hash="f" * 64, data=self._data(),
                iteration=2, package="pkg",
            )


if __name__ == "__main__":
    unittest.main()
