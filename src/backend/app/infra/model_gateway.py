"""
Model Gateway：四类模型角色的统一调用入口（单证工作流方案 10.1、10.2）。

对上提供结构化生成、可选结构化增量、文本与图像调用，
对下按 ModelBinding 的协议选适配器。网关本身负责：
- 能力位：只使用 D0 实测确认过的能力；未探测的模型不因「OpenAI 兼容」就推定支持 json_schema 或图片。
- 分类重试：模型传输中断按配置有限重试，每次单独落账并保留未知费用；
  账本写入失败、未分类故障与进程崩溃仍须核查。
- 结构化输出：优先原生严格 json_schema；无论模型是否自称符合，服务端都用 Pydantic 复验，
  结构无效且已有明确响应时最多一次修复请求。
- 调用账本：每次 HTTP 尝试（含重试与修复）各记一条 `ModelCallRecord`；
  API Key 只在发请求那一刻从 Settings 读取，不进记录、不进日志、不进异常文本。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import re
import time
import types
import uuid
from collections.abc import Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any, Generic, Literal, Protocol, TypeVar, Union, get_args, get_origin

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import AdapterKind, ModelRole, Settings
from app.prompts.registry import RenderedPrompt, estimate_tokens
from app.prompts.schemas import (
    TEXT_OUTPUT,
    AnswerDraft,
    IntentCandidate,
    TranslationResult,
    resolve_output_schema,
)
from app.workflows.contracts import ModelBinding

logger = logging.getLogger(__name__)

ResultT = TypeVar("ResultT", bound=BaseModel)
T = TypeVar("T")

OPENAI_PROTOCOL = "openai_chat_completions"
OPENAI_ADAPTER_VERSION = "openai_chat_completions/2"
FAKE_PROTOCOL = "deterministic_fake"
FAKE_ADAPTER_VERSION = "deterministic_fake/1"
FAKE_MODEL_NAME = "deterministic-fake"
# 假适配器产出的每一段文字都带这个标记，任何展示层都能据此识别，绝不能冒充真实结果
FAKE_MARKER = "【假模型输出·非真实结果】"

# 首次加两次；间隔下限 2 秒（方案 10.2）
MIN_RETRY_INTERVAL_SECONDS = 2.0


def model_call_timeout(settings: Settings, binding: ModelBinding) -> float:
    attempts = settings.model_max_attempts
    return min(settings.model_retry_total_timeout_seconds,
        binding.timeout_seconds * attempts + MIN_RETRY_INTERVAL_SECONDS * (2 ** (attempts - 1) - 1))

# 修复请求与 JSON 输出说明是网关协议的一部分，随适配器版本冻结；
# 改动这两段文字必须同时提升 OPENAI_ADAPTER_VERSION，账本才能区分新旧行为。
REPAIR_INSTRUCTION = (
    "上一条输出不符合要求的 JSON Schema，无法被系统接受。"
    "请只输出一个修正后的 JSON 对象，不要附加解释。问题位置："
)
JSON_TEXT_INSTRUCTION = "只输出一个 JSON 对象，不要附加解释，且必须符合以下 JSON Schema：\n"
REPAIR_INSTRUCTION_SHA256 = hashlib.sha256(REPAIR_INSTRUCTION.encode("utf-8")).hexdigest()


# ---- 能力位 ------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilityBits:
    json_object: bool
    json_schema: bool
    image_input: bool


# D0 外部服务探测实测结果（后端实施记录_D0外部服务探测.md 第 2 节）。
# 按「协议 + 模型名」登记：换模型必须重新探测后在此登记，否则按未探测处理。
# 非视觉角色的图片输入「未测」，因此记为不支持，而不是推定支持。
PROBED_CAPABILITIES: dict[tuple[str, str], dict[ModelRole, CapabilityBits]] = {
    (OPENAI_PROTOCOL, "grok-4.6"): {
        ModelRole.CHAT: CapabilityBits(json_object=True, json_schema=True, image_input=False),
        ModelRole.EXTRACT: CapabilityBits(json_object=True, json_schema=True, image_input=False),
        ModelRole.TRANSLATE: CapabilityBits(json_object=True, json_schema=True, image_input=False),
        ModelRole.VISION: CapabilityBits(json_object=True, json_schema=True, image_input=True),
    },
}
UNPROBED_CAPABILITIES = CapabilityBits(json_object=False, json_schema=False, image_input=False)
FAKE_CAPABILITIES = CapabilityBits(json_object=True, json_schema=True, image_input=True)


def capabilities_for(binding: ModelBinding) -> CapabilityBits:
    if binding.protocol == FAKE_PROTOCOL:
        return FAKE_CAPABILITIES
    return PROBED_CAPABILITIES.get((binding.protocol, binding.model_name), {}).get(
        binding.role, UNPROBED_CAPABILITIES
    )


# ---- 账本 --------------------------------------------------------------------


class Usage(BaseModel):
    model_config = ConfigDict(frozen=True)

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    # 推理型模型的推理 token 计入输出预算（D0 实测占绝大部分），单独记下便于评估预算
    reasoning_tokens: int | None = None
    total_tokens: int | None = None


class PromptRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    version: int
    sha256: str


AttemptKind = Literal["initial", "transport_retry", "repair"]
CallOutcome = Literal[
    "succeeded",
    "invalid_output",
    "truncated",
    "not_sent",
    "rejected_retryable",
    "permanent_error",
    "provider_protocol_error",
    "needs_reconcile",
    "interrupted",
    "budget_exhausted",
]


class ModelCallRecord(BaseModel):
    """
    一次 HTTP 尝试的账本记录。只含非秘密配置与指纹：
    不存 API Key、不存 Prompt 渲染后的全文、不存模型原始输出。
    """

    model_config = ConfigDict(frozen=True)

    call_id: str
    provider_request_key: str
    attempt_no: int
    attempt_kind: AttemptKind
    role: ModelRole
    is_fake: bool
    adapter_version: str
    protocol: str
    model_id: str
    provider_model_id: str | None
    endpoint_config_hash: str
    prompt_refs: tuple[PromptRef, ...]
    repair_instruction_sha256: str | None
    params: dict[str, Any]
    input_fingerprint: str
    image_sha256: str | None
    started_at: datetime
    finished_at: datetime
    duration_ms: int
    outcome: CallOutcome
    http_status: int | None
    first_delta_ms: int | None = None
    error_class: str | None
    provider_response_id: str | None
    usage: Usage | None
    # 中转服务没有公开计价（D0 第 4 节），在取得可信计价前保持为空，不编造费用
    cost_cny: Decimal | None = None
    # 结果对象由上层写入对象存储后回填；网关不持有对象存储
    result_object_id: str | None = None


class CallLedger(Protocol):
    def append(self, record: ModelCallRecord) -> None: ...


class CallLedgerWriteError(RuntimeError):
    """已发出请求但持久化响应失败；调用方须保留 SUBMITTING 并暂停核查。"""
    kind = "acceptance_unknown"
    unknown = True


class ModelRetryLimitReached(RuntimeError):
    """同批次跨恢复消息的自动重试仍受同一次数上限约束。"""


class InMemoryCallLedger:
    """测试与单进程联调用；正式环境由上层提供落库实现。"""

    def __init__(self) -> None:
        self.records: list[ModelCallRecord] = []

    def append(self, record: ModelCallRecord) -> None:
        self.records.append(record)


class StepExecutionCallLedger:
    """每次供应商尝试独立提交，Worker 崩溃也不会丢失已收到响应的 usage。

    构造前 Step 必须已提交。只锁定有权步骤，合并 usage 而不覆盖其他计量信息；
    写失败直接抛出，网关停止后续修复/重试，由恢复流程核查请求。
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        tenant_id: uuid.UUID,
        step_execution_id: uuid.UUID,
        lease_token: uuid.UUID | None = None,
        execution_fence: int | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._tenant_id = uuid.UUID(str(tenant_id))
        self.tenant_id = self._tenant_id
        self._step_id = uuid.UUID(str(step_execution_id))
        self._owner = (str(lease_token),execution_fence) if lease_token is not None else None

    def before_send(self) -> None:
        self._before_send()

    def begin_attempt(self, call_id: str, provider_request_key: str, timeout_seconds: float) -> datetime:
        return self._before_send((call_id, provider_request_key, timeout_seconds))

    def _before_send(self, attempt=None) -> datetime | None:
        from app.config import get_settings
        from app.infra.database import TENANT_SETTING
        from app.documents.models import Case,CaseStatus
        from app.workflows.models import Run,RunStatus,StepExecution,StepStatus
        from app.management.ledger import DeletionLedger
        settings=get_settings()
        with self._session_factory() as session,session.begin():
            session.execute(text("SET LOCAL ROLE logistics_worker"))
            session.execute(text(f"SET LOCAL {TENANT_SETTING} = '{self._tenant_id}'"))
            identity=session.get(StepExecution,self._step_id)
            case_id=session.scalar(select(Run.case_id).where(Run.id==identity.run_id)) if identity else None
            case=session.scalar(select(Case).where(Case.id==case_id).with_for_update()) if case_id else None
            run=session.scalar(select(Run).where(Run.id==identity.run_id).with_for_update()) if identity else None
            step=session.scalar(select(StepExecution).where(StepExecution.id==self._step_id).with_for_update()
                .execution_options(populate_existing=True))
            if (case is None or case.status!=CaseStatus.ACTIVE or case.retention_expires_at<=datetime.now(UTC)
                or run.status not in {RunStatus.QUEUED,RunStatus.PROCESSING}
                or step.epoch!=run.current_epoch or step.status!=StepStatus.SUBMITTING):
                raise RuntimeError("模型外发对应单票或步骤已失效")
            owner=(str(step.lease_token),run.execution_fence)
            if self._owner is not None and owner!=self._owner:
                raise RuntimeError("模型外发对应执行所有者已失效")
            if session.scalar(select(StepExecution.id).where(StepExecution.run_id == run.id,
                StepExecution.id != step.id,
                StepExecution.status.in_([StepStatus.SUBMITTING, StepStatus.NEEDS_RECONCILE])).limit(1)):
                raise RuntimeError("本任务仍有其他外部请求待核查，停止模型外发")
            if settings.deletion_ledger_path or settings.deletion_ledger_key:
                if DeletionLedger(settings).is_denied(self._tenant_id,case.id):
                    raise RuntimeError("模型外发对应单票已撤权")
            if attempt:
                call_id,key,seconds=attempt
                usage=dict(step.usage or {})
                if key!=step.provider_request_key or (usage.get("model_attempt") or {}).get("state")=="in_flight":
                    raise RuntimeError("模型尝试请求键无效或上次请求尚未结束")
                recovered_usages=list(session.scalars(select(StepExecution.usage).where(StepExecution.run_id==run.id,
                    StepExecution.input_fingerprint==step.input_fingerprint,StepExecution.epoch==step.epoch,
                    StepExecution.id!=step.id,StepExecution.error_class=="interrupted_model_recovery")))
                if recovered_usages and sum(len((item or {}).get("model_calls") or []) for item in [*recovered_usages,usage])>=settings.model_max_attempts:
                    raise ModelRetryLimitReached("模型中断自动恢复次数已用完")
                usage["model_attempt"]={"call_id":call_id,"provider_request_key":key,"state":"in_flight",
                    "lease_token":owner[0],"execution_fence":owner[1]}
                step.usage=usage
                step.lease_until=datetime.now(UTC)+timedelta(seconds=max(90,seconds+45))
            self._owner=owner
            return step.lease_until if attempt else None

    def append(self, record: ModelCallRecord) -> None:
        from app.infra.database import TENANT_SETTING
        from app.workflows.models import StepExecution

        with self._session_factory() as session, session.begin():
            session.execute(text("SET LOCAL ROLE logistics_worker"))
            session.execute(text(f"SET LOCAL {TENANT_SETTING} = '{self._tenant_id}'"))
            step = session.scalar(
                select(StepExecution)
                .where(StepExecution.id == self._step_id, StepExecution.tenant_id == self._tenant_id)
                .with_for_update()
            )
            if step is None:
                raise RuntimeError("模型调用对应的有权步骤不存在")
            if step.provider_request_key != record.provider_request_key:
                raise RuntimeError("模型调用请求键与持久化步骤不一致")
            usage = dict(step.usage or {})
            calls = list(usage.get("model_calls", []))
            if any(call.get("call_id") == record.call_id for call in calls):
                return
            calls.append(record.model_dump(mode="json"))
            usage["model_calls"] = calls
            marker=usage.get("model_attempt") or {}
            if marker.get("call_id")==record.call_id and marker.get("provider_request_key")==record.provider_request_key:
                usage["model_attempt"]={**marker,"state":"finished","outcome":record.outcome}
                # 结束回执落账后给重试或本地封存留时间；扫描器已撤销的栅栏不在此恢复。
                step.lease_until=datetime.now(UTC)+timedelta(seconds=90)
            step.usage = usage


