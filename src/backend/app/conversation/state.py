from typing import Literal, TypedDict


NextStep = Literal["plan", "tools", "clarify", "answer", "replan", "done"]


class ConversationState(TypedDict, total=False):
    conversation_id: str
    turn_id: str
    active_case_id: str | None
    reference_snapshot_id: str | None
    intent_candidate_id: str | None
    tool_result_ids: list[str]
    evidence_ids: list[str]
    draft_id: str | None
    decision: NextStep
    plan_rounds: int
    tool_calls: int
    error_class: str | None
