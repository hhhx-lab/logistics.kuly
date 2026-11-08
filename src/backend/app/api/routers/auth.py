"""S01–S04：登录、登出、身份与数据流向告知。"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api.dto import PublishedView
from app.api.dependencies import CurrentUser, DbSession, SettingsDep, WriteUser
from app.config import AdapterKind, ModelRole, Settings
from app.identity import service as auth_service
from app.identity.service import SESSION_COOKIE
from app.identity import registration

router = APIRouter(tags=["identity"])

# 告知版本随「实际会接触材料的服务集合」变化；服务配置改变后旧版本不能签发新上传。
DISCLOSURE_VERSION_PREFIX = "disclosure"


class LoginRequest(BaseModel):
    account: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class IdentityView(PublishedView):
    user_id: str
    tenant_id: str
    display_name: str
    avatar_initial: str
    permissions: list[str]
    csrf_token: str
    policy_version: str = "system-policy-v1"


class RegistrationConfigView(PublishedView):
    enabled: bool
    email_code_required: bool
    resend_seconds: int = registration.RESEND_SECONDS
    code_expire_seconds: int = registration.CODE_TTL


class EmailCodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=254)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        return registration.normalize_email(value)


class RegisterRequest(EmailCodeRequest):
    account: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_][A-Za-z0-9_-]{2,31}$")
    password: str = Field(min_length=8, max_length=128)
    verification_code: str | None = Field(default=None, pattern=r"^[0-9]{6}$")

    @field_validator("account")
    @classmethod
    def normalized_account(cls, value: str) -> str:
        return value.lower()


class EmailCodeView(PublishedView):
    message: str
    resend_seconds: int = registration.RESEND_SECONDS
    code_expire_seconds: int = registration.CODE_TTL


class DisclosureService(PublishedView):
    role: str
    display_name: str
    purpose: str
    data_scope: str
    region: str
    retention: str
    enabled: bool
    policy_verified: bool = False
    deletion: str = "未核实"
    evidence_reference: str | None = None


class DisclosureView(PublishedView):
    disclosure_version: str
    services: list[DisclosureService]
    case_retention_days: int
    deletion_scope: str
    upload_allowed: bool
    upload_blocked_reason: str | None = None
    policy_url: str | None = None
    material_mode: str = "synthetic_only"
    material_notice: str = "仅用于自行构造的合成样本，不得上传真实个人或商业信息。"


@router.post("/auth/login", status_code=status.HTTP_200_OK)
async def login(body: LoginRequest, request: Request, response: Response, db: DbSession, settings: SettingsDep) -> IdentityView:
    registration.check_origin(request.headers.get("origin"), settings)
    async with db.begin():
        principal, issued = await auth_service.login(db, body.account, body.password)

    return _identity_response(response, settings, principal, issued)


def _identity_response(response: Response, settings: Settings, principal, issued) -> IdentityView:

    # 同源部署，SameSite=Lax 足够；本地开发不是 HTTPS 故 secure 跟随部署环境
    response.set_cookie(
        SESSION_COOKIE,
        issued.token,
        httponly=True,
        secure=settings.app_env != "development",
        samesite="lax",
        expires=issued.expires_at,
        path="/",
    )
    return IdentityView(
        user_id=str(principal.user_id),
        tenant_id=str(principal.tenant_id),
        display_name=principal.display_name,
        avatar_initial=auth_service.avatar_initial(principal.display_name),
        permissions=list(principal.permissions),
        csrf_token=issued.csrf_token,
    )


@router.get("/auth/registration-config")
async def registration_config(settings: SettingsDep) -> RegistrationConfigView:
    return RegistrationConfigView(enabled=settings.registration_enabled,
                                  email_code_required=settings.registration_require_email_code)


@router.post("/auth/register/send-code", status_code=status.HTTP_202_ACCEPTED)
async def send_registration_code(body: EmailCodeRequest, request: Request, db: DbSession, settings: SettingsDep) -> EmailCodeView:
    registration.check_origin(request.headers.get("origin"), settings)
    registration.require_registration(settings)
    async with db.begin():
        await registration.ensure_available(db, None, body.email)
    await registration.send_code(settings, body.email, request.client.host if request.client else "unknown")
    return EmailCodeView(message="验证码已发送，10 分钟内有效，请检查收件箱或垃圾邮件")


@router.post("/auth/register", status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, request: Request, response: Response, db: DbSession, settings: SettingsDep) -> IdentityView:
    registration.check_origin(request.headers.get("origin"), settings)
    principal, issued = await registration.register(db, settings, body.account, body.email, body.password,
                                                   body.verification_code, request.client.host if request.client else "unknown")
    return _identity_response(response, settings, principal, issued)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(response: Response, principal: WriteUser, db: DbSession) -> None:
    async with db.begin():
        await auth_service.logout(db, principal.session_id)
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get("/me")
async def me(request: Request, principal: CurrentUser, db: DbSession) -> IdentityView:
    """同源身份读取返回可用 CSRF 明文，兼容旧会话并保持多标签页稳定。"""
    from app.identity.models import AuthSession

    csrf_token = auth_service.csrf_for_session(request.cookies[SESSION_COOKIE])
    async with db.begin():
        record = await db.get(AuthSession, principal.session_id)
        if record is not None:
            record.csrf_secret_hash = auth_service._digest(csrf_token)
    return IdentityView(
        user_id=str(principal.user_id),
        tenant_id=str(principal.tenant_id),
        display_name=principal.display_name,
        avatar_initial=auth_service.avatar_initial(principal.display_name),
        permissions=list(principal.permissions),
        csrf_token=csrf_token,
    )


def _host_of(url: str | None) -> str:
    if not url:
        return "未配置"
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return parsed.netloc or url


def build_disclosure(settings: Settings) -> DisclosureView:
    """
    S04：按当前**实际发布的非秘密部署配置**列出会接触材料的服务。

    显示名取自配置的主机名，不从模型 ID 猜供应商；禁用项不写成正在使用。
    """
    services: list[DisclosureService] = [
        DisclosureService(
            role="对象存储",
            display_name=_host_of(settings.s3_public_endpoint or settings.s3_endpoint),
            purpose="保存原件、解析产物、预览与正式交付文件",
            data_scope="原件字节及其派生文件",
            region="本地部署",
            retention=f"随单票保留 {settings.case_retention_days} 天，到期或主动删除后清除全部对象版本",
            enabled=bool(settings.s3_bucket),
        )
    ]

    parser_fake = settings.parser_adapter is AdapterKind.DETERMINISTIC_FAKE
    services.append(
        DisclosureService(
            role="文档解析（OCR）",
            display_name="本地确定性假解析（仅用于联调）" if parser_fake else _host_of(settings.mineru_base_url),
            purpose="把原件转成可定位的文本、表格与来源锚点",
            data_scope="原件全文或需要复读的局部图像" if not parser_fake else "不外发，本地生成占位结果",
            region="服务商侧" if not parser_fake else "本机",
                retention="未核实" if not parser_fake else "不留存",
            enabled=settings.parser_ready(),
        )
    )

    model_fake = settings.model_adapter is AdapterKind.DETERMINISTIC_FAKE
    role_purpose = {
        ModelRole.CHAT: ("对话模型", "理解问题意图并组织解释"),
        ModelRole.EXTRACT: ("字段抽取模型", "从解析片段中提取字段与明细行候选"),
        ModelRole.TRANSLATE: ("翻译模型", "按片段翻译需要交付的内容"),
        ModelRole.VISION: ("视觉复读模型", "对低清局部做第二次转写候选"),
    }
    for role, (label, purpose) in role_purpose.items():
        profile = None
        if not model_fake:
            try:
                profile = settings.model_profile(role)
            except ValueError:
                profile = None
        services.append(
            DisclosureService(
                role=label,
                display_name="本地确定性假模型（仅用于联调）" if model_fake else _host_of(profile.base_url if profile else None),
                purpose=purpose,
                data_scope="该步骤实际所需的最小文本片段或局部图像" if not model_fake else "不外发，本地生成占位结果",
                region="服务商侧" if not model_fake else "本机",
                retention="未核实" if not model_fake else "不留存",
                enabled=settings.model_role_ready(role),
            )
        )

    policy_roles = ["storage", "ocr", "chat", "extract", "translate", "vision"]
    for service, role in zip(services, policy_roles, strict=True):
        configured = settings.service_policies.get(role)
        policy = configured.model_dump() if configured else {}
        days = policy.get("retention_days")
        valid_days = type(days) is int and 0 <= days <= 30
        service.policy_verified = bool(policy.get("verified") is True and valid_days
            and policy.get("deletion_supported") is True
            and all(isinstance(policy.get(k), str) and policy[k].strip()
                    for k in ("display_name", "region", "evidence_reference")))
        service.display_name = policy.get("display_name") or service.display_name
        service.region = policy.get("region") or "未核实"
        service.retention = f"最长 {days} 天" if type(days) is int and days >= 0 else "未核实"
        service.deletion = "支持主动删除" if policy.get("deletion_supported") is True else "主动删除能力未核实"
        service.evidence_reference = policy.get("evidence_reference") or None

    blocked: str | None = None
    if not settings.s3_bucket:
        blocked = "对象存储尚未配置，无法接收文件。"
    elif settings.using_fake_externals():
        # 假适配器只用于打通工程链路，绝不能让真实单证走这条路
        blocked = "当前解析或模型使用本地假实现，仅供联调；请勿上传真实单证材料。"
    elif settings.material_mode == "real_documents" and any(s.enabled and not s.policy_verified for s in services):
        blocked = "启用服务的区域、30天内留存及主动删除依据未完整核实，暂不能接收真实单证。"

    import hashlib
    import json
    public_config = {"services": [s.model_dump() for s in services], "material_mode": settings.material_mode,
        "retention_days": settings.case_retention_days,
        "parser_adapter": settings.parser_adapter.value, "model_adapter": settings.model_adapter.value}
    version = DISCLOSURE_VERSION_PREFIX + "-" + hashlib.sha256(json.dumps(public_config, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:32]
    return DisclosureView(
        disclosure_version=version,
        services=services,
        case_retention_days=settings.case_retention_days,
        deletion_scope="删除这一票会清除其原件全部版本、解析产物、译文、预览、正式交付与该票审计；只留不含票据内容的最小清理回执。",
        upload_allowed=blocked is None,
        upload_blocked_reason=blocked,
        material_mode=settings.material_mode,
        material_notice="仅用于自行构造的合成样本，不得上传真实个人或商业信息。" if settings.material_mode == "synthetic_only" else "真实材料模式；仅按已核实服务政策处理，请确认你有权提交本票资料。",
    )


@router.get("/data-processing-disclosure")
async def data_processing_disclosure(settings: SettingsDep) -> DisclosureView:
    return build_disclosure(settings)
