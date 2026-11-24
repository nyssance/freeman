---
icon: lucide/zap
hide:
  - navigation
  - toc
---

<div class="bolt-hero" markdown>

<img class="bolt-hero__logo" src="images/bolt-logo.png" alt="Freeman logo" width="140" height="127">

<p class="bolt-hero__eyebrow">Freeman</p>

# An HTTP engine for Python APIs on Django { .bolt-hero__title }

<p class="bolt-hero__tagline">
Rust does the routing, the checks and the limits.
Your handler is plain Python, with Pydantic models in and out.
Your models, Admin and middleware keep working.
</p>

<div class="bolt-hero__actions">
<a class="md-button md-button--primary" href="getting-started/quickstart/">Get started</a>
<a class="md-button" href="benchmarks/">See benchmarks</a>
<a class="md-button" href="https://github.com/nyssance/freeman">GitHub</a>
</div>

<div class="bolt-hero__install">
<code>pip install freeman</code>
</div>

<div class="bolt-stats">
<div class="bolt-stat"><span class="bolt-stat__value">125k</span><span class="bolt-stat__label">requests / second, one process, empty route</span></div>
<div class="bolt-stat"><span class="bolt-stat__value">0</span><span class="bolt-stat__label">Python before your handler: routing, auth, CORS, rate limits run in Rust</span></div>
<div class="bolt-stat"><span class="bolt-stat__value">1</span><span class="bolt-stat__label">command: <code>runbolt</code> replaces gunicorn, uvicorn and the process manager</span></div>
</div>
<p class="bolt-stats__note">125k measured on Freeman with one process, one worker thread and 100 connections. See the <a href="https://github.com/nyssance/freeman#readme">README</a>.</p>

</div>

<div class="bolt-example" markdown>

## Your first endpoint

```python title="api.py"
from freeman import BoltAPI

api = BoltAPI()

@api.get("/users/{user_id}")
async def get_user(user_id: int):
    return {"user_id": user_id}
```

```bash
python manage.py runbolt --dev
```

Add `freeman` to `INSTALLED_APPS`, run the command above, and open `http://localhost:8000/docs` for interactive API docs. Follow the [Quick Start](getting-started/quickstart.md) for the full walkthrough.

</div>

## Why Freeman { .bolt-section__title }

<div class="bolt-grid" markdown>

<div class="bolt-card" markdown>
### :lucide-rocket: Rust speed, Python code
Actix Web and Tokio serve every request. Auth, guards, CORS, rate limiting and compression run in Rust before the GIL.
</div>

<div class="bolt-card" markdown>
### :lucide-shield-check: Typed validation
Parameters and bodies are validated from type hints. One Pydantic model gives you validation, serialization and OpenAPI.
</div>

<div class="bolt-card" markdown>
### :lucide-database: All of Django
Return a QuerySet and it is evaluated and serialized for you. Models, migrations, signals, sessions and third-party apps work unchanged.
</div>

<div class="bolt-card" markdown>
### :lucide-lock: Authentication built in
JWT, API key and session auth, with guards such as `IsAuthenticated` and `Requires`. Evaluated natively in Rust.
</div>

<div class="bolt-card" markdown>
### :lucide-book-open: OpenAPI by default
Swagger UI, Redoc, Scalar, RapiDoc and Stoplight Elements are served at `/docs`. Schema comes from your type hints.
</div>

<div class="bolt-card" markdown>
### :lucide-radio: Streaming, WebSocket, MCP
Server-Sent Events, WebSocket handlers, ASGI mounts and an MCP server for LLM clients via `bolt-mcp` (a build with the `mcp` Cargo feature).
</div>

</div>

## More examples { .bolt-section__title }

=== "Validation"

    ```python
    from pydantic import BaseModel

    class CreateUser(BaseModel):
        username: str
        email: str

    @api.post("/users")
    async def create_user(user: CreateUser):
        return {"username": user.username}
    ```

=== "Django ORM"

    ```python
    from myapp.models import User

    @api.get("/users")
    async def list_users():
        return User.objects.all()[:20]
    ```

=== "Authentication"

    ```python
    from freeman.auth import JWTAuthentication, IsAuthenticated

    @api.get("/profile", auth=[JWTAuthentication()], guards=[IsAuthenticated()])
    async def profile(request):
        return {"user_id": request.user.id}
    ```

=== "MCP server"

    ```python
    from bolt_mcp import MCP

    mcp = MCP("my-server")

    @mcp.tool
    async def add(a: int, b: int) -> dict:
        return {"sum": a + b}

    api.mount_mcp(mcp)
    ```

## Where to go next { .bolt-section__title }

<div class="bolt-grid bolt-grid--links" markdown>

<a class="bolt-card bolt-card--link" href="getting-started/quickstart/" markdown>
**Quick Start**
Build and run your first API in a few minutes.
</a>

<a class="bolt-card bolt-card--link" href="getting-started/deployment/" markdown>
**Deployment**
Run multiple processes and put Freeman behind a proxy.
</a>

<a class="bolt-card bolt-card--link" href="architecture/" markdown>
**How it works**
Request flow from Rust to your handler and back.
</a>

<a class="bolt-card bolt-card--link" href="comparison/" markdown>
**Comparison**
Freeman vs Django Ninja, DRF, FastAPI and Litestar.
</a>

<a class="bolt-card bolt-card--link" href="faq/" markdown>
**FAQ**
Production readiness, WSGI, gunicorn, admin and ORM support.
</a>

<a class="bolt-card bolt-card--link" href="ref/api/" markdown>
**API Reference**
`BoltAPI`, responses, exceptions, auth and settings.
</a>

</div>
