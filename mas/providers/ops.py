"""离线 Provider 的“算子库”：把需求功能点映射为真实可执行的实现与用例契约。

说明（重要）：
- 离线 Provider 用于「无 API Key 的端到端回归与演示」，它把需求功能点识别为标准算子，
  生成真实可运行的 Python 代码与真实可执行的测试用例；
- 接入真实 LLM（llm.provider=openai_compatible）后，同样的流水线可处理任意需求，
  本算子库仅作为离线兜底与确定性回归基线；
- `impl` 为正确实现，`bug_impl` 为首轮注入的缺陷实现（用于演示 bug→修复→复测闭环），
  `cases` 为需求语义的“真值契约”，测试 Agent 依据它独立构造用例，不读取研发实现代码。
"""

from __future__ import annotations

from typing import Any, Dict, List

__all__ = ["OPS", "HELPERS", "CASE_ASSERT_KINDS"]

CASE_ASSERT_KINDS = ["eq", "truthy", "falsy", "empty", "len_ge", "contains"]

HELPERS = r'''
from decimal import Decimal, ROUND_HALF_UP, ROUND_FLOOR, ROUND_CEILING
import re
from typing import Any, List, Optional, Sequence

CENT = Decimal("0.01")
LEVEL_FACTORS = {
    "NORMAL": Decimal("1.00"),
    "SILVER": Decimal("0.95"),
    "GOLD": Decimal("0.90"),
    "VIP": Decimal("0.80"),
}
COUPON_PATTERN = re.compile(r"^[A-Z]{2}\d{4}$")
REMOTE_REGIONS = ("新疆", "西藏")
COUPON_AMOUNT = Decimal("5.00")
BASE_SHIPPING = Decimal("10.00")
INCLUDED_WEIGHT = Decimal("1")
EXTRA_STEP_WEIGHT = Decimal("0.5")
EXTRA_STEP_FEE = Decimal("5.00")
REMOTE_SURCHARGE = Decimal("15.00")
AMOUNT_PATTERN = re.compile(r"^[+-]?\d{1,3}(,\d{3})*(\.\d+)?$|^[+-]?\d+(\.\d+)?$")


def _to_decimal(value: Any, field: str = "金额") -> Decimal:
    """把 str/int/float/Decimal 统一为两位小数的 Decimal。"""
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, bool):
        raise ValueError(f"{field}不支持布尔值")
    elif isinstance(value, (int, float)):
        result = Decimal(str(value))
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"{field}不能为空")
        try:
            result = Decimal(text)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"{field}格式非法: {value!r}") from exc
    else:
        raise ValueError(f"{field}类型不支持: {type(value).__name__}")
    return result.quantize(CENT, rounding=ROUND_HALF_UP)
'''

OPS: Dict[str, Dict[str, Any]] = {}

