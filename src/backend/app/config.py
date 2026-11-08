from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[3]


class ModelRole(StrEnum):
    CHAT = "chat"
    EXTRACT = "extract"
    TRANSLATE = "translate"
    VISION = "vision"


class AdapterKind(StrEnum):
    """
    外部服务适配器实现。

    `DETERMINISTIC_FAKE` 用确定性本地实现替代真实模型与 OCR，使工作流、版本链、
    导出回检在未配置 Key 时也能端到端验证。它产出的内容一律带显著标记，
    绝不能在告知、报告或界面上冒充真实识别与翻译结果。
    """

    OPENAI_COMPATIBLE = "openai_compatible"
    MINERU = "mineru"
    DETERMINISTIC_FAKE = "deterministic_fake"


class ModelPrice(BaseModel):
    """供应商已确认的含税全包人民币 token 报价；不推导汇率或阶梯价格。"""
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    version: str = Field(min_length=1)
    evidence_reference: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    protocol: str = Field(min_length=1)
    verified: bool = Field(default=False, strict=True)
    currency: Literal["CNY"] = "CNY"
    billing_basis: Literal["all_inclusive_prompt_completion_tokens"]
    input_cny_per_million: Decimal = Field(ge=0, allow_inf_nan=False)
    output_cny_per_million: Decimal = Field(ge=0, allow_inf_nan=False)


class OCRPrice(BaseModel):
    """供应商核实的全包OCR单价；处理模式必须与实际选项一致。"""
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    version: str = Field(min_length=1)
    evidence_reference: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    protocol: Literal["mineru_v4_batch/1"] = "mineru_v4_batch/1"
    model_version: Literal["vlm", "pipeline"]
    language: str = Field(default="ch", min_length=1)
    enable_formula: bool = Field(default=False, strict=True)
    enable_table: bool = Field(default=True, strict=True)
    is_ocr: bool = Field(default=True, strict=True)
    verified: bool = Field(default=False, strict=True)
    currency: Literal["CNY"] = "CNY"
    billing_basis: Literal["all_inclusive_per_page", "all_inclusive_per_task"]
    unit_cny: Decimal = Field(ge=0, allow_inf_nan=False)


class OCRBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url: str
    protocol: Literal["mineru_v4_batch/1"]
    options: dict[str, bool | str]
    price: OCRPrice | None = None


