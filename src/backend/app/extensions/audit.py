"""只保留参数结构与冻结引用，不保存客户正文或秘密参数。"""
from datetime import UTC, datetime
import json
from urllib.parse import urlsplit
from uuid import UUID

from app.conversation.models import ConversationTurn


def safe_parameter_summary(parameters):
    def shape(value, depth=0):
        if depth >= 3:
            return {"type": type(value).__name__}
        if isinstance(value, dict):
            return {str(key)[:80]: "[已脱敏]" if any(word in str(key).lower() for word in ("key", "token", "secret", "password", "authorization", "credential")) else shape(item, depth+1)
                    for key, item in list(value.items())[:20]}
        if isinstance(value, list):
            return {"type": "array", "count": len(value), "items": [shape(item, depth+1) for item in value[:3]]}
        return {"type": type(value).__name__, "supplied": value is not None}
    return json.dumps(shape(parameters or {}), ensure_ascii=False, separators=(",", ":"))[:2000]


def safe_destination(endpoint):
    parsed = urlsplit(endpoint or "")
    return f"{parsed.scheme}://{parsed.hostname}"[:300] if parsed.scheme and parsed.hostname else "项目内受控业务动作"


async def audit_context(db, turn_id, capability, started_at=None):
    now = datetime.now(UTC)
    start = started_at
    if start is None and turn_id:
        turn = await db.get(ConversationTurn, UUID(str(turn_id)))
        start = turn.created_at if turn else None
    return {"params_redacted": safe_parameter_summary(capability.get("user_parameters")),
            "destination": "项目内受控业务动作", "started_at": start,
            "finished_at": now, "duration_ms": max(0, int((now-start).total_seconds()*1000)) if start else None}