# --------------------------------------------------------------------- FP-01
OPS["parse_amount"] = {
    "key": "parse_amount",
    "title": "金额解析",
    "keywords": ["parse_amount", "金额解析", "解析金额", "金额字符串", "货币符号", "千分位"],
    "summary": "解析金额字符串为两位小数 Decimal，支持千分位与货币符号",
    "impl": r'''
def parse_amount(text: str) -> Decimal:
    """解析金额字符串为两位小数的 Decimal 金额。

    支持前后空白、¥/￥ 前缀与千分位分隔符；非法格式抛 ValueError。
    """
    if not isinstance(text, str):
        raise ValueError("金额必须是字符串")
    raw = text.strip().replace("¥", "").replace("￥", "").replace(" ", "")
    if raw == "":
        raise ValueError("金额不能为空")
    if not AMOUNT_PATTERN.match(raw):
        raise ValueError(f"金额格式非法: {text!r}")
    return Decimal(raw.replace(",", "")).quantize(CENT, rounding=ROUND_HALF_UP)
''',
    "bug_impl": None,
    "cases": [
        {"name": "currency_prefix_and_thousands", "category": "functional",
         "call": 'parse_amount("¥1,234.5")', "expect": 'Decimal("1234.50")'},
        {"name": "zero_value", "category": "functional", "call": 'parse_amount("0")', "expect": 'Decimal("0.00")'},
        {"name": "plain_decimal", "category": "functional",
         "call": 'parse_amount("99.99")', "expect": 'Decimal("99.99")'},
        {"name": "whitespace_tolerated", "category": "functional",
         "call": 'parse_amount("  58.10  ")', "expect": 'Decimal("58.10")'},
        {"name": "round_half_up", "category": "functional",
         "call": 'parse_amount("1.005")', "expect": 'Decimal("1.01")'},
        {"name": "non_string_rejected", "category": "exception", "call": "parse_amount(None)",
         "expect_error": "ValueError"},
        {"name": "numeric_type_rejected", "category": "exception", "call": "parse_amount(123)",
         "expect_error": "ValueError"},
        {"name": "empty_string_rejected", "category": "exception", "call": 'parse_amount("")',
         "expect_error": "ValueError"},
        {"name": "malformed_grouping_rejected", "category": "exception",
         "call": 'parse_amount("1,23,4")', "expect_error": "ValueError"},
        {"name": "letters_rejected", "category": "exception", "call": 'parse_amount("abc")',
         "expect_error": "ValueError"},
        {"name": "huge_amount_supported", "category": "boundary",
         "call": 'parse_amount("9999999999999.99")', "expect": 'Decimal("9999999999999.99")'},
        {"name": "single_cent_precision", "category": "boundary", "call": 'parse_amount("0.005")',
         "expect": 'Decimal("0.01")'},
    ],
    "threshold_builder": None,
}

# --------------------------------------------------------------------- FP-02
OPS["apply_discount"] = {
    "key": "apply_discount",
    "title": "会员折扣",
    "keywords": ["apply_discount", "会员折扣", "折扣", "会员等级"],
    "summary": "按会员等级（NORMAL/SILVER/GOLD/VIP）计算折扣后金额",
    "impl": r'''
def apply_discount(amount: Any, level: str) -> Decimal:
    """按会员等级计算折扣后金额（等级不区分大小写，未知等级抛 ValueError）。"""
    if not isinstance(level, str):
        raise ValueError("会员等级必须是字符串")
    key = level.strip().upper()
    if key not in LEVEL_FACTORS:
        raise ValueError(f"未知会员等级: {level!r}")
    value = _to_decimal(amount)
    if value < 0:
        raise ValueError("折扣金额不能为负")
    return (value * LEVEL_FACTORS[key]).quantize(CENT, rounding=ROUND_HALF_UP)
''',
    "bug_impl": None,
    "cases": [
        {"name": "vip_factor", "category": "functional",
         "call": 'apply_discount("100", "VIP")', "expect": 'Decimal("80.00")'},
        {"name": "silver_rounding", "category": "functional",
         "call": 'apply_discount("99.99", "SILVER")', "expect": 'Decimal("94.99")'},
        {"name": "normal_keeps_amount", "category": "functional",
         "call": 'apply_discount("58.10", "NORMAL")', "expect": 'Decimal("58.10")'},
        {"name": "lowercase_level", "category": "functional",
         "call": 'apply_discount("100", "vip")', "expect": 'Decimal("80.00")'},
        {"name": "unknown_level_rejected", "category": "exception",
         "call": 'apply_discount("100", "PLATINUM")', "expect_error": "ValueError"},
        {"name": "non_string_level_rejected", "category": "exception",
         "call": "apply_discount(100, None)", "expect_error": "ValueError"},
        {"name": "negative_amount_rejected", "category": "exception",
         "call": 'apply_discount("-1", "VIP")', "expect_error": "ValueError"},
        {"name": "zero_amount", "category": "boundary",
         "call": 'apply_discount("0", "VIP")', "expect": 'Decimal("0.00")'},
        {"name": "huge_amount", "category": "boundary",
         "call": 'apply_discount("1000000000000.00", "GOLD")',
         "expect": 'Decimal("900000000000.00")'},
    ],
    "threshold_builder": None,
}

