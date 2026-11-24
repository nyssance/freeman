"""Metrics and tracing on a real server.

``DJANGO_BOLT_METRICS`` makes Rust answer the metrics path. ``DJANGO_BOLT_TRACING``
makes Rust read the W3C trace context, make the server span and export it over
OTLP/HTTP. The spans of a handler, made with the Python SDK, join that span.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from .apps import app_module

pytestmark = pytest.mark.server_integration

APP = app_module("observability")
PARENT_TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT_SPAN = "00f067aa0ba902b7"
SAMPLED_PARENT = f"00-{PARENT_TRACE}-{PARENT_SPAN}-01"
UNSAMPLED_PARENT = f"00-{PARENT_TRACE}-{PARENT_SPAN}-00"
SERVER_KIND = 2


class Collector:
    """An OTLP/HTTP receiver of spans, and an echo of the `traceparent` of a call."""

    def __init__(self) -> None:
        self.spans: list[dict] = []
        self.lock = threading.Lock()
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["content-length"]))
                request = ExportTraceServiceRequest.FromString(body)
                with collector.lock:
                    for resource_spans in request.resource_spans:
                        for scope_spans in resource_spans.scope_spans:
                            for span in scope_spans.spans:
                                collector.spans.append({
                                    "trace_id": span.trace_id.hex(),
                                    "span_id": span.span_id.hex(),
                                    "parent_id": span.parent_span_id.hex(),
                                    "name": span.name,
                                    "kind": span.kind,
                                    "status": span.status.code,
                                    "attributes": {
                                        item.key: item.value.WhichOneof("value")
                                        and getattr(item.value, item.value.WhichOneof("value"))
                                        for item in span.attributes
                                    },
                                })
                self.send_response(200)
                self.send_header("content-type", "application/x-protobuf")
                self.send_header("content-length", "0")
                self.end_headers()

            def do_GET(self):
                body = json.dumps({"traceparent": self.headers.get("traceparent")}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def trace(self, trace_id: str, count: int, timeout: float = 10.0) -> list[dict]:
        """Wait for `count` spans of `trace_id`."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                spans = [span for span in self.spans if span["trace_id"] == trace_id]
            if len(spans) >= count:
                return spans
            time.sleep(0.05)
        raise AssertionError(f"{len(spans)} spans of {trace_id}, not {count}: {spans}")

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def collector() -> Iterator[Collector]:
    receiver = Collector()
    yield receiver
    receiver.close()


def tracing_env(collector: Collector, **more: str) -> dict[str, str]:
    return {
        "DJANGO_BOLT_TRACING": "1",
        "OTEL_EXPORTER_OTLP_ENDPOINT": collector.url,
        "OTEL_SERVICE_NAME": "observability-test",
        "OTEL_BSP_SCHEDULE_DELAY": "50",
        "FREEMAN_TEST_ECHO": f"{collector.url}/echo",
        **more,
    }


def _samples(text: str) -> dict[str, float]:
    """The samples of a Prometheus text, by name and labels."""
    samples = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            name, value = line.rsplit(" ", 1)
            samples[name] = float(value)
    return samples


def _get(server, path: str, **kwargs) -> httpx.Response:
    """A request on a new connection, so a worker process can take it."""
    return httpx.get(server.url(path), timeout=10, **kwargs)


