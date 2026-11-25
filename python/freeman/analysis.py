"""
Static analysis of handler functions.

Reads the source of a handler to find the request parts that it reads, so
Rust parses only those parts.
"""

from __future__ import annotations

import ast
import inspect
import sys
import textwrap
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .typing import REQUEST_PARAM_NAMES, HandlerMetadata

__all__ = [
    "DependencyNeeds",
    "HandlerAnalysis",
    "analyze_dependency_tree",
    "analyze_handler",
    "annotation_globals",
    "resolve_introspection_target",
]


def resolve_introspection_target(fn: Any) -> Any:
    """Return a form of ``fn`` that ``inspect.getsource`` / ``get_type_hints`` can handle.

    Class-callable instances (``Depends(Backend(...))``) are routed to their
    ``__call__`` method so downstream introspection works.

    Every other object gets the ``__call__`` of its type. For a callable in C,
    such as ``functools.partial``, that is a slot wrapper. ``inspect.getsource``
    raises for a slot wrapper, and ``get_type_hints`` gives ``{}``.
    """
    if inspect.isfunction(fn) or inspect.ismethod(fn) or inspect.isclass(fn) or inspect.ismodule(fn):
        return fn
    # The metaclass has a __call__, so the lookup always finds one.
    return inspect.getattr_static(type(fn), "__call__")


def annotation_globals(fn: Any) -> dict[str, Any]:
    """Return the globals that resolve the string annotations of ``fn``.

    These are the globals of the function, as for ``typing.get_type_hints``.
    A wrapper with ``__wrapped__`` gives the globals of the function that it
    wraps, because its annotations come from there. Bolt sets ``__wrapped__``
    on each wrapper that it makes. ``functools.wraps`` and ``sync_to_async``
    do the same.

    ``fn.__module__`` is not a safe source. It can name a module that is not
    in ``sys.modules``, or a module that did not define the function. A
    callable object gives the globals of its ``__call__``. Only an object with
    no globals at all, such as a class, uses the dict of its module.
    """
    target = inspect.unwrap(fn)
    for candidate in (target, resolve_introspection_target(target)):
        globalns = getattr(candidate, "__globals__", None)
        if globalns is not None:
            return globalns
    module = sys.modules.get(getattr(fn, "__module__", None) or "")
    return {} if module is None else module.__dict__


@dataclass
class DependencyNeeds:
    """Request-component needs aggregated across a handler's Depends() tree."""

    needs_body: bool = False
    needs_query: bool = False
    needs_headers: bool = False
    needs_cookies: bool = False

    @property
    def saturated(self) -> bool:
        return self.needs_body and self.needs_query and self.needs_headers and self.needs_cookies

    def set_all(self) -> None:
        self.needs_body = True
        self.needs_query = True
        self.needs_headers = True
        self.needs_cookies = True


@dataclass
class HandlerAnalysis:
    """The request parts that a handler reads."""

    request_needs_body: bool = False
    """Whether the handler reads request.body"""

    request_needs_query: bool = False
    """Whether the handler reads request.query or query-derived helpers"""

    request_needs_headers: bool = False
    """Whether the handler reads request.headers or header-derived helpers"""

    request_needs_cookies: bool = False
    """Whether the handler reads request.cookies"""

    # Analysis metadata
    analysis_failed: bool = False
    """Whether AST analysis failed (e.g., couldn't get source)"""

    failure_reason: str | None = None
    """Reason for analysis failure if any"""