# --------------------------------------------------------------------- FP-03
OPS["calc_shipping"] = {
    "key": "calc_shipping",
    "title": "运费计算",
    "keywords": ["calc_shipping", "运费", "首重", "续重", "新疆", "西藏"],
    "summary": "按重量与地区计算运费：首重 1kg 内 10 元，续重每 500g 加收 5 元，偏远地区加收 15 元",
    "impl": r'''
def calc_shipping(weight_kg: Any, region: str) -> Decimal:
    """按重量与地区计算运费。

    首重 1kg（含）内 10 元；超出部分每 500g（不足 500g 按 500g 计）加收 5 元；
    新疆/西藏地区额外加收 15 元；重量非正或地区为空抛 ValueError。
    """
    if not isinstance(region, str) or not region.strip():
        raise ValueError("收货地区不能为空")
    weight = _to_decimal(weight_kg, field="重量")
    if weight <= 0:
        raise ValueError("重量必须大于 0")
    if weight <= INCLUDED_WEIGHT:
        fee = BASE_SHIPPING
    else:
        extra = weight - INCLUDED_WEIGHT
        steps = (extra / EXTRA_STEP_WEIGHT).to_integral_value(rounding=ROUND_CEILING)
        fee = BASE_SHIPPING + EXTRA_STEP_FEE * steps
    if region.strip() in REMOTE_REGIONS:
        fee += REMOTE_SURCHARGE
    return fee.quantize(CENT, rounding=ROUND_HALF_UP)
''',
    "bug_impl": r'''
def calc_shipping(weight_kg: Any, region: str) -> Decimal:
    """按重量与地区计算运费。"""
    if not isinstance(region, str) or not region.strip():
        raise ValueError("收货地区不能为空")
    weight = _to_decimal(weight_kg, field="重量")
    if weight <= 0:
        raise ValueError("重量必须大于 0")
    if weight <= INCLUDED_WEIGHT:
        fee = BASE_SHIPPING
    else:
        steps = (weight - INCLUDED_WEIGHT) / EXTRA_STEP_WEIGHT
        fee = BASE_SHIPPING + EXTRA_STEP_FEE * Decimal(int(steps))
    return fee.quantize(CENT, rounding=ROUND_HALF_UP)
''',
    "defect_note": "续重步数用 int() 截断而非向上取整，且遗漏新疆/西藏偏远地区加收",
    "cases": [
        {"name": "within_first_weight", "category": "functional",
         "call": 'calc_shipping("0.5", "北京")', "expect": 'Decimal("10.00")'},
        {"name": "extra_step", "category": "functional",
         "call": 'calc_shipping("1.5", "上海")', "expect": 'Decimal("15.00")'},
        {"name": "remote_region_surcharge", "category": "functional",
         "call": 'calc_shipping("1.0", "西藏")', "expect": 'Decimal("25.00")'},
        {"name": "exact_first_weight", "category": "functional",
         "call": 'calc_shipping("1", "北京")', "expect": 'Decimal("10.00")'},
        {"name": "zero_weight_rejected", "category": "exception", "call": 'calc_shipping("0", "北京")',
         "expect_error": "ValueError"},
        {"name": "negative_weight_rejected", "category": "exception",
         "call": 'calc_shipping("-1", "北京")', "expect_error": "ValueError"},
        {"name": "empty_region_rejected", "category": "exception",
         "call": 'calc_shipping("1", "")', "expect_error": "ValueError"},
        {"name": "non_string_region_rejected", "category": "exception",
         "call": "calc_shipping(1, None)", "expect_error": "ValueError"},
        {"name": "remote_region_uppercase_variant", "category": "functional",
         "call": 'calc_shipping("2.0", "新疆")', "expect": 'Decimal("35.00")'},
        {"name": "partial_step_rounds_up", "category": "boundary",
         "call": 'calc_shipping("1.6", "上海")', "expect": 'Decimal("20.00")'},
    ],
    # 阈值用例由测试 Agent 从需求规则的阈值（1kg）自动派生：t / t-ε / t+ε
    "threshold_builder": True,
    "threshold_unit": "kg",
}

