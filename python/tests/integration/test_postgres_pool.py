"""``freeman.postgres``: native async connections against a real PostgreSQL database.

Set ``DJANGO_BOLT_TEST_POSTGRES_DSN`` to run them. Without the variable, they skip.
"""

import asyncio
import time

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import connections

from freeman import BoltAPI
from freeman.testing import TestClient

pytestmark = pytest.mark.postgres

ALIAS = "native"
QUERIES = 20
SLEEP = 0.1


@pytest.fixture
def native(postgres_database, monkeypatch):
    """Add the database ``native`` to the settings: the PostgreSQL test database, with a pool for every query."""
    database = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": postgres_database["dbname"],
        "USER": postgres_database.get("user", ""),
        "PASSWORD": postgres_database.get("password", ""),
        "HOST": postgres_database.get("host", ""),
        "PORT": postgres_database.get("port", ""),
        "OPTIONS": {"async_pool": {"min_size": QUERIES, "max_size": QUERIES}},
    }
    # Django reads every database of the settings, so this one gets the defaults of each key.
    configured = connections.configure_settings({"default": connections.settings["default"], ALIAS: database})
    monkeypatch.setitem(connections.settings, ALIAS, configured[ALIAS])
    assert ALIAS in settings.DATABASES
    return database


@pytest.fixture
def client(native):
    # Imported here: the module needs psycopg, and with it libpq.
    from freeman import postgres  # noqa: PLC0415

    api = BoltAPI()

    @api.get("/one")
    async def one(value: int):
        async with postgres.connection(ALIAS) as conn:
            cursor = await conn.execute("SELECT %s::int + 1", [value])
            return {"value": (await cursor.fetchone())[0]}

    @api.get("/many")
    async def many():
        async def slow() -> int:
            async with postgres.connection(ALIAS) as conn:
                cursor = await conn.execute("SELECT 1 FROM pg_sleep(%s)", [SLEEP])
                return (await cursor.fetchone())[0]

        started = time.perf_counter()
        results = await asyncio.gather(*(slow() for _ in range(QUERIES)))
        return {"sum": sum(results), "seconds": time.perf_counter() - started}

    @api.get("/write")
    async def write():
        async with postgres.connection(ALIAS) as conn:
            await conn.execute("CREATE TABLE IF NOT EXISTS note (body text)")
            await conn.execute("INSERT INTO note VALUES ('kept')")
        async with postgres.connection(ALIAS) as conn:
            cursor = await conn.execute("SELECT count(*) FROM note")
            return {"rows": (await cursor.fetchone())[0]}

    @api.get("/close")
    async def close():
        first = await postgres.pool(ALIAS)
        await postgres.close_pools()
        second = await postgres.pool(ALIAS)
        closed = first.closed
        await postgres.close_pools()
        return {"first_closed": closed, "new_pool": second is not first}

    @api.get("/shutdown")
    async def shutdown():
        await postgres.close_pools()
        return {}

    with TestClient(api) as client:
        try:
            yield client
        finally:
            # The pool belongs to the event loop, which outlives this client. The database does not.
            client.get("/shutdown")


def test_a_route_reads_through_the_pool(client):
    assert client.get("/one?value=41").json() == {"value": 42}
    # The second request takes the pool that the first one opened.
    assert client.get("/one?value=1").json() == {"value": 2}


def test_queries_of_one_request_run_at_the_same_time(client):
    data = client.get("/many").json()
    assert data["sum"] == QUERIES
    # One after the other they take QUERIES * SLEEP seconds.
    assert data["seconds"] < QUERIES * SLEEP / 4, data


def test_a_block_that_ends_with_no_error_commits(client):
    assert client.get("/write").json() == {"rows": 1}


def test_close_pools_closes_the_pool_and_the_next_call_opens_a_new_one(client):
    assert client.get("/close").json() == {"first_closed": True, "new_pool": True}


def test_a_database_that_is_not_postgresql_is_refused(native):
    from freeman import postgres  # noqa: PLC0415

    with pytest.raises(ImproperlyConfigured, match="needs a PostgreSQL database"):
        postgres._conninfo("default")
    with pytest.raises(ImproperlyConfigured, match="has no 'missing'"):
        postgres._conninfo("missing")


def test_close_all_pools_closes_a_pool_from_outside_its_event_loop(client):
    from freeman import postgres  # noqa: PLC0415

    assert client.get("/one?value=1").json() == {"value": 2}
    opened = [found for (_, alias), found in postgres._pools.items() if alias == ALIAS]
    assert len(opened) == 1
    postgres.close_all_pools()
    assert opened[0].closed
    assert not postgres._pools
    # The next request opens a new pool.
    assert client.get("/one?value=2").json() == {"value": 3}