def successful_step_model_calls(step, *, require_real=False):
    """只认持久化的同请求成功回执；未知或缺失账本不能授权本地失败重试。"""
    calls=(step.usage or {}).get("model_calls",[])
    if not isinstance(calls,list) or not calls or not step.provider_request_key or any(not isinstance(call,dict) or call.get("provider_request_key")!=step.provider_request_key or
            call.get("outcome")=="needs_reconcile" for call in calls):return []
    last=calls[-1]
    if last.get("outcome")!="succeeded" or not last.get("call_id") or not last.get("finished_at"):return []
    if require_real and (last.get("is_fake") is not False or type(last.get("http_status")) is not int
            or not 200<=last["http_status"]<300):return []
    return [call["call_id"] for call in calls if call.get("outcome")=="succeeded" and call.get("call_id")]


# ---- 调用结果 ----------------------------------------------------------------

FailureKind = Literal[
    "unavailable",
    "binding_mismatch",
    "invalid_request",
    "auth",
    "transient_exhausted",
    "invalid_output",
    "output_truncated",
    "provider_protocol_error",
    "budget_exhausted",
]


@dataclass(frozen=True)
class Succeeded(Generic[T]):
    value: T
    records: tuple[ModelCallRecord, ...]
    # 为真时调用方必须在产物、告知与界面上标注为假实现
    is_fake: bool


@dataclass(frozen=True)
class NeedsReconcile:
    """
    请求可能已被供应商受理（可能已收费），但无法确认。上层必须把 Step 置
    `needs_reconcile`、Run 置 `paused_reconcile`，只有核查确认「未执行」后才允许重新提交。
    """

    provider_request_key: str
    reason: str
    records: tuple[ModelCallRecord, ...]


