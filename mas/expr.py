"""门禁表达式求值器。

为“配置驱动”提供安全的表达式判定能力：
- 只允许白名单 AST 节点与内置函数，禁止属性访问/导入/赋值等副作用
- 变量按 `artifact.字段.子字段` 的点路径在黑板快照中取值
- 字段缺失时返回可读的失败原因，而不是抛异常

示例::

    evaluate("test_report.summary.failed == 0", snapshot)  ->  (True, True, "")
"""

from __future__ import annotations

import ast
import operator
from typing import Any, Dict, List, Optional, Tuple

from .errors import GateError

__all__ = ["evaluate", "ExprResult"]

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARY_OPS = {
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
    ast.Not: operator.not_,
}

_CMP_OPS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
}

_FUNCS = {
    "len": len,
    "min": min,
    "max": max,
    "abs": abs,
    "round": round,
    "sum": sum,
    "any": any,
    "all": all,
    "int": int,
    "float": float,
    "str": str,
    "bool": bool,
    "list": list,
    "sorted": sorted,
}


class _MissingField(Exception):
    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.path = path


class ExprResult:
    """表达式求值结果。"""

    def __init__(self, ok: bool, value: Any = None, error: str = "") -> None:
        self.ok = ok
        self.value = value
        self.error = error

    def __bool__(self) -> bool:  # 便于直接作为条件使用
        return bool(self.ok and self.value)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"ExprResult(ok={self.ok!r}, value={self.value!r}, error={self.error!r})"


def _lookup(name: str, context: Dict[str, Any]) -> Any:
    if name in context:
        return context[name]
    parts = name.split(".")
    if parts[0] not in context:
        raise _MissingField(parts[0])
    current = context[parts[0]]
    for index, part in enumerate(parts[1:], start=1):
        if isinstance(current, dict):
            if part not in current:
                raise _MissingField(".".join(parts[: index + 1]))
            current = current[part]
        elif isinstance(current, (list, tuple)) and part.lstrip("-").isdigit():
            position = int(part)
            if -len(current) <= position < len(current):
                current = current[position]
            else:
                raise _MissingField(".".join(parts[: index + 1]))
        else:
            raise _MissingField(".".join(parts[: index + 1]))
    return current


