from __future__ import annotations

import re
from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import Settings
from app.conversation.models import Conversation, ConversationSummary, ConversationTurn, ConversationWorkItem, Message, ReferenceSnapshot
from app.api.idempotency import request_digest
from app.prompts.registry import estimate_tokens

SUMMARY_VERSION = "deterministic_requirements@5"


def requests_prior_constraints(question: str) -> bool:
    return bool(re.search(
        r"(?:按|照|沿用|保留|遵循).{0,5}(?:之前|先前|前面|上次|此前).{0,8}(?:要求|约定|规则|设置|格式|术语|风格)"
        r"|(?:按|照|沿用|保留|遵循).{0,5}(?:要求|约定|规则|设置|格式|术语|风格).{0,5}(?:之前|先前|前面|上次|此前)",
        question))


def historical_requirements(messages):
    """只提取白名单任务要求，不复制字段、客户事实或任意指令。"""
    notes = []
    for message in messages:
        text = message.text
        if message.role == "user":
            if "翻译" in text:
                notes.append("用户曾要求翻译" + ("成中文" if any(x in text for x in ("中文", "汉语")) else "成英文" if any(x in text for x in ("英文", "英语")) else "，目标语言未明确"))
            if "货描" in text:
                notes.append("用户曾要求只处理货描，需沿对应任务范围核验")
            if "DOCX" in text.upper() or "PDF" in text.upper():
                notes.append("用户曾指定输出格式：" + "、".join(x for x in ("DOCX", "PDF") if x in text.upper()))
            if "识别" in text:
                notes.append("用户曾要求识别原件")
            if "进度" in text:
                notes.append("用户曾询问任务进度")
            if "来源" in text:
                notes.append("用户曾要求查看来源")
        for card in message.cards or []:
            if card.get("type") == "task_draft":
                notes.append("历史待启动草稿曾需补充要求；当前状态必须重新读取")
            if card.get("type") == "memory_suggestion":
                notes.append("历史记忆建议曾待本人确认；不视为有效偏好")
        if message.continuation and message.continuation.get("status") in {"pending", "waiting", "awaiting_retry", "awaiting_reconcile"}:
            notes.append("历史内容询问曾等待识别续答；当前状态必须重新读取")
    return list(dict.fromkeys(notes))[-6:]


def bounded_summary(value: str | None, token_limit: int) -> str | None:
    if not value:
        return None
    items = value.split("；")
    while items and estimate_tokens("；".join(items)) > token_limit:
        items.pop()
    return "；".join(items) or None


def expression_constraints(messages):
    """保留本会话表达与术语约束，不复制客户事实或授权指令。"""
    notes = []
    for message in messages:
        if message.role != "user":
            continue
        for sentence in re.split(r"[。！？\n]", message.text):
            if re.search(r"称为|称作|叫作|术语|表达|格式|先.{0,12}再|简明|简洁|详细|用.{0,4}(?:中文|英文)|列表|表格", sentence) and not re.search(r"\d|客户|发票号|地址|密钥|token|忽略|权限|放行|导出", sentence, re.I):
                notes.append(sentence.strip()[:250])
    return list(dict.fromkeys(notes))[-20:]


