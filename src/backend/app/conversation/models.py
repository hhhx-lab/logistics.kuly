"""
对话表（迁移⑥ 对话部分）。

不变量（数据方案第 2.5 节、Agent 方案第 3–5 节）：
- 一个会话只绑定一个活动 Case；跨票引用拒绝并引导新建会话。
- 一次用户提交＋最终答复算一轮；同会话同时只允许一个非终态 Turn。
- 每轮发送时冻结引用为不可变 ReferenceSnapshot，后续版本变化不改写旧回答。
- TurnEvent 有独立 seq；`message_delta` 是已通过证据校验的段落，不是模型原始 token。
- 续答挂在原 parent turn 上，不计为新的用户轮次；按 (parent_turn, run) 唯一。
- 滚动摘要只属于本会话，新会话查询绝不包含其 ID。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
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


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_tenant_id_owner_updated_at", "tenant_id", "owner_user_id", "updated_at", "id"),
        Index("ix_conversations_purge_after", "purge_after", postgresql_where=text("deleted_at IS NOT NULL")),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    owner_user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    # active / ended
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    # 首次绑定后不可换票；Case 被删时会话随整票清理
    case_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("cases.id", ondelete="CASCADE"))
    active_run_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    active_task_draft_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    # 预置的结构化标签；刷新后仍能恢复
    initial_reference_selections: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)
    entry_source: Mapped[str | None] = mapped_column(String(40))
    conversation_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    next_message_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    turn_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purge_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = created_at_column()


class ReferenceSnapshot(Base):
    """发送时服务端授权并冻结的完整 ID／版本集合，不可变。"""

    __tablename__ = "reference_snapshots"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    case_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    # 用户所选标签原样保存，便于回显
    selections: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)
    # 冻结结果：每个标签解析出的具体原件版、解析版、业务版、快照等
    frozen: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)
    capabilities: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)
    business_revision_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    created_at: Mapped[datetime] = created_at_column()


class ConversationTurn(Base):
    __tablename__ = "conversation_turns"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "conversation_id", "idempotency_key",
            name="uq_conversation_turns_tenant_id_conversation_id_idempotency_key",
        ),
        # 同一会话最多一个非终态回合：并发发送由数据库兜底，返回 TURN_BUSY
        Index(
            "uq_conversation_turns_one_open_per_conversation",
            "tenant_id", "conversation_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    turn_no: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(80), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # pending / running / completed / interrupted / failed
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reference_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    graph_version: Mapped[str] = mapped_column(String(40), nullable=False)
    # 本轮冻结的 Prompt 版本、记忆版本与非秘密模型配置
    prompt_versions: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    memory_version: Mapped[int | None] = mapped_column(Integer)
    model_binding_hash: Mapped[str | None] = mapped_column(String(64))
    next_event_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    final_message_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    error_class: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = created_at_column()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Message(Base):
    """已落库消息。历史回答保留当时版本，新版本只追加「基于历史版本」提示。"""

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("tenant_id", "conversation_id", "seq", name="uq_messages_tenant_id_conversation_id_seq"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    turn_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    # 续答挂在原问题的回合上，不新建用户轮次
    parent_turn_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    # user / assistant
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    author: Mapped[str] = mapped_column(String(120), nullable=False)
    # sending / generating / completed / interrupted / failed
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    references: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)
    capabilities: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)
    attachments: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)
    # 证据卡、任务卡、问题卡等结构化对象，由服务端生成，不从模型输出猜
    cards: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)
    continuation: Mapped[dict | None] = mapped_column(JSONB)
    reference_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    created_at: Mapped[datetime] = created_at_column()


class TurnEvent(Base):
    __tablename__ = "turn_events"
    __table_args__ = (UniqueConstraint("tenant_id", "turn_id", "seq", name="uq_turn_events_tenant_id_turn_id_seq"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    turn_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("conversation_turns.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    # message_delta / tool_started / tool_finished / evidence_card / message_final / message_error
    # continuation_pending / continuation_status / continuation_final
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    message_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = created_at_column()


class AssistantContinuation(Base):
    """
    上传后只问内容时的异步续答登记。

    首轮说明、本登记、Turn 完成与 Outbox 同一事务提交；
    Run 到 `indexed` 时在同一 Run 行锁下唤醒。
    """

    __tablename__ = "assistant_continuations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "parent_turn_id", "run_id", name="uq_assistant_continuations_parent_turn_run"),
        Index("ix_assistant_continuations_status_next_attempt", "status", "next_attempt_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    parent_turn_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    case_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    source_versions: Mapped[list[dict]] = mapped_column(JSONB, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    # waiting / awaiting_retry / awaiting_reconcile / running / completed / failed / cancelled / restricted / closed
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_class: Mapped[str | None] = mapped_column(String(64))
    lease_token: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 实际使用的索引产物指纹，与 continuation_id 一起去重
    indexed_artifact_set_hash: Mapped[str | None] = mapped_column(String(64))
    answer_message_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = created_at_column()


class ConversationSummary(Base):
    """滚动摘要只属于本会话；摘要不作为单票事实证据。"""

    __tablename__ = "conversation_summaries"
    __table_args__ = (Index("ix_conversation_summaries_conversation_id", "tenant_id", "conversation_id", "created_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    covers_through_turn_no: Mapped[int] = mapped_column(Integer, nullable=False)
    source_message_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(40), nullable=False)
    summary_text: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = created_at_column()


class CheckpointRef(Base):
    """Case 与图线程 ID 的映射；整票删除时据此清理 LangGraph 检查点。"""

    __tablename__ = "checkpoint_refs"
    __table_args__ = (UniqueConstraint("tenant_id", "thread_id", name="uq_checkpoint_refs_tenant_id_thread_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    case_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    # run:{uuid}:epoch:{n} / conversation:{opaque} / continuation:{uuid}
    thread_id: Mapped[str] = mapped_column(String(160), nullable=False)
    created_at: Mapped[datetime] = created_at_column()


class ConversationWorkItem(Base):
    """图中的短 ID 指向的暂存对象；不把正文或模型草稿放入检查点。"""

    __tablename__ = "conversation_work_items"
    __table_args__ = (Index("ix_conversation_work_items_turn_kind", "tenant_id", "turn_id", "kind"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    turn_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("conversation_turns.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = created_at_column()
