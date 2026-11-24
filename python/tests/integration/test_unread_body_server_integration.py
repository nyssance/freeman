"""A request body that Bolt does not read does not cost the connection.

Rust answers a 401 or a 429 before it reads the body. Bolt then reads and
drops a small body, so the client sends its next request on the same
connection. A body above 64 KiB is not read, and the connection closes.
TestClient has no connections, so these tests send raw bytes to runbolt.
"""

from __future__ import annotations

import socket
import time

import pytest

from .apps import app_module

pytestmark = pytest.mark.server_integration

_BODY = b'{"data":"x"}'
_DISCARD_LIMIT = 64 * 1024


def _read_response(sock: socket.socket, buffer: bytearray) -> tuple[bytes, dict[bytes, bytes], bytes]:
    """Read one response with a Content-Length body. ``buffer`` keeps bytes after it."""
    while b"\r\n\r\n" not in buffer:
        chunk = sock.recv(65536)
        if not chunk:
            raise ConnectionError(f"the connection closed before a response: {bytes(buffer)!r}")
        buffer += chunk
    head, _, rest = bytes(buffer).partition(b"\r\n\r\n")
    status, *lines = head.split(b"\r\n")
    headers = {name.strip().lower(): value.strip() for name, _, value in (line.partition(b":") for line in lines)}
    length = int(headers.get(b"content-length", b"0"))
    while len(rest) < length:
        chunk = sock.recv(65536)
        if not chunk:
            raise ConnectionError("the connection closed in the body of a response")
        rest += chunk
    buffer[:] = rest[length:]
    return status, headers, rest[:length]


def _post(host: bytes, path: bytes, body: bytes, extra: bytes = b"") -> bytes:
    return b"POST %s HTTP/1.1\r\nHost: %s\r\nContent-Type: application/json\r\nContent-Length: %d\r\n%s\r\n%s" % (
        path,
        host,
        len(body),
        extra,
        body,
    )


def _health(host: bytes) -> bytes:
    return b"GET /health HTTP/1.1\r\nHost: %s\r\n\r\n" % host


def _closes(sock: socket.socket, buffer: bytearray) -> bool:
    """Whether the server closes the connection, with no more bytes."""
    if buffer:
        return False
    try:
        return sock.recv(1) == b""
    except ConnectionResetError:
        return True


@pytest.mark.parametrize(
    ("path", "first", "status"),
    [(b"/protected", 0, b"401"), (b"/limited", 1, b"429"), (b"/no-body", 0, b"200")],
    ids=["401", "429", "handler-without-body"],
)
def test_a_small_unread_body_keeps_the_connection(make_server_project, path, first, status):
    """A small body that no path reads is discarded, and the connection stays open."""
    project = make_server_project(api_module=app_module("unread_body"))
    with project.start() as server:
        host = server.host.encode()
        with socket.create_connection((server.host, server.port), timeout=5) as sock:
            buffer = bytearray()
            # `/limited` lets the first request through. The second gets the 429.
            for _ in range(first):
                sock.sendall(_post(host, path, _BODY))
                assert _read_response(sock, buffer)[0].split()[1] == b"200"
            sock.sendall(_post(host, path, _BODY))
            rejected, headers, _ = _read_response(sock, buffer)
            sock.sendall(_health(host))
            health, _, body = _read_response(sock, buffer)
    assert rejected.split()[1] == status, rejected
    assert headers.get(b"connection") != b"close", headers
    assert health.split()[1] == b"200", health
    assert body == b'{"status":"ok"}'


def test_a_body_that_arrives_after_the_response_is_discarded(make_server_project):
    """The body may arrive after the 401. Bolt waits for it, under a second."""
    project = make_server_project(api_module=app_module("unread_body"))
    with project.start() as server:
        host = server.host.encode()
        with socket.create_connection((server.host, server.port), timeout=5) as sock:
            buffer = bytearray()
            request = _post(host, b"/protected", _BODY)
            sock.sendall(request[: -len(_BODY)])
            time.sleep(0.2)
            sock.sendall(request[-len(_BODY) :])
            rejected, headers, _ = _read_response(sock, buffer)
            sock.sendall(_health(host))
            health, _, _ = _read_response(sock, buffer)
    assert rejected.split()[1] == b"401", rejected
    assert headers.get(b"connection") != b"close", headers
    assert health.split()[1] == b"200", health