@dataclass(frozen=True)
class Failed:
    kind: FailureKind
    # 可直接显示的中文，不含 Key、原文或供应商原始响应
    message: str
    records: tuple[ModelCallRecord, ...]
    # 仅瞬时错误耗尽重试时为真：稍后可凭同一请求键重新发起
    retry_later: bool = False


ModelOutcome = Union[Succeeded[T], NeedsReconcile, Failed]


@dataclass
class CallBudget:
    """
    单 Run / 单回合的调用与 token 上限。重试与修复同样计入，防止分类重试把预算放大。
    没有可信计价时只能用这两项保守上限，不能声称精确的人民币预算。
    """

    max_calls: int | None = None
    max_total_tokens: int | None = None
    calls_used: int = 0
    tokens_used: int = 0

    def can_start(self, reserved_tokens: int = 0) -> bool:
        if self.max_calls is not None and self.calls_used >= self.max_calls:
            return False
        if self.max_total_tokens is not None and self.tokens_used + reserved_tokens > self.max_total_tokens:
            return False
        return True

    def consume(self, usage: Usage | None, fallback_tokens: int = 0) -> None:
        self.tokens_used += usage.total_tokens if usage and usage.total_tokens is not None else fallback_tokens


@dataclass(frozen=True)
class ImageInput:
    data: bytes = field(repr=False)
    mime: Literal["image/png", "image/jpeg"]

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


# ---- 适配器层 ----------------------------------------------------------------


class _NotSent(Exception):
    """连接未建立，请求确定没有到达供应商。"""


class _Rejected(Exception):
    """供应商明确拒绝且未处理（限流、过载、请求超时未收全）。"""

    def __init__(self, status: int, retry_after: float | None) -> None:
        super().__init__(status)
        self.status = status
        self.retry_after = retry_after


class _Permanent(Exception):
    def __init__(self, kind: Literal["invalid_request", "auth", "unavailable"], status: int | None) -> None:
        super().__init__(kind)
        self.kind = kind
        self.status = status


class _AcceptanceUnknown(Exception):
    def __init__(self, status: int | None, *, reason="transport_error", partial=None) -> None:
        super().__init__(status)
        self.status = status
        self.reason, self.partial = reason, partial


class _ProtocolError(Exception):
    """供应商已给出 200 但回包结构不可用；已受理，不能重投。"""

    def __init__(self, status: int, *, partial: RawCompletion | None = None, reason: str = "invalid_payload") -> None:
        super().__init__(status)
        self.status = status
        self.partial = partial
        self.reason = reason


class _StreamConsumerError(Exception):
    """已收到正文，但本地增量校验失败；不误记为供应商协议异常。"""

    def __init__(self, status: int, *, partial: RawCompletion, reason: str) -> None:
        super().__init__(status)
        self.status, self.partial, self.reason = status, partial, reason


class _SharedCapacityUnavailable(Exception):
    pass


@dataclass(frozen=True)
class RawCompletion:
    content: str | None = field(repr=False)
    finish_reason: str | None
    usage: Usage | None
    provider_model_id: str | None
    provider_response_id: str | None
    http_status: int | None
    first_delta_ms: int | None = None


@dataclass(frozen=True)
class ChatRequest:
    messages: list[dict[str, Any]] = field(repr=False)
    response_format: dict[str, Any] | None = field(repr=False)
    output_mode: Literal["text", "json_schema", "json_object", "prompt_json"]
    max_tokens: int
    temperature: float | None
    output_model: type[BaseModel] | None
    prompts: tuple[RenderedPrompt, ...] = field(repr=False)
    image_sha256: str | None
    on_delta: Callable[[str], Awaitable[None]] | None = None
    on_retry: Callable[[], Awaitable[None]] | None = None
    stream_max_bytes: int = 262144
    stream_event_max_bytes: int = 65536


class ChatAdapter(Protocol):
    version: str
    is_fake: bool

    async def complete(self, binding: ModelBinding, request: ChatRequest, api_key: str | None) -> RawCompletion: ...


def chat_completions_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    # D0 实测：BASE_URL 不含 /v1，端点为 {BASE_URL}/v1/chat/completions。
    # 配成带 /v1 会拼出 /v1/v1，这里按配置错误拒绝，而不是猜测性地去掉一段路径。
    if base.endswith("/v1"):
        raise ValueError("模型 BASE_URL 不应包含 /v1")
    return f"{base}/v1/chat/completions"


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _parse_usage(raw: Any) -> Usage | None:
    if not isinstance(raw, dict):
        return None
    details = raw.get("completion_tokens_details") or {}

    def as_int(v: Any) -> int | None:
        return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None

    return Usage(
        prompt_tokens=as_int(raw.get("prompt_tokens")),
        completion_tokens=as_int(raw.get("completion_tokens")),
        reasoning_tokens=as_int(details.get("reasoning_tokens")) if isinstance(details, dict) else None,
        total_tokens=as_int(raw.get("total_tokens")),
    )


