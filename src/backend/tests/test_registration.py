"""注册定向集成测试；使用隔离 PostgreSQL/Redis，SMTP 仅在测试中替换。"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis

from app.api.main import app
from app.config import get_settings
from app.identity import registration

ORIGIN = "http://localhost:5173"
PASSWORD = "CargoTest972!"


@pytest.fixture
async def registration_client(monkeypatch):
    from app.infra import outbox
    async def idle(stop):
        await stop.wait()
    monkeypatch.setattr(outbox, "publisher_loop", idle)
    settings = get_settings()
    redis_url = settings.redis_url
    monkeypatch.setattr(settings, "registration_enabled", True)
    monkeypatch.setattr(settings, "registration_require_email_code", True)
    monkeypatch.setattr(settings, "mail_provider", "smtp")
    monkeypatch.setattr(settings, "mail_from", "sender@example.com")
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_username", "sender@example.com")
    from pydantic import SecretStr
    monkeypatch.setattr(settings, "smtp_password", SecretStr("test-only"))
    deliveries = {}
    monkeypatch.setattr(registration, "send_email", lambda _settings, email, code: deliveries.update({email: code}))
    ip = uuid.uuid4().hex
    async with LifespanManager(app) as managed:
        async with AsyncClient(transport=ASGITransport(app=managed.app, client=(ip, 123)), base_url="http://test", headers={"Origin": ORIGIN}) as client:
            yield client, deliveries
    async with Redis.from_url(redis_url) as broker:
        await broker.delete(registration._key("send-ip", ip), registration._key("register-ip", ip))
        for email in deliveries:
            await broker.delete(registration._key("code", email), registration._key("cooldown", email))


def new_body():
    unique = uuid.uuid4().hex[:16]
    return {"account": "cargo_" + unique, "email": unique + "@example.com", "password": PASSWORD}


async def test_verified_registration_login_and_permissions(registration_client):
    client, deliveries = registration_client
    body = new_body()
    sent = await client.post("/api/v1/auth/register/send-code", json={"email": body["email"].upper()})
    assert sent.status_code == 202
    assert deliveries[body["email"]] not in sent.text
    body["verification_code"] = deliveries[body["email"]]
    created = await client.post("/api/v1/auth/register", json=body)
    assert created.status_code == 201, created.text
    identity = created.json()
    assert set(identity["permissions"]) == set(registration.USER_PERMISSIONS)
    assert not any(p.startswith("ops_") for p in identity["permissions"])
    assert (await client.get("/api/v1/me")).json()["user_id"] == identity["user_id"]
    assert "HttpOnly" in created.headers["set-cookie"]
    assert "SameSite=lax" in created.headers["set-cookie"]
    assert (await client.get("/api/v1/ops/metrics")).status_code in (403, 404)
    for account in (body["account"].upper(), body["email"].upper()):
        response = await client.post("/api/v1/auth/login", json={"account": account, "password": PASSWORD})
        assert response.status_code == 200
        assert response.json()["user_id"] == identity["user_id"]
    assert (await client.post("/api/v1/auth/register", json=body)).status_code == 409
    async with Redis.from_url(get_settings().redis_url) as broker:
        assert not await broker.exists(registration._key("code", body["email"]))


async def test_code_cooldown_attempt_limit_and_expiry(registration_client):
    client, deliveries = registration_client
    body = new_body()
    assert (await client.post("/api/v1/auth/register/send-code", json={"email": body["email"]})).status_code == 202
    assert (await client.post("/api/v1/auth/register/send-code", json={"email": body["email"]})).status_code == 429
    for _ in range(5):
        assert (await client.post("/api/v1/auth/register", json={**body, "verification_code": "000000"})).status_code == 422
    assert (await client.post("/api/v1/auth/register", json={**body, "verification_code": deliveries[body["email"]]})).status_code == 422
    async with Redis.from_url(get_settings().redis_url) as broker:
        await broker.delete(registration._key("cooldown", body["email"]))
    assert (await client.post("/api/v1/auth/register/send-code", json={"email": body["email"]})).status_code == 202
    async with Redis.from_url(get_settings().redis_url) as broker:
        await broker.pexpire(registration._key("code", body["email"]), 1)
    await asyncio.sleep(0.02)
    assert (await client.post("/api/v1/auth/register", json={**body, "verification_code": deliveries[body["email"]]})).status_code == 422


async def test_registration_disabled_and_cross_origin(registration_client, monkeypatch):
    client, _ = registration_client
    body = new_body()
    monkeypatch.setattr(get_settings(), "registration_enabled", False)
    assert (await client.get("/api/v1/auth/registration-config")).json()["enabled"] is False
    assert (await client.post("/api/v1/auth/register", json=body)).status_code == 403
    assert (await client.post("/api/v1/auth/register/send-code", json={"email": body["email"]})).status_code == 403
    monkeypatch.setattr(get_settings(), "registration_enabled", True)
    for route, data in (("/auth/register", body), ("/auth/register/send-code", {"email": body["email"]}), ("/auth/login", {"account": "demo", "password": PASSWORD})):
        assert (await client.post("/api/v1" + route, json=data, headers={"Origin": "https://foreign.example"})).status_code == 401


async def test_bad_fields_and_cannot_choose_permissions(registration_client):
    client, _ = registration_client
    body = new_body()
    for override in ({"email": "a\r\nBcc:x@example.com"}, {"email": "bad@domain"}, {"account": "admin@example.com"}, {"password": "password123"}, {"password": "123456789"}, {"tenant_id": str(uuid.uuid4())}, {"permissions": ["ops_health"]}):
        response = await client.post("/api/v1/auth/register", json={**body, **override})
        assert response.status_code == 422, response.text


async def test_smtp_failure_does_not_bypass_verification(registration_client, monkeypatch):
    client, _ = registration_client
    body = new_body()
    def fail(*_args):
        raise RuntimeError("secret-smtp-detail")
    monkeypatch.setattr(registration, "send_email", fail)
    failed = await client.post("/api/v1/auth/register/send-code", json={"email": body["email"]})
    assert failed.status_code == 503
    assert "secret-smtp-detail" not in failed.text
    assert (await client.post("/api/v1/auth/register", json=body)).status_code == 422
    async with Redis.from_url(get_settings().redis_url) as broker:
        assert not await broker.exists(registration._key("code", body["email"]))
        assert not await broker.exists(registration._key("cooldown", body["email"]))


async def test_simple_mode_explicit_only_and_unique_race(registration_client, monkeypatch):
    client, _ = registration_client
    monkeypatch.setattr(get_settings(), "registration_require_email_code", False)
    body = new_body()
    results = await asyncio.gather(*(client.post("/api/v1/auth/register", json={**body, "account": body["account"] + str(i)}) for i in range(2)))
    assert sorted(r.status_code for r in results) == [201, 409]
    first = next(r.json() for r in results if r.status_code == 201)
    first_account = body["account"] + str(next(i for i, r in enumerate(results) if r.status_code == 201))
    client.cookies.clear()
    login = await client.post("/api/v1/auth/login", json={"account": first_account, "password": PASSWORD})
    csrf = login.json()["csrf_token"]
    conversation = await client.post("/api/v1/conversations", json={}, headers={"X-CSRF-Token": csrf, "Idempotency-Key": uuid.uuid4().hex})
    assert conversation.status_code == 201, conversation.text
    conversation_id = conversation.json()["conversation_id"]
    second = await client.post("/api/v1/auth/register", json=new_body())
    assert second.status_code == 201
    assert second.json()["tenant_id"] != first["tenant_id"]
    assert (await client.get("/api/v1/conversations/" + conversation_id)).status_code == 404
    assert all(item["conversation_id"] != conversation_id for item in (await client.get("/api/v1/conversations")).json()["items"])


async def test_missing_redis_fails_closed(registration_client, monkeypatch):
    client, _ = registration_client
    monkeypatch.setattr(get_settings(), "redis_url", None)
    response = await client.post("/api/v1/auth/register", json=new_body())
    assert response.status_code == 503


async def test_legacy_email_account_cannot_be_shadowed(registration_client):
    from app.identity.models import Tenant, User
    from app.identity.service import hash_password
    client, _ = registration_client
    body = new_body()
    async with app.state.session_factory() as db, db.begin():
        tenant = Tenant(name="历史邮箱登录账号")
        db.add(tenant)
        await db.flush()
        db.add(User(tenant_id=tenant.id, account=body["email"], display_name="历史用户",
                    password_hash=hash_password(PASSWORD), permissions=registration.USER_PERMISSIONS))
    assert (await client.post("/api/v1/auth/register", json=body)).status_code == 409
    assert (await client.post("/api/v1/auth/register/send-code", json={"email": body["email"]})).status_code == 409
    assert (await client.post("/api/v1/auth/login", json={"account": body["email"], "password": PASSWORD})).status_code == 200