def test_a_body_that_does_not_arrive_closes_the_connection(make_server_project):
    """Bolt waits at most a second for the body, then closes the connection."""
    project = make_server_project(api_module=app_module("unread_body"))
    with project.start() as server:
        host = server.host.encode()
        with socket.create_connection((server.host, server.port), timeout=5) as sock:
            buffer = bytearray()
            sock.sendall(_post(host, b"/protected", _BODY)[: -len(_BODY)])
            started = time.monotonic()
            rejected, headers, _ = _read_response(sock, buffer)
            closed = _closes(sock, buffer)
            elapsed = time.monotonic() - started
    assert rejected.split()[1] == b"401", rejected
    assert headers.get(b"connection") == b"close", headers
    assert closed
    assert elapsed < 3, elapsed


def test_a_large_unread_body_closes_the_connection(make_server_project):
    """A body above 64 KiB is not read: the 401 comes at once, and the connection closes."""
    project = make_server_project(api_module=app_module("unread_body"))
    with project.start() as server:
        host = server.host.encode()
        with socket.create_connection((server.host, server.port), timeout=5) as sock:
            buffer = bytearray()
            # Only the head is sent. Bolt answers without waiting for the body.
            head = _post(host, b"/protected", b"x" * (_DISCARD_LIMIT + 1))[: -(_DISCARD_LIMIT + 1)]
            sock.sendall(head)
            started = time.monotonic()
            rejected, headers, _ = _read_response(sock, buffer)
            # Up to the response only: the wait for the close is not part of it.
            elapsed = time.monotonic() - started
            closed = _closes(sock, buffer)
    assert rejected.split()[1] == b"401", rejected
    assert headers.get(b"connection") == b"close", headers
    assert closed
    assert elapsed < 1, elapsed


def test_expect_continue_with_an_unread_body_keeps_the_connection(make_server_project):
    """Actix sends `100 Continue` before the handler runs, so the client sends the body."""
    project = make_server_project(api_module=app_module("unread_body"))
    with project.start() as server:
        host = server.host.encode()
        with socket.create_connection((server.host, server.port), timeout=5) as sock:
            buffer = bytearray()
            sock.sendall(_post(host, b"/protected", _BODY, b"Expect: 100-continue\r\n")[: -len(_BODY)])
            interim, _, _ = _read_response(sock, buffer)
            sock.sendall(_BODY)
            rejected, headers, _ = _read_response(sock, buffer)
            sock.sendall(_health(host))
            health, _, _ = _read_response(sock, buffer)
    assert interim.split()[1] == b"100", interim
    assert rejected.split()[1] == b"401", rejected
    assert headers.get(b"connection") != b"close", headers
    assert health.split()[1] == b"200", health


def test_a_chunked_unread_body_keeps_the_connection(make_server_project):
    """Actix itself reads and drops a chunked body that nothing reads."""
    project = make_server_project(api_module=app_module("unread_body"))
    with project.start() as server:
        host = server.host.encode()
        with socket.create_connection((server.host, server.port), timeout=5) as sock:
            buffer = bytearray()
            chunked = b"%x\r\n%s\r\n0\r\n\r\n" % (len(_BODY), _BODY)
            sock.sendall(
                b"POST /protected HTTP/1.1\r\nHost: %s\r\nContent-Type: application/json\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n%s" % (host, chunked)
            )
            rejected, headers, _ = _read_response(sock, buffer)
            sock.sendall(_health(host))
            health, _, _ = _read_response(sock, buffer)
    assert rejected.split()[1] == b"401", rejected
    assert headers.get(b"connection") != b"close", headers
    assert health.split()[1] == b"200", health