# --------------------------------------------------------------------- FP-04
OPS["validate_coupon"] = {
    "key": "validate_coupon",
    "title": "优惠券校验",
    "keywords": ["validate_coupon", "优惠券", "券码", "券编码"],
    "summary": "校验优惠券编码格式（两位大写字母 + 四位数字）",
    "impl": r'''
def validate_coupon(code: str) -> bool:
    """校验优惠券编码格式：两位大写字母 + 四位数字。"""
    if not isinstance(code, str):
        raise ValueError("优惠券编码必须是字符串")
    if code.strip() == "":
        raise ValueError("优惠券编码不能为空")
    return bool(COUPON_PATTERN.match(code.strip()))
''',
    "bug_impl": None,
    "cases": [
        {"name": "valid_code", "category": "functional", "call": 'validate_coupon("AB1234")',
         "assert_kind": "truthy"},
        {"name": "lowercase_rejected", "category": "functional", "call": 'validate_coupon("ab1234")',
         "assert_kind": "falsy"},
        {"name": "too_short_rejected", "category": "functional", "call": 'validate_coupon("AB123")',
         "assert_kind": "falsy"},
        {"name": "too_many_letters_rejected", "category": "functional",
         "call": 'validate_coupon("ABC123")', "assert_kind": "falsy"},
        {"name": "letters_after_digits_rejected", "category": "functional",
         "call": 'validate_coupon("1234AB")', "assert_kind": "falsy"},
        {"name": "non_string_rejected", "category": "exception", "call": "validate_coupon(None)",
         "expect_error": "ValueError"},
        {"name": "numeric_rejected", "category": "exception", "call": "validate_coupon(123456)",
         "expect_error": "ValueError"},
        {"name": "empty_string_rejected", "category": "exception", "call": 'validate_coupon("")',
         "expect_error": "ValueError"},
        {"name": "blank_string_rejected", "category": "exception", "call": 'validate_coupon("   ")',
         "expect_error": "ValueError"},
        {"name": "digit_boundary_all_zeros", "category": "boundary",
         "call": 'validate_coupon("AB0000")', "assert_kind": "truthy"},
        {"name": "digit_boundary_all_nines", "category": "boundary",
         "call": 'validate_coupon("ZZ9999")', "assert_kind": "truthy"},
    ],
    "threshold_builder": None,
}