async def assemble_context(db: AsyncSession, conversation: Conversation, settings: Settings, *, before_turn_no: int | None = None, input_budget: int | None = None, knowledge_only: bool = False) -> dict:
    boundary = before_turn_no if before_turn_no is not None else conversation.turn_count + 1
    conditions = (ConversationTurn.tenant_id == conversation.tenant_id, ConversationTurn.conversation_id == conversation.id,
        ConversationTurn.turn_no < boundary, ConversationTurn.status == "completed",
        ~ConversationTurn.id.in_(select(ConversationWorkItem.turn_id).where(
            ConversationWorkItem.conversation_id == conversation.id, ConversationWorkItem.kind == "domain_rejected")))
    total = (await db.execute(select(func.count()).select_from(ConversationTurn).where(*conditions))).scalar_one()
    recent = list((await db.execute(select(ConversationTurn).where(*conditions).order_by(ConversationTurn.turn_no.desc()).limit(20))).scalars())
    summary = (await db.execute(select(ConversationSummary).where(ConversationSummary.tenant_id == conversation.tenant_id,
        ConversationSummary.conversation_id == conversation.id, ConversationSummary.covers_through_turn_no < boundary,
        ConversationSummary.prompt_version.in_([SUMMARY_VERSION, "chat.session_summary@1"])).order_by(
        case((ConversationSummary.prompt_version == "chat.session_summary@1", 1), else_=0).desc(), ConversationSummary.covers_through_turn_no.desc(),
        ConversationSummary.created_at.desc(), ConversationSummary.id.desc()).limit(1))).scalar_one_or_none()
    cutoff = recent[-1].turn_no if len(recent) == 20 else 0
    through = summary.covers_through_turn_no if summary else 0
    if total > 20 and through < cutoff - 1 and (not summary or summary.prompt_version == SUMMARY_VERSION):
        archive = list((await db.execute(select(ConversationTurn.id, ConversationTurn.turn_no).where(*conditions,
            ConversationTurn.turn_no < cutoff, ConversationTurn.turn_no > through).order_by(ConversationTurn.turn_no.asc()).limit(20))).all())
        if archive:
            old_messages = list((await db.execute(select(Message).where(Message.tenant_id == conversation.tenant_id,
                Message.conversation_id == conversation.id, Message.turn_id.in_([t.id for t in archive]),
                Message.role == "user", Message.status == "completed").order_by(Message.seq))).scalars())
            prior = []
            if summary and "历史要求：" in summary.summary_text:
                prior = summary.summary_text.split("历史要求：", 1)[1].split("；")
            notes = list(dict.fromkeys(prior + historical_requirements(old_messages)))[-6:]
            remaining = "其后旧轮尚待后续增量摘要，当前未载入。" if archive[-1].turn_no < cutoff - 1 else ""
            text = f"本会话摘要本次读取第{archive[0].turn_no}至第{archive[-1].turn_no}轮的用户请求，覆盖边界仅到第{archive[-1].turn_no}轮。{remaining}历史资料不作事实或本次任务要求，未完事项须重新读取。历史要求：" + "；".join(notes or ["单证任务讨论，无可安全复用的明确要求"])
            digest = request_digest([summary.source_message_hash if summary else None, [(str(m.id), m.text, m.cards) for m in old_messages]])
            summary = ConversationSummary(tenant_id=conversation.tenant_id, conversation_id=conversation.id,
                covers_through_turn_no=archive[-1].turn_no, source_message_hash=digest, prompt_version=SUMMARY_VERSION,
                summary_text=text, token_count=estimate_tokens(text))
            db.add(summary)
            await db.flush()
    messages = list((await db.execute(select(Message).where(Message.tenant_id == conversation.tenant_id,
        Message.conversation_id == conversation.id, Message.turn_id.in_([t.id for t in recent]), Message.status == "completed")
        .order_by(Message.seq.desc()))).scalars()) if recent else []
    budget = max(0, (input_budget if input_budget is not None else settings.chat_input_token_budget - settings.response_token_reserve) - settings.retrieved_context_token_limit)
    direct = list(reversed(messages))
    # 消息条数上限不能留下只有续答而没有用户请求的半轮。
    complete_turns = {m.turn_id for m in direct if m.role == "user"}
    direct = [m for m in direct if m.turn_id in complete_turns]
    summary_limit = min(settings.session_summary_token_limit, budget)
    summary_text = summary.summary_text if summary and summary.prompt_version == "chat.session_summary@1" and estimate_tokens(summary.summary_text) <= summary_limit else bounded_summary(summary.summary_text if summary and summary.prompt_version != "chat.session_summary@1" else None, summary_limit)
    if knowledge_only:
        # 知识路径未读客户正文；即使会话已绑定Case，也可保留安全知识轮次。
        knowledge_query = select(ConversationTurn).join(ReferenceSnapshot, ReferenceSnapshot.id == ConversationTurn.reference_snapshot_id).where(
            *conditions, or_(
                ConversationTurn.id.in_(select(ConversationWorkItem.turn_id).where(ConversationWorkItem.conversation_id == conversation.id,
                    ConversationWorkItem.kind.in_(["general_answer", "general_request"]))),
                ConversationTurn.id.in_(select(Message.turn_id).where(Message.conversation_id == conversation.id,
                    Message.cards.contains([{"type": "knowledge_source"}])))))
        knowledge_ids = {t.id for t in (await db.execute(knowledge_query.where(ConversationTurn.id.in_([t.id for t in recent])))).scalars()}
        from app.conversation.domain import asks_case_specific, is_instruction_attack
        safe_ids = {m.turn_id for m in direct if m.role == "user" and not asks_case_specific(m.text)
            and not is_instruction_attack(m.text) and not re.search(r"发票号|客户|地址|密钥|token|\d{5,}", m.text, re.I)}
        direct = [m for m in direct if m.turn_id in knowledge_ids & safe_ids and m.role in {"user", "assistant"}]
        knowledge_summary = (await db.execute(select(ConversationSummary).where(ConversationSummary.tenant_id == conversation.tenant_id,
            ConversationSummary.conversation_id == conversation.id, ConversationSummary.prompt_version == "knowledge_discussion@1",
            ConversationSummary.covers_through_turn_no < boundary).order_by(ConversationSummary.covers_through_turn_no.desc()).limit(1))).scalar_one_or_none()
        through = knowledge_summary.covers_through_turn_no if knowledge_summary else 0
        first_direct = min((t.turn_no for t in recent if t.id in knowledge_ids), default=boundary)
        old_turns = list((await db.execute(knowledge_query.where(ConversationTurn.turn_no > through,
            ConversationTurn.turn_no < first_direct).order_by(ConversationTurn.turn_no).limit(20))).scalars())
        old_ids = [t.id for t in old_turns]
        old_questions = list((await db.execute(select(Message).where(Message.conversation_id == conversation.id,
            Message.tenant_id == conversation.tenant_id, Message.turn_id.in_(old_ids), Message.role == "user", Message.status == "completed")
            .order_by(Message.seq))).scalars()) if old_ids else []
        # 安全摘要仅保留表达/术语约束；不复制一般知识答案、数字或客户内容。
        notes = knowledge_summary.summary_text.split("：", 1)[-1].split("；") if knowledge_summary else []
        notes.extend(expression_constraints(old_questions))
        summary_text = bounded_summary("本会话先前一般知识讨论的低信任表达约束：" + "；".join(list(dict.fromkeys(notes))[-20:]), summary_limit) if notes else None
        if old_turns:
            digest = request_digest([knowledge_summary.source_message_hash if knowledge_summary else None,
                [(str(m.id), m.text) for m in old_questions]])
            db.add(ConversationSummary(tenant_id=conversation.tenant_id, conversation_id=conversation.id,
                covers_through_turn_no=old_turns[-1].turn_no, source_message_hash=digest, prompt_version="knowledge_discussion@1",
                summary_text=summary_text or "本会话先前一般知识讨论没有可安全保留的表达约束", token_count=estimate_tokens(summary_text or "")))
            await db.flush()
        # 只消费后台明确标记的无客户来源摘要；历史讨论不作为知识真值或本票事实。
        import json
        semantic = (await db.execute(select(ConversationSummary).where(ConversationSummary.tenant_id == conversation.tenant_id,
            ConversationSummary.conversation_id == conversation.id, ConversationSummary.prompt_version == "chat.session_summary@1",
            ConversationSummary.covers_through_turn_no < boundary).order_by(ConversationSummary.covers_through_turn_no.desc()).limit(1))).scalar_one_or_none()
        if semantic and estimate_tokens(semantic.summary_text) <= summary_limit:
            try:
                value = json.loads(semantic.summary_text)
                if value.get("summary_scope") == "knowledge_unverified" and not value.get("source_reference_versions"):
                    summary_text = semantic.summary_text
            except (ValueError, TypeError, AttributeError):
                pass
    dropped = []
    entries = [{"role": m.role, "text": m.text} for m in direct]
    while direct and estimate_tokens(str(entries)) + estimate_tokens(summary_text or "") > budget:
        oldest = direct[0].turn_id
        dropped.extend(m for m in direct if m.turn_id == oldest)
        direct = [m for m in direct if m.turn_id != oldest]
        if knowledge_only:
            constraints = expression_constraints(dropped)
            if constraints:
                summary_text = bounded_summary((knowledge_summary.summary_text if knowledge_summary else "") +
                    " 本会话因预算压缩的低信任表达约束：" + "；".join(constraints), min(settings.session_summary_token_limit, budget))
        notes = historical_requirements(dropped)
        if notes and not knowledge_only and (not summary or summary.prompt_version != "chat.session_summary@1"):
            summary_text = bounded_summary((summary.summary_text if summary else "") + " 本会话因预算未直接载入的旧轮要求，仅供重问：" + "；".join(notes), min(settings.session_summary_token_limit, budget))
        entries = [{"role": m.role, "text": m.text} for m in direct]
    direct_from = min((turn.turn_no for turn in recent if turn.id in {message.turn_id for message in direct}), default=boundary)
    uncovered = (await db.execute(select(func.count()).select_from(ConversationTurn).where(*conditions,
        ConversationTurn.turn_no > summary.covers_through_turn_no, ConversationTurn.turn_no < direct_from))).scalar_one() if summary else total
    return {"messages": entries, "turns": [[{"role": m.role, "text": m.text} for m in direct if m.turn_id == identifier]
            for identifier in dict.fromkeys(m.turn_id for m in direct)], "summary": summary_text,
        "summary_trust": "historical_unverified", "summary_requires_current_confirmation": True,
        "direct_from_turn_no": direct_from, "recent_turn_count": len({m.turn_id for m in direct}),
        "summary_complete": bool(summary_text and summary and summary.prompt_version == "chat.session_summary@1" and uncovered == 0) if not knowledge_only else True,
        "total_turns": total, "used_tokens": estimate_tokens(str(entries)) + estimate_tokens(summary_text or ""), "budget": budget}
