"""The access log that Rust writes itself, on a real server.

With ``DJANGO_BOLT_ACCESS_LOG=stdout`` each request gives one JSON line on
stdout, and no request calls a Python logger for it.
"""

import json
import time

import pytest

from .apps import app_module

pytestmark = pytest.mark.server_integration

ENV = {"DJANGO_BOLT_ACCESS_LOG": "stdout"}


def _lines(stdout: str) -> list[dict]:
    return [json.loads(line) for line in stdout.splitlines() if line.startswith('{"ts":')]


def test_each_request_writes_one_json_line(make_server_project):
    project = make_server_project(api_module=app_module("hello"))

    with project.start(env=ENV) as server:
        before = time.time()
        assert server.get("/health").status_code == 200
        assert server.get("/hello").status_code == 200
        assert server.get("/no-such-path").status_code == 404
        assert server.get('/quoted"path').status_code == 404
        # The writer rests for a moment after each batch.
        time.sleep(0.2)
        stdout, _ = server.stop()

    lines = _lines(stdout)
    paths = [line["path"] for line in lines]
    assert "/no-such-path" in paths
    assert '/quoted"path' in paths or "/quoted%22path" in paths
    missing = next(line for line in lines if line["path"] == "/no-such-path")
    assert set(missing) == {"ts", "method", "path", "status", "ms"}
    assert (missing["method"], missing["status"]) == ("GET", 404)
    assert before - 1 <= missing["ts"] <= time.time() + 1
    assert missing["ms"] >= 0
    assert any(line["path"] == "/hello" and line["status"] == 200 for line in lines)
    # The filter of the route still applies: /health is a path that writes no line.
    assert "/health" not in paths


def test_without_the_variable_stdout_has_no_json_line(make_server_project):
    project = make_server_project(api_module=app_module("hello"))

    with project.start() as server:
        assert server.get("/no-such-path").status_code == 404
        time.sleep(0.2)
        stdout, _ = server.stop()

    assert _lines(stdout) == []
