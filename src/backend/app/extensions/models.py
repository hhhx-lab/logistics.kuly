from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.models_base import Base, TenantMixin


class Skill(TenantMixin, Base):
    __tablename__ = "skills"
    __table_args__ = (UniqueConstraint("tenant_id", "owner_user_id", "identifier", name="uq_skills_owner_identifier"),)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    identifier: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="draft", nullable=False)
    enabled_version: Mapped[int | None] = mapped_column(Integer)
    next_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class SkillVersion(TenantMixin, Base):
    __tablename__ = "skill_versions"
    __table_args__ = (UniqueConstraint("tenant_id", "skill_id", "version", name="uq_skill_versions_skill_version"),)
    skill_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("skills.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    edit_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="draft", nullable=False)
    definition: Mapped[dict] = mapped_column(JSONB, nullable=False)
    validation_errors: Mapped[list[dict]] = mapped_column(JSONB, default=list, nullable=False)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ever_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class CredentialEnvelope(TenantMixin, Base):
    __tablename__ = "credential_envelopes"
    owner_user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    nonce: Mapped[str] = mapped_column(String(80), nullable=False)
    encrypted_key: Mapped[str] = mapped_column(Text, nullable=False)
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    algorithm: Mapped[str] = mapped_column(String(40), nullable=False)
    key_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class McpConnection(TenantMixin, Base):
    __tablename__ = "mcp_connections"
    owner_user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="draft", nullable=False)
    enabled_version: Mapped[int | None] = mapped_column(Integer)
    next_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class McpConnectionVersion(TenantMixin, Base):
    __tablename__ = "mcp_connection_versions"
    __table_args__ = (UniqueConstraint("tenant_id", "connection_id", "version", name="uq_mcp_versions_connection_version"),)
    connection_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("mcp_connections.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    edit_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="draft", nullable=False)
    endpoint: Mapped[str] = mapped_column(String(600), nullable=False)
    auth_type: Mapped[str] = mapped_column(String(16), nullable=False)
    credential_envelope_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("credential_envelopes.id"))
    ever_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_class: Mapped[str | None] = mapped_column(String(64))


class McpConnectionTest(TenantMixin, Base):
    __tablename__ = "mcp_connection_tests"
    connection_version_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("mcp_connection_versions.id", ondelete="CASCADE"), nullable=False)
    stages: Mapped[list[dict]] = mapped_column(JSONB, nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error_class: Mapped[str | None] = mapped_column(String(64))


class McpToolSnapshot(TenantMixin, Base):
    __tablename__ = "mcp_tool_snapshots"
    __table_args__ = (UniqueConstraint("tenant_id", "connection_version_id", "tool_id", name="uq_mcp_snapshots_version_tool"),)
    connection_version_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("mcp_connection_versions.id", ondelete="CASCADE"), nullable=False)
    tool_id: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    input_schema: Mapped[dict] = mapped_column(JSONB, nullable=False)
    schema_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    previous_schema_hash: Mapped[str | None] = mapped_column(String(64))
    previous_schema: Mapped[dict | None] = mapped_column(JSONB)
    schema_changed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class McpToolGrant(TenantMixin, Base):
    __tablename__ = "mcp_tool_grants"
    connection_version_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("mcp_connection_versions.id", ondelete="CASCADE"), nullable=False)
    tool_id: Mapped[str] = mapped_column(String(160), nullable=False)
    subject_user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    schema_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(80), nullable=False)
    data_level: Mapped[str] = mapped_column(String(40), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class McpChallenge(TenantMixin, Base):
    __tablename__ = "mcp_challenges"
    owner_user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    connection_version_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("mcp_connection_versions.id"), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("conversations.id"), nullable=False)
    parent_turn_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("conversation_turns.id"), nullable=False)
    reference_snapshot_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    case_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    tool_id: Mapped[str] = mapped_column(String(160), nullable=False)
    schema_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    destination: Mapped[str] = mapped_column(String(300), nullable=False)
    arguments: Mapped[dict] = mapped_column(JSONB, nullable=False)
    frozen_scope: Mapped[list[dict]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ExtensionCall(TenantMixin, Base):
    __tablename__ = "extension_calls"
    extension_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    extension_version: Mapped[int] = mapped_column(Integer, nullable=False)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    parent_turn_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    action: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    result_redacted: Mapped[str] = mapped_column(String(800), nullable=False)
    tool_schema_hash: Mapped[str | None] = mapped_column(String(64))
    reject_class: Mapped[str | None] = mapped_column(String(64))
    error_class: Mapped[str | None] = mapped_column(String(64))
    params_redacted: Mapped[str | None] = mapped_column(String(2000))
    destination: Mapped[str | None] = mapped_column(String(300))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