class OpenAICompatibleAdapter:
    version = OPENAI_ADAPTER_VERSION
    is_fake = False

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def complete(self, binding: ModelBinding, request: ChatRequest, api_key: str | None) -> RawCompletion:
        if not api_key:
            raise _Permanent("unavailable", None)
        try:
            url = chat_completions_url(binding.base_url)
        except ValueError:
            raise _Permanent("unavailable", None) from None

        body: dict[str, Any] = {
            "model": binding.model_name,
            "messages": request.messages,
            "max_tokens": request.max_tokens,
        }
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.response_format is not None:
            body["response_format"] = request.response_format
        if binding.enable_thinking is not None:
            body["enable_thinking"] = binding.enable_thinking

        timeout = httpx.Timeout(binding.timeout_seconds, connect=min(10.0, float(binding.timeout_seconds)))
        try:
            resp = await self._client.post(
                url, json=body, headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            # 连接阶段失败：请求体从未离开本机，重试不会造成重复收费
            raise _NotSent() from None
        except (httpx.UnsupportedProtocol, httpx.InvalidURL):
            raise _Permanent("unavailable", None) from None
        except httpx.TransportError as exc:
            raise _AcceptanceUnknown(None, reason=type(exc).__name__) from None

        status = resp.status_code
        if status == 200:
            try:
                payload = resp.json()
                choice = payload["choices"][0]
                message = choice.get("message") or {}
                content = message.get("content")
                if content is not None and not isinstance(content, str):
                    raise TypeError
            except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                raise _ProtocolError(status) from None
            return RawCompletion(
                content=content,
                finish_reason=choice.get("finish_reason"),
                usage=_parse_usage(payload.get("usage")),
                provider_model_id=payload.get("model") if isinstance(payload.get("model"), str) else None,
                provider_response_id=payload.get("id") if isinstance(payload.get("id"), str) else None,
                http_status=status,
            )
        # 408 声明未收全请求、429 为限流拒绝；503/529 只说明不可用，
        # 中转服务可能在上游完成收费后返回此状态，不能仅凭状态码盲重投。
        if status in {408, 429}:
            raise _Rejected(status, _parse_retry_after(resp.headers.get("Retry-After")))
        if status in {401, 403}:
            raise _Permanent("auth", status)
        if 400 <= status < 500:
            raise _Permanent("invalid_request", status)
        # 500/502/504 等：网关或上游可能已完成推理后才失败，无法确认未收费
        raise _AcceptanceUnknown(status)


class DeterministicFakeAdapter:
    """
    无 Key 联调用。输出只由输入指纹决定，便于回放；所有文字都带 FAKE_MARKER，
    布尔与枚举取最保守的值（如 legible=false、意图=clarify），不给出看似可信的结论。
    """

    version = FAKE_ADAPTER_VERSION
    is_fake = True

    async def complete(self, binding: ModelBinding, request: ChatRequest, api_key: str | None) -> RawCompletion:
        canonical = json.dumps(
            {"m": [_message_text(m) for m in request.messages], "img": request.image_sha256},
            ensure_ascii=False,
            sort_keys=True,
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        if request.output_model is None:
            content = f"{FAKE_MARKER} 输入摘要 {digest}"
        else:
            content = json.dumps(_fake_instance(request.output_model, request.prompts, digest), ensure_ascii=False)
        return RawCompletion(
            content=content,
            finish_reason="stop",
            usage=Usage(prompt_tokens=0, completion_tokens=0, reasoning_tokens=0, total_tokens=0),
            provider_model_id=FAKE_MODEL_NAME,
            provider_response_id=f"fake-{digest}",
            http_status=None,
        )


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return f"{message.get('role')}:{content}"
    parts = []
    for part in content or []:
        if part.get("type") == "text":
            parts.append(part.get("text", ""))
    return f"{message.get('role')}:{''.join(parts)}"


_PROTECTED_TOKEN_RE = re.compile(r"⟦([^⟧]+)⟧")


def _fake_instance(model: type[BaseModel], prompts: Sequence[RenderedPrompt], digest: str) -> dict[str, Any]:
    marker = f"{FAKE_MARKER}{digest}"
    variables = prompts[-1].variables if prompts else {}
    if issubclass(model, IntentCandidate):
        return {
            "action": "clarify",
            "workflow_type": None,
            "target_language": None,
            "referenced_labels": [],
            "clarification_question": marker,
            "rationale": marker,
        }
    if model is AnswerDraft:
        return {
            "paragraphs": [
                {"text": marker, "claim_kind": "general_explanation", "evidence_ids": [], "status": "cannot_confirm"}
            ]
        }
    if model is TranslationResult:
        # 逐段回显片段 ID 与占位符，使覆盖与保护符检查的工程链路可走通；译文本身只有标记
        target = variables.get("target_language")
        segments = []
        for seg in variables.get("segments") or []:
            if not isinstance(seg, dict) or "id" not in seg:
                continue
            tokens = _PROTECTED_TOKEN_RE.findall(str(seg.get("text", "")))
            segments.append(
                {
                    "source_segment_ids": [str(seg["id"])],
                    "translated_text": " ".join([marker, *(f"⟦{t}⟧" for t in tokens)]),
                    "protected_token_ids": tokens,
                }
            )
        from app.languages import LANGUAGES
        return {"target_language": target if target in LANGUAGES else "en", "segments": segments}
    return _fake_value_for_model(model, marker)


def _fake_value_for_model(model: type[BaseModel], marker: str) -> dict[str, Any]:
    return {name: _fake_value(f.annotation, marker) for name, f in model.model_fields.items()}


def _fake_value(annotation: Any, marker: str) -> Any:
    origin = get_origin(annotation)
    if origin is Literal:
        return get_args(annotation)[0]
    if origin in (Union, types.UnionType):
        # 可空字段取 null：不编造值
        return None
    if origin in (list, tuple):
        return []
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return _fake_value_for_model(annotation, marker)
    if annotation is bool:
        return False
    if annotation is int:
        return 0
    if annotation is float:
        return 0.0
    return marker


# ---- 结构化 schema -----------------------------------------------------------

# 严格模式下部分供应商拒绝这些校验关键字；它们仍由服务端 Pydantic 执行，发给模型时去掉
_UNSENT_KEYWORDS = {
    "default",
    "title",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "pattern",
    "format",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
}


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            out: dict[str, Any] = {}
            for k, v in node.items():
                if k in {"properties", "$defs"} and isinstance(v, dict):
                    # 这两处的键是字段名/类型名而不是关键字，字段恰好叫 title 也不能被删
                    out[k] = {name: walk(sub) for name, sub in v.items()}
                elif k not in _UNSENT_KEYWORDS:
                    out[k] = walk(v)
            if out.get("type") == "object" and "properties" in out:
                out["required"] = list(out["properties"].keys())
                out["additionalProperties"] = False
            return out
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(model.model_json_schema())


def _strip_fence(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text


def _validation_summary(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        # 只给位置与错误类型，不回传输入值，避免把原文再绕一圈写进日志
        errs = exc.errors(include_url=False, include_input=False, include_context=False)
        return "；".join(f"{'.'.join(str(p) for p in e['loc']) or '(根)'}: {e['type']}" for e in errs[:20])
    return "(根): 不是合法 JSON"


# ---- 网关 --------------------------------------------------------------------


class ModelGateway(Protocol):
    async def aclose(self) -> None: ...

    async def structured_stream(
        self, binding: ModelBinding, prompts: Sequence[RenderedPrompt],
        output_model: type[ResultT], provider_request_key: str,
        on_delta: Callable[[str], Awaitable[None]], *, budget: CallBudget | None = None,
        max_bytes: int = 262144, event_max_bytes: int = 65536,
    ) -> ModelOutcome[ResultT]: ...

    async def structured_generate(
        self,
        binding: ModelBinding,
        prompts: Sequence[RenderedPrompt],
        result_type: type[ResultT],
        provider_request_key: str,
        *,
        budget: CallBudget | None = None,
    ) -> ModelOutcome[ResultT]: ...

    async def text_generate(
        self,
        binding: ModelBinding,
        prompts: Sequence[RenderedPrompt],
        provider_request_key: str,
        *,
        budget: CallBudget | None = None,
    ) -> ModelOutcome[str]: ...

    async def image_generate(
        self,
        binding: ModelBinding,
        prompts: Sequence[RenderedPrompt],
        image: ImageInput,
        provider_request_key: str,
        *,
        result_type: type[ResultT] | None = None,
        budget: CallBudget | None = None,
    ) -> ModelOutcome[Any]: ...


def fake_binding(role: ModelRole) -> ModelBinding:
    values = {
        "role": role.value,
        "base_url": "fake://deterministic",
        "model_name": FAKE_MODEL_NAME,
        "protocol": FAKE_PROTOCOL,
    }
    return ModelBinding(
        role=role,
        base_url=values["base_url"],
        model_name=FAKE_MODEL_NAME,
        protocol=FAKE_PROTOCOL,
        timeout_seconds=1,
        max_output_tokens=8000,
        max_concurrency=4,
        temperature=None,
        public_config_hash=hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest(),
        adapter_version=FAKE_ADAPTER_VERSION,
    )


@dataclass(frozen=True)
class _NeedsRepair:
    summary: str


@dataclass
class _Attempt:
    kind: AttemptKind
    started_at: datetime
    started_mono: float
    call_id: str = field(default_factory=lambda: uuid.uuid4().hex)


class DefaultModelGateway:
    def __init__(
        self,
        settings: Settings,
        *,
        adapter: ChatAdapter | None = None,
        ledger: CallLedger | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._fake_mode = settings.model_adapter is AdapterKind.DETERMINISTIC_FAKE
        if adapter is None:
            adapter = DeterministicFakeAdapter() if self._fake_mode else OpenAICompatibleAdapter()
        if adapter.is_fake != self._fake_mode:
            raise ValueError("适配器与 MODEL_ADAPTER 配置不一致")
        self._adapter = adapter
        self._ledger = ledger or InMemoryCallLedger()
        self._sleep = sleep
        # 进程内许可减少争用，实际跨进程角色/租户额度由Redis原子许可控制。
        self._limiters: dict[ModelRole, asyncio.Semaphore] = {}
        self._closed = False
        self._redis = None

    async def aclose(self) -> None:
        """在创建网关的同一事件循环关闭连接；Worker 可用固定 Runner 执行。"""
        if self._closed:
            return
        close = getattr(self._adapter, "aclose", None)
        if close is not None:
            await close()
        if self._redis is not None:
            await self._redis.aclose()
        self._limiters.clear()
        self._closed = True

    # ---- 绑定与就绪 ----

    @asynccontextmanager
    async def _shared_permit(self, binding: ModelBinding, deadline: float):
        # 真实多进程部署按角色共用Redis许可；许可覆盖单次HTTP最长时间，
        # 过期自动回收，不把每进程semaphore当全局额度。
        if self._fake_mode or not self._settings.redis_url:
            yield
            return
        from redis.asyncio import Redis
        if self._redis is None:
            self._redis=Redis.from_url(self._settings.redis_url,socket_connect_timeout=3,socket_timeout=3)
        key="lda:model:permits:"+binding.role.value
        tenant_id=getattr(self._ledger,"tenant_id",None)
        tenant_key=key+":tenant:"+str(tenant_id) if tenant_id else key+":probe"
        token=uuid.uuid4().hex
        lease_ms=(binding.timeout_seconds+30)*1000
        script="""
        local now=redis.call('TIME')
        local ms=now[1]*1000+math.floor(now[2]/1000)
        if redis.call('EXISTS',KEYS[3])==1 then return -1 end
        redis.call('ZREMRANGEBYSCORE',KEYS[1],'-inf',ms)
        if redis.call('ZCARD',KEYS[1])>=tonumber(ARGV[1]) then return 0 end
        if ARGV[5]=='1' then
          redis.call('ZREMRANGEBYSCORE',KEYS[2],'-inf',ms)
          if redis.call('ZCARD',KEYS[2])>=tonumber(ARGV[4]) then return 0 end
          redis.call('ZADD',KEYS[2],ms+tonumber(ARGV[2]),ARGV[3])
          redis.call('PEXPIRE',KEYS[2],tonumber(ARGV[2])+1000)
        end
        redis.call('ZADD',KEYS[1],ms+tonumber(ARGV[2]),ARGV[3])
        redis.call('PEXPIRE',KEYS[1],tonumber(ARGV[2])+1000)
        return 1
        """
        acquired=False
        try:
            while time.monotonic()<deadline:
                try:
                    result=await self._redis.eval(script,3,key,tenant_key,self._health_key(binding)+":cooldown",
                        binding.max_concurrency,lease_ms,token,self._settings.model_tenant_max_concurrency,"1" if tenant_id else "0")
                except Exception:raise _SharedCapacityUnavailable() from None
                if result==-1:raise _SharedCapacityUnavailable()
                acquired=result==1
                if acquired:break
                await asyncio.sleep(min(.2,max(0,deadline-time.monotonic())))
            if not acquired:raise _SharedCapacityUnavailable()
            yield
        finally:
            if acquired:
                try:
                    await self._redis.zrem(key,token)
                    if tenant_id:await self._redis.zrem(tenant_key,token)
                except Exception:
                    # 许可最迟按TTL回收，不能以释放失败掩盖已受理模型结果。
                    logger.warning("model_shared_permit_release_deferred role=%s",binding.role.value)

    def _health_key(self,binding):
        return "lda:model:health:"+binding.role.value+":"+binding.public_config_hash

    async def _provider_health(self,binding,*,success=False):
        if self._fake_mode or self._redis is None:return
        key=self._health_key(binding)
        try:
            if success:
                await self._redis.delete(key+":failures")
            else:
                await self._redis.eval("""
                local count=redis.call('INCR',KEYS[1])
                if count==1 then redis.call('EXPIRE',KEYS[1],ARGV[2]) end
                if count>=tonumber(ARGV[1]) then redis.call('SET',KEYS[2],'1','EX',ARGV[3],'NX') end
                return count
                """,2,key+":failures",key+":cooldown",self._settings.model_failure_threshold,
                    self._settings.model_failure_window_seconds,self._settings.model_cooldown_seconds)
        except Exception:
            # 供应商响应已确定，健康指标失败不能改写其受理结论或触发重投。
            logger.warning("model_health_update_deferred role=%s",binding.role.value)

    def binding_for(self, role: ModelRole) -> ModelBinding | None:
        """当前配置下的非秘密绑定，供首个 ExecutionEpoch / 回合冻结。未配置返回 None。"""
        if self._fake_mode:
            return fake_binding(role)
        try:
            profile = self._settings.model_profile(role)
        except ValueError:
            return None
        if profile is None:
            return None
        return ModelBinding.from_profile(role, profile, OPENAI_ADAPTER_VERSION)

    def role_ready(self, role: ModelRole, *, needs_image: bool = False) -> tuple[bool, str | None]:
        binding = self.binding_for(role)
        if binding is None:
            return False, f"{role.value} 模型未配置"
        if binding.protocol not in {OPENAI_PROTOCOL, FAKE_PROTOCOL}:
            return False, f"{role.value} 模型协议 {binding.protocol} 没有经验证的适配器"
        if needs_image and not capabilities_for(binding).image_input:
            return False, f"{role.value} 模型未经探测确认支持图片输入"
        return True, None

    def _limiter(self, binding: ModelBinding) -> asyncio.Semaphore:
        sem = self._limiters.get(binding.role)
        if sem is None:
            sem = asyncio.Semaphore(binding.max_concurrency)
            self._limiters[binding.role] = sem
        return sem

    def _api_key_for(self, binding: ModelBinding) -> tuple[str | None, Failed | None]:
        """
        Key 只在这里读取并直接交给适配器。冻结绑定的端点、模型名或协议与当前配置不同，
        说明 .env 已被改动：旧 Run 不能用新端点的 Key 静默改版，须经运营迁移。
        """
        if self._fake_mode:
            if binding.protocol != FAKE_PROTOCOL:
                return None, Failed("binding_mismatch", "当前为假模型模式，不能执行真实模型绑定的调用", ())
            return None, None
        if binding.protocol == FAKE_PROTOCOL:
            return None, Failed("binding_mismatch", "假模型绑定不能在真实模型模式下执行", ())
        try:
            profile = self._settings.model_profile(binding.role)
        except ValueError:
            profile = None
        if profile is None:
            return None, Failed("unavailable", f"{binding.role.value} 模型未配置或配置不完整", ())
        if (profile.base_url, profile.name, profile.protocol) != (
            binding.base_url,
            binding.model_name,
            binding.protocol,
        ):
            return None, Failed("binding_mismatch", "冻结的模型绑定与当前配置不一致，需要运营迁移后再继续", ())
        return profile.api_key.get_secret_value(), None

    # ---- 三种内部能力 ----

    async def structured_generate(
        self,
        binding: ModelBinding,
        prompts: Sequence[RenderedPrompt],
        result_type: type[ResultT],
        provider_request_key: str,
        *,
        budget: CallBudget | None = None,
    ) -> ModelOutcome[ResultT]:
        self._check_prompts(binding, prompts, result_type)
        return await self._generate(binding, prompts, result_type, None, provider_request_key, budget)

    async def text_generate(
        self,
        binding: ModelBinding,
        prompts: Sequence[RenderedPrompt],
        provider_request_key: str,
        *,
        budget: CallBudget | None = None,
    ) -> ModelOutcome[str]:
        self._check_prompts(binding, prompts, None)
        return await self._generate(binding, prompts, None, None, provider_request_key, budget)

    async def image_generate(
        self,
        binding: ModelBinding,
        prompts: Sequence[RenderedPrompt],
        image: ImageInput,
        provider_request_key: str,
        *,
        result_type: type[ResultT] | None = None,
        budget: CallBudget | None = None,
    ) -> ModelOutcome[Any]:
        self._check_prompts(binding, prompts, result_type)
        if not capabilities_for(binding).image_input:
            return Failed("unavailable", "该模型角色未经探测确认支持图片输入", ())
        return await self._generate(binding, prompts, result_type, image, provider_request_key, budget)

    # ---- 内部 ----

    async def structured_stream(self, binding, prompts, output_model, provider_request_key, on_delta, *, budget=None, max_bytes=262144, event_max_bytes=65536, on_retry=None):
        self._check_prompts(binding, prompts, output_model)
        return await self._generate(binding, prompts, output_model, None, provider_request_key, budget,
            on_delta=on_delta, on_retry=on_retry, stream_max_bytes=max_bytes, stream_event_max_bytes=event_max_bytes)

    @staticmethod
    def _check_prompts(
        binding: ModelBinding, prompts: Sequence[RenderedPrompt], result_type: type[BaseModel] | None
    ) -> None:
        # 这些是编程错误而非运行时状况，直接抛出，不伪装成可重试的失败
        if not prompts:
            raise ValueError("至少需要一个 Prompt")
        if prompts[-1].message_role != "user":
            raise ValueError("最后一个 Prompt 必须是 user 段")
        seen_user = False
        for p in prompts:
            if p.role is not binding.role:
                raise ValueError(f"{p.ref} 属于 {p.role.value} 角色，不能用 {binding.role.value} 绑定调用")
            if p.message_role == "user":
                seen_user = True
            elif seen_user:
                raise ValueError("system 段必须位于 user 段之前")
        expected = resolve_output_schema(prompts[-1].output_schema)
        if result_type is None:
            if prompts[-1].output_schema != TEXT_OUTPUT:
                raise ValueError(f"{prompts[-1].ref} 声明输出 {prompts[-1].output_schema}，不能按自由文本调用")
        elif expected is not result_type:
            raise ValueError(f"{prompts[-1].ref} 的输出 schema 为 {prompts[-1].output_schema}，与调用方类型不符")

    def _build_request(
        self,
        binding: ModelBinding,
        prompts: Sequence[RenderedPrompt],
        output_model: type[BaseModel] | None,
        image: ImageInput | None,
    ) -> ChatRequest:
        caps = capabilities_for(binding)
        messages: list[dict[str, Any]] = [{"role": p.message_role, "content": p.text} for p in prompts[:-1]]
        user_text = prompts[-1].text
        response_format: dict[str, Any] | None = None
        mode: Literal["text", "json_schema", "json_object", "prompt_json"] = "text"
        if output_model is not None:
            schema = strict_json_schema(output_model)
            if caps.json_schema:
                mode = "json_schema"
                response_format = {
                    "type": "json_schema",
                    "json_schema": {"name": output_model.__name__, "strict": True, "schema": schema},
                }
            else:
                # 未确认原生约束时退到「JSON 文本 + 服务端 Pydantic 校验」（方案 10.1）
                mode = "json_object" if caps.json_object else "prompt_json"
                if caps.json_object:
                    response_format = {"type": "json_object"}
                user_text = f"{user_text}\n\n{JSON_TEXT_INSTRUCTION}{json.dumps(schema, ensure_ascii=False)}"
        if image is not None:
            data_url = f"data:{image.mime};base64,{base64.b64encode(image.data).decode('ascii')}"
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_text},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            )
        else:
            messages.append({"role": "user", "content": user_text})
        return ChatRequest(
            messages=messages,
            response_format=response_format,
            output_mode=mode,
            max_tokens=min(binding.max_output_tokens, prompts[-1].max_output_tokens),
            temperature=binding.temperature,
            output_model=output_model,
            prompts=tuple(prompts),
            image_sha256=image.sha256 if image else None,
        )

    @staticmethod
    def _fingerprint(binding: ModelBinding, request: ChatRequest) -> str:
        canonical = json.dumps(
            {
                "binding": binding.public_config_hash,
                "messages": [_message_text(m) for m in request.messages],
                "image": request.image_sha256,
                "response_format": request.response_format,
                "max_tokens": request.max_tokens,
                "temperature": request.temperature,
                "stream": request.on_delta is not None,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    async def _generate(
        self,
        binding: ModelBinding,
        prompts: Sequence[RenderedPrompt],
        output_model: type[BaseModel] | None,
        image: ImageInput | None,
        provider_request_key: str,
        budget: CallBudget | None,
        *, on_delta=None, on_retry=None, stream_max_bytes=262144, stream_event_max_bytes=65536,
    ) -> ModelOutcome[Any]:
        if self._closed:
            return Failed("unavailable", "模型网关已经关闭", ())
        if not provider_request_key or len(provider_request_key) > 128:
            raise ValueError("provider_request_key 必须是 1 至 128 字符的已持久化请求键")
        if binding.adapter_version != self._adapter.version:
            return Failed("binding_mismatch", "冻结的协议适配器版本不可用，须受控迁移后继续", ())
        api_key, problem = self._api_key_for(binding)
        if problem is not None:
            return problem
        ok, reason = self.role_ready(binding.role, needs_image=image is not None)
        if not ok:
            return Failed("unavailable", reason or "模型不可用", ())

        request = self._build_request(binding, prompts, output_model, image)
        from dataclasses import replace
        buffered_stream = (on_delta is None and image is None and not self._fake_mode
            and self._settings.model_workflow_stream and binding.role in {ModelRole.EXTRACT, ModelRole.TRANSLATE})
        if buffered_stream:
            async def receive(_text):
                pass
            on_delta = receive
            stream_max_bytes = 2_097_152
        request = replace(request, on_delta=on_delta, on_retry=on_retry, stream_max_bytes=stream_max_bytes, stream_event_max_bytes=stream_event_max_bytes)
        if self._reserved_tokens(request) > binding.context_window_tokens:
            return Failed("budget_exhausted", "请求输入及输出预留超过冻结模型上下文窗口，请减少输入或调整已验证的窗口配置", ())
        records: list[ModelCallRecord] = []
        attempt_counter = [0]
        deadline = time.monotonic() + model_call_timeout(self._settings, binding)

        async with self._limiter(binding):
            sent = await self._send(binding, request, api_key, provider_request_key, budget, records, attempt_counter, "initial", deadline)
            if not isinstance(sent, tuple):
                return sent
            raw, attempt = sent
            result = self._evaluate(binding, request, provider_request_key, raw, attempt, records, attempt_counter, budget)
            if not isinstance(result, _NeedsRepair):
                return result

            if on_delta is not None and not buffered_stream:
                return Failed("invalid_output", "增量输出结构不完整，已收到的候选不自动重新生成", tuple(records))

            # 仅一次修复：保留原对话，追加模型自己的无效输出与问题位置
            repair_request = ChatRequest(
                messages=[
                    *request.messages,
                    {"role": "assistant", "content": raw.content or ""},
                    {"role": "user", "content": REPAIR_INSTRUCTION + result.summary},
                ],
                response_format=request.response_format,
                output_mode=request.output_mode,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                output_model=request.output_model,
                prompts=request.prompts,
                image_sha256=request.image_sha256,
                on_delta=request.on_delta if buffered_stream else None,
                stream_max_bytes=request.stream_max_bytes,
                stream_event_max_bytes=request.stream_event_max_bytes,
            )
            if self._reserved_tokens(repair_request) > binding.context_window_tokens:
                return Failed("budget_exhausted", "原输出结构无效，修复请求超过冻结上下文窗口，未发出第二次请求", tuple(records))
            sent = await self._send(
                binding, repair_request, api_key, provider_request_key, budget, records, attempt_counter, "repair", deadline
            )
            if not isinstance(sent, tuple):
                return sent
            raw, attempt = sent
            final = self._evaluate(
                binding, repair_request, provider_request_key, raw, attempt, records, attempt_counter, budget
            )
            if isinstance(final, _NeedsRepair):
                return Failed("invalid_output", "模型输出结构两次均不符合要求，本步骤未产生结果", tuple(records))
            return final

    async def _send(
        self,
        binding: ModelBinding,
        request: ChatRequest,
        api_key: str | None,
        provider_request_key: str,
        budget: CallBudget | None,
        records: list[ModelCallRecord],
        counter: list[int],
        first_kind: AttemptKind,
        deadline: float,
    ) -> tuple[RawCompletion, _Attempt] | Failed | NeedsReconcile:
        limit = self._settings.model_max_attempts
        for i in range(limit):
            kind: AttemptKind = first_kind if i == 0 else "transport_retry"
            if counter[0] >= limit:
                return Failed("transient_exhausted", "模型调用已达到重试次数上限，请稍后重试", tuple(records), retry_later=True)
            if budget is not None and not budget.can_start(self._reserved_tokens(request)):
                return Failed("budget_exhausted", "已达到本次任务的模型调用或 token 上限", tuple(records))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return Failed("transient_exhausted", "模型调用总时限已用完，请稍后重试", tuple(records), retry_later=True)
            if budget is not None:
                budget.calls_used += 1
            attempt = _Attempt(kind=kind, started_at=datetime.now(UTC), started_mono=time.monotonic())
            try:
                async with self._shared_permit(binding,deadline):
                    begin_attempt=getattr(self._ledger,"begin_attempt",None)
                    before_send=getattr(self._ledger,"before_send",None)
                    if begin_attempt or before_send:
                        try:
                            if begin_attempt:
                                lease_until=await asyncio.to_thread(begin_attempt,attempt.call_id,provider_request_key,
                                    min(binding.timeout_seconds,deadline-time.monotonic()))
                            else:await asyncio.to_thread(before_send)
                        except ModelRetryLimitReached:
                            return Failed("transient_exhausted","模型中断自动恢复次数已用完，已完成步骤保留，可稍后重试",tuple(records),retry_later=True)
                        except Exception:return Failed("unavailable","单票外发门禁不可用或已撤权，本次未发送请求",tuple(records))
                        if begin_attempt:
                            try:
                                if before_send:await asyncio.to_thread(before_send)
                            except Exception:
                                self._record(binding,request,provider_request_key,attempt,counter,records,
                                    "not_sent",error_class="preflight_revoked")
                                return Failed("unavailable","模型请求发送前单票或步骤已失效，本次未发送请求",tuple(records))
                            if lease_until is not None and lease_until<=datetime.now(UTC):
                                self._record(binding,request,provider_request_key,attempt,counter,records,
                                    "not_sent",error_class="preflight_lease_expired")
                                return Failed("transient_exhausted","模型请求发送前租约已过期，已完成步骤保留，可稍后重试",tuple(records),retry_later=True)
                    remaining=min(binding.timeout_seconds, deadline-time.monotonic())
                    if request.on_delta is not None:
                        from app.infra.model_stream import complete_stream
                        raw = await asyncio.wait_for(complete_stream(self._adapter._client, binding, request, api_key), timeout=remaining)
                    else:
                        raw = await asyncio.wait_for(self._adapter.complete(binding, request, api_key), timeout=remaining)
                await self._provider_health(binding,success=True)
                return raw, attempt
            except _SharedCapacityUnavailable:
                return Failed("unavailable","共享模型许可不可用或已超时，本次未发送请求",tuple(records),retry_later=True)
            except _NotSent:
                await self._provider_health(binding)
                self._record(binding, request, provider_request_key, attempt, counter, records, "not_sent", error_class="connect")
                delay = MIN_RETRY_INTERVAL_SECONDS
            except _Rejected as exc:
                await self._provider_health(binding)
                self._record(
                    binding, request, provider_request_key, attempt, counter, records, "rejected_retryable",
                    http_status=exc.status, error_class="rejected",
                )
                # 尊重 Retry-After，但不低于方案规定的 2 秒下限
                delay = max(MIN_RETRY_INTERVAL_SECONDS, exc.retry_after or 0.0)
                # Retry-After 超过单次调用超时说明短期内不会恢复，与其占着许可空等，不如交回上层
                if exc.retry_after is not None and exc.retry_after > binding.timeout_seconds:
                    return Failed("transient_exhausted", "模型服务限流，请稍后重试", tuple(records), retry_later=True)
            except _Permanent as exc:
                self._record(
                    binding, request, provider_request_key, attempt, counter, records, "permanent_error",
                    http_status=exc.status, error_class=exc.kind,
                )
                message = {
                    "auth": "模型服务拒绝了凭据或权限",
                    "invalid_request": "模型服务判定请求参数无效",
                    "unavailable": "模型配置不可用",
                }[exc.kind]
                return Failed(exc.kind, message, tuple(records))
            except _ProtocolError as exc:
                self._record(
                    binding, request, provider_request_key, attempt, counter, records, "provider_protocol_error",
                    raw=exc.partial, http_status=exc.status, error_class="protocol:" + exc.reason,
                )
                return Failed("provider_protocol_error", "模型服务回包结构异常，本步骤未产生结果", tuple(records))
            except _StreamConsumerError as exc:
                self._record(
                    binding, request, provider_request_key, attempt, counter, records, "invalid_output",
                    raw=exc.partial, http_status=exc.status, error_class="stream_consumer:" + exc.reason,
                )
                return Failed("invalid_output", "回复增量未通过本地校验，本次候选未定稿且未自动重新请求", tuple(records))
            except (_AcceptanceUnknown, TimeoutError) as exc:
                await self._provider_health(binding)
                partial = exc.partial if isinstance(exc, _AcceptanceUnknown) else None
                may_retry = self._settings.model_retry_interrupted and (
                    request.on_delta is None or binding.role in {ModelRole.EXTRACT, ModelRole.TRANSLATE} or request.on_retry is not None)
                self._record(
                    binding, request, provider_request_key, attempt, counter, records, "interrupted" if may_retry else "needs_reconcile",
                    raw=partial, http_status=exc.status if isinstance(exc, _AcceptanceUnknown) else None,
                    error_class="transport:" + exc.reason if isinstance(exc, _AcceptanceUnknown) else "transport:attempt_timeout",
                )
                if not may_retry:
                    return NeedsReconcile(provider_request_key, "模型中断自动重试未启用，受理状态待核查", tuple(records))
                if budget is not None:
                    budget.consume(partial.usage if partial else None, self._reserved_tokens(request))
                if counter[0] >= limit:
                    return Failed("transient_exhausted", "模型请求中断且重试次数已用完，已完成步骤保留，可稍后重试", tuple(records), retry_later=True)
                delay = MIN_RETRY_INTERVAL_SECONDS * 2 ** i
                if request.on_retry is not None:
                    await request.on_retry()
            except asyncio.CancelledError:
                # 已进入适配器后取消，不能证明未受理；先落账，仍传播取消信号。
                self._record(binding, request, provider_request_key, attempt, counter, records,
                             "needs_reconcile", error_class="cancelled_in_flight")
                raise
            except Exception:
                # 已进入适配器后出现未分类异常，不能证明供应商未受理。
                self._record(binding, request, provider_request_key, attempt, counter, records,
                             "needs_reconcile", error_class="unexpected_in_flight")
                return NeedsReconcile(provider_request_key=provider_request_key,
                    reason="模型请求在发送阶段异常中断，受理与计费状态需核查；本次未自动重投",
                    records=tuple(records))
            if counter[0] < limit:
                if delay >= deadline - time.monotonic():
                    return Failed("transient_exhausted", "重试等待超过模型调用总时限，请稍后重试", tuple(records), retry_later=True)
                await self._sleep(delay)
        return Failed("transient_exhausted", "模型服务暂时不可用，重试后仍未成功", tuple(records), retry_later=True)

    def _evaluate(
        self,
        binding: ModelBinding,
        request: ChatRequest,
        provider_request_key: str,
        raw: RawCompletion,
        attempt: _Attempt,
        records: list[ModelCallRecord],
        counter: list[int],
        budget: CallBudget | None,
    ) -> ModelOutcome[Any] | _NeedsRepair:
        if budget is not None:
            budget.consume(raw.usage, self._reserved_tokens(request))
        content = raw.content or ""
        if raw.finish_reason == "length":
            # 推理型模型的推理 token 计入输出上限；被截断时同预算的修复请求大概率再被截断，
            # 因此不修复，直接交回上层按预算或缩小片段处理
            self._record(binding, request, provider_request_key, attempt, counter, records, "truncated",
                         raw=raw, error_class="finish_length")
            return Failed("output_truncated", "模型输出被截断，本步骤未产生结果", tuple(records))
        if request.output_model is None:
            if not content.strip():
                self._record(binding, request, provider_request_key, attempt, counter, records, "invalid_output",
                             raw=raw, error_class="empty")
                return Failed("invalid_output", "模型返回了空内容，本步骤未产生结果", tuple(records))
            self._record(binding, request, provider_request_key, attempt, counter, records, "succeeded", raw=raw)
            return Succeeded(content, tuple(records), self._adapter.is_fake)
        try:
            value = request.output_model.model_validate_json(_strip_fence(content))
        except ValidationError as exc:
            self._record(binding, request, provider_request_key, attempt, counter, records, "invalid_output",
                         raw=raw, error_class="schema")
            return _NeedsRepair(_validation_summary(exc))
        self._record(binding, request, provider_request_key, attempt, counter, records, "succeeded", raw=raw)
        return Succeeded(value, tuple(records), self._adapter.is_fake)

    @staticmethod
    def _reserved_tokens(request: ChatRequest) -> int:
        from app.prompts.registry import estimate_tokens

        # 不含图片的供应商 tokenizer 时，图片输入预算无法精确估算，额外保守预留。
        return sum(estimate_tokens(_message_text(m)) + 12 for m in request.messages) + request.max_tokens + (4096 if request.image_sha256 else 0)

    def _record(
        self,
        binding: ModelBinding,
        request: ChatRequest,
        provider_request_key: str,
        attempt: _Attempt,
        counter: list[int],
        records: list[ModelCallRecord],
        outcome: CallOutcome,
        *,
        raw: RawCompletion | None = None,
        http_status: int | None = None,
        error_class: str | None = None,
    ) -> None:
        counter[0] += 1
        finished = datetime.now(UTC)
        record = ModelCallRecord(
            call_id=attempt.call_id,
            provider_request_key=provider_request_key,
            attempt_no=counter[0],
            attempt_kind=attempt.kind,
            role=binding.role,
            is_fake=self._adapter.is_fake,
            adapter_version=self._adapter.version,
            protocol=binding.protocol,
            model_id=binding.model_name,
            provider_model_id=raw.provider_model_id if raw else None,
            endpoint_config_hash=binding.public_config_hash,
            prompt_refs=tuple(PromptRef(key=p.key, version=p.version, sha256=p.sha256) for p in request.prompts),
            repair_instruction_sha256=REPAIR_INSTRUCTION_SHA256 if isinstance(request.messages[-1].get("content"), str) and request.messages[-1]["content"].startswith(REPAIR_INSTRUCTION) else None,
            params={
                "max_tokens": request.max_tokens,
                "enable_thinking": binding.enable_thinking,
                "temperature": request.temperature,
                "stream": request.on_delta is not None,
                "output_mode": request.output_mode,
                "output_schema": request.prompts[-1].output_schema,
                "estimated_total_tokens": self._reserved_tokens(request),
                "estimated_input_tokens": self._reserved_tokens(request) - request.max_tokens,
                "actual_input_tokens": raw.usage.prompt_tokens if raw and raw.usage else None,
                "input_estimation_delta": raw.usage.prompt_tokens - (self._reserved_tokens(request) - request.max_tokens) if raw and raw.usage and raw.usage.prompt_tokens is not None else None,
            },
            input_fingerprint=self._fingerprint(binding, request),
            image_sha256=request.image_sha256,
            started_at=attempt.started_at,
            finished_at=finished,
            duration_ms=int((time.monotonic() - attempt.started_mono) * 1000),
            outcome=outcome,
            http_status=raw.http_status if raw else http_status,
            first_delta_ms=raw.first_delta_ms if raw else None,
            error_class=error_class,
            provider_response_id=raw.provider_response_id if raw else None,
            usage=raw.usage if raw else None,
        )
        records.append(record)
        try:
            self._ledger.append(record)
        except Exception:
            raise CallLedgerWriteError("模型响应台账写入失败，供应商受理结果需核查") from None
        # 日志只含 ID、角色、结果与耗时；不含 Key、Prompt 正文或模型输出
        logger.info(
            "model_call key=%s attempt=%d kind=%s role=%s outcome=%s status=%s ms=%d fake=%s",
            provider_request_key,
            record.attempt_no,
            record.attempt_kind,
            record.role.value,
            outcome,
            record.http_status,
            record.duration_ms,
            record.is_fake,
        )


def build_model_gateway(
    settings: Settings,
    *,
    ledger: CallLedger | None = None,
    client: httpx.AsyncClient | None = None,
) -> DefaultModelGateway:
    if settings.model_adapter is AdapterKind.DETERMINISTIC_FAKE:
        return DefaultModelGateway(settings, adapter=DeterministicFakeAdapter(), ledger=ledger)
    return DefaultModelGateway(settings, adapter=OpenAICompatibleAdapter(client), ledger=ledger)