class ModelProfile(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    base_url: str = Field(min_length=1)
    api_key: SecretStr = Field(min_length=1)
    name: str = Field(min_length=1)
    protocol: str = Field(min_length=1)
    timeout_seconds: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    max_concurrency: int = Field(gt=0)
    context_window_tokens: int = Field(default=16000, gt=0)
    temperature: float | None = Field(default=None, ge=0, le=2)
    enable_thinking: bool | None = None
    price: ModelPrice | None = None

    def public_fingerprint(self) -> str:
        values = self.model_dump(exclude={"api_key"}, mode="json")
        if self.enable_thinking is None:
            values.pop("enable_thinking")
        encoded = json.dumps(values, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class ServicePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    display_name: str | None = Field(default=None, max_length=200)
    region: str | None = Field(default=None, max_length=200)
    retention_days: int | None = Field(default=None, ge=0, strict=True)
    deletion_supported: bool = Field(default=False, strict=True)
    verified: bool = Field(default=False, strict=True)
    evidence_reference: str | None = Field(default=None, max_length=2000)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_ignore_empty=True,
        extra="ignore",
    )

    app_env: str = "development"
    material_mode: Literal["synthetic_only", "real_documents"] = "synthetic_only"
    service_policies: dict[str, ServicePolicy] = Field(default_factory=dict)
    reviewed_rule_publications: list[dict] = Field(default_factory=list)
    rule_evidence_directory: str | None = None
    rule_official_domains: list[str] = Field(default_factory=list)
    database_url: str | None = None
    checkpoint_database_url: str | None = None
    redis_url: str | None = None
    app_public_origin: str | None = None
    registration_enabled: bool = False
    registration_require_email_code: bool = True
    mail_provider: str = ""
    mail_from: str = ""
    mail_reply_to: str = ""
    smtp_host: str = ""
    smtp_port: int = Field(default=465, ge=1, le=65535)
    smtp_username: str = ""
    smtp_password: SecretStr | None = None
    smtp_timeout_seconds: int = Field(default=20, ge=1, le=60)
    s3_endpoint: str | None = None
    s3_public_endpoint: str | None = None
    s3_region: str | None = None
    s3_bucket: str | None = None
    s3_access_key: SecretStr | None = None
    s3_secret_key: SecretStr | None = None
    session_signing_key: SecretStr | None = None
    mcp_credential_master_key: SecretStr | None = None
    mineru_base_url: str | None = None
    mineru_api_key: SecretStr | None = Field(default=None, validation_alias=AliasChoices("mineru_api_key", "MINERU_API_KEY", "MINERU_API_TOKEN"))
    mineru_model_version: Literal["vlm", "pipeline"] = "vlm"
    mineru_language: Literal["ch", "ch_server", "en", "japan", "korean", "chinese_cht", "ta", "te", "ka", "el", "th", "latin", "arabic", "cyrillic", "east_slavic", "devanagari"] = "ch"
    ocr_prices: list[OCRPrice] = Field(default_factory=list)

    chat_model_base_url: str | None = None
    chat_model_api_key: SecretStr | None = None
    chat_model_name: str | None = None
    chat_model_protocol: str | None = None
    chat_model_timeout_seconds: int | None = None
    chat_model_max_output_tokens: int | None = None
    chat_model_context_window_tokens: int = Field(default=16000, gt=0)
    chat_model_max_concurrency: int | None = None
    chat_model_temperature: float | None = None
    chat_model_enable_thinking: bool | None = None

    extract_model_base_url: str | None = None
    extract_model_api_key: SecretStr | None = None
    extract_model_name: str | None = None
    extract_model_protocol: str | None = None
    extract_model_timeout_seconds: int | None = None
    extract_model_max_output_tokens: int | None = None
    extract_model_context_window_tokens: int = Field(default=16000, gt=0)
    extract_model_max_concurrency: int | None = None
    extract_model_temperature: float | None = None
    extract_model_enable_thinking: bool | None = None

    translate_model_base_url: str | None = None
    translate_model_api_key: SecretStr | None = None
    translate_model_name: str | None = None
    translate_model_protocol: str | None = None
    translate_model_timeout_seconds: int | None = None
    translate_model_max_output_tokens: int | None = None
    translate_model_context_window_tokens: int = Field(default=16000, gt=0)
    translate_model_max_concurrency: int | None = None
    translate_model_temperature: float | None = None
    translate_model_enable_thinking: bool | None = None

    vision_model_base_url: str | None = None
    vision_model_api_key: SecretStr | None = None
    vision_model_name: str | None = None
    vision_model_protocol: str | None = None
    vision_model_timeout_seconds: int | None = None
    vision_model_max_output_tokens: int | None = None
    vision_model_context_window_tokens: int = Field(default=16000, gt=0)
    vision_model_max_concurrency: int | None = None
    vision_model_temperature: float | None = None
    vision_model_enable_thinking: bool | None = None

    # 适配器选择：没有真实 Key 时用确定性假实现联调工程链路。
    # 由服务端配置决定，前端与普通用户都不能切换。
    parser_adapter: AdapterKind = AdapterKind.DETERMINISTIC_FAKE
    model_adapter: AdapterKind = AdapterKind.DETERMINISTIC_FAKE

    upload_signed_url_ttl_seconds: int = Field(default=900, gt=0)
    mcp_allowed_hosts: str | None = None
    mcp_public_tool_registry_json: str | None = None
    mcp_timeout_seconds: int = Field(default=20, gt=0)
    mcp_max_response_bytes: int = Field(default=262144, gt=0)

    run_max_files: int = Field(default=5, gt=0)
    run_max_total_bytes: int = Field(default=104857600, gt=0)
    run_max_total_pages: int = Field(default=100, gt=0)
    workflow_task_soft_limit_seconds: int = Field(default=900, gt=0)
    workflow_task_hard_limit_seconds: int = Field(default=1020, gt=0)
    celery_visibility_timeout_seconds: int = Field(default=1200, gt=0)
    case_purge_scan_seconds: int = Field(default=3600, gt=0)

    chat_input_token_budget: int = Field(default=24000, gt=0)
    session_summary_token_limit: int = Field(default=2000, gt=0)
    retrieved_context_token_limit: int = Field(default=6000, gt=0)
    retrieved_segment_count_limit: int = Field(default=8, gt=0)
    response_token_reserve: int = Field(default=4000, gt=0)
    chat_stream_enabled: bool = True
    chat_stream_max_bytes: int = Field(default=2097152, ge=4096, le=2097152)
    chat_stream_event_max_bytes: int = Field(default=65536, ge=1024, le=262144)
    chat_stream_max_chars: int = Field(default=50000, ge=1000, le=200000)
    chat_stream_event_chars: int = Field(default=256, ge=64, le=4096)
    chat_stream_poll_seconds: float = Field(default=0.2, ge=0.1, le=5)
    chat_stream_pending_chars: int = Field(default=2048, ge=128, le=8192)
    chat_max_plan_rounds: int = Field(default=2, gt=0)
    chat_max_tool_calls: int = Field(default=4, gt=0)
    chat_max_model_cost_cny: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    upload_max_bytes: int = Field(default=52428800, gt=0)
    upload_max_pages: int = Field(default=100, gt=0)
    case_retention_days: int = Field(default=30, gt=0)
    run_max_model_calls: int = Field(default=120, gt=0)
    run_max_ocr_tasks: int = Field(default=30,gt=0)
    run_max_ocr_polls: int = Field(default=120,gt=0)
    docx_max_embedded_images: int = Field(default=8,gt=0)
    docx_max_embedded_image_bytes: int = Field(default=10485760,gt=0)
    docx_max_embedded_total_bytes: int = Field(default=31457280,gt=0)
    run_max_model_tokens: int = Field(default=1000000, gt=0)
    run_max_model_cost_cny: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    model_prices: dict[ModelRole, ModelPrice] = Field(default_factory=dict)
    model_tenant_max_concurrency: int = Field(default=1, gt=0)
    model_failure_threshold: int = Field(default=4, gt=0)
    model_workflow_stream: bool = False
    model_retry_interrupted: bool = True
    model_max_attempts: int = Field(default=3, ge=1, le=5)
    model_retry_total_timeout_seconds: int = Field(default=600, gt=0, le=3600)
    model_failure_window_seconds: int = Field(default=60, gt=0)
    model_cooldown_seconds: int = Field(default=30, gt=0)
    event_poll_seconds: int = Field(default=5, gt=0)
    recovery_scan_seconds: int = Field(default=30, gt=0)
    deletion_ledger_path: str | None = Field(default=None, validation_alias="DELETE_LEDGER_PATH")
    deletion_ledger_key: SecretStr | None = Field(default=None, validation_alias="DELETE_LEDGER_KEY")
    purge_inflight_grace_seconds: int = Field(default=60, ge=1)

    def model_profile(self, role: ModelRole) -> ModelProfile | None:
        prefix = f"{role.value}_model_"
        fields = (
            "base_url",
            "api_key",
            "name",
            "protocol",
            "timeout_seconds",
            "max_output_tokens",
            "max_concurrency",
        )
        values = {field: getattr(self, prefix + field) for field in fields}

        def filled(value: object) -> bool:
            if isinstance(value, SecretStr):
                return bool(value.get_secret_value().strip())
            if isinstance(value, str):
                return bool(value.strip())
            return value is not None

        if not any(filled(values[field]) for field in fields[:4]):
            return None
        missing = [field for field, value in values.items() if not filled(value)]
        if missing:
            raise ValueError(f"{role.value} 模型配置缺少：{', '.join(missing)}")
        return ModelProfile(
            **values,
            temperature=getattr(self, prefix + "temperature"),
            enable_thinking=getattr(self, prefix + "enable_thinking"),
            context_window_tokens=getattr(self, prefix + "context_window_tokens"),
            price=self.model_price(role, values["base_url"], values["name"], values["protocol"]),
        )

    def model_role_ready(self, role: ModelRole) -> bool:
        """该模型角色是否可受理请求。假适配器下视为可用，但调用方须标明是假实现。"""
        if self.model_adapter is AdapterKind.DETERMINISTIC_FAKE:
            return True
        try:
            return self.model_profile(role) is not None
        except ValueError:
            return False

    def parser_ready(self) -> bool:
        if self.parser_adapter is AdapterKind.DETERMINISTIC_FAKE:
            return True
        return bool(self.mineru_base_url and self.mineru_api_key and self.mineru_api_key.get_secret_value().strip())

    def model_price(self, role: ModelRole, base_url: str, model_name: str, protocol: str) -> ModelPrice | None:
        price = self.model_prices.get(role)
        if price and price.verified and (price.base_url, price.model_name, price.protocol) == (base_url, model_name, protocol):
            return price
        return None

    def model_budget_ready(self, roles: tuple[ModelRole, ...] | None = None) -> tuple[bool, str | None]:
        if self.run_max_model_cost_cny is None or self.model_adapter is AdapterKind.DETERMINISTIC_FAKE:
            return True, None
        for role in roles or tuple(ModelRole):
            try:
                profile = self.model_profile(role)
            except ValueError:
                profile = None
            if profile is None or profile.price is None:
                return False, f"{role.value} 缺少对应模型的已核实人民币全包报价；无法执行人民币费用上限"
        return True, None

    def using_fake_externals(self) -> bool:
        """是否有任一外部服务在用假实现。为真时告知、报告与界面必须显著标注。"""
        return AdapterKind.DETERMINISTIC_FAKE in {self.parser_adapter, self.model_adapter}

    def mcp_allowed_host_list(self) -> tuple[str, ...]:
        """留空表示不允许新增任何外部 MCP 连接，而不是允许全部。"""
        if not self.mcp_allowed_hosts:
            return ()
        return tuple(h.strip().lower() for h in self.mcp_allowed_hosts.split(",") if h.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