class RequestVisitor(ast.NodeVisitor):
    """AST visitor that finds the request parts that a function reads."""

    def __init__(self, request_param_names: set[str] | None = None) -> None:
        self.analysis = HandlerAnalysis()
        self.request_param_names = request_param_names or set()

    def _is_request_name(self, node: ast.AST) -> bool:
        return isinstance(node, ast.Name) and node.id in self.request_param_names

    def _mark_request_component_attr(self, attr_name: str) -> None:
        if attr_name == "body":
            self.analysis.request_needs_body = True
        elif attr_name == "query":
            self.analysis.request_needs_query = True
        elif attr_name == "headers":
            self.analysis.request_needs_headers = True
        elif attr_name in {"cookies", "raw_cookies"}:
            self.analysis.request_needs_cookies = True
        elif attr_name == "META":
            self.analysis.request_needs_headers = True
            self.analysis.request_needs_query = True
        elif attr_name == "get_full_path":
            self.analysis.request_needs_query = True
        elif attr_name in {"build_absolute_uri", "scheme", "is_secure"}:
            # These read META, as the "META" branch above.
            self.analysis.request_needs_headers = True
            self.analysis.request_needs_query = True
        elif attr_name not in {"method", "path", "params", "context", "state", "user", "auser"}:
            # Unknown methods and descriptors can read any request component.
            self._mark_all_request_components()

    def _mark_all_request_components(self) -> None:
        for attr_name in ("body", "query", "headers", "cookies"):
            self._mark_request_component_attr(attr_name)

    def _mark_request_component_key(self, key: str) -> None:
        # Of the request parts, request.get(key) and request[key] serve only
        # these. Another key, such as "META", gives the default or a KeyError.
        if key in ("body", "query", "headers", "cookies"):
            self._mark_request_component_attr(key)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        """Mark the request part that ``request.<attr>`` reads."""
        reads_request = self._is_request_name(node.value)
        if reads_request:
            self._mark_request_component_attr(node.attr)

        # Continue visiting children. A request receiver is a read, not a hand-on.
        if not reads_request:
            self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        """Mark every request part when the request goes anywhere but a read.

        A request handed on (to a callable, another name, a container or an
        attribute) can be read where this analysis does not look.
        """
        if isinstance(node.ctx, ast.Load) and node.id in self.request_param_names:
            self._mark_all_request_components()

    def visit_Call(self, node: ast.Call) -> None:
        """Mark the part that ``request.get(key)`` reads."""
        if isinstance(node.func, ast.Attribute) and self._is_request_name(node.func.value) and node.func.attr == "get":
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                self._mark_request_component_key(node.args[0].value)
            else:
                self._mark_all_request_components()
            for argument in (*node.args, *node.keywords):
                self.visit(argument)
            return
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        """Detect request['headers'] style access."""
        if self._is_request_name(node.value):
            if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                self._mark_request_component_key(node.slice.value)
            else:
                # A key known only at run time can name any part.
                self._mark_all_request_components()
            # A request receiver is a read, not a hand-on.
            self.visit(node.slice)
            return

        self.generic_visit(node)


def analyze_handler(fn: Callable[..., Any], request_param_names: set[str] | None = None) -> HandlerAnalysis:
    """
    Find the request parts that a handler reads, from its source.

    Args:
        fn: The handler function to analyze
        request_param_names: The parameters of ``fn`` that get the request

    Returns:
        HandlerAnalysis with detected patterns
    """
    analysis = HandlerAnalysis()

    visitor = RequestVisitor(request_param_names=request_param_names)
    target = _unwrap(resolve_introspection_target(_unwrap(fn, visitor)), visitor)
    # A call of a class runs its __init__, which can come from a base class.
    if inspect.isclass(target):
        # A metaclass __call__ and a __new__ run before __init__. The analysis
        # reads __init__ only, so it cannot see what they read.
        if type(target).__call__ is not type.__call__ or any("__new__" in vars(base) for base in target.__mro__[:-1]):
            analysis.analysis_failed = True
            analysis.failure_reason = f"{target.__name__} runs a __new__ or a metaclass __call__ before __init__"
            return analysis
        target = _unwrap(target.__init__, visitor)

    # Try to get source code
    try:
        source = inspect.getsource(target)
    except (OSError, TypeError) as e:
        # Can't get source (e.g., built-in, C extension, or lambda)
        analysis.analysis_failed = True
        analysis.failure_reason = f"Could not get source: {e}"
        return analysis

    # Dedent source code (handles indented methods)
    source = textwrap.dedent(source)

    # Parse AST
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        analysis.analysis_failed = True
        analysis.failure_reason = f"Syntax error parsing source: {e}"
        return analysis

    name = target.__code__.co_name
    definition = _find_definition(tree, name)
    if definition is None:
        analysis.analysis_failed = True
        analysis.failure_reason = f"Could not find one definition of {name} in its source"
        return analysis

    # Analyze only the body, not the decorators.
    if isinstance(definition, ast.Lambda):
        visitor.visit(definition.body)
    else:
        for stmt in definition.body:
            visitor.visit(stmt)

    return visitor.analysis