def test_the_metrics_count_each_request_by_its_route_template(make_server_project):
    project = make_server_project(api_module=APP)

    with project.start(env={"DJANGO_BOLT_METRICS": "/metrics", "DJANGO_BOLT_ACCESS_LOG": "stdout"}) as server:
        assert server.get("/items/1").status_code == 200
        assert server.get("/items/2").status_code == 200
        assert server.get("/no-such-path").status_code == 404
        assert server.get("/private").status_code == 401
        assert server.request("POST", "/items/1").status_code == 405
        response = server.get("/metrics")
        time.sleep(0.2)
        stdout, _ = server.stop()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    samples = _samples(response.text)
    item = 'method="GET",route="/items/{item_id}",status="200"'
    assert samples[f"http_server_requests_total{{{item}}}"] == 2
    assert samples[f"http_server_request_duration_seconds_count{{{item}}}"] == 2
    assert samples[f'http_server_request_duration_seconds_bucket{{{item},le="+Inf"}}'] == 2
    assert samples['http_server_requests_total{method="GET",route="unmatched",status="404"}'] == 1
    assert samples['http_server_requests_total{method="GET",route="/private",status="401"}'] == 1
    assert samples['http_server_requests_total{method="POST",route="/items/{item_id}",status="405"}'] == 1
    assert samples["http_server_active_requests"] == 0
    assert samples['freeman_metrics_processes{state="live"}'] == 1
    # A label never holds the path of a request, and a scrape is not in the numbers.
    assert "/items/1" not in response.text
    assert "/no-such-path" not in response.text
    assert "/metrics" not in response.text
    # The scrape writes no access-log line.
    paths = [json.loads(line)["path"] for line in stdout.splitlines() if line.startswith('{"ts":')]
    assert "/items/1" in paths
    assert "/metrics" not in paths


def test_without_a_token_the_metrics_are_for_a_local_client_with_no_proxy(make_server_project):
    project = make_server_project(api_module=APP)

    with project.start(env={"DJANGO_BOLT_METRICS": "/metrics"}) as server:
        assert server.get("/metrics").status_code == 200
        forwarded = server.get("/metrics", headers={"x-forwarded-for": "203.0.113.9"})

    assert forwarded.status_code == 403
    assert "DJANGO_BOLT_METRICS_TOKEN" in forwarded.text


def test_with_a_token_the_metrics_need_it(make_server_project):
    project = make_server_project(api_module=APP)
    env = {"DJANGO_BOLT_METRICS": "/metrics", "DJANGO_BOLT_METRICS_TOKEN": "s3cret"}

    with project.start(env=env) as server:
        missing = server.get("/metrics")
        wrong = server.get("/metrics", headers={"authorization": "Bearer wrong"})
        right = server.get("/metrics", headers={"authorization": "Bearer s3cret"})

    assert (missing.status_code, wrong.status_code, right.status_code) == (401, 401, 200)
    assert missing.headers["www-authenticate"] == "Bearer"
    assert "http_server_requests_total" in right.text


def test_without_the_variable_the_metrics_path_is_a_route_miss(make_server_project):
    project = make_server_project(api_module=APP)

    with project.start() as server:
        assert server.get("/metrics").status_code == 404


def test_a_scrape_counts_the_requests_of_each_worker_process(make_server_project):
    project = make_server_project(api_module=APP)

    with project.start(processes=2, env={"DJANGO_BOLT_METRICS": "/metrics"}) as server:
        # Each process starts to listen on its own. Wait until both wrote a file.
        server.wait_for_text("/metrics", 'freeman_metrics_processes{state="live"} 2')
        for index in range(40):
            assert _get(server, f"/items/{index}").status_code == 200
        # A process writes its file once a second.
        time.sleep(2.5)
        counts = set()
        for _ in range(8):
            samples = _samples(_get(server, "/metrics").text)
            counts.add(samples['http_server_requests_total{method="GET",route="/items/{item_id}",status="200"}'])
            assert samples['freeman_metrics_processes{state="live"}'] == 2

    # Each scrape gives the total of both processes, whichever process answers it.
    assert counts == {40}


def test_runbolt_removes_the_metrics_folder_that_it_made(make_server_project, tmp_path):
    project = make_server_project(api_module=APP)
    # runbolt makes the folder in the temporary folder of the process.
    env = {"DJANGO_BOLT_METRICS": "/metrics", "TMPDIR": str(tmp_path)}

    with project.start(processes=2, env=env) as server:
        server.wait_for_text("/metrics", 'freeman_metrics_processes{state="live"} 2')
        [folder] = tmp_path.glob("freeman-metrics-*")
        assert len(list(folder.glob("metrics-*.json"))) == 2
        server.stop()

    assert not folder.exists()


