"""Tests for the Phase 1 foundation hardening: request IDs, security headers,
production exception handling, registration password floor, and the startup
schema-version guard."""

from pathlib import Path

import pytest
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from httpx import ASGITransport, AsyncClient

import sutr.main as main_module
from sutr.config import settings
from sutr.main import app, check_database_schema


def _migration_head() -> str:
    server_root = Path(main_module.__file__).resolve().parent.parent.parent
    script = ScriptDirectory.from_config(AlembicConfig(str(server_root / "alembic.ini")))
    return script.get_current_head()


async def test_register_rejects_short_password(client):
    resp = await client.post(
        "/api/users/register",
        json={"email": "short@example.com", "password": "abc"},
    )
    assert resp.status_code == 400
    assert "at least 6 characters" in resp.json()["detail"]


async def test_register_accepts_six_char_password(client):
    resp = await client.post(
        "/api/users/register",
        json={"email": "okpass@example.com", "password": "abcdef"},
    )
    assert resp.status_code == 201


async def test_responses_carry_request_id_and_security_headers(client):
    resp = await client.get("/api/config")
    assert resp.status_code == 200
    assert resp.headers.get("x-request-id")
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert resp.headers.get("x-frame-options") == "DENY"
    assert resp.headers.get("referrer-policy") == "no-referrer"


async def test_inbound_request_id_is_echoed(client):
    resp = await client.get("/api/config", headers={"X-Request-ID": "proxy-assigned-123"})
    assert resp.headers.get("x-request-id") == "proxy-assigned-123"


async def test_unhandled_exception_returns_500_with_request_id():
    @app.get("/__test_boom", include_in_schema=False)
    async def _boom():
        raise RuntimeError("boom")

    # When server/ui_dist exists (a packaged build, or a local single-port run)
    # main.py has already registered the SPA catch-all, and Starlette matches
    # routes in registration order — so a route appended now would be shadowed
    # and answered with index.html. Move ours to the front so this test asserts
    # the exception handler in both layouts.
    app.router.routes.insert(0, app.router.routes.pop())

    try:
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/__test_boom")
        assert resp.status_code == 500
        body = resp.json()
        assert body["detail"] == "Internal Server Error"
        assert body["request_id"]
        assert resp.headers.get("x-request-id") == body["request_id"]
    finally:
        app.router.routes[:] = [
            r for r in app.router.routes if getattr(r, "path", None) != "/__test_boom"
        ]


def test_check_database_schema_raises_when_unmigrated(session, monkeypatch):
    # The test engine was built with create_all — no alembic_version table.
    monkeypatch.setattr(settings, "dev", False)
    with pytest.raises(RuntimeError, match="alembic upgrade head"):
        check_database_schema()


def test_check_database_schema_warns_in_dev(session, monkeypatch, caplog):
    monkeypatch.setattr(settings, "dev", True)
    check_database_schema()  # must not raise


def test_check_database_schema_passes_at_head(session, monkeypatch):
    monkeypatch.setattr(settings, "dev", False)
    head = _migration_head()

    from sutr import db as db_module

    with db_module.engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        conn.exec_driver_sql(f"INSERT INTO alembic_version VALUES ('{head}')")

    check_database_schema()  # at head — must not raise
