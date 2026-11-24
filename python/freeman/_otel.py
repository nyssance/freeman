"""Make the server span of Rust the parent of the spans of a handler.

Rust makes the server span of each request and gives its ids to Python as
``request._trace``. While a handler runs, this module makes that span the
current OpenTelemetry span. Thus a span that the handler makes, for example a
query of an instrumented database driver or an outgoing httpx call, is a child
of the server span, and the trace is one tree.

Each request attaches the same context object. Its span reads the request of
the current context, and makes the ids only when OpenTelemetry asks for them.
A request whose handler makes no span thus pays two context-variable writes,
not the cost of a new ``SpanContext``.

Bolt imports this module only when ``DJANGO_BOLT_TRACING`` is on and the
``opentelemetry`` package is installed. Without them, nothing imports
``opentelemetry``.
"""

from __future__ import annotations

import contextvars
import functools
from collections.abc import Callable
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import trace as otel_trace
from opentelemetry.context.contextvars_context import ContextVarsRuntimeContext
from opentelemetry.trace import INVALID_SPAN_CONTEXT, NonRecordingSpan, SpanContext, TraceFlags, TraceState

_SAMPLED = TraceFlags(TraceFlags.SAMPLED)
_NOT_SAMPLED = TraceFlags(TraceFlags.DEFAULT)

# The request whose handler runs in this context.
_request: contextvars.ContextVar[Any] = contextvars.ContextVar("freeman_traced_request", default=None)


@functools.lru_cache(maxsize=256)
def span_context(trace: tuple[int, int, bool, str]) -> SpanContext:
    """The span context of the server span ``(trace_id, span_id, sampled, tracestate)``.

    The span is not sampled when Rust did not sample the request. A sampler
    that follows the parent, the default of the SDK, then drops the spans of
    the handler too. The cache serves the spans of one request after the first.
    """
    trace_id, span_id, sampled, trace_state = trace
    return SpanContext(
        trace_id,
        span_id,
        is_remote=True,
        trace_flags=_SAMPLED if sampled else _NOT_SAMPLED,
        trace_state=TraceState.from_header([trace_state]) if trace_state else None,
    )


class _ServerSpan(NonRecordingSpan):
    """The server span of the request of the current context. Rust records it."""

    def __init__(self) -> None:
        super().__init__(INVALID_SPAN_CONTEXT)

    def get_span_context(self) -> SpanContext:
        request = _request.get()
        trace = None if request is None else request._trace
        if trace is None:
            return INVALID_SPAN_CONTEXT
        return span_context(trace)


# One context for each request: the span in it reads `_request`.
_CONTEXT = otel_trace.set_span_in_context(_ServerSpan(), otel_context.Context())

# `attach` and `detach` of OpenTelemetry set and reset one context variable
# under two Python calls. With the default runtime context, set that variable
# here: it saves about 1% of the throughput of an empty route. Another runtime
# context gets the public functions.
_runtime = getattr(otel_context, "_RUNTIME_CONTEXT", None)
_variable = getattr(_runtime, "_current_context", None) if isinstance(_runtime, ContextVarsRuntimeContext) else None
if isinstance(_variable, contextvars.ContextVar):
    _attach, _detach = _variable.set, _variable.reset
else:
    _attach, _detach = otel_context.attach, otel_context.detach


def traced_dispatch_sync(dispatch_sync: Callable) -> Callable:
    """Wrap ``BoltAPI._dispatch_sync``: the handler runs with the server span as its parent."""

    def dispatch(handler: Callable, request: Any, handler_id: int | None = None) -> Any:
        reset = _request.set(request)
        token = _attach(_CONTEXT)
        try:
            result = dispatch_sync(handler, request, handler_id)
        finally:
            _detach(token)
            _request.reset(reset)
        # A sync executor returns a callable when its result needs the event
        # loop, such as a QuerySet. The callable runs later as a task, so it
        # sets the parent again.
        if callable(result):
            return _deferred(result)
        return result

    return dispatch


def _deferred(call: Callable) -> Callable:
    async def run(request: Any) -> Any:
        reset = _request.set(request)
        token = _attach(_CONTEXT)
        try:
            return await call(request)
        finally:
            _detach(token)
            _request.reset(reset)

    return run


def traced_dispatch(dispatch: Callable) -> Callable:
    """Wrap ``BoltAPI._dispatch``: the handler runs with the server span as its parent.

    The coroutine runs in the context of its own task, or in the new context
    of a request lane, so it sets the parent there.
    """

    async def run(handler: Callable, request: Any, handler_id: int | None = None) -> Any:
        reset = _request.set(request)
        token = _attach(_CONTEXT)
        try:
            return await dispatch(handler, request, handler_id)
        finally:
            _detach(token)
            _request.reset(reset)

    return run
