from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.models_base import Base, TenantMixin


class UserPreference(TenantMixin, Base):
    __tablename__ = "user_preferences"
    __table_args__ = (UniqueConstraint("tenant_id", "user_id", name="uq_user_preferences_owner"),)
    user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    interface_language: Mapped[str] = mapped_column(String(8), default="zh-CN", nullable=False)
    timezone: Mapped[str] = mapped_column(String(80), default="Asia/Shanghai", nullable=False)
    panel_width: Mapped[int] = mapped_column(Integer, default=640, nullable=False)
    motion: Mapped[str] = mapped_column(String(16), default="system", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    memory_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    saved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserMemory(TenantMixin, Base):
    __tablename__ = "user_memories"
    user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    source_conversation_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserMemoryVersion(TenantMixin, Base):
    __tablename__ = "user_memory_versions"
    __table_args__ = (UniqueConstraint("tenant_id", "memory_id", "version", name="uq_user_memory_versions_memory_version"),)
    memory_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("user_memories.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    category: Mapped[str] = mapped_column(String(24), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    applies_when: Mapped[str | None] = mapped_column(String(300))
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MemorySuggestion(TenantMixin, Base):
    __tablename__ = "memory_suggestions"
    user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    parent_turn_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    category: Mapped[str] = mapped_column(String(24), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    applies_when: Mapped[str | None] = mapped_column(String(300))
    origin: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    memory_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
