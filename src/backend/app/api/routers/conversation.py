from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response
from sqlalchemy import exists, or_, select, tuple_

from app.api.dependencies import CurrentUser, DbSession, SettingsDep, WriteUser, set_tenant_context
from app.api.errors import InvalidRequest, NotFoundOrForbidden
from app.conversation.context import assemble_context
from app.conversation.contracts import ConversationVersionBody, CreateConversationBody, RenameConversationBody, TurnBody
from app.conversation.models import AssistantContinuation, Conversation, ConversationTurn, Message, ReferenceSnapshot, TurnEvent
from app.conversation.service import conversation_view, create_conversation, create_turn, end_conversation, load_conversation, message_view, rename_conversation, turn_view
from app.documents.models import Case, CaseAccessGrant, CaseStatus, DocumentVersion
from app.documents.retention import CaseGone

from app.api import response_dto as d
router = APIRouter(tags=["conversation"])
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=80)]


def cursor_number(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        value = int(cursor)
        if value < 0:
            raise ValueError()
        return value
    except ValueError as exc:
        raise InvalidRequest("分页游标无效") from exc


@router.get("/conversations", response_model=d.PageView[d.ConversationView], response_model_exclude_unset=True)
async def conversations(principal: CurrentUser, db: DbSession, settings: SettingsDep, q: str | None = None,
    cursor: str | None = None, limit: Annotated[int, Query(ge=1, le=100)] = 30, archived: bool = False):
    from app.api.idempotency import request_digest
    from app.api.routers.review import _decode_cursor, _encode_cursor
    from app.management.search import contains
    search = (q or "")[:100]
    scope = request_digest({"tenant": principal.tenant_id, "owner": principal.user_id, "q": search, "archived":archived})
    as_of, anchor, offset = datetime.now(UTC), None, 0
    if cursor:
        if cursor.isdecimal():
            offset = cursor_number(cursor)
            if offset > 100000:
                raise InvalidRequest("旧分页游标过大，请从第一页重新读取")
        else:
            value = _decode_cursor(cursor)
            try:
                if value["v"] != "conversations@1" or value["scope"] != scope:
                    raise ValueError()
                as_of = datetime.fromisoformat(value["as_of"])
                anchor = (datetime.fromisoformat(value["updated_at"]), UUID(value["id"]))
                if as_of.tzinfo is None or anchor[0].tzinfo is None or anchor[0] > as_of:
                    raise ValueError()
            except (KeyError, ValueError, TypeError) as exc:
                raise InvalidRequest("会话游标与当前搜索不一致，请从第一页重新读取") from exc
    async with db.begin():
        await set_tenant_context(db, principal)
        now = datetime.now(UTC)
        grant = exists(select(CaseAccessGrant.id).where(
            CaseAccessGrant.case_id == Case.id, CaseAccessGrant.subject_user_id == principal.user_id,
            or_(CaseAccessGrant.expires_at.is_(None), CaseAccessGrant.expires_at > now),
            or_(*(CaseAccessGrant.capabilities.contains([capability]) for capability in ("read", "view", "write"))),
        ))
        readable_case = exists(select(Case.id).where(
            Case.id == Conversation.case_id, Case.status == CaseStatus.ACTIVE,
            Case.retention_expires_at > now, or_(Case.owner_user_id == principal.user_id, grant),
        ))
        query = select(Conversation).where(
            Conversation.owner_user_id == principal.user_id, Conversation.tenant_id == principal.tenant_id,
            Conversation.deleted_at.is_(None), Conversation.archived_at.is_not(None) if archived else Conversation.archived_at.is_(None),
            Conversation.updated_at <= as_of, or_(Conversation.case_id.is_(None), readable_case),
        )
        if search:
            query = query.where(contains(Conversation.title, search))
        page = []
        more_rows = False
        for _ in range(3):
            batch = query
            if anchor:
                batch = batch.where(tuple_(Conversation.updated_at, Conversation.id) < anchor)
            rows = list((await db.execute(batch.order_by(Conversation.updated_at.desc(), Conversation.id.desc())
                .offset(offset).limit(limit + 1))).scalars())
            offset = 0
            more_rows = len(rows) > limit
            for conversation in rows:
                try:
                    await load_conversation(db, principal, settings, conversation.id)
                except (NotFoundOrForbidden, CaseGone):
                    continue
                page.append(conversation)
                if len(page) > limit:
                    break
            if len(page) > limit or not more_rows:
                break
            anchor = (rows[-1].updated_at, rows[-1].id)
        has_more = len(page) > limit or more_rows
        next_anchor = (page[limit - 1].updated_at, page[limit - 1].id) if len(page) > limit else anchor
        page = page[:limit]
        items = [await conversation_view(db, conversation) for conversation in page]
        next_cursor = _encode_cursor({
            "v": "conversations@1", "scope": scope, "as_of": as_of.isoformat(),
            "updated_at": next_anchor[0].isoformat(), "id": str(next_anchor[1]),
        }) if has_more else None
        return {"items": items, "has_more": has_more, "next_cursor": next_cursor, "as_of": as_of}


@router.post("/conversations", status_code=201, response_model=d.ConversationView, response_model_exclude_unset=True)
async def create(body: CreateConversationBody, principal: WriteUser, db: DbSession, settings: SettingsDep, idempotency_key: Key):
    async with db.begin():
        await set_tenant_context(db, principal)
        return await conversation_view(db, await create_conversation(db, principal, settings, body, idempotency_key))


@router.get("/conversations/{conversation_id}", response_model=d.ConversationView, response_model_exclude_unset=True)
async def get(conversation_id: UUID, principal: CurrentUser, db: DbSession, settings: SettingsDep):
    async with db.begin():
        await set_tenant_context(db, principal)
        return await conversation_view(db, await load_conversation(db, principal, settings, conversation_id))


@router.post("/conversations/{conversation_id}/archive", response_model=d.ConversationView)
async def archive(conversation_id: UUID, body: ConversationVersionBody, principal: WriteUser, db: DbSession, settings: SettingsDep, idempotency_key: Key):
    from app.conversation.lifecycle import manage_conversation
    async with db.begin():
        await set_tenant_context(db, principal)
        return await conversation_view(db, await manage_conversation(db, principal, settings, conversation_id, body, idempotency_key, "archive"))


@router.post("/conversations/{conversation_id}/restore", response_model=d.ConversationView)
async def restore(conversation_id: UUID, body: ConversationVersionBody, principal: WriteUser, db: DbSession, settings: SettingsDep, idempotency_key: Key):
    from app.conversation.lifecycle import manage_conversation
    async with db.begin():
        await set_tenant_context(db, principal)
        return await conversation_view(db, await manage_conversation(db, principal, settings, conversation_id, body, idempotency_key, "restore"))


@router.delete("/conversations/{conversation_id}", status_code=204)
async def remove(conversation_id: UUID, body: ConversationVersionBody, principal: WriteUser, db: DbSession, settings: SettingsDep, idempotency_key: Key):
    from app.conversation.lifecycle import manage_conversation
    async with db.begin():
        await set_tenant_context(db, principal)
        await manage_conversation(db, principal, settings, conversation_id, body, idempotency_key, "delete")
    return Response(status_code=204)


@router.patch("/conversations/{conversation_id}", response_model=d.ConversationView, response_model_exclude_unset=True)
async def rename(conversation_id: UUID, body: RenameConversationBody, principal: WriteUser, db: DbSession, settings: SettingsDep, idempotency_key: Key):
    async with db.begin():
        await set_tenant_context(db, principal)
        return await conversation_view(db, await rename_conversation(db, principal, settings, conversation_id, body, idempotency_key))


@router.post("/conversations/{conversation_id}/end", response_model=d.ConversationView, response_model_exclude_unset=True)
async def end(conversation_id: UUID, principal: WriteUser, db: DbSession, settings: SettingsDep, idempotency_key: Key):
    async with db.begin():
        await set_tenant_context(db, principal)
        return await conversation_view(db, await end_conversation(db, principal, settings, conversation_id, idempotency_key))


@router.get("/conversations/{conversation_id}/messages", response_model=d.PageView[d.MessageView], response_model_exclude_unset=True)
async def messages(conversation_id: UUID, principal: CurrentUser, db: DbSession, settings: SettingsDep,
    cursor: str | None = None, limit: Annotated[int, Query(ge=1, le=100)] = 50,
    before_seq: Annotated[int | None, Query(ge=0)] = None):
    if cursor is not None and before_seq is not None:
        raise InvalidRequest("不能同时使用正向游标与旧消息游标")
    seq = cursor_number(cursor)
    async with db.begin():
        await set_tenant_context(db, principal)
        await load_conversation(db, principal, settings, conversation_id)
        query = select(Message).where(Message.conversation_id == conversation_id)
        if before_seq is None:
            query = query.where(Message.seq > seq).order_by(Message.seq)
        else:
            if before_seq:
                query = query.where(Message.seq < before_seq)
            query = query.order_by(Message.seq.desc())
        rows = list((await db.execute(query.limit(limit + 1))).scalars())
        page = rows[:limit]
        if before_seq is not None:
            page.reverse()
        from app.conversation.service import decorate_continuations
        items = await decorate_continuations(db, page)
        for item, row in zip(items, page):
            item["seq"] = row.seq
        return {"items": items, "has_more": len(rows) > limit,
            "next_cursor": str(page[0].seq if before_seq is not None else page[-1].seq) if len(rows) > limit else None}


@router.post("/conversations/{conversation_id}/turns", status_code=202, response_model=d.TurnView, response_model_exclude_unset=True)
async def send(conversation_id: UUID, body: TurnBody,
    principal: WriteUser, db: DbSession, settings: SettingsDep, idempotency_key: Key):
    async with db.begin():
        await set_tenant_context(db, principal)
        turn, _ = await create_turn(db, principal, settings, conversation_id, body, idempotency_key)
        result = turn_view(turn)
    return result


async def authorized_turn(db, principal, settings, conversation_id, turn_id):
    await load_conversation(db, principal, settings, conversation_id)
    turn = await db.get(ConversationTurn, turn_id)
    if turn is None or turn.conversation_id != conversation_id:
        raise NotFoundOrForbidden()
    return turn


@router.get("/conversations/{conversation_id}/turns/{turn_id}", response_model=d.TurnView, response_model_exclude_unset=True)
async def get_turn(conversation_id: UUID, turn_id: UUID, principal: CurrentUser, db: DbSession, settings: SettingsDep):
    async with db.begin():
        await set_tenant_context(db, principal)
        return turn_view(await authorized_turn(db, principal, settings, conversation_id, turn_id))


@router.get("/conversations/{conversation_id}/turns/{turn_id}/events", response_model=d.TurnEventsView, response_model_exclude_unset=True)
async def events(conversation_id: UUID, turn_id: UUID, principal: CurrentUser, db: DbSession, settings: SettingsDep,
    after_seq: Annotated[int, Query(ge=0)] = 0):
    async with db.begin():
        await set_tenant_context(db, principal)
        turn = await authorized_turn(db, principal, settings, conversation_id, turn_id)
        rows = list((await db.execute(select(TurnEvent).where(TurnEvent.turn_id == turn_id, TurnEvent.seq > after_seq)
            .order_by(TurnEvent.seq).limit(200))).scalars())
        return {"items": [{"seq": e.seq, "occurred_at": e.occurred_at, "event_type": e.event_type,
            "message_id": str(e.message_id) if e.message_id else None, **e.payload} for e in rows], "last_seq": turn.next_event_seq}


@router.get("/conversations/{conversation_id}/context", response_model=d.ContextView, response_model_exclude_unset=True)
async def context(conversation_id: UUID, principal: CurrentUser, db: DbSession, settings: SettingsDep):
    async with db.begin():
        await set_tenant_context(db, principal)
        conversation = await load_conversation(db, principal, settings, conversation_id)
        result = await assemble_context(db, conversation, settings)
        turn = (await db.execute(select(ConversationTurn).where(ConversationTurn.conversation_id == conversation_id)
            .order_by(ConversationTurn.turn_no.desc()).limit(1))).scalar_one_or_none()
        snapshot = await db.get(ReferenceSnapshot, turn.reference_snapshot_id) if turn else None
        case = await db.get(Case, conversation.case_id) if conversation.case_id else None
        from app.preferences.service import list_memories
        memories = await list_memories(db, principal) if turn is None else None
        return {"policy_version": "chat.system@" + str(turn.prompt_versions["chat.system"]["version"]) if turn else "chat.system@1",
            "memory_version": turn.memory_version or 0 if turn else memories["memory_version"], "confirmed_preference_count": len(turn.prompt_versions.get("confirmed_preferences", [])) if turn else len(memories["items"]),
            "case_label": case.name if case else None, "frozen_references": snapshot.selections if snapshot else [],
            "upload_status": "以已验收文件为准", "turn_range": f"最近 {result['recent_turn_count']} 轮", "total_turns": result["total_turns"],
            "summary_in_use": bool(result["summary"]), "token_budget_note": f"语境 {result['used_tokens']} / {result['budget']} tokens；摘要不作事实证据"}


@router.get("/search/references", response_model=d.PageView[d.ReferenceCandidateView], response_model_exclude_unset=True)
async def references(request: Request, principal: CurrentUser, db: DbSession, settings: SettingsDep, q: str = "", kind: str | None = None,
    conversation_id: UUID | None = None, case_id: UUID | None = None, limit: Annotated[int, Query(ge=1, le=50)] = 30):
    from datetime import UTC, datetime
    from app.documents.evidence_models import DocumentSection, LogicalDocument, ParseArtifact, ParseQuality
    from app.workflows.models import Run
    if kind not in {None, "case", "run", "document", "section", "snapshot"}:
        raise InvalidRequest("引用类型不合法")
    async with db.begin():
        await set_tenant_context(db, principal)
        bound = await load_conversation(db, principal, settings, conversation_id) if conversation_id else None
        bound_case_id = bound.case_id if bound else case_id
        if case_id and not bound:
            from app.documents.retention import load_case_for_read
            await load_case_for_read(db, case_id, user_id=principal.user_id, settings=settings)
        shared_keys = {"date_type", "local_date_from", "local_date_to", "origin_role", "origin_value", "recipient_role", "recipient_value",
            "actor_role", "actor_value", "doc_type", "format", "language", "risk", "version_scope", "match_scope", "only_confirmed", "tab"}
        shared_filters = {key: request.query_params.getlist(key) if key in {"doc_type", "format", "language", "risk"} else request.query_params[key]
            for key in request.query_params if key in shared_keys}
        matched_runs = None
        matched_case_ids = None
        matched_snapshot_ids = None
        matched_source_ids = None
        matched_projections = []
        if shared_filters or q.strip():
            from app.management.service import query_runs
            rows = await query_runs(db, principal, settings, {"q": q, **shared_filters})
            matched_runs = {row["run_id"] for row in rows}
            matched_case_ids = {UUID(row["case_id"]) for row in rows}
            matched_snapshot_ids = {row["displayed_snapshot_id"] for row in rows if row.get("displayed_snapshot_id")}
            from app.management.models import SearchProjection
            pairs = {(UUID(row['run_id']),UUID(row['displayed_snapshot_id']) if row['displayed_snapshot_id'] else None) for row in rows}
            from sqlalchemy import and_
            from app.management.search import projection_conditions
            from app.management.service import timezone
            projection_query=select(SearchProjection).where(or_(False, *[
                and_(SearchProjection.run_id==run_id,SearchProjection.snapshot_id==snapshot_id) for run_id,snapshot_id in pairs]))
            if shared_filters.get('match_scope','same_logical_document')!='any_in_case':
                projection_query=projection_query.where(*projection_conditions(SearchProjection,{'q':q,**shared_filters},await timezone(db,principal.user_id)))
            matched_projections = list((await db.execute(projection_query)).scalars()) if pairs else []
            matched_source_ids = {(str(p.document_id), p.document_version, str(p.parse_artifact_id) if p.parse_artifact_id else None) for p in matched_projections}
        now = datetime.now(UTC)
        grant = select(CaseAccessGrant.case_id).where(CaseAccessGrant.subject_user_id == principal.user_id,
            or_(CaseAccessGrant.expires_at.is_(None), CaseAccessGrant.expires_at > now))
        cases_query=select(Case).where(or_(Case.owner_user_id==principal.user_id,Case.id.in_(grant)),Case.status==CaseStatus.ACTIVE,Case.retention_expires_at>now)
        if matched_case_ids is not None:
            if q.strip() and not shared_filters:
                from app.management.search import contains
                draft_match=select(DocumentVersion.case_id).where(contains(DocumentVersion.filename,q.strip()),
                    ~exists(select(Run.id).where(Run.case_id==DocumentVersion.case_id)))
                cases_query=cases_query.where(or_(Case.id.in_(matched_case_ids),contains(Case.name,q.strip()),contains(Case.short_code,q.strip()),Case.id.in_(draft_match)))
            else:cases_query=cases_query.where(Case.id.in_(matched_case_ids))
        cases = list((await db.execute(cases_query.order_by(Case.created_at.desc(),Case.id.desc()).limit(100))).scalars())
        from app.documents.retention import load_case_for_read,CaseGone
        visible=[]
        for case in cases:
            try:await load_case_for_read(db,case.id,user_id=principal.user_id,settings=settings);visible.append(case)
            except (CaseGone,NotFoundOrForbidden):continue
        cases=visible
        case_map = {c.id: c for c in cases}
        items = []
        def add(case, selection, group, title, version_label, time, parent=None):
            if selection["kind"] == "run" and matched_runs is not None and selection["id"] not in matched_runs:
                return
            if selection["kind"] == "snapshot" and matched_snapshot_ids is not None and selection["id"] not in matched_snapshot_ids:
                return
            if parent is None and selection["kind"] in {"document", "section"} and matched_source_ids is not None and not any(doc == selection["document_id"] and version == selection["document_version"] and (selection["kind"] != "section" or parse == selection.get("parse_artifact_id")) for doc, version, parse in matched_source_ids):
                if shared_filters or selection['kind']=='section' or case.id in (matched_case_ids or set()) or q.casefold() not in (title+case.name+case.short_code).casefold():return
            cross = bool(bound_case_id and bound_case_id != case.id)
            candidate = {"case_name": case.name, "time_label": time.isoformat(), "status_label": case.status.value,
                "cross_case": cross, "selectable": not cross, "not_selectable_reason": "请新建会话查看另一票" if cross else None,
                "selection": {"case_id": str(case.id), "label": title, **selection},
                "group": group, "title": title, "version_label": version_label}
            (parent["children"] if parent is not None else items).append(candidate)
            return candidate
        if kind in {None, "case"}:
            for case in cases:
                add(case, {"kind": "case", "id": str(case.id), "selection_mode": "current"}, "Case", case.name, "发送时冻结当前版", case.created_at)
        if kind in {None, "run"}:
            run_query=select(Run).where(Run.case_id.in_(case_map))
            if matched_runs is not None:run_query=run_query.where(Run.id.in_([UUID(identifier) for identifier in matched_runs]))
            runs = list((await db.execute(run_query.order_by(Run.created_at.desc()).limit(100))).scalars()) if cases else []
            for run in runs:
                historical = next((str(p.snapshot_id) for p in matched_projections if p.run_id == run.id and p.snapshot_id), None)
                add(case_map[run.case_id], {"kind": "snapshot", "id": historical, "snapshot_id": historical, "run_id": str(run.id), "selection_mode": "fixed"} if historical else {"kind": "run", "id": str(run.id), "selection_mode": "current"}, "Run",
                    "识别任务" if run.workflow_type == "read_index" else "翻译复核任务", f"{run.status.value} · 工作流 v{run.workflow_version}", run.created_at)
        if kind in {None, "document", "section"}:
            version_query=select(DocumentVersion).where(DocumentVersion.case_id.in_(case_map))
            if matched_source_ids is not None:
                from sqlalchemy import and_
                source_match=or_(False, *[and_(DocumentVersion.document_id==UUID(doc),DocumentVersion.version==version) for doc,version,parse in matched_source_ids])
                if q.strip() and not shared_filters:
                    from app.management.search import contains
                    source_match=or_(source_match,contains(DocumentVersion.filename,q.strip()),DocumentVersion.case_id.in_(select(Case.id).where(or_(contains(Case.name,q.strip()),contains(Case.short_code,q.strip())))))
                version_query=version_query.where(source_match)
            versions = list((await db.execute(version_query.order_by(DocumentVersion.created_at.desc()).limit(100))).scalars()) if cases else []
            from sqlalchemy import and_
            parse_query = select(ParseArtifact).where(
                ParseArtifact.tenant_id == principal.tenant_id, ParseArtifact.case_id.in_(case_map),
                ParseArtifact.quality != ParseQuality.FAILED,
                or_(False, *[and_(ParseArtifact.document_id == v.document_id, ParseArtifact.document_version == v.version) for v in versions]))
            if matched_source_ids is None:
                from sqlalchemy.dialects.postgresql import distinct_on
                parse_query = parse_query.ext(distinct_on(ParseArtifact.document_id, ParseArtifact.document_version)).order_by(
                    ParseArtifact.document_id, ParseArtifact.document_version, ParseArtifact.created_at.desc(), ParseArtifact.id.desc())
            else:
                from sqlalchemy.dialects.postgresql import distinct_on
                projected_versions = {(p.document_id, p.document_version) for p in matched_projections}
                fallback_versions = [v for v in versions if (v.document_id, v.version) not in projected_versions]
                latest_ids = select(ParseArtifact.id).where(ParseArtifact.tenant_id == principal.tenant_id,
                    ParseArtifact.quality != ParseQuality.FAILED, or_(False, *[and_(
                        ParseArtifact.document_id == v.document_id, ParseArtifact.document_version == v.version,
                        ParseArtifact.case_id == v.case_id) for v in fallback_versions])).ext(
                            distinct_on(ParseArtifact.document_id, ParseArtifact.document_version)).order_by(
                                ParseArtifact.document_id, ParseArtifact.document_version, ParseArtifact.created_at.desc(), ParseArtifact.id.desc())
                parse_query = parse_query.where(or_(ParseArtifact.id.in_({p.parse_artifact_id for p in matched_projections if p.parse_artifact_id}),
                    ParseArtifact.id.in_(latest_ids)))
            parses = list((await db.execute(parse_query)).scalars()) if versions else []
            latest = {}
            for parse in parses:
                latest.setdefault((parse.document_id, parse.document_version), parse)
            parse_map = {parse.id: parse for parse in parses}
            sections = list((await db.execute(select(DocumentSection, LogicalDocument).join(LogicalDocument, and_(
                LogicalDocument.id == DocumentSection.logical_document_id,
                LogicalDocument.parse_artifact_id == DocumentSection.parse_artifact_id,
                LogicalDocument.case_id == DocumentSection.case_id,
                LogicalDocument.tenant_id == DocumentSection.tenant_id,
            )).where(DocumentSection.tenant_id == principal.tenant_id, DocumentSection.case_id.in_(case_map),
                DocumentSection.parse_artifact_id.in_(parse_map)).order_by(
                    LogicalDocument.ordinal, DocumentSection.ordinal, DocumentSection.id))).all()) if parse_map else []
            section_map = {}
            for section, logical in sections:
                section_map.setdefault(section.parse_artifact_id, []).append((section, logical))
            type_labels = {"commercial_invoice": "商业发票", "packing_list": "装箱单", "bill_of_lading": "提单",
                "supporting": "辅助单据", "other": "辅助单据", "unknown": "类型未确定", "unclassified": "类型未确定"}
            for version in versions:
                scopes = {}
                for projection in matched_projections:
                    if (projection.case_id, projection.document_id, projection.document_version) == (version.case_id, version.document_id, version.version):
                        scopes.setdefault((projection.parse_artifact_id, projection.snapshot_id, projection.run_id if projection.snapshot_id else None), []).append(projection)
                if not scopes:
                    parse = latest.get((version.document_id, version.version))
                    scopes[(parse.id if parse else None, None, None)] = []
                for (parse_id, snapshot_id, run_id), projections in scopes.items():
                    parse = parse_map.get(parse_id)
                    if parse_id and (parse is None or (parse.case_id, parse.document_id, parse.document_version)
                        != (version.case_id, version.document_id, version.version)):
                        continue
                    if snapshot_id:
                        from app.exports.models import ExportSnapshot
                        snapshot = await db.get(ExportSnapshot, snapshot_id)
                        if (snapshot is None or snapshot.tenant_id != principal.tenant_id or snapshot.case_id != version.case_id
                            or snapshot.run_id != run_id or not any(
                                source.get("document_id") == str(version.document_id) and source.get("document_version") == version.version
                                and source.get("parse_artifact_id") == (str(parse_id) if parse_id else None)
                                for source in snapshot.manifest.get("source_versions", []))):
                            continue
                    fixed = {"parse_artifact_id": str(parse.id)} if parse else {}
                    if snapshot_id:
                        fixed.update(snapshot_id=str(snapshot_id), run_id=str(run_id))
                    parent = add(case_map[version.case_id], {"kind": "document", "id": str(version.document_id), "selection_mode": "fixed",
                        "document_id": str(version.document_id), "document_version": version.version, **fixed}, "原件", version.filename,
                        f"原件 v{version.version}" + (" · 固定成功快照" if snapshot_id else ""), version.created_at)
                    if parent is None:
                        continue
                    parent["children"] = []
                    if kind == "section":
                        items.remove(parent)
                    for section, logical in section_map.get(parse_id, []):
                        if section.case_id != version.case_id or (projections and not any(p.logical_document_id in {None, logical.id} for p in projections)):
                            continue
                        projection = next((p for p in projections if p.logical_document_id == logical.id), None)
                        doc_type = projection.doc_type if projection else logical.type_candidate.value
                        start, end = section.page_start, section.page_end
                        page_label = f"第 {start}–{end} 页" if start and end and start != end else f"第 {start or end} 页" if start or end else "原页码不可用"
                        suffix = f" · {type_labels.get(doc_type, doc_type)} · {page_label}"
                        name = logical.title or section.title or "逻辑单据区段"
                        if logical.title and section.title and logical.title != section.title:
                            name += f" · {section.title}"
                        title = name[:200-len(suffix)] + suffix
                        add(case_map[version.case_id], {"kind": "section", "id": str(section.id), "selection_mode": "fixed",
                            "document_id": str(version.document_id), "document_version": version.version, **fixed,
                            "logical_document_id": str(logical.id), "section_id": str(section.id)}, "区段", title,
                            f"原件 v{version.version} · 固定解析版" + (" · 固定成功快照" if snapshot_id else ""), section.created_at,
                            parent=parent)
                    if kind == "section":
                        items.extend(parent["children"])
        if kind in {None, "snapshot"}:
            from app.exports.models import ExportSnapshot
            snapshot_query=select(ExportSnapshot).where(ExportSnapshot.case_id.in_(case_map))
            if matched_snapshot_ids is not None:snapshot_query=snapshot_query.where(ExportSnapshot.id.in_([UUID(identifier) for identifier in matched_snapshot_ids]))
            snapshots = list((await db.execute(snapshot_query.order_by(ExportSnapshot.exported_at.desc()).limit(100))).scalars()) if cases else []
            for snapshot in snapshots:
                add(case_map[snapshot.case_id], {"kind": "snapshot", "id": str(snapshot.id), "snapshot_id": str(snapshot.id),
                    "selection_mode": "fixed", "run_id": str(snapshot.run_id)}, "成功快照", "已导出成功快照",
                    "固定历史清单", snapshot.exported_at)
        page = items[:limit]
        if kind is None and len(items) > limit:
            # 默认搜索为每组留位置，防止Case/Run占满首页、原件无法展开。
            selected = []
            other_quota = max(1, limit // 8)
            for group, quota in (("原件", max(1, limit - 3 * other_quota)), ("Case", other_quota),
                ("Run", other_quota), ("成功快照", other_quota)):
                selected.extend([item for item in items if item["group"] == group][:min(quota, limit - len(selected))])
            selected_ids = {id(item) for item in selected}
            selected.extend(item for item in items if id(item) not in selected_ids)
            selected_ids = {id(item) for item in selected[:limit]}
            page = [item for item in items if id(item) in selected_ids]
        return {"items": page, "has_more": len(items) > limit}


@router.get("/extensions/available", response_model=d.CapabilityListView, response_model_exclude_unset=True)
async def capabilities(principal: CurrentUser, db: DbSession, settings: SettingsDep, q: str = ""):
    from app.conversation.tools import available_capabilities
    from app.extensions.service import public_registry_warnings
    async with db.begin():
        await set_tenant_context(db, principal)
        return {"items": await available_capabilities(db, principal, settings, q), "warnings": public_registry_warnings(settings)}
