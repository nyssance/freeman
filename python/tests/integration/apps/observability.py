"""Metrics and tracing app: routes whose metrics and traces a real server makes.

With ``DJANGO_BOLT_TRACING=1`` and the OpenTelemetry SDK installed, the app
exports its own spans over OTLP/HTTP, as Rust does, and instruments httpx.
Without them, the app imports nothing of OpenTelemetry.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys

import httpx

from freeman import BoltAPI, Request
from freeman.auth import APIKeyAuthentication, IsAuthenticated
from freeman.logging.config import LoggingConfig

TRACING = os.environ.get("DJANGO_BOLT_TRACING") == "1" and importlib.util.find_spec("opentelemetry") is not None

if TRACING:
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    # The provider reads OTEL_TRACES_SAMPLER, as the SDK of Rust does.
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument()
    tracer = trace.get_tracer("observability-app")


def report_exception(logger, request, exception, exc_info):
    """What an error reporter such as Sentry can read in the exception handler."""
    auth = request.get("auth") or {}
    line = {
        "reported": type(exception).__name__,
        "method": request.get("method"),
        "path": request.get("path"),
        "route": request.route,
        "trace_id": request.trace_id,
        "user_id": auth.get("user_id"),
    }
    print(json.dumps(line), flush=True)


api = BoltAPI(logging_config=LoggingConfig(exception_logging_handler=report_exception))
KEY = [APIKeyAuthentication(api_keys={"secret"}, header="x-api-key")]


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/items/{item_id}")
def item(item_id: int):
    return {"item_id": item_id}


@api.get("/private", auth=KEY, guards=[IsAuthenticated()])
async def private():
    return {"ok": True}


def _trace_of(request: Request) -> dict:
    """The ids that Rust gave, and the ids of a child span and an outgoing call."""
    result = {"trace_id": request.trace_id, "span_id": request.span_id, "route": request.route}
    if TRACING:
        with tracer.start_as_current_span("child") as child:
            context = child.get_span_context()
            result["child"] = {
                "trace_id": format(context.trace_id, "032x"),
                # An unsampled child records nothing, so it has no parent to read.
                "parent_id": format(child.parent.span_id, "016x") if getattr(child, "parent", None) else None,
                "sampled": context.trace_flags.sampled,
            }
            echo = os.environ.get("FREEMAN_TEST_ECHO")
            if echo:
                result["outgoing"] = httpx.get(echo).json()["traceparent"]
    return result


@api.get("/trace/{name}")
def trace_sync(request: Request, name: str):
    return _trace_of(request)


@api.get("/trace-async/{name}")
async def trace_async(request: Request, name: str):
    await asyncio.sleep(0)
    return _trace_of(request)


@api.get("/modules")
async def modules():
    loaded = sorted(name for name, module in sys.modules.items() if name.startswith("opentelemetry") and module)
    return {"opentelemetry": loaded, "freeman_otel": "freeman._otel" in sys.modules}


@api.get("/boom")
async def boom():
    raise RuntimeError("boom")


@api.get("/boom-auth/{item_id}", auth=KEY, guards=[IsAuthenticated()])
async def boom_auth(item_id: int):
    raise RuntimeError("boom")


# A route with Django middleware runs on a request lane, in a new context.
lane_api = BoltAPI(django_middleware=["django.middleware.clickjacking.XFrameOptionsMiddleware"])


@lane_api.get("/health")
async def lane_health():
    return {"status": "ok"}


@lane_api.get("/lane/{name}")
def lane_trace(request: Request, name: str):
    return _trace_of(request)
