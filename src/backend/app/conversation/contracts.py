from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.conversation.references import ReferenceSelection

TurnStatus = Literal["pending", "running", "completed", "interrupted", "failed"]


class ConversationTurnSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)
    turn_id: UUID
    conversation_id: UUID
    idempotency_key: str = Field(min_length=1)
    status: TurnStatus
    graph_version: str
    prompt_version: str
    model_profile_version: str
    reference_snapshot_id: UUID | None
    lease_token: UUID | None
    lease_until: AwareDatetime | None
    last_heartbeat_at: AwareDatetime | None
    final_message_id: UUID | None


class CapabilitySelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["builtin_skill", "user_skill", "mcp_tool"]
    extension_id: UUID
    extension_version: int = Field(ge=1)
    tool_id: str | None = None
    tool_schema_hash: str | None = None
    user_parameters: dict | None = None


class CreateConversationBody(BaseModel):
    initial_reference_selections: list[ReferenceSelection] = Field(default_factory=list, max_length=20)
    entry_source: str | None = Field(default=None, max_length=40)


class RenameConversationBody(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    expected_conversation_version: int = Field(ge=1)


class ConversationVersionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_conversation_version: int = Field(ge=1)


class TurnBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=12000)
    references: list[ReferenceSelection] = Field(default_factory=list, max_length=20)
    capabilities: list[CapabilitySelection] = Field(default_factory=list, max_length=4)
    upload_file_ids: list[UUID] = Field(default_factory=list, max_length=5)
    task_draft_id: UUID | None = None
