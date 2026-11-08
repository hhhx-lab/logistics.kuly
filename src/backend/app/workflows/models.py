"""
工作流执行表（迁移④）：TaskDraft、Run、Epoch、步骤账本、人工输入、事件与 Outbox。

不变量（后端数据方案第 2.4 与 2.6 节）：
- Run 一经创建，参与的源版本、工作流、目标语言、范围与格式不可改；改变须新 Run。
- 同一草稿最多一个 Run：`runs(tenant_id, draft_task_id)` 唯一。
- 同一 Run 最多一条活动 InputRequest：部分唯一索引兜底。
- 成功步骤对 `(run, step, input_fingerprint)` 唯一，崩溃重入读回既有产物而不再收费。
- 事件序号与业务状态同事务分配；Redis、Celery 与 LangGraph 流不作为历史真值。
- 队列消息只含 `run_id + epoch`，Worker 从数据库读冻结定义。
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.models_base import Base, created_at_column, tenant_column, uuid_pk


class RunStatus(enum.StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    AWAITING_INPUT = "awaiting_input"
    AWAITING_REVIEW = "awaiting_review"
    READY_TO_EXPORT = "ready_to_export"
    EXPORTING = "exporting"
    EXPORTED = "exported"
    # 只属于 read_index
    INDEXED = "indexed"
    CHECKED = "checked"
    PAUSED_RECONCILE = "paused_reconcile"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_FINAL = "failed_final"
    CANCELLED = "cancelled"


class DraftStatus(enum.StrEnum):
    NEEDS_CLARIFICATION = "needs_clarification"
    READY = "ready"
    UNSUPPORTED = "unsupported"


class StepStatus(enum.StrEnum):
    RUNNING = "running"
    # 已持久化请求意图、尚未拿到供应商回执
    SUBMITTING = "submitting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    # 供应商可能已受理但本地无法确认：不得盲重投
    NEEDS_RECONCILE = "needs_reconcile"


class InputRequestStatus(enum.StrEnum):
    ACTIVE = "active"
    RESOLVED = "resolved"
    SUPERSEDED = "superseded"
    CANCELLED = "cancelled"


class TaskDraft(Base):
    __tablename__ = "task_drafts"
    __table_args__ = (
        Index("ix_task_drafts_tenant_id_case_id", "tenant_id", "case_id"),
        UniqueConstraint("tenant_id", "case_id", "id", name="uq_task_drafts_tenant_id_case_id_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    case_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    created_by: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    workflow_type: Mapped[str] = mapped_column(String(40), nullable=False)
    workflow_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 有序的固定源版本集合：[{document_id, document_version}]
    source_versions: Mapped[list[dict]] = mapped_column(JSONB, nullable=False)
    # 按工作流判别的可编辑参数；刷新与冲突后从这里还原表单，不从 task_summary 反解析
    draft_parameters: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # 所选区段的解析基线；W03 据此判定 TASK_DRAFT_PARSE_STALE
    parse_baseline: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[DraftStatus] = mapped_column(
        SAEnum(DraftStatus, name="draft_status", native_enum=False, length=24), nullable=False
    )
    missing_fields: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    questions: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    task_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    draft_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # 软撤销：不删 Case、UploadSession 或已验收原件
    discarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_run_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    created_at: Mapped[datetime] = created_at_column()


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        # 同一草稿最多一个 Run：并发启动由数据库兜底
        UniqueConstraint("tenant_id", "draft_task_id", name="uq_runs_tenant_id_draft_task_id"),
        UniqueConstraint("tenant_id", "case_id", "id", name="uq_runs_tenant_id_case_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "case_id", "draft_task_id"],
            ["task_drafts.tenant_id", "task_drafts.case_id", "task_drafts.id"],
            name="fk_runs_draft_same_case",
        ),
        Index("ix_runs_tenant_id_case_id_status_created_at", "tenant_id", "case_id", "status", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    case_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    draft_task_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    workflow_type: Mapped[str] = mapped_column(String(40), nullable=False)
    workflow_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 不可变 Task Spec 与其规范化哈希；禁止更新由触发器兜底
    task_spec: Mapped[dict] = mapped_column(JSONB, nullable=False)
    task_spec_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # 冻结的 Prompt 版本清单；后续 Epoch 继承，不因代码发布静默改版
    prompt_versions: Mapped[dict] = mapped_column(JSONB, nullable=False)
    target_language: Mapped[str | None] = mapped_column(String(8))
    initiated_by: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    status: Mapped[RunStatus] = mapped_column(
        SAEnum(RunStatus, name="run_status", native_enum=False, length=24), nullable=False
    )
    current_stage: Mapped[str] = mapped_column(String(60), nullable=False, default="等待处理")
    current_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    next_event_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 取消或整票删除时递增；旧 Worker 的迟到结果因栅栏不符而被拒
    execution_fence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_business_revision_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    current_comparison_binding_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    current_translation_revision_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    pause_explanation: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = created_at_column()


class RunDocument(Base):
    """Run 固定参与的原件版本集合，创建后冻结。"""

    __tablename__ = "run_documents"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "run_id", "document_id", "document_version",
            name="uq_run_documents_tenant_id_run_id_document_version",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "case_id", "run_id"],
            ["runs.tenant_id", "runs.case_id", "runs.id"],
            name="fk_run_documents_run_same_case",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "document_id", "document_version"],
            ["document_versions.tenant_id", "document_versions.document_id", "document_versions.version"],
            name="fk_run_documents_document_version",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    case_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    document_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = created_at_column()


class ExecutionEpoch(Base):
    __tablename__ = "execution_epochs"
    __table_args__ = (UniqueConstraint("tenant_id", "run_id", "epoch", name="uq_execution_epochs_tenant_id_run_id_epoch"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    run_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    # initial / recompute / resume_input / retry / export
    purpose: Mapped[str] = mapped_column(String(24), nullable=False)
    graph_version: Mapped[str] = mapped_column(String(40), nullable=False)
    input_business_revision_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    input_parse_artifact_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    repair_request: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # 非秘密 ModelBinding：只含端点主机、模型名与参数指纹，不含 Key
    model_bindings: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = created_at_column()


class StepExecution(Base):
    """
    步骤账本。领域事务先提交产物与账本，再让 LangGraph 写检查点；
    崩溃恰在两者之间时，节点重入按指纹读回既有产物，不再收费。
    """

    __tablename__ = "step_executions"
    __table_args__ = (
        Index("ix_step_executions_tenant_id_run_id_epoch", "tenant_id", "run_id", "epoch"),
        # 有效成功产物对 (run, step, 输入指纹) 唯一；失败尝试仍可追加
        Index(
            "uq_step_executions_succeeded_fingerprint",
            "tenant_id", "run_id", "step_name", "input_fingerprint",
            unique=True,
            postgresql_where=text("status = 'succeeded'"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    run_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    step_name: Mapped[str] = mapped_column(String(60), nullable=False)
    step_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # 须含源版、业务／译文输入、模型、Prompt、规则与适配器版本
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[StepStatus] = mapped_column(
        SAEnum(StepStatus, name="step_status", native_enum=False, length=24), nullable=False
    )
    lease_token: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 发送前就持久化；恢复时先按它向供应商查询
    provider_request_key: Mapped[str | None] = mapped_column(String(128))
    external_task_id: Mapped[str | None] = mapped_column(String(128))
    artifact_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    error_class: Mapped[str | None] = mapped_column(String(64))
    error_detail: Mapped[str | None] = mapped_column(Text)
    usage: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = created_at_column()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StepReconciliation(Base):
    """运营核查结论；只能凭供应商任务记录或账单证据，不能凭主观判断。"""

    __tablename__ = "step_reconciliations"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    step_execution_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("step_executions.id", ondelete="CASCADE"), nullable=False
    )
    # 核查结论与诊断补正追加保存；已响应但本地失败不得标记外部未执行。
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence_reference: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    decided_by: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = created_at_column()


class ModelMigration(Base):
    __tablename__ = "model_migrations"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    run_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    from_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    to_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    old_binding_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    new_binding_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    invalidated_steps: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    decided_by: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = created_at_column()


class InputRequest(Base):
    __tablename__ = "input_requests"
    __table_args__ = (
        # 同一 Run 一次只等待一个运行期问题
        Index(
            "uq_input_requests_one_active_per_run",
            "tenant_id", "run_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    run_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    # document_boundary / source_language / translation_scope
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    request_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # 每条只针对一份物理源文件的一版解析
    document_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    document_version: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_parse_artifact_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    # 首次抽取前尚无业务版时为 null；resolve 须核对当前仍为 null
    expected_business_revision_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    question: Mapped[str] = mapped_column(Text, nullable=False)
    options: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)
    source_refs: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)
    suggested_value: Mapped[str | None] = mapped_column(Text)
    status: Mapped[InputRequestStatus] = mapped_column(
        SAEnum(InputRequestStatus, name="input_request_status", native_enum=False, length=16), nullable=False
    )
    answer: Mapped[dict | None] = mapped_column(JSONB)
    answered_by: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()


class RunEvent(Base):
    """与业务状态同事务分配 seq；SSE 只复播这些行。"""

    __tablename__ = "run_events"
    __table_args__ = (UniqueConstraint("tenant_id", "run_id", "seq", name="uq_run_events_tenant_id_run_id_seq"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    run_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str | None] = mapped_column(String(24))
    stage: Mapped[str | None] = mapped_column(String(60))
    message: Mapped[str | None] = mapped_column(Text)
    # 人读动作分类：process / human / export / ops
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="process")
    actor: Mapped[str | None] = mapped_column(String(120))
    document_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    page_no: Mapped[int | None] = mapped_column(Integer)
    # 只有分母可靠时才填，抽取与翻译没有可靠总量就留空
    completed: Mapped[int | None] = mapped_column(Integer)
    total: Mapped[int | None] = mapped_column(Integer)
    unit: Mapped[str | None] = mapped_column(String(16))
    payload: Mapped[dict | None] = mapped_column(JSONB)
    occurred_at: Mapped[datetime] = created_at_column()


class QueueOutbox(Base):
    """
    与 Run／Epoch 同事务写入；发布器至少一次投递，
    重复投递由唯一键、租约与指纹去重。
    """

    __tablename__ = "queue_outbox"
    __table_args__ = (
        Index("ix_queue_outbox_status_next_attempt_at", "status", "next_attempt_at"),
        UniqueConstraint("tenant_id", "dedupe_key", name="uq_queue_outbox_tenant_id_dedupe_key"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    # run / continuation / purge
    topic: Mapped[str] = mapped_column(String(32), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(160), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # pending / published / failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = created_at_column()
    last_error: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()


class WorkerTenantLookup(Base):
    """
    占位说明：Worker 解析租户用的是数据库函数 `worker_tenant_of_run(uuid)`，
    由迁移创建为 SECURITY DEFINER，只返回 tenant_id，
    不开放任何业务表的无租户直读。这里不建表。
    """

    __abstract__ = True
    is_placeholder: Mapped[bool] = mapped_column(Boolean, default=True)
