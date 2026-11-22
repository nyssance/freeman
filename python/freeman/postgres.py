"""Native async PostgreSQL connections for the routes where the ORM is the limit.

The async API of the Django ORM runs each query on a thread, with a sync
driver below. That is right for most routes. A route that only reads, and
that many clients call at once, can skip the ORM and the thread:

    from freeman.postgres import connection

    @api.get("/feed")
    async def feed(limit: int = 20):
        async with connection() as conn:
            cursor = await conn.execute("SELECT id, title FROM post ORDER BY id DESC LIMIT %s", [limit])
            return await cursor.fetchall()

The pool reads ``DATABASES`` of the Django settings, so the route uses the
database of the models. Each worker thread runs its own event loop, and an
async connection belongs to one loop, so each loop has its own pool.

The size is in ``OPTIONS["async_pool"]`` of the database, for example
``{"min_size": 2, "max_size": 16}``. Count these connections with the others:
the database serves ``processes * workers * max_size`` of them at most.

Call ``close_pools()`` on shutdown, from the ``lifespan`` of the API. A test run calls
``close_all_pools()`` before it drops the test database.
"""

import asyncio
from contextlib import AbstractAsyncContextManager
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

try:
    from psycopg import AsyncConnection
    from psycopg.conninfo import make_conninfo
    from psycopg_pool import AsyncConnectionPool
except ImportError as e:  # pragma: no cover - the message is the behaviour
    raise ImportError('freeman.postgres needs psycopg and psycopg_pool. Install "freeman[pool]".') from e

__all__ = ("close_all_pools", "close_pools", "connection", "pool")

_DEFAULT_SIZE = {"min_size": 1, "max_size": 16}
# One pool for each (event loop, database alias).
_pools: dict[tuple[asyncio.AbstractEventLoop, str], AsyncConnectionPool] = {}
_CONNINFO_KEYS = {"NAME": "dbname", "USER": "user", "PASSWORD": "password", "HOST": "host", "PORT": "port"}


def _conninfo(alias: str) -> str:
    try:
        database = settings.DATABASES[alias]
    except KeyError:
        raise ImproperlyConfigured(f"DATABASES has no {alias!r}.") from None
    if "postgresql" not in database.get("ENGINE", ""):
        raise ImproperlyConfigured(
            f"freeman.postgres needs a PostgreSQL database. {alias!r} is {database.get('ENGINE')!r}."
        )
    params = {key: database[name] for name, key in _CONNINFO_KEYS.items() if database.get(name)}
    if settings.USE_TZ:
        # As the connections of Django: a timestamp comes back in the time zone of the database setting, or in UTC.
        params["options"] = f"-c timezone={database.get('TIME_ZONE') or 'UTC'}"
    return make_conninfo(**params)


async def pool(alias: str = "default") -> AsyncConnectionPool:
    """The pool of ``alias`` for the running event loop. The first call opens it."""
    loop = asyncio.get_running_loop()
    key = (loop, alias)
    found = _pools.get(key)
    if found is not None:
        return found
    size: dict[str, Any] = _DEFAULT_SIZE | settings.DATABASES.get(alias, {}).get("OPTIONS", {}).get("async_pool", {})
    created = AsyncConnectionPool(_conninfo(alias), open=False, **size)
    # A second task of this loop can get here while the first one opens the
    # pool. The first pool in the dict is the pool of the loop.
    found = _pools.setdefault(key, created)
    if found is created:
        await created.open()
    return found


class _Connection(AbstractAsyncContextManager[AsyncConnection]):
    def __init__(self, alias: str) -> None:
        self._alias = alias
        self._lease: AbstractAsyncContextManager[AsyncConnection] | None = None

    async def __aenter__(self) -> AsyncConnection:
        self._lease = (await pool(self._alias)).connection()
        return await self._lease.__aenter__()

    async def __aexit__(self, *exc_info: object) -> bool | None:
        assert self._lease is not None
        return await self._lease.__aexit__(*exc_info)


def connection(alias: str = "default") -> AbstractAsyncContextManager[AsyncConnection]:
    """A connection of the pool for one ``async with`` block. A block that ends with no error commits."""
    return _Connection(alias)


async def close_pools() -> None:
    """Close the pools of the running event loop."""
    loop = asyncio.get_running_loop()
    for key in [key for key in _pools if key[0] is loop]:
        await _pools.pop(key).close()


def close_all_pools(timeout: float = 10) -> None:
    """Close the pools of every event loop. Call it from a thread that runs no event loop.

    The event loop of a worker outlives a request, and a test client. A test
    run calls this before it drops the test database, which an open
    connection would keep.
    """
    for key in list(_pools):
        loop, _ = key
        found = _pools.pop(key, None)
        if found is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(found.close(), loop).result(timeout)