# --------------------------------------------------------------------- FP-05
OPS["split_payment"] = {
    "key": "split_payment",
    "title": "分摊金额",
    "keywords": ["split_payment", "分摊", "均分", "余数"],
    "summary": "把总额按份数均分，余数分给前几份，保证合计与总额一致",
    "impl": r'''
def split_payment(total: Any, parts: int) -> List[Decimal]:
    """把总额按份数均分并保证合计与总额完全一致（余数分给前几份）。"""
    amount = _to_decimal(total, field="总额")
    if amount < 0:
        raise ValueError("总额不能为负")
    if isinstance(parts, bool) or not isinstance(parts, int):
        raise ValueError("分摊份数必须是整数")
    if parts <= 0:
        raise ValueError("分摊份数必须大于 0")
    base = (amount / parts).quantize(CENT, rounding=ROUND_FLOOR)
    shares = [base] * parts
    remainder = (amount - base * parts).quantize(CENT)
    steps = int((remainder / CENT).to_integral_value())
    for index in range(steps):
        shares[index] += CENT
    return [share.quantize(CENT) for share in shares]
''',
    "bug_impl": None,
    "cases": [
        {"name": "three_parts_remainder_first", "category": "functional",
         "call": 'split_payment("100", 3)', "expect": '[Decimal("33.34"), Decimal("33.33"), Decimal("33.33")]'},
        {"name": "two_parts_single_cent", "category": "functional",
         "call": 'split_payment("0.01", 2)', "expect": '[Decimal("0.01"), Decimal("0.00")]'},
        {"name": "exact_division", "category": "functional",
         "call": 'split_payment("10", 4)',
         "expect": '[Decimal("2.50"), Decimal("2.50"), Decimal("2.50"), Decimal("2.50")]'},
        {"name": "single_part", "category": "functional",
         "call": 'split_payment("88.88", 1)', "expect": '[Decimal("88.88")]'},
        {"name": "zero_parts_rejected", "category": "exception", "call": 'split_payment("10", 0)',
         "expect_error": "ValueError"},
        {"name": "negative_parts_rejected", "category": "exception", "call": 'split_payment("10", -2)',
         "expect_error": "ValueError"},
        {"name": "non_integer_parts_rejected", "category": "exception",
         "call": 'split_payment("10", "3")', "expect_error": "ValueError"},
        {"name": "negative_total_rejected", "category": "exception",
         "call": 'split_payment("-0.01", 2)', "expect_error": "ValueError"},
        {"name": "zero_total", "category": "boundary", "call": 'split_payment("0", 3)',
         "expect": '[Decimal("0.00"), Decimal("0.00"), Decimal("0.00")]'},
        {"name": "many_parts_remainder", "category": "boundary",
         "call": 'split_payment("0.05", 3)',
         "expect": '[Decimal("0.02"), Decimal("0.02"), Decimal("0.01")]'},
        {"name": "penny_total_many_parts", "category": "boundary",
         "call": 'split_payment("0.01", 5)',
         "expect": '[Decimal("0.01"), Decimal("0.00"), Decimal("0.00"), Decimal("0.00"), Decimal("0.00")]'},
    ],
    "threshold_builder": None,
}

