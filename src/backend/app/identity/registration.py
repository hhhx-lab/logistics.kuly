"""独立租户注册、SMTP 发信及 Redis 原子验证码门禁。"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import secrets
import smtplib
import ssl
from datetime import UTC, datetime
from email.message import EmailMessage

from redis.asyncio import Redis
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ApiError, InvalidRequest, RateLimited, ServiceUnavailable, Unauthenticated
from app.config import Settings
from app.identity.models import Tenant, User
from app.identity.service import hash_password, issue_session

USER_PERMISSIONS = ["case_view", "case_create", "run_start", "review_revise", "review_adjudicate", "export_confirm"]
CODE_TTL = 600
RESEND_SECONDS = 60
WEAK_PASSWORDS = {"password", "password123", "12345678", "123456789", "qwerty123", "admin123", "letmein123"}


def normalize_email(value: str) -> str:
    value = value.strip().lower()
    if (len(value) > 254 or not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+", value)
        or ".." in value or value.startswith(".") or ".@" in value
        or any(len(label) > 63 for label in value.split("@")[-1].split("."))):
        raise ValueError("请输入有效邮箱")
    return value


def validate_password(account: str, email: str, password: str) -> None:
    if len(password) < 8 or len(password) > 128 or not re.search(r"[A-Za-z]", password) or not re.search(r"[0-9]", password):
        raise InvalidRequest("密码需要 8–128 位，并同时包含字母和数字")
    if password.lower().strip() in WEAK_PASSWORDS | {account.lower(), email, email.split("@", 1)[0]}:
        raise InvalidRequest("密码过于简单，请勿使用用户名、邮箱或常见密码")


def require_registration(settings: Settings) -> None:
    if not settings.registration_enabled:
        raise ApiError("暂未开放注册", code="REGISTRATION_DISABLED", status_code=403)


def check_origin(origin: str | None, settings: Settings) -> None:
    if origin and settings.app_public_origin and origin.rstrip("/") != settings.app_public_origin.rstrip("/"):
        raise Unauthenticated("请求来源不被信任，请从本站重试")


def smtp_ready(settings: Settings) -> bool:
    return bool(settings.mail_provider == "smtp" and settings.mail_from and settings.smtp_host
                and settings.smtp_username and settings.smtp_password)


def _key(scope: str, value: str) -> str:
    return "lda:registration:" + scope + ":" + hashlib.sha256(value.encode()).hexdigest()


def _code_digest(settings: Settings, email: str, code: str) -> str:
    if not settings.session_signing_key:
        raise ServiceUnavailable("验证码服务配置不完整，请联系管理员")
    return hmac.new(settings.session_signing_key.get_secret_value().encode(),
                    (email + "\0" + code).encode(), hashlib.sha256).hexdigest()


def _broker(settings: Settings) -> Redis:
    if not settings.redis_url:
        raise ServiceUnavailable("注册服务暂不可用，请稍后重试")
    return Redis.from_url(settings.redis_url, socket_connect_timeout=3, socket_timeout=3)


async def rate_limit(settings: Settings, scope: str, ip: str, limit: int, seconds: int) -> None:
    try:
        async with _broker(settings) as broker:
            count = await broker.eval("""
                local n=redis.call('INCR',KEYS[1])
                if n==1 then redis.call('EXPIRE',KEYS[1],ARGV[1]) end
                return n
            """, 1, _key(scope, ip), seconds)
    except ServiceUnavailable:
        raise
    except Exception:
        raise ServiceUnavailable("注册服务暂不可用，请稍后重试") from None
    if count > limit:
        raise RateLimited("操作过于频繁，请稍后再试")


def send_email(settings: Settings, email: str, code: str) -> None:
    message = EmailMessage()
    message["From"] = settings.mail_from
    message["To"] = email
    message["Subject"] = "物流单证智能体 · 注册验证码"
    if settings.mail_reply_to:
        message["Reply-To"] = settings.mail_reply_to
    message.set_content(f"你的物流单证智能体注册验证码为：{code}\n\n10 分钟内有效，请勿向他人透露。\n如果不是你本人操作，请忽略这封邮件。")
    context = ssl.create_default_context()
    if settings.smtp_port == 465:
        connection = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port,
                                     timeout=settings.smtp_timeout_seconds, context=context)
    else:
        connection = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout_seconds)
    with connection as server:
        if settings.smtp_port != 465:
            server.starttls(context=context)
        server.login(settings.smtp_username, settings.smtp_password.get_secret_value())
        refused = server.send_message(message)
        if refused:
            raise ServiceUnavailable("验证码邮件未能送达，请检查邮箱后重试")


async def send_code(settings: Settings, email: str, ip: str) -> None:
    require_registration(settings)
    if not settings.registration_require_email_code or not smtp_ready(settings):
        raise ServiceUnavailable("邮箱验证码发送暂不可用，请稍后重试")
    await rate_limit(settings, "send-ip", ip, 10, 3600)
    code = f"{secrets.randbelow(900000) + 100000:06d}"
    digest = _code_digest(settings, email, code)
    code_key, cooldown_key = _key("code", email), _key("cooldown", email)
    try:
        async with _broker(settings) as broker:
            if not await broker.set(cooldown_key, digest, ex=RESEND_SECONDS, nx=True):
                raise RateLimited("请等待 60 秒后重新获取验证码")
            await broker.eval("""
                redis.call('DEL',KEYS[1])
                redis.call('HSET',KEYS[1],'digest',ARGV[1],'attempts',5)
                redis.call('EXPIRE',KEYS[1],ARGV[2])
            """, 1, code_key, digest, CODE_TTL)
            try:
                await asyncio.to_thread(send_email, settings, email, code)
            except Exception:
                await broker.eval("""
                    if redis.call('HGET',KEYS[1],'digest')==ARGV[1] then redis.call('DEL',KEYS[1]) end
                    if redis.call('GET',KEYS[2])==ARGV[1] then redis.call('DEL',KEYS[2]) end
                """, 2, code_key, cooldown_key, digest)
                raise ServiceUnavailable("验证码邮件发送失败，请稍后重试") from None
    except ApiError:
        raise
    except Exception:
        raise ServiceUnavailable("验证码服务暂不可用，请稍后重试") from None


async def consume_code(settings: Settings, email: str, code: str | None) -> None:
    if not code or not re.fullmatch(r"[0-9]{6}", code):
        raise InvalidRequest("请输入六位邮箱验证码")
    try:
        async with _broker(settings) as broker:
            accepted = await broker.eval("""
                local digest=redis.call('HGET',KEYS[1],'digest')
                if not digest then return 0 end
                if digest==ARGV[1] then redis.call('DEL',KEYS[1]); return 1 end
                if redis.call('HINCRBY',KEYS[1],'attempts',-1)<=0 then redis.call('DEL',KEYS[1]) end
                return 0
            """, 1, _key("code", email), _code_digest(settings, email, code))
    except ApiError:
        raise
    except Exception:
        raise ServiceUnavailable("验证码服务暂不可用，请稍后重试") from None
    if not accepted:
        raise InvalidRequest("验证码错误、已过期或尝试次数过多，请重新获取")


async def ensure_available(db: AsyncSession, account: str | None, email: str) -> None:
    # 兼容历史上可能以邮箱作为 account 的管理员账号，避免两种登录标识歧义。
    predicates = [func.lower(User.email) == email, func.lower(User.account) == email]
    if account:
        predicates.append(func.lower(User.account) == account.lower())
    if (await db.execute(select(User.id).where(or_(*predicates)).limit(1))).first():
        raise ApiError("用户名或邮箱已被使用，请登录或更换后重试", code="ACCOUNT_EXISTS", status_code=409)


async def register(db: AsyncSession, settings: Settings, account: str, email: str,
                   password: str, code: str | None, ip: str):
    require_registration(settings)
    validate_password(account, email, password)
    await rate_limit(settings, "register-ip", ip, 10, 600)
    try:
        async with db.begin():
            await ensure_available(db, account, email)
            if settings.registration_require_email_code:
                await consume_code(settings, email, code)
            tenant = Tenant(name=account + "的个人空间")
            db.add(tenant)
            await db.flush()
            user = User(tenant_id=tenant.id, account=account, email=email,
                        email_verified_at=datetime.now(UTC) if settings.registration_require_email_code else None,
                        display_name=account, password_hash=await asyncio.to_thread(hash_password, password),
                        permissions=USER_PERMISSIONS, is_active=True)
            db.add(user)
            await db.flush()
            return await issue_session(db, user)
    except IntegrityError:
        raise ApiError("用户名或邮箱已被使用，请登录或更换后重试", code="ACCOUNT_EXISTS", status_code=409) from None
