"""
认证管线定向测试（方案实施段 A）。

覆盖：账号枚举防护、CSRF 门禁、会话撤销、告知接口反映真实配置。
只测本模块及其直接调用链，不跑 OCR 与模型。
"""

from __future__ import annotations

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from app.api.main import app
from app.config import AdapterKind, get_settings
from app.identity.seed import DEMO_PASSWORD, seed

ORIGIN = "http://localhost:5173"


@pytest.fixture(scope="module", autouse=True)
async def _seeded() -> None:
    await seed()


@pytest.fixture
async def client(monkeypatch):
    # 认证测试不运行发布/清理后台，避免隔离库经 SSH 时后台收尾干扰断言。
    from app.infra import outbox
    async def idle(stop):
        await stop.wait()
    monkeypatch.setattr(outbox, "publisher_loop", idle)
    # LifespanManager 驱动 startup，让 app.state.session_factory 就绪
    async with LifespanManager(app) as managed:
        async with AsyncClient(transport=ASGITransport(app=managed.app), base_url="http://test") as c:
            yield c


async def _login(client: AsyncClient, account: str = "demo") -> str:
    r = await client.post(
        "/api/v1/auth/login",
        json={"account": account, "password": DEMO_PASSWORD},
        headers={"Origin": ORIGIN},
    )
    assert r.status_code == 200, r.text
    return r.json()["csrf_token"]


async def test_unauthenticated_me_is_401(client: AsyncClient) -> None:
    r = await client.get("/api/v1/me")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHENTICATED"


async def test_login_failures_are_indistinguishable(client: AsyncClient) -> None:
    """账号不存在与口令错误必须返回完全相同的错误，避免账号枚举。"""
    wrong_password = await client.post(
        "/api/v1/auth/login", json={"account": "demo", "password": "wrong"}, headers={"Origin": ORIGIN}
    )
    unknown_account = await client.post(
        "/api/v1/auth/login", json={"account": "nobody", "password": DEMO_PASSWORD}, headers={"Origin": ORIGIN}
    )
    assert wrong_password.status_code == unknown_account.status_code == 401
    # code 与 message 必须完全一致；trace_id 每次不同，不参与比较
    a, b = wrong_password.json()["error"], unknown_account.json()["error"]
    assert (a["code"], a["message"]) == (b["code"], b["message"])


async def test_identity_exposes_permissions_not_secrets(client: AsyncClient) -> None:
    await _login(client)
    r = await client.get("/api/v1/me")
    assert r.status_code == 200
    body = r.json()
    assert body["display_name"] == "陈晓"
    assert "ops_reconcile" in body["permissions"]
    # 不得返回口令哈希、会话令牌等字段
    assert not {"password_hash", "token", "session_token"} & set(body)


async def test_write_requires_csrf(client: AsyncClient) -> None:
    await _login(client)
    without = await client.post("/api/v1/auth/logout", headers={"Origin": ORIGIN})
    assert without.status_code == 401
    assert without.json()["error"]["code"] == "UNAUTHENTICATED"


async def test_logout_revokes_session(client: AsyncClient) -> None:
    csrf = await _login(client)
    out = await client.post("/api/v1/auth/logout", headers={"Origin": ORIGIN, "X-CSRF-Token": csrf})
    assert out.status_code == 204
    # 撤销后同一 Cookie 不可复用
    assert (await client.get("/api/v1/me")).status_code == 401


async def test_cross_tenant_users_are_separate(client: AsyncClient) -> None:
    await _login(client, "demo")
    first = (await client.get("/api/v1/me")).json()
    await _login(client, "demo2")
    second = (await client.get("/api/v1/me")).json()
    assert first["tenant_id"] != second["tenant_id"]
    assert first["user_id"] != second["user_id"]
    # demo2 没有运营权限
    assert "ops_reconcile" not in second["permissions"]


async def test_disclosure_reflects_real_configuration(client: AsyncClient) -> None:
    r = await client.get("/api/v1/data-processing-disclosure")
    assert r.status_code == 200
    body = r.json()
    settings = get_settings()

    assert body["case_retention_days"] == settings.case_retention_days
    roles = {s["role"] for s in body["services"]}
    assert {"对象存储", "文档解析（OCR）", "对话模型", "翻译模型"} <= roles

    # 告知版本随适配器变化，服务配置改变后旧版本不能签发新上传
    assert body["disclosure_version"].startswith("disclosure-")
    assert len(body["disclosure_version"]) <= 64
    assert body["material_mode"] == settings.material_mode

    # 用假实现时必须阻断真实单证上传，不能标为可用
    if settings.using_fake_externals():
        assert body["upload_allowed"] is False
        assert body["upload_blocked_reason"]
    else:
        parser = next(s for s in body["services"] if s["role"] == "文档解析（OCR）")
        assert parser["display_name"] != "未配置"
        assert AdapterKind.DETERMINISTIC_FAKE.value not in parser["display_name"]


async def test_private_no_store_headers(client: AsyncClient) -> None:
    r = await client.get("/api/v1/data-processing-disclosure")
    assert r.headers["cache-control"] == "private, no-store"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-request-id"]