# --------------------------------------------------------------------- FP-06
OPS["order_total"] = {
    "key": "order_total",
    "title": "订单总价",
    "keywords": ["order_total", "订单总价", "应付总额", "订单金额"],
    "summary": "综合明细、会员折扣、优惠券与运费的订单应付总额",
    "impl": r'''
def order_total(items: Sequence[dict], level: str, region: str, weight_kg: Any,
                coupon: Optional[str] = None) -> Decimal:
    """计算订单应付总额：明细金额 → 会员折扣 → 优惠券固定抵扣 → 叠加运费。"""
    if not isinstance(items, (list, tuple)) or len(items) == 0:
        raise ValueError("商品明细不能为空")
    subtotal = Decimal("0.00")
    for index, item in enumerate(items):
        if not isinstance(item, dict) or "price" not in item or "quantity" not in item:
            raise ValueError(f"商品明细第 {index + 1} 项缺少 price/quantity")
        quantity = item["quantity"]
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise ValueError(f"商品明细第 {index + 1} 项数量必须为正整数")
        price = _to_decimal(item["price"], field="商品单价")
        if price < 0:
            raise ValueError(f"商品明细第 {index + 1} 项单价不能为负")
        subtotal += price * quantity
    if subtotal <= 0:
        raise ValueError("订单金额必须大于 0")
    discounted = apply_discount(subtotal, level)
    if coupon is not None:
        if not validate_coupon(coupon):
            raise ValueError(f"优惠券编码非法: {coupon!r}")
        discounted -= COUPON_AMOUNT
    if discounted < 0:
        discounted = Decimal("0.00")
    shipping = calc_shipping(weight_kg, region)
    return (discounted + shipping).quantize(CENT, rounding=ROUND_HALF_UP)
''',
    "bug_impl": None,
    "cases": [
        {"name": "vip_with_coupon_and_shipping", "category": "functional",
         "call": ('order_total([{"price": "100", "quantity": 2}], "VIP", "上海", "1.5", "AB1234")'),
         "expect": 'Decimal("170.00")'},
        {"name": "normal_without_coupon", "category": "functional",
         "call": ('order_total([{"price": "50.00", "quantity": 1}], "NORMAL", "北京", "0.5")'),
         "expect": 'Decimal("60.00")'},
        {"name": "multiple_items", "category": "functional",
         "call": ('order_total([{"price": "10.00", "quantity": 2}, {"price": "5.50", "quantity": 2}], '
                  '"GOLD", "上海", "1.0")'),
         "expect": 'Decimal("37.90")'},
        {"name": "coupon_exceeds_amount_clamped", "category": "functional",
         "call": ('order_total([{"price": "3.00", "quantity": 1}], "NORMAL", "北京", "1.0", "AB1234")'),
         "expect": 'Decimal("10.00")'},
        {"name": "empty_items_rejected", "category": "exception",
         "call": 'order_total([], "NORMAL", "北京", "1.0")', "expect_error": "ValueError"},
        {"name": "invalid_coupon_rejected", "category": "exception",
         "call": ('order_total([{"price": "10", "quantity": 1}], "NORMAL", "北京", "1.0", "bad")'),
         "expect_error": "ValueError"},
        {"name": "bad_quantity_rejected", "category": "exception",
         "call": ('order_total([{"price": "10", "quantity": 0}], "NORMAL", "北京", "1.0")'),
         "expect_error": "ValueError"},
        {"name": "missing_region_rejected", "category": "exception",
         "call": ('order_total([{"price": "10", "quantity": 1}], "NORMAL", "", "1.0")'),
         "expect_error": "ValueError"},
        {"name": "boundary_weight_threshold", "category": "boundary",
         "call": ('order_total([{"price": "100", "quantity": 1}], "NORMAL", "上海", "1.0")'),
         "expect": 'Decimal("110.00")'},
        {"name": "boundary_remote_region", "category": "boundary",
         "call": ('order_total([{"price": "100", "quantity": 1}], "NORMAL", "新疆", "1.0")'),
         "expect": 'Decimal("125.00")'},
    ],
    "threshold_builder": None,
}

