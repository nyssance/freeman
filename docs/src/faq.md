---
icon: lucide/circle-help
description: Direct answers to the most common questions about Freeman — production readiness, Django Admin and ORM support, WSGI vs async, gunicorn, and how it compares to Django Ninja, DRF and FastAPI.
---

# Frequently asked questions

Short, direct answers. Each answer links to the page with the full detail.

## Is Freeman a stripped-down version of Django?

**No.** Freeman removes nothing from Django. It *replaces the HTTP server and the API layer* — the part that WSGI/ASGI servers, DRF, or Django Ninja would otherwise handle — with a Rust server (Actix Web) and a typed, msgspec-based routing layer. Everything else is the same Django you already run:

| Django feature | In Freeman | Notes |
| --- | --- | --- |
| Django ORM | ✅ Full | Sync and async (`aget`, `afilter`, …); return a `QuerySet` from an async handler and Freeman evaluates it on a bounded thread pool. [Async ORM](topics/async-orm.md) |
| Django Admin | ✅ Full | Auto-mounted from `INSTALLED_APPS`; served by `runbolt`, no second server. |
| Django middleware | ✅ Full | `BoltAPI(django_middleware=True)` runs your `settings.MIDDLEWARE` stack (sessions, CSRF, auth, messages, CSP…) on API routes. [Middleware](topics/middleware.md#django-middleware-integration) |
| Django auth / sessions | ✅ Full | JWT and API-key backends are built in (validated in Rust); with `django_middleware=True`, Django's `SessionMiddleware` + `AuthenticationMiddleware` populate `request.user` / `request.session` exactly as in Django. [Authentication](topics/authentication.md) |
| Django signals | ✅ Full | `request_started`, `request_finished`, and model signals fire. [Signals](topics/signals.md) |
| Django settings / apps | ✅ Full | Same `settings.py`, `INSTALLED_APPS`, `manage.py`. |
| Static & media files | ✅ Full | Served natively from Rust — no WhiteNoise. [Static files](topics/static-files.md) |
| Third-party Django apps | ✅ Full | Anything that plugs into Django's ORM, admin, or middleware works. |
| Templates / server-rendered views | ✅ | Existing Django URLconf views run through the ASGI mount alongside API routes. [ASGI mounts](topics/asgi-mounts.md) |

The performance comes from *where* work runs (routing, auth, guards, CORS, rate limiting, compression in Rust without the GIL) — not from removing features. Django middleware is opt-in per API (`BoltAPI(django_middleware=True)`); the [example project](https://github.com/nyssance/freeman/tree/main/python/example) used for the published benchmarks runs the admin and sessions but not `settings.MIDDLEWARE` on Freeman routes.

## Does Freeman use WSGI? Is it synchronous?

**No WSGI, no ASGI server, not sync-only.** Freeman ships its own HTTP server: [Actix Web](https://actix.rs/) on the Tokio runtime, called from `python manage.py runbolt`. Handlers are `async def` (sync `def` is also supported). Requests flow HTTP → Rust → your Python handler via PyO3, with no WSGI or ASGI protocol layer in between. See [How Freeman works](architecture.md).

## Do I need gunicorn or uvicorn?

**No.** `runbolt` *is* the production server. It runs multiple processes with `SO_REUSEPORT` kernel load balancing on Linux, recycles workers by RSS or lifetime, respawns on crash, and drains WebSockets on shutdown. See [Deployment](getting-started/deployment.md).

```bash
python manage.py runbolt --host 0.0.0.0 --port 8000 --processes 4
```

## Is Freeman production ready?

Freeman is pre-1.0. It has a full test suite covering routing, auth, middleware, ORM, streaming, WebSockets, and multi-process behavior. Minor releases can still change APIs; the [changelog](https://github.com/nyssance/freeman/blob/main/CHANGELOG.md) lists every breaking change.

## How fast is Freeman?

Upstream django-bolt 0.10 measured these numbers on a 12-core desktop (Ryzen 5 5600G, 8 processes, `C=100`, loopback): **~311,000 requests/second** on a JSON hello-world, ~187,000 req/s for a 10 KB JSON response, and ~21,000–27,000 req/s for a 10-row Django ORM query on SQLite. Freeman has not repeated them. The [README](https://github.com/nyssance/freeman#readme) has the numbers measured on Freeman. The upstream conditions and reproduction commands are on the [Benchmarks](benchmarks.md) page.

## How does Freeman compare to Django Ninja and Django REST Framework?

All three run inside a Django project. DRF and Django Ninja are Python view layers that still need gunicorn/uvicorn; Freeman replaces the server with Rust and moves auth, guards, and middleware out of Python. Freeman's syntax is closest to Django Ninja/FastAPI (type-hinted function handlers) and it also provides DRF-style `ViewSet`/`ModelViewSet` classes. Full grid: [Comparison](comparison.md).

## How does Freeman compare to FastAPI and Litestar?

FastAPI and Litestar are standalone ASGI frameworks — you bring your own ORM, admin, auth, and migrations. Freeman gives you the same type-hinted handler style plus the whole Django stack, and serves requests faster because the HTTP layer is Rust. See [Comparison](comparison.md).

## Can I migrate from DRF or Django Ninja incrementally?

**Yes.** Add `freeman` to `INSTALLED_APPS`, create an `api.py`, and move one endpoint at a time. Existing Django URLconf views (including DRF/Ninja routes) keep working through the ASGI mount while you migrate.

## Does Freeman support WebSockets, SSE, and streaming?

Yes — [WebSockets](topics/websocket.md), [Server-Sent Events](topics/sse.md), and [streaming responses](topics/responses.md) are built in and served by the Rust layer.

## Does Freeman generate OpenAPI docs?

Yes. Swagger UI, ReDoc, Scalar, RapiDoc, and Stoplight Elements are served at `/docs` by default. See [OpenAPI](topics/openapi.md).

## Which Python and Django versions are supported?

Python 3.15 (CPython) and Django 6.1 only. Wheels are built for Linux x86_64, Linux arm64 and macOS arm64. A project on an older Python or Django can use django-bolt.

## Why does `runbolt` hang on macOS when it connects to PostgreSQL?

This affects local development on macOS with libpq (psycopg). A forked `runbolt` worker can hang, or stop with a segmentation fault, when it opens a PostgreSQL connection. This occurs with and without Django's connection pool. Workers fork with `--processes 2` or more, and with a worker recycling option (`--max-rss`, `--workers-lifetime`, `--respawn-failed-workers`). When libpq is built with GSSAPI support, the libpq option `gssencmode` is `prefer` by default. With `prefer`, libpq calls the macOS Kerberos (GSS) framework on each TCP connection. That framework is not safe to use after `fork()`. A libpq build without GSSAPI support does not call the framework, so this cause does not apply to it.

To prevent this, disable GSSAPI encryption. Set the environment variable before you start the server:

```bash
PGGSSENCMODE=disable python manage.py runbolt --processes 2
```

Alternatively, set the connection option in `DATABASES`:

```python
# settings.py
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        # ...
        "OPTIONS": {"gssencmode": "disable"},
    }
}
```

This setting turns off GSSAPI encryption only. If the connection must be encrypted, use TLS with `sslmode` (for example `verify-full`). A server that accepts only GSSAPI-encrypted connections refuses the connection with this setting. Linux does not need this setting.

## Where can I get help?

[GitHub Issues](https://github.com/nyssance/freeman/issues)

<script type="application/ld+json">
{
 "@context": "https://schema.org",
 "@type": "FAQPage",
 "mainEntity": [
  {
   "@type": "Question",
   "name": "Is Freeman a stripped-down version of Django?",
   "acceptedAnswer": {
    "@type": "Answer",
    "text": "No. Freeman removes nothing from Django. It replaces the HTTP server and the API layer — the part that WSGI/ASGI servers, DRF, or Django Ninja would otherwise handle — with a Rust server (Actix Web) and a typed, msgspec-based routing layer. Everything else is the same Django you already run:"
   }
  },
  {
   "@type": "Question",
   "name": "Does Freeman use WSGI? Is it synchronous?",
   "acceptedAnswer": {
    "@type": "Answer",
    "text": "No WSGI, no ASGI server, not sync-only. Freeman ships its own HTTP server: Actix Web on the Tokio runtime, called from python manage.py runbolt. Handlers are async def (sync def is also supported). Requests flow HTTP → Rust → your Python handler via PyO3, with no WSGI or ASGI protocol layer in between. See How Freeman works."
   }
  },
  {
   "@type": "Question",
   "name": "Do I need gunicorn or uvicorn?",
   "acceptedAnswer": {
    "@type": "Answer",
    "text": "No. runbolt is the production server. It runs multiple processes with SO_REUSEPORT kernel load balancing on Linux, recycles workers by RSS or lifetime, respawns on crash, and drains WebSockets on shutdown. See Deployment."
   }
  },
  {
   "@type": "Question",
   "name": "Is Freeman production ready?",
   "acceptedAnswer": {
    "@type": "Answer",
    "text": "Freeman is pre-1.0. It has a full test suite covering routing, auth, middleware, ORM, streaming, WebSockets, and multi-process behavior. Minor releases can still change APIs; the changelog lists every breaking change."
   }
  },
  {
   "@type": "Question",
   "name": "How fast is Freeman?",
   "acceptedAnswer": {
    "@type": "Answer",
    "text": "Upstream django-bolt 0.10 measured these numbers on a 12-core desktop (Ryzen 5 5600G, 8 processes, C=100, loopback): ~311,000 requests/second on a JSON hello-world, ~187,000 req/s for a 10 KB JSON response, and ~21,000–27,000 req/s for a 10-row Django ORM query on SQLite. Freeman has not repeated them. The README has the numbers measured on Freeman."
   }
  },
  {
   "@type": "Question",
   "name": "How does Freeman compare to Django Ninja and Django REST Framework?",
   "acceptedAnswer": {
    "@type": "Answer",
    "text": "All three run inside a Django project. DRF and Django Ninja are Python view layers that still need gunicorn/uvicorn; Freeman replaces the server with Rust and moves auth, guards, and middleware out of Python. Freeman's syntax is closest to Django Ninja/FastAPI (type-hinted function handlers) and it also provides DRF-style ViewSet/ModelViewSet classes. Full grid: Comparison."
   }
  },
  {
   "@type": "Question",
   "name": "How does Freeman compare to FastAPI and Litestar?",
   "acceptedAnswer": {
    "@type": "Answer",
    "text": "FastAPI and Litestar are standalone ASGI frameworks — you bring your own ORM, admin, auth, and migrations. Freeman gives you the same type-hinted handler style plus the whole Django stack, and serves requests faster because the HTTP layer is Rust. See Comparison."
   }
  },
  {
   "@type": "Question",
   "name": "Can I migrate from DRF or Django Ninja incrementally?",
   "acceptedAnswer": {
    "@type": "Answer",
    "text": "Yes. Add freeman to INSTALLED_APPS, create an api.py, and move one endpoint at a time. Existing Django URLconf views (including DRF/Ninja routes) keep working through the ASGI mount while you migrate."
   }
  },
  {
   "@type": "Question",
   "name": "Does Freeman support WebSockets, SSE, and streaming?",
   "acceptedAnswer": {
    "@type": "Answer",
    "text": "Yes — WebSockets, Server-Sent Events, and streaming responses are built in and served by the Rust layer."
   }
  },
  {
   "@type": "Question",
   "name": "Does Freeman generate OpenAPI docs?",
   "acceptedAnswer": {
    "@type": "Answer",
    "text": "Yes. Swagger UI, ReDoc, Scalar, RapiDoc, and Stoplight Elements are served at /docs by default. See OpenAPI."
   }
  },
  {
   "@type": "Question",
   "name": "Which Python and Django versions are supported?",
   "acceptedAnswer": {
    "@type": "Answer",
    "text": "Python 3.15 (CPython) and Django 6.1 only. Wheels are built for Linux x86_64, Linux arm64 and macOS arm64. A project on an older Python or Django can use django-bolt."
   }
  },
  {
   "@type": "Question",
   "name": "Why does runbolt hang on macOS when it connects to PostgreSQL?",
   "acceptedAnswer": {
    "@type": "Answer",
    "text": "This affects local development on macOS with libpq (psycopg). A forked runbolt worker can hang, or stop with a segmentation fault, when it opens a PostgreSQL connection. This occurs with and without Django's connection pool. Workers fork with --processes 2 or more, and with a worker recycling option. When libpq is built with GSSAPI support, gssencmode is prefer by default. Then libpq calls the macOS Kerberos (GSS) framework, which is not safe to use after fork(). Set PGGSSENCMODE=disable, or set \"gssencmode\": \"disable\" in DATABASES[\"default\"][\"OPTIONS\"]. This turns off GSSAPI encryption only; use TLS with sslmode if the connection must be encrypted. Linux does not need this setting."
   }
  }
 ]
}
</script>
