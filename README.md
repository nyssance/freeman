# Freeman

Freeman is an HTTP engine for Python APIs on Django. Rust receives the request,
routes it, checks the parameters, the credential, the guards and the rate limit,
and then calls your handler. The handler is plain Python. It can take and return
Pydantic models.

Freeman is not a full framework. It has no opinion about the layout of a
project, about models or about views. For that layer, use
[railguns](https://github.com/nyssance/railguns). Most projects should start
there.

Freeman is not an ASGI or WSGI application. It does not run behind uvicorn or
gunicorn. `python manage.py runbolt` is the server.

## Design

Rust ([Actix Web](https://actix.rs/), [Tokio](https://tokio.rs/) and
[PyO3](https://pyo3.rs/)) does the routing, the path and query parameters,
authentication (JWT, API key, session), guards, CORS, rate limits,
compression, the access log, metrics and tracing. Python runs the handler, the
body model and the result. Rust refuses a request with a bad parameter, a
failed guard or too many requests before the handler runs.

There is no ASGI layer. With ASGI, each request enters Python first, and the
routing and the checks run there. Freeman keeps them in Rust, without the GIL.
The Django ORM, admin, auth and middleware still work. A route with Django
middleware runs it on a thread of its own.

## Install

```bash
pip install freeman
```

Freeman needs Python 3.15 and Django 6.1. It supports no older versions.
The wheels are for Linux x86_64, Linux arm64 and macOS arm64.

## Example

Add `"freeman"` to `INSTALLED_APPS`, and put an `api.py` next to `settings.py`:

```python
# mysite/api.py
from pydantic import BaseModel, Field

from freeman import BoltAPI
from freeman.error_handlers import code_detail
from freeman.exceptions import NotFound

api = BoltAPI(error_formatter=code_detail)


class ItemIn(BaseModel):
    name: str = Field(min_length=1)
    price: float = Field(ge=0)


class Item(BaseModel):
    id: int
    name: str
    price: float


ITEMS: dict[int, Item] = {}


@api.get("/hello/{name}")
async def hello(name: str) -> dict:
    return {"message": f"Hello, {name}!"}


@api.post("/items", status_code=201)
async def create_item(item: ItemIn) -> Item:
    created = Item(id=len(ITEMS) + 1, **item.model_dump())
    ITEMS[created.id] = created
    return created


@api.get("/items/{item_id}")
async def get_item(item_id: int) -> Item:
    if item_id not in ITEMS:
        raise NotFound(f"Item {item_id} does not exist.")
    return ITEMS[item_id]
```

Start the server. `runbolt` finds `api.py` by itself:

```bash
python manage.py runbolt --dev
```

```console
$ curl localhost:8000/hello/world
{"message":"Hello, world!"}
$ curl -X POST localhost:8000/items -d '{"name": "Pen", "price": 1.5}'
{"id":1,"name":"Pen","price":1.5}
$ curl localhost:8000/items/7
{"code":"not_found","detail":"Item 7 does not exist."}
$ curl -X POST localhost:8000/items -d '{"name": "Pen", "price": -1}'
{"code":"validation","detail":"Request validation failed","fields":[{"loc":["body","price"],"msg":"Input should be greater than or equal to 0","type":"greater_than_equal"}]}
```

The OpenAPI document is at `/docs/openapi.json`, and Swagger UI is at `/docs`.

## Pydantic bodies and results

A parameter whose type is a Pydantic model is the request body. Pydantic
validates it from the raw bytes with `validate_json`. A body that does not
validate gets a 422 with every error of the body, with the same `loc`, `msg`
and `type` keys as the other 422 responses.

A return type that is a Pydantic model is the response. Pydantic writes it with
`dump_json`. A handler can also return a dict, an object with attributes or a
QuerySet. The value goes through the model first, so only the fields of the
model reach the client. In the OpenAPI document, a request body has the
validation schema of the model, and a response has its serialization schema.

msgspec structs and the `Serializer` class still work.

## Errors

The errors of Rust, of the body validation, of `HTTPException` and of an
unhandled exception all go through one `error_formatter`. With `code_detail`,
each error has the same shape: `{"code", "detail", "fields"}`.

- `code` is a stable name, such as `not_authenticated`, `not_found` or `validation`.
- `detail` is always a string.
- `fields` is present only when the error has field errors. Each part of `loc` is a string.

A handler can name its own code:

```python
from freeman.exceptions import HTTPException


def reserve(stock: int) -> None:
    if stock == 0:
        raise HTTPException(409, detail={"code": "out_of_stock", "detail": "No stock left."})
```

## Access log, metrics and tracing

Freeman does not go through ASGI or the request handler of Django. Thus the
Django and ASGI integrations of Prometheus, OpenTelemetry and Sentry do not see
the requests. Rust writes the access log, the metrics and the server spans
itself. Each part is off by default. An environment variable turns it on:

| Variable | Gives |
|---|---|
| `DJANGO_BOLT_ACCESS_LOG=stdout` | One JSON line for each request on stdout, with no Python logger |
| `DJANGO_BOLT_METRICS=/metrics` | Prometheus metrics by method, route template and status |
| `DJANGO_BOLT_TRACING=1` | W3C trace context, and server spans over OTLP/HTTP |

With all three off, the request path does not change. Measured on one process,
one worker thread, an empty route and 100 connections:

| Setting | Requests per second | Part of "all off" |
|---|---:|---:|
| All off | 125,327 | 100% |
| Access log (`stdout`) | 121,598 | 97% |
| Metrics | 123,944 | 99% |
| Tracing, sampler at 0 | 120,822 | 96% |
| Tracing, each request sampled | 110,597 | 88% |
| All three, sampler at 0 | 115,781 | 92% |
| All three, each request sampled | 107,437 | 86% |

For comparison, an access log through a Python logger at `INFO` gave 35,000
requests per second. See [Metrics and Tracing](docs/src/topics/observability.md)
for the method and the details.

## Native PostgreSQL connections

The async API of the Django ORM runs each query on a thread. For a route that
only reads and that many clients call at once, `freeman.postgres` gives psycopg
async connections from a pool. It reads `DATABASES`, so it uses the database of
the models. Install it with `pip install "freeman[pool]"`.

```python
from freeman import BoltAPI
from freeman.postgres import connection

api = BoltAPI()


@api.get("/feed")
async def feed(limit: int = 20) -> list:
    async with connection() as conn:
        cursor = await conn.execute("SELECT id, title FROM post ORDER BY id DESC LIMIT %s", [limit])
        return await cursor.fetchall()
```

Measured on one process with a trivial query: 13,200 requests per second
through `freeman.postgres`, and 6,500 through the async ORM.

## MCP

The MCP server support (`python/bolt-mcp`) is behind the `mcp` Cargo feature,
which is off by default. The wheels on PyPI do not have it. See
[MCP Server](docs/src/topics/mcp.md) to build with it.

## Names

The distribution and the import package are `freeman`. Some names are older
than the project, and they are part of its API: `BoltAPI` and the other class
names, the `runbolt` command, the `BOLT_*` settings and the `DJANGO_BOLT_*`
environment variables. They keep these names.

## Relation to django-bolt and railguns

Freeman began as a fork of [django-bolt](https://github.com/dj-bolt/django-bolt)
by Farhan Ali Raza and its contributors (MIT), at commit `23c1990`. The Rust
core is their work. Freeman is now an independent project. It does not track
upstream. A project that needs an older Python or Django should use
django-bolt.

[railguns](https://github.com/nyssance/railguns) is built on Freeman and is its
main user. It is the opinionated layer above the engine.

## Development

```bash
git clone https://github.com/nyssance/freeman.git && cd freeman
uv sync
uv run maturin develop --release
uv run pytest python/tests -n auto
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [CHANGELOG.md](CHANGELOG.md).

## License

MIT. See [LICENSE](LICENSE).