class _Evaluator(ast.NodeVisitor):
    def __init__(self, context: Dict[str, Any]) -> None:
        self.context = context

    def visit_Expression(self, node: ast.Expression) -> Any:  # noqa: N802
        return self.visit(node.body)

    def visit_Constant(self, node: ast.Constant) -> Any:  # noqa: N802
        return node.value

    def visit_Name(self, node: ast.Name) -> Any:  # noqa: N802
        return _lookup(node.id, self.context)

    def visit_Attribute(self, node: ast.Attribute) -> Any:  # noqa: N802
        parts: List[str] = []
        cursor: Any = node
        while isinstance(cursor, ast.Attribute):
            parts.append(cursor.attr)
            cursor = cursor.value
        if not isinstance(cursor, ast.Name):
            raise GateError("非法表达式：属性访问只能用于数据字段")
        parts.append(cursor.id)
        return _lookup(".".join(reversed(parts)), self.context)

    def visit_List(self, node: ast.List) -> Any:  # noqa: N802
        return [self.visit(item) for item in node.elts]

    def visit_Tuple(self, node: ast.Tuple) -> Any:  # noqa: N802
        return tuple(self.visit(item) for item in node.elts)

    def visit_Set(self, node: ast.Set) -> Any:  # noqa: N802
        return {self.visit(item) for item in node.elts}

    def visit_Dict(self, node: ast.Dict) -> Any:  # noqa: N802
        return {self.visit(k): self.visit(v) for k, v in zip(node.keys, node.values)}

    def visit_Subscript(self, node: ast.Subscript) -> Any:  # noqa: N802
        target = self.visit(node.value)
        key = self.visit(node.slice)
        try:
            return target[key]
        except Exception as exc:  # noqa: BLE001
            raise _MissingField(f"{ast.unparse(node.value)}[{key!r}]") from exc

    def visit_BinOp(self, node: ast.BinOp) -> Any:  # noqa: N802
        func = _BIN_OPS.get(type(node.op))
        if func is None:
            raise GateError(f"非法表达式：不支持的运算符 {type(node.op).__name__}")
        return func(self.visit(node.left), self.visit(node.right))

    def visit_UnaryOp(self, node: ast.UnaryOp) -> Any:  # noqa: N802
        func = _UNARY_OPS.get(type(node.op))
        if func is None:
            raise GateError(f"非法表达式：不支持的一元运算 {type(node.op).__name__}")
        return func(self.visit(node.operand))

    def visit_BoolOp(self, node: ast.BoolOp) -> Any:  # noqa: N802
        values = [self.visit(value) for value in node.values]
        if isinstance(node.op, ast.And):
            return all(bool(value) for value in values)
        return any(bool(value) for value in values)

    def visit_Compare(self, node: ast.Compare) -> Any:  # noqa: N802
        left = self.visit(node.left)
        for op, comparator in zip(node.ops, node.comparators):
            func = _CMP_OPS.get(type(op))
            if func is None:
                raise GateError(f"非法表达式：不支持的比较符 {type(op).__name__}")
            right = self.visit(comparator)
            if not func(left, right):
                return False
            left = right
        return True

    def visit_IfExp(self, node: ast.IfExp) -> Any:  # noqa: N802
        return self.visit(node.body) if self.visit(node.test) else self.visit(node.orelse)

    def visit_Call(self, node: ast.Call) -> Any:  # noqa: N802
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS:
            raise GateError("非法表达式：只允许调用白名单内置函数")
        args = [self.visit(arg) for arg in node.args]
        kwargs = {kw.arg: self.visit(kw.value) for kw in node.keywords if kw.arg}
        return _FUNCS[node.func.id](*args, **kwargs)

    def generic_visit(self, node: ast.AST) -> Any:  # noqa: N802
        raise GateError(f"非法表达式：不支持的语法 {type(node).__name__}")


def evaluate(expr: str, context: Dict[str, Any]) -> ExprResult:
    """在给定上下文（通常是黑板快照）中求值表达式。"""
    if not expr or not str(expr).strip():
        return ExprResult(False, None, "表达式为空")
    try:
        tree = ast.parse(str(expr), mode="eval")
    except SyntaxError as exc:
        return ExprResult(False, None, f"表达式语法错误: {exc.msg}")
    try:
        value = _Evaluator(context).visit(tree)
    except _MissingField as exc:
        return ExprResult(False, None, f"字段缺失: {exc.path}")
    except GateError as exc:
        return ExprResult(False, None, str(exc))
    except TypeError as exc:
        return ExprResult(False, None, f"类型不匹配: {exc}")
    except Exception as exc:  # noqa: BLE001
        return ExprResult(False, None, f"求值失败: {exc}")
    return ExprResult(True, value, "")


def check_config_expressions(exprs: List[Tuple[str, str]]) -> List[str]:
    """配置加载期校验：只做语法与白名单检查，不要求字段存在。"""
    problems: List[str] = []
    for owner, expr in exprs:
        if not expr:
            problems.append(f"{owner}: 表达式为空")
            continue
        try:
            ast.parse(expr, mode="eval")
        except SyntaxError as exc:
            problems.append(f"{owner}: 语法错误 {exc.msg}")
            continue
        try:
            _Evaluator({}).visit(ast.parse(expr, mode="eval"))
        except _MissingField:
            continue  # 语法合法，仅缺运行期数据
        except GateError as exc:
            problems.append(f"{owner}: {exc}")
        except Exception:  # noqa: BLE001 - 运行期错误在配置校验阶段忽略
            continue
    return problems


_ = Optional  # 保持 typing 导入语义（供类型注解使用）