def _check_tree(collector: Collector, body: dict, route: str) -> None:
    """The server span of Rust, the child span of Python and the httpx span are one tree."""
    trace_id = body["trace_id"]
    spans = collector.trace(trace_id, 3)
    server = next(span for span in spans if span["kind"] == SERVER_KIND)
    child = next(span for span in spans if span["name"] == "child")
    outgoing = next(span for span in spans if span["parent_id"] == child["span_id"])

    assert server["name"] == f"GET {route}"
    assert server["span_id"] == body["span_id"]
    assert server["parent_id"] == PARENT_SPAN
    assert server["attributes"]["http.route"] == route
    assert server["attributes"]["http.response.status_code"] == 200
    assert server["attributes"]["http.request.method"] == "GET"
    assert child["parent_id"] == server["span_id"]
    assert body["child"] == {"trace_id": trace_id, "parent_id": server["span_id"], "sampled": True}
    # The outgoing call carries the trace, with its own span as the parent.
    assert body["outgoing"] == f"00-{trace_id}-{outgoing['span_id']}-01"


@pytest.mark.parametrize("path", ["/trace/a", "/trace-async/a"])
def test_a_handler_continues_the_trace_of_a_parent_traceparent(make_server_project, collector, path):
    project = make_server_project(api_module=APP)
    env = tracing_env(collector, DJANGO_BOLT_ACCESS_LOG="stdout")

    with project.start(env=env) as server:
        response = server.get(path, headers={"traceparent": SAMPLED_PARENT, "tracestate": "vendor=value"})
        body = response.json()
        route = "/trace/{name}" if path.startswith("/trace/") else "/trace-async/{name}"
        _check_tree(collector, body, route)
        time.sleep(0.2)
        stdout, _ = server.stop()

    assert body["trace_id"] == PARENT_TRACE
    assert re.fullmatch("[0-9a-f]{16}", body["span_id"]) and body["span_id"] != PARENT_SPAN
    assert body["route"] == route
    # The access-log line has the trace id, so the log joins the trace.
    line = next(json.loads(line) for line in stdout.splitlines() if f'"path":"{path}"' in line)
    assert line["trace"] == PARENT_TRACE


def test_a_handler_on_a_request_lane_continues_the_trace(make_server_project, collector):
    project = make_server_project(api_module=app_module("observability", "lane_api"))

    with project.start(env=tracing_env(collector)) as server:
        response = server.get("/lane/a", headers={"traceparent": SAMPLED_PARENT})
        assert response.headers["x-frame-options"] == "DENY"
        _check_tree(collector, response.json(), "/lane/{name}")


def test_a_request_with_no_valid_parent_starts_a_new_trace(make_server_project, collector):
    project = make_server_project(api_module=APP)

    with project.start(env=tracing_env(collector)) as server:
        none = server.get("/trace/a").json()
        malformed = server.get("/trace/a", headers={"traceparent": f"00-{PARENT_TRACE.upper()}-{PARENT_SPAN}-01"})
        zeros = server.get("/trace/a", headers={"traceparent": f"00-{'0' * 32}-{PARENT_SPAN}-01"})
        bodies = [none, malformed.json(), zeros.json()]
        for body in bodies:
            [server_span] = [span for span in collector.trace(body["trace_id"], 3) if span["kind"] == SERVER_KIND]
            assert server_span["parent_id"] == ""

    trace_ids = {body["trace_id"] for body in bodies}
    assert len(trace_ids) == 3
    assert all(re.fullmatch("[0-9a-f]{32}", trace_id) for trace_id in trace_ids)
    assert PARENT_TRACE not in trace_ids


def test_a_request_that_rust_rejects_gets_a_span(make_server_project, collector):
    project = make_server_project(api_module=APP)

    with project.start(env=tracing_env(collector)) as server:
        assert server.get("/private", headers={"traceparent": SAMPLED_PARENT}).status_code == 401
        missing = f"00-{'1' * 32}-{PARENT_SPAN}-01"
        assert server.get("/no-such-path", headers={"traceparent": missing}).status_code == 404
        [rejected] = collector.trace(PARENT_TRACE, 1)
        [unmatched] = collector.trace("1" * 32, 1)

    assert rejected["name"] == "GET /private"
    assert rejected["attributes"]["http.response.status_code"] == 401
    # A route miss has no route: the name is the method only.
    assert unmatched["name"] == "GET"
    assert "http.route" not in unmatched["attributes"]


