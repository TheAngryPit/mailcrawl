"""Declared inference points. Each one is off unless config turns it on."""

from __future__ import annotations

import ast
import contextvars
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

DECLARED_POINTS = (
    "embeddings",
    "search-embedding",
    "jev-battery",
    "decision-comparison",
    "ocr-pilot",
    "search-reranker",
    "low-confidence-llm",
)
MODEL_ATTRS = frozenset({"predict", "embed", "generate", "complete", "systemone"})
MODEL_CLIENTS = frozenset(
    {
        "openai",
        "anthropic",
        "transformers",
        "sentence_transformers",
        "torch",
        "ollama",
        "setfit",
        "httpx",
        "requests",
        "aiohttp",
        "huggingface_hub",
    }
)

T = TypeVar("T")
_point: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mailcrawl_inference_point",
    default=None,
)


class BoundaryError(Exception):
    """A model call is outside a declared, enabled inference point."""


def enabled_points(config: dict[str, bool] | None = None) -> frozenset[str]:
    if not config:
        return frozenset()
    enabled: set[str] = set()
    for name, flag in config.items():
        if name not in DECLARED_POINTS:
            raise BoundaryError(name)
        if flag is True:
            enabled.add(name)
    return frozenset(enabled)


def enter_model() -> None:
    if _point.get() not in DECLARED_POINTS:
        raise BoundaryError("model call outside a declared point")


def invoke(
    point: str,
    call: Callable[[], T],
    config: dict[str, bool] | None = None,
) -> T | None:
    if point not in DECLARED_POINTS:
        raise BoundaryError(point)
    if point not in enabled_points(config):
        return None
    token = _point.set(point)
    try:
        return call()
    finally:
        _point.reset(token)


def scan_source(text: str, filename: str) -> list[str]:
    tree = ast.parse(text, filename=filename)
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_invoke(node):
            point = node.args[0] if node.args else None
            if not isinstance(point, ast.Constant) or point.value not in DECLARED_POINTS:
                problems.append(f"{filename}:{node.lineno}: undeclared point")
        if isinstance(node, ast.Call) and _is_model_call(node) and not _inside_declared_invoke(node, parents):
            problems.append(f"{filename}:{node.lineno}: model call outside a declared point")
        if (
            isinstance(node, (ast.Import, ast.ImportFrom))
            and not _inside_declared_invoke(node, parents)
            and not _guarded_by_enter_model(node, parents)
        ):
            for root_name in _import_roots(node):
                if root_name in MODEL_CLIENTS:
                    problems.append(f"{filename}:{node.lineno}: model client import {root_name}")
    return problems


def scan_package(root: Path) -> list[str]:
    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        found.extend(scan_source(path.read_text(encoding="utf-8"), str(path)))
    return found


def _is_invoke(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "invoke"
    if isinstance(func, ast.Attribute):
        return func.attr == "invoke"
    return False


def _is_model_call(node: ast.Call) -> bool:
    func = node.func
    return isinstance(func, ast.Attribute) and func.attr in MODEL_ATTRS


def _import_roots(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name.split(".", 1)[0] for alias in node.names]
    if node.module is None:
        return []
    return [node.module.split(".", 1)[0]]


def _guarded_by_enter_model(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    current: ast.AST | None = node
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)) and _calls_enter_model(current):
            return True
        current = parents.get(current)
    return False


def _calls_enter_model(fn: ast.AST) -> bool:
    for child in ast.walk(fn):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Name) and func.id == "enter_model":
            return True
        if isinstance(func, ast.Attribute) and func.attr == "enter_model":
            return True
    return False


def _inside_declared_invoke(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    current: ast.AST | None = node
    while current is not None:
        parent = parents.get(current)
        if isinstance(parent, ast.Call) and _is_invoke(parent) and len(parent.args) >= 2:
            point = parent.args[0]
            if (
                isinstance(point, ast.Constant)
                and point.value in DECLARED_POINTS
                and _contains(parent.args[1], node)
            ):
                return True
        current = parent
    return False


def _contains(root: ast.AST, target: ast.AST) -> bool:
    for node in ast.walk(root):
        if node is target:
            return True
    return False
