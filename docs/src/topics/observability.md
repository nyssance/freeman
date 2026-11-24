---
icon: lucide/activity
---

# Metrics and Tracing

Freeman routes, authenticates and rate-limits a request in Rust, before Python runs.
It does not use ASGI or the request handler of Django.
Thus the Django and ASGI integrations of Prometheus, OpenTelemetry and Sentry do not see the requests.
Rust makes the metrics and the server spans itself.

Each part is off by default. An environment variable turns it on. Freeman reads it once, when the server starts.

| Variable | Gives |
|---|---|
| `DJANGO_BOLT_METRICS=/metrics` | Prometheus metrics at this path |
| `DJANGO_BOLT_TRACING=1` | W3C trace context, server spans over OTLP/HTTP, the parent of the spans of a handler |
| `DJANGO_BOLT_ACCESS_LOG=stdout` | The JSON access log, with the trace id when tracing is on |

The [settings reference](../ref/settings.md#django_bolt_metrics) gives each variable.
The [test client](testing.md) does not make metrics or spans: they need `runbolt`.

## Metrics

```bash
export DJANGO_BOLT_METRICS=/metrics
python manage.py runbolt
```

```text
http_server_requests_total{method="GET",route="/items/{id}",status="200"} 1042
http_server_request_duration_seconds_bucket{method="GET",route="/items/{id}",status="200",le="0.005"} 1030
...
http_server_request_duration_seconds_sum{method="GET",route="/items/{id}",status="200"} 0.81
http_server_request_duration_seconds_count{method="GET",route="/items/{id}",status="200"} 1042
http_server_active_requests 3
freeman_metrics_processes{state="live"} 4
freeman_metrics_processes{state="exited"} 0
```

Rust answers the path. Python does not run for it, and a scrape is not in the numbers or in the access log.

The labels have a limit, so a client cannot make new series:

- `route` is the path of the route as you registered it, such as `/items/{id}`. It is never the path of the request. A request that matches no route gets `unmatched`. A request for an ASGI or MCP mount gets the prefix of the mount.
- `method` is one of the HTTP methods that Freeman routes. Another method gets `_OTHER`.
- `status` is the status code. A value out of 100 to 599 gets `0`.

The duration is the time from the request to the response headers. A streamed body can take longer.
The buckets are the bounds that the OpenTelemetry HTTP conventions advise, from 5 ms to 10 s.
Static files, media files and WebSocket connections are not in the metrics.

### Who can read the metrics

The metrics tell the routes and the load of the server, so they are not public by default.

- With `DJANGO_BOLT_METRICS_TOKEN`, a scrape must send `Authorization: Bearer <token>`. Other requests get a `401`.
- Without a token, only a client on the same host can read the metrics: the peer must be a loopback address, and the request must have no `Forwarded`, `X-Forwarded-For` or `X-Real-IP` header. Thus a proxy on the same host does not make a public request look local. Other requests get a `403`.

A Prometheus job with a token:

```yaml
scrape_configs:
  - job_name: api
    metrics_path: /metrics
    authorization:
      credentials: <token>
    static_configs:
      - targets: ["api.internal:8000"]
```

### Many worker processes

`runbolt --processes 4` runs four processes on one port with `SO_REUSEPORT`. A scrape reaches one of them.
If each process gave only its own numbers, each scrape would give a different part of the total.
Freeman does what the multiprocess mode of the Prometheus Python client does for Gunicorn:

1. Before the first fork, `runbolt` makes a folder and gives it to the workers in `DJANGO_BOLT_METRICS_DIR`.
2. Once a second, each worker writes its numbers to its own file in the folder. The worker writes a new file and then renames it, so a reader never sees half a file.
3. The worker that gets the scrape adds the files of the other workers to its own live numbers.
4. When `runbolt` stops, it removes the folder.

The limits of this method:

- The numbers of the other workers are up to about one second old. The numbers of the worker that answers are live.
- A worker that stops, for example by `--max-rss` or `--workers-lifetime`, keeps its file. Thus a counter does not go down when a worker is recycled. Its requests in flight do not count after five seconds without a write.
- `freeman_metrics_processes` tells how many files are in the answer: `live` for the workers that wrote in the last five seconds, `exited` for the others. Compare `live` with `--processes`.
- The folder grows by one small file for each worker that starts. A server that recycles workers for weeks keeps all of them until it stops.
- With your own `DJANGO_BOLT_METRICS_DIR`, `runbolt` deletes the old `metrics-*.json` files in it at start, and it does not remove the folder. Do not give the same folder to two servers.

One process with no folder (`runbolt` without `--processes`) gives its live numbers only.

## Tracing

```bash
pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-http   # for the spans of Python, see below
export DJANGO_BOLT_TRACING=1
export OTEL_SERVICE_NAME=api
export OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318
python manage.py runbolt
```

For each request, Rust:

1. Reads `traceparent` by the W3C Trace Context rules, and keeps `tracestate`. A header that is not valid, such as an id of all zeros, a short id, upper-case hex or version `ff`, starts a new trace.
2. Makes the server span and its ids with the OpenTelemetry SDK for Rust. The sampler decides here.
3. Gives the ids to Python with the request: `request.trace_id`, `request.span_id`.
4. Ends the span after the response. Its name is the method and the route template, such as `GET /items/{id}`. It has the attributes of the OpenTelemetry HTTP conventions: `http.request.method`, `http.route`, `http.response.status_code`, `url.path`, `url.scheme`, `server.address`, `server.port`, `network.protocol.version`, `network.peer.address` and `user_agent.original`. A 5xx sets the status to error and `error.type`.
5. Sends the sampled spans in batches over OTLP/HTTP with protobuf, from a thread of its own.

A request that Rust answers before Python runs, such as a 401, 403, 404 or 429, also gets its span.
A request that matches no route gets the name `GET`, with no `http.route`.

The SDK reads the standard variables: `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_TRACES_SAMPLER`, `OTEL_TRACES_SAMPLER_ARG` and `OTEL_BSP_*`.
`OTEL_TRACES_EXPORTER=none` makes the spans and drops them: the ids still go to Python and to the access log.
`OTEL_SDK_DISABLED=true` turns tracing off. Freeman has no gRPC exporter: a protocol other than `http/protobuf` stops the start.

When the queue of the exporter is full (`OTEL_BSP_MAX_QUEUE_SIZE`, 2048 by default), the SDK drops new spans. A slow collector thus does not slow the requests.

### Sampling

The sampler of Rust decides for each request. The default is `parentbased_always_on`: a request follows the flag of its `traceparent`, and a request with no parent is sampled.
To keep one request in ten:

```bash
export OTEL_TRACES_SAMPLER=parentbased_traceidratio
export OTEL_TRACES_SAMPLER_ARG=0.1
```

An unsampled request records no span, and its end costs nothing.
Use a sampler that follows the parent (`parentbased_*`) also in Python, the default of the Python SDK. Then the spans of a handler follow the decision of Rust.

### Spans of Python

With the `opentelemetry` package installed, a handler runs with the server span as its current OpenTelemetry span.
Thus the spans that the handler makes are children of the server span:
a span of your own, a query of an instrumented database driver, an outgoing call of an instrumented httpx client.
The outgoing call carries the trace id to the next service.
Configure the Python SDK as usual, for example in `settings.py`:

```python
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

provider = TracerProvider()
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(provider)
HTTPXClientInstrumentor().instrument()
PsycopgInstrumentor().instrument()
```

Rust and Python export to the same collector with the same variables, and the collector joins the spans by their trace id into one trace.
Do not add the ASGI or Django instrumentation of OpenTelemetry: Freeman does not run them, and Rust already makes the server span.

The sync, async and request-lane handlers get the parent. A QuerySet that a handler returns loads with the parent too.
A thread that the handler starts gets the parent only with a copy of the context (`contextvars.copy_context()`), as in any OpenTelemetry code.

Without the `opentelemetry` package, or with tracing off, Freeman imports nothing of OpenTelemetry. The server spans of Rust and the request ids still work.

How it stays cheap: each request attaches the same context object. Its span reads the request of the current context, and makes the `SpanContext` only when OpenTelemetry asks for it. A request whose handler makes no span thus pays two context-variable writes.

### Logs and errors

With `DJANGO_BOLT_ACCESS_LOG=stdout`, each line has the trace id:

```json
{"ts":1795344954.612,"method":"GET","path":"/items/7","status":200,"ms":0.4,"trace":"4bf92f3577b34da6a3ce929d0e0e4736"}
```

The exception handler of `LoggingConfig` gets the request. It has what an error reporter needs. See [Custom exception handler](logging.md#custom-exception-handler).

## Cost

Measured with the method of `scripts/gate.py`: one process, one worker thread, the empty route `/` of the example project, 100 connections, bombardier, the best of two runs of 8 s, and the best of four such rounds. Between rounds, "all off" moved from 120,000 to 125,000, so a difference under 4% is noise.
With tracing on, the OpenTelemetry SDK for Python is installed, so each handler gets the parent.
`OTEL_TRACES_EXPORTER=none`: Rust makes and ends each sampled span, and drops it.

| Setting | Requests per second | Part of "all off" |
|---|---:|---:|
| All off | 125,327 | 100% |
| Access log (`stdout`) | 121,598 | 97% |
| Metrics | 123,944 | 99% |
| Tracing, sampler at 0 | 120,822 | 96% |
| Tracing, each request sampled | 110,597 | 88% |
| All three, sampler at 0 | 115,781 | 92% |
| All three, each request sampled | 107,437 | 86% |

With all three off, the compiler removes their code from the request path (a const generic), so the throughput does not change.
Most of the cost of tracing is in Python: the handler gets its parent with two context-variable writes. In Rust, tracing with the sampler at 0 costs less than 1%.