# --------------------------------------------------------------------- FP-07
OPS["validate_order"] = {
    "key": "validate_order",
    "title": "订单校验",
    "keywords": ["validate_order", "订单校验", "订单结构", "字段校验"],
    "summary": "校验订单结构，返回错误描述列表（合法时为空）",
    "impl": r'''
ORDER_REQUIRED_FIELDS = ("order_id", "items", "customer", "level", "region", "weight_kg")


def validate_order(order: dict) -> List[str]:
    """校验订单结构，返回错误描述列表；订单合法时返回空列表。"""
    if not isinstance(order, dict):
        raise ValueError("订单必须是字典")
    errors: List[str] = []
    for field in ORDER_REQUIRED_FIELDS:
        if field not in order or order[field] in (None, ""):
            errors.append(f"缺少字段: {field}")
    if "items" in order and order["items"] not in (None, ""):
        items = order["items"]
        if not isinstance(items, (list, tuple)) or len(items) == 0:
            errors.append("商品明细不能为空")
        else:
            for index, item in enumerate(items):
                if not isinstance(item, dict):
                    errors.append(f"商品明细第 {index + 1} 项必须是字典")
                    continue
                for key in ("price", "quantity"):
                    if key not in item:
                        errors.append(f"商品明细第 {index + 1} 项缺少 {key}")
                quantity = item.get("quantity")
                if isinstance(quantity, int) and not isinstance(quantity, bool) and quantity <= 0:
                    errors.append(f"商品明细第 {index + 1} 项数量必须大于 0")
                try:
                    if "price" in item and _to_decimal(item["price"], field="商品单价") < 0:
                        errors.append(f"商品明细第 {index + 1} 项单价不能为负")
                except ValueError as exc:
                    errors.append(f"商品明细第 {index + 1} 项单价非法: {exc}")
    level = order.get("level")
    if isinstance(level, str) and level.strip().upper() not in LEVEL_FACTORS:
        errors.append(f"会员等级非法: {level}")
    region = order.get("region")
    if "region" in order and (not isinstance(region, str) or not region.strip()):
        errors.append("收货地区不能为空")
    if "weight_kg" in order and order["weight_kg"] is not None:
        try:
            if _to_decimal(order["weight_kg"], field="重量") <= 0:
                errors.append("重量必须大于 0")
        except ValueError as exc:
            errors.append(str(exc))
    return errors
''',
    "bug_impl": None,
    "cases": [
        {"name": "valid_order_passes", "category": "functional",
         "call": ('validate_order({"order_id": "O1", "items": [{"price": "10.00", "quantity": 2}], '
                  '"customer": "C1", "level": "VIP", "region": "上海", "weight_kg": "1.0"})'),
         "assert_kind": "empty"},
        {"name": "empty_order_has_errors", "category": "functional", "call": "validate_order({})",
         "assert_kind": "len_ge", "expect": "1"},
        {"name": "bad_quantity_reported", "category": "functional",
         "call": ('validate_order({"order_id": "O2", "items": [{"price": "10.00", "quantity": 0}], '
                  '"customer": "C1", "level": "NORMAL", "region": "北京", "weight_kg": "1.0"})'),
         "assert_kind": "contains", "expect": "数量必须大于 0"},
        {"name": "unknown_level_reported", "category": "functional",
         "call": ('validate_order({"order_id": "O3", "items": [{"price": "10.00", "quantity": 1}], '
                  '"customer": "C1", "level": "PLATINUM", "region": "北京", "weight_kg": "1.0"})'),
         "assert_kind": "contains", "expect": "会员等级非法"},
        {"name": "non_dict_rejected", "category": "exception", "call": "validate_order(None)",
         "expect_error": "ValueError"},
        {"name": "list_input_rejected", "category": "exception", "call": "validate_order([])",
         "expect_error": "ValueError"},
        {"name": "empty_items_reported", "category": "boundary",
         "call": ('validate_order({"order_id": "O4", "items": [], "customer": "C1", '
                  '"level": "NORMAL", "region": "北京", "weight_kg": "1.0"})'),
         "assert_kind": "contains", "expect": "商品明细不能为空"},
        {"name": "zero_weight_reported", "category": "boundary",
         "call": ('validate_order({"order_id": "O5", "items": [{"price": "10.00", "quantity": 1}], '
                  '"customer": "C1", "level": "NORMAL", "region": "北京", "weight_kg": "0"})'),
         "assert_kind": "contains", "expect": "重量必须大于 0"},
    ],
    "threshold_builder": None,
}


def match_op(fp: Dict[str, Any]) -> str:
    """把单个需求功能点匹配到算子 key：标识符优先，其次关键词。"""
    text = " ".join([
        str(fp.get("id", "")),
        str(fp.get("title", "")),
        str(fp.get("description", "")),
        " ".join(str(item) for item in fp.get("inputs", [])),
        " ".join(str(item) for item in fp.get("rules", [])),
        " ".join(str(item) for item in fp.get("samples", [])),
    ])
    for op_key in OPS:
        if op_key in text:
            return op_key
    for op_key, op in OPS.items():
        if any(keyword in text for keyword in op["keywords"]):
            return op_key
    return ""


def ops_for_spec(spec: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """按需求功能点顺序返回匹配到的算子（保序，便于生成实现模块与用例）。"""
    ordered: Dict[str, Dict[str, Any]] = {}
    for fp in spec.get("function_points", []):
        op_key = match_op(fp)
        if op_key and op_key not in ordered:
            ordered[op_key] = OPS[op_key]
    return ordered