def test_an_unsampled_request_records_no_span(make_server_project, collector):
    project = make_server_project(api_module=APP)

    with project.start(env=tracing_env(collector)) as server:
        body = server.get("/trace/a", headers={"traceparent": UNSAMPLED_PARENT}).json()
        # A sampled request after it: its spans reach the collector, the others do not.
        marker = f"00-{'2' * 32}-{PARENT_SPAN}-01"
        server.get("/trace/a", headers={"traceparent": marker})
        collector.trace("2" * 32, 3)
        time.sleep(0.3)

    assert body["trace_id"] == PARENT_TRACE
    assert body["child"]["sampled"] is False
    # The outgoing call keeps the trace id and the flag that tells it not to sample.
    assert body["outgoing"].startswith(f"00-{PARENT_TRACE}-") and body["outgoing"].endswith("-00")
    assert [span for span in collector.spans if span["trace_id"] == PARENT_TRACE] == []


def test_the_sampler_of_rust_drops_a_request_with_no_parent(make_server_project, collector):
    project = make_server_project(api_module=APP)
    env = tracing_env(collector, OTEL_TRACES_SAMPLER="parentbased_traceidratio", OTEL_TRACES_SAMPLER_ARG="0")

    with project.start(env=env) as server:
        body = server.get("/trace/a").json()
        marker = f"00-{'3' * 32}-{PARENT_SPAN}-01"
        server.get("/trace/a", headers={"traceparent": marker})
        collector.trace("3" * 32, 3)
        time.sleep(0.3)

    assert body["child"]["sampled"] is False
    assert [span for span in collector.spans if span["trace_id"] == body["trace_id"]] == []


def test_the_exception_handler_gets_the_route_the_trace_and_the_user(make_server_project, collector):
    project = make_server_project(api_module=APP)

    with project.start(env=tracing_env(collector)) as server:
        headers = {"traceparent": SAMPLED_PARENT, "x-api-key": "secret"}
        assert server.get("/boom-auth/7", headers=headers).status_code == 500
        [span] = collector.trace(PARENT_TRACE, 1)
        time.sleep(0.2)
        stdout, _ = server.stop()

    reported = next(json.loads(line) for line in stdout.splitlines() if line.startswith('{"reported"'))
    assert reported == {
        "reported": "RuntimeError",
        "method": "GET",
        "path": "/boom-auth/7",
        "route": "/boom-auth/{item_id}",
        "trace_id": PARENT_TRACE,
        "user_id": "apikey:secret",
    }
    assert span["attributes"]["http.response.status_code"] == 500
    assert span["attributes"]["error.type"] == "500"
    assert span["status"] == 2


def test_tracing_works_when_the_opentelemetry_package_is_absent(make_server_project, collector):
    # A None in sys.modules makes `import opentelemetry` fail and `find_spec` give None.
    # The project folder is on the PYTHONPATH of the server, so Python runs this file first.
    blocker = {"sitecustomize.py": "import sys\nsys.modules['opentelemetry'] = None\n"}
    project = make_server_project(api_module=APP, extra_files=blocker)

    with project.start(env=tracing_env(collector)) as server:
        body = server.get("/trace/a", headers={"traceparent": SAMPLED_PARENT}).json()
        modules = server.get("/modules").json()
        [span] = collector.trace(PARENT_TRACE, 1)

    assert body == {"trace_id": PARENT_TRACE, "span_id": span["span_id"], "route": "/trace/{name}"}
    assert modules == {"opentelemetry": [], "freeman_otel": False}


def test_with_tracing_off_nothing_imports_opentelemetry(make_server_project):
    project = make_server_project(api_module=APP)

    with project.start() as server:
        body = server.get("/trace/a", headers={"traceparent": SAMPLED_PARENT}).json()
        modules = server.get("/modules").json()

    assert body == {"trace_id": None, "span_id": None, "route": "/trace/{name}"}
    assert modules == {"opentelemetry": [], "freeman_otel": False}