def _unwrap(fn: Any, visitor: RequestVisitor) -> Any:
    """Return the function under the wrappers of ``fn``.

    A wrapper (a function with ``__wrapped__``) gets the request before the
    function that it wraps. It can read any part, so mark every part.
    """
    target = inspect.unwrap(fn)
    if target is not fn:
        visitor._mark_all_request_components()
    return target


def _find_definition(tree: ast.AST, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | None:
    """Return the definition named ``name`` in ``tree``.

    The source of a lambda is its whole line, so it must hold one lambda only.
    Otherwise this cannot tell which lambda the handler is, and returns None.
    """
    if name == "<lambda>":
        lambdas = [node for node in ast.walk(tree) if isinstance(node, ast.Lambda)]
        return lambdas[0] if len(lambdas) == 1 else None
    return next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        ),
        None,
    )


def _dep_request_param_names(dep_meta: HandlerMetadata) -> set[str]:
    """Return the set of parameter names that receive the raw request object."""
    # compile_binder always sets mode, sig and fields.
    if dep_meta["mode"] == "request_only":
        return {p.name for p in dep_meta["sig"].parameters.values() if p.name in REQUEST_PARAM_NAMES}
    return {f.name for f in dep_meta["fields"] if f.source == "request"}


def analyze_dependency_tree(
    meta: HandlerMetadata, compile_dep_fn: Callable[[Callable[..., Any]], HandlerMetadata]
) -> DependencyNeeds:
    """Recursively analyze a handler's Depends targets for request-component access.

    Walks every field in ``meta["fields"]`` that is a Depends-backed field,
    compiles its own parameter binder (via ``compile_dep_fn``), and aggregates
    the request components (body/query/headers/cookies) that any dep reads —
    either through a typed parameter (Query/Header/Cookie/Body/Form/File) or by
    touching ``request.query`` / ``request.headers`` / ``request.body`` /
    ``request.cookies`` inside its body.

    Without this, static AST analysis would stop at the handler boundary,
    causing Rust to skip request-component parsing even though a dep needs it.

    Args:
        meta: Handler metadata (must have ``fields`` list already populated).
        compile_dep_fn: Callable that returns a dep's compiled HandlerMetadata.
            Responsible for caching so repeated deps aren't recompiled.

    Returns:
        DependencyNeeds aggregated across the whole dep subtree.
    """
    result = DependencyNeeds()
    _walk_dep_tree(meta, compile_dep_fn, result, visited=set())
    return result


def _walk_dep_tree(
    meta: HandlerMetadata,
    compile_dep_fn: Callable[[Callable[..., Any]], HandlerMetadata],
    acc: DependencyNeeds,
    visited: set[int],
) -> None:
    for dep_field in meta["fields"]:
        if acc.saturated:
            return
        if dep_field.source != "dependency":
            continue
        # A bare Depends() has no callable. compile_dep_fn raises for it below.
        dep_fn = dep_field.dependency.dependency
        dep_id = id(dep_fn)
        if dep_id in visited:
            continue
        visited.add(dep_id)

        try:
            dep_meta = compile_dep_fn(dep_fn)
        except Exception:
            # Unknown introspection failure — assume the dep needs every component.
            acc.set_all()
            return

        # A request_only dependency has no needs_* flags.
        acc.needs_body |= bool(dep_meta.get("needs_body"))
        acc.needs_query |= bool(dep_meta.get("needs_query"))
        acc.needs_headers |= bool(dep_meta.get("needs_headers"))
        acc.needs_cookies |= bool(dep_meta.get("needs_cookies"))

        dep_request_names = _dep_request_param_names(dep_meta)
        if dep_request_names:
            dep_analysis = analyze_handler(dep_fn, request_param_names=dep_request_names)
            if dep_analysis.analysis_failed:
                acc.set_all()
                return
            acc.needs_body |= dep_analysis.request_needs_body
            acc.needs_query |= dep_analysis.request_needs_query
            acc.needs_headers |= dep_analysis.request_needs_headers
            acc.needs_cookies |= dep_analysis.request_needs_cookies

        _walk_dep_tree(dep_meta, compile_dep_fn, acc, visited)
