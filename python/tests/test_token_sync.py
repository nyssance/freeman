"""The sync token API: the ``_sync`` methods of the stores and ``rotate_refresh_token_sync``."""

from __future__ import annotations

import asyncio

import pytest

from freeman.auth import TokenRotationError, create_token_pair, rotate_refresh_token, rotate_refresh_token_sync
from freeman.auth.revocation import DjangoCacheRevocation, DjangoORMRevocation, InMemoryRevocation, RevocationStore

SECRET = "token-sync-secret"


@pytest.fixture(params=["memory", "cache", pytest.param("orm", marks=pytest.mark.django_db(transaction=True))])
def store(request, settings):
    if request.param == "memory":
        return InMemoryRevocation()
    if request.param == "orm":
        return DjangoORMRevocation(model="bolt_revocation.RevokedToken")
    settings.CACHES = {"tokens": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "sync"}}
    store = DjangoCacheRevocation(cache_alias="tokens")
    store.cache.clear()
    return store


def issue(store: RevocationStore, user_id: str = "7"):
    return create_token_pair(user_id, secret=SECRET, version=store.get_user_version_sync(user_id))


def test_revoke_and_consume(store):
    assert store.is_revoked_sync("a") is False
    store.revoke_sync("a", exp=None)
    assert store.is_revoked_sync("a") is True
    assert asyncio.run(store.is_revoked("a")) is True

    assert store.consume_sync("b") is True
    assert store.consume_sync("b") is False


def test_rotate_and_replay_revokes_the_family(store):
    pair = issue(store)
    new = rotate_refresh_token_sync(pair.refresh_claims, store=store, secret=SECRET)
    assert new.refresh_claims["fam"] == pair.refresh_claims["fam"]

    # A replay of the rotated-out token burns the family: the new token fails too.
    with pytest.raises(TokenRotationError):
        rotate_refresh_token_sync(pair.refresh_claims, store=store, secret=SECRET)
    assert store.is_family_revoked_sync(pair.refresh_claims["fam"]) is True
    with pytest.raises(TokenRotationError, match="family revoked"):
        rotate_refresh_token_sync(new.refresh_claims, store=store, secret=SECRET)


def test_revoke_family_and_bump_user_version(store):
    pair = issue(store)
    store.revoke_family_sync(pair.refresh_claims["fam"])
    with pytest.raises(TokenRotationError, match="family revoked"):
        rotate_refresh_token_sync(pair.refresh_claims, store=store, secret=SECRET)

    pair = issue(store)
    assert store.bump_user_version_sync("7") == 1
    assert store.get_user_version_sync("7") == 1
    with pytest.raises(TokenRotationError, match="version"):
        rotate_refresh_token_sync(pair.refresh_claims, store=store, secret=SECRET)
    assert issue(store).refresh_claims["ver"] == 1


def test_the_async_names_read_what_the_sync_forms_write(store):
    pair = issue(store)
    store.bump_user_version_sync("8")
    assert asyncio.run(store.get_user_version("8")) == 1
    asyncio.run(store.revoke_family("f"))
    assert store.is_family_revoked_sync("f") is True
    new = asyncio.run(rotate_refresh_token(pair.refresh_claims, store=store, secret=SECRET))
    assert asyncio.run(store.consume(pair.refresh_claims["jti"])) is False
    assert new.refresh_claims["sub"] == "7"
