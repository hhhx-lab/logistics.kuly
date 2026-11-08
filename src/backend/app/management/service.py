"""管理台按同一授权／版本筛选：摘要只用于定位，事实仍查来源。"""
from __future__ import annotations
import base64,json
from datetime import UTC,datetime,date
from uuid import UUID
from zoneinfo import ZoneInfo
from sqlalchemy import select,func,or_,and_
from sqlalchemy.dialects.postgresql import aggregate_order_by
from app.api.errors import InvalidRequest,NotFoundOrForbidden,VersionConflict
from app.documents import evidence_models as em
from app.documents.models import Case,CaseStatus,SourceDocument,DocumentVersion,UploadSession
from app.documents.retention import load_case_for_read,CaseGone
from app.exports.models import ExportSnapshot,ExportAttempt
from app.preferences.models import UserPreference
from app.review import models as rm
from app.review.commands import issue_view
from app.identity.models import User
from app.workflows import commands as wc
from app.workflows.models import Run,RunDocument,RunEvent
from app.management.models import RunDocumentSummary


async def source_view(db,document_id,version):
    value=(await db.execute(select(DocumentVersion).where(DocumentVersion.document_id==document_id,DocumentVersion.version==version))).scalar_one()
    return {"document_id":str(value.document_id),"document_version":value.version,"case_id":str(value.case_id),"filename":value.filename,
        "mime":value.mime,"size_bytes":value.size_bytes,"uploaded_by":str(value.uploaded_by),"uploaded_at":value.created_at.isoformat()}


async def timezone(db,user_id):
    preference=(await db.execute(select(UserPreference).where(UserPreference.user_id==user_id))).scalar_one_or_none()
    return preference.timezone if preference else "Asia/Shanghai"


class RunRows(list):
    """保持共享检索的list契约，给M01游标附带身份与查询水位。"""
    scope=None


def cursor_value(cursor,filters):
    from app.api.idempotency import request_digest
    try:
        value=json.loads(base64.urlsafe_b64decode(cursor))
        if value['filter']!=request_digest(filters):raise ValueError
        moment=datetime.fromisoformat(value['last'][0])
        if moment.tzinfo is None:raise ValueError
        return value,(moment,UUID(value['last'][1]))
    except (ValueError,KeyError,TypeError,IndexError) as exc:
        raise InvalidRequest('管理台游标与当前筛选不一致') from exc


async def query_runs(db,principal,settings,filters,*,cursor=None,limit=None):
    from app.management.search import ordered_candidates,candidate_page
    from app.api.idempotency import request_digest
    tz=await timezone(db,principal.user_id)
    query=ordered_candidates(principal,filters,tz)
    scope=None;anchor=None
    if limit is not None:
        source=query.subquery()
        # SQL内聚合小元数据，不把全部候选的正文、字段和Issue装入单页。
        watermark=(await db.execute(select(func.md5(func.string_agg(func.concat(
            source.c.run_id,':',source.c.snapshot_id,':',source.c.updated_at,':',source.c.status,':',source.c.review_state_version,':',source.c.case_version,
            ':',source.c.current_business_revision_id,':',source.c.current_translation_revision_id),aggregate_order_by(',',source.c.run_id)))))).scalar_one()
        scope=request_digest({'user':principal.user_id,'tenant':principal.tenant_id,'timezone':tz,'watermark':watermark})
        if cursor:
            value,anchor=cursor_value(cursor,filters)
            if value.get('scope')!=scope:
                raise VersionConflict('管理记录已变化，请刷新列表',code='PAGE_STALE')
    output=RunRows();output.scope=scope
    # 删除账本和风险处置必须继续真实重验；某批无命中就读取下一批，不能提前截断。
    batch_size=max(limit or 100,25)
    while True:
        candidates=(await db.execute(candidate_page(query,filters,anchor=anchor,limit=batch_size if limit is not None else None))).all()
        if not candidates:break
        output.extend(await _run_rows(db,principal,settings,filters,candidates))
        if limit is None or len(output)>=limit or len(candidates)<batch_size:break
        last=candidates[-1];anchor=(last.sort_at,last.run_id)
    if limit is not None:del output[limit:]
    return output


async def facets(db,principal,settings,filters):
    """每个tab复用真实展示版本与筛选；完整计数只读取候选元数据。"""
    from app.management.search import ordered_candidates
    tz=await timezone(db,principal.user_id)
    counts={};languages=set();access={}
    for tab in ('all','exported','awaiting','processing','failed','indexed'):
        value={**filters,'tab':tab}
        candidates=ordered_candidates(principal,value,tz).subquery()
        rows=(await db.execute(select(candidates,Run.case_id,Run.target_language)
            .join(Run,Run.id==candidates.c.run_id))).all()
        visible=[]
        for row in rows:
            if row.case_id not in access:
                try:await load_case_for_read(db,row.case_id,user_id=principal.user_id,settings=settings);access[row.case_id]=True
                except (CaseGone,NotFoundOrForbidden):access[row.case_id]=False
            if access[row.case_id]:visible.append(row)
        if filters.get('risk'):
            # 处置规则仍由同一DTO入口解释，分批控制每次内容读取，计数不裁剪。
            matched=[]
            for start in range(0,len(visible),100):matched.extend(await _run_rows(db,principal,settings,value,visible[start:start+100]))
            counts[tab]=len(matched)
            if tab=='all':languages.update(row['target_language'] for row in matched if row['target_language'])
        else:
            counts[tab]=len(visible)
            if tab=='all':languages.update(row.target_language for row in visible if row.target_language)
    from app.documents.uploads import draft_session_predicate
    draft_rows = (await db.execute(select(UploadSession.case_id).join(Case, Case.id == UploadSession.case_id).where(
        Case.status == CaseStatus.ACTIVE, Case.owner_user_id == principal.user_id,
        Case.retention_expires_at > datetime.now(UTC), draft_session_predicate(),
    ))).scalars().all()
    counts['drafts'] = 0
    for case_id in draft_rows:
        if case_id not in access:
            try:
                await load_case_for_read(db, case_id, user_id=principal.user_id, settings=settings)
                access[case_id] = True
            except (CaseGone, NotFoundOrForbidden):
                access[case_id] = False
        if access[case_id]:
            counts['drafts'] += 1
    return {'status_counts':counts,'languages':sorted(languages),'timezone':tz}


async def _run_rows(db,principal,settings,filters,candidates):
    from app.management.search import ROLE_NAMES
    from app.management.models import SearchProjection,SearchFieldValue
    if not candidates:return []
    run_ids={r[0] for r in candidates}
    runs=list((await db.execute(select(Run).where(Run.id.in_(run_ids)))).scalars())
    cases={c.id:c for c in (await db.execute(select(Case).where(Case.id.in_({r.case_id for r in runs})))).scalars()}
    visible={}
    for case in cases.values():
        try:await load_case_for_read(db,case.id,user_id=principal.user_id,settings=settings);visible[case.id]=case
        except (CaseGone,NotFoundOrForbidden):continue
    snapshots=list((await db.execute(select(ExportSnapshot).where(ExportSnapshot.run_id.in_(run_ids)).order_by(ExportSnapshot.exported_at.desc(),ExportSnapshot.id.desc()))).scalars())
    snapshot_by_id={s.id:s for s in snapshots}
    chosen={}
    for candidate in candidates:
        run_id,snapshot_id=candidate[0],candidate[1]
        value=snapshot_by_id.get(snapshot_id)
        if run_id not in chosen or value and (chosen[run_id] is None or (value.exported_at,str(value.id))>(chosen[run_id].exported_at,str(chosen[run_id].id))):chosen[run_id]=value
    projections=list((await db.execute(select(SearchProjection).where(or_(*[
        and_(SearchProjection.run_id==run_id,SearchProjection.snapshot_id==(snapshot.id if snapshot else None))
        for run_id,snapshot in chosen.items()])))).scalars())
    search_values=list((await db.execute(select(SearchFieldValue).where(SearchFieldValue.projection_id.in_([p.id for p in projections])))).scalars())
    values_by_projection={}
    for value in search_values:values_by_projection.setdefault(value.projection_id,[]).append(value)
    actor_ids={r.initiated_by for r in runs}|{s.exported_by for s in snapshots}|{UUID(s.manifest["confirmed_by"]) for s in snapshots}
    users={u.id:u for u in (await db.execute(select(User).where(User.id.in_(actor_ids)))).scalars()}
    attempts=list((await db.execute(select(ExportAttempt).where(ExportAttempt.run_id.in_(run_ids)).order_by(ExportAttempt.created_at.desc(),ExportAttempt.id.desc()))).scalars())
    latest_attempt={}
    for item in attempts:latest_attempt.setdefault(item.run_id,item)
    output=[]
    for run in runs:
        if run.case_id not in visible:continue
        case=visible[run.case_id];picked=chosen[run.id];all_snapshots=[s for s in snapshots if s.run_id==run.id]
        ps=[p for p in projections if p.run_id==run.id and p.snapshot_id==(picked.id if picked else None)]
        if picked:
            issues=list((await db.execute(select(rm.Issue).where(rm.Issue.id.in_([UUID(x) for x in picked.manifest['issue_ids']])))).scalars())
            checks_completed=bool(picked.manifest.get('rule_check_ids'))
        else:
            from app.consignment.service import review_issues,review_checks
            issues=await db.run_sync(lambda s:review_issues(s,run,case,strict_current=False))
            checks_completed=bool(await db.run_sync(lambda s:review_checks(s,run,case,strict_current=False)))
        views=[await issue_view(db,i,picked.manifest['issue_disposition_ids'] if picked else None) for i in issues]
        counts={'blocking':sum(v['blocking_delivery'] for v in views),'warning':sum(v['severity']=='warning' and v['status'] in {'open','unresolved'} for v in views),
            'undetermined':sum(v['severity']=='undetermined' and v['status']=='open' or v['status']=='unresolved' for v in views)}
        risk=filters.get('risk',[]);risk=risk if isinstance(risk,list) else [risk]
        if risk and not any(counts.get(k,0)>0 for k in risk):continue
        hits=[]
        for projection in ps:
            for value in values_by_projection.get(projection.id,[]):
                if filters.get('only_confirmed') in {True,'true'} and value.state!='confirmed':continue
                for condition in ('origin','recipient'):
                    role=filters.get(condition+'_role');roles=ROLE_NAMES.get(role,{role}) if role else None
                    if not filters.get(condition+'_value') or filters[condition+'_value'].casefold() not in value.normalized_value.casefold():continue
                    if roles and value.role not in roles and value.field_key not in roles:continue
                    hits.append({'condition':condition,'filename':projection.filename,'document_version':projection.document_version,
                        'logical_document':projection.summary,'field_role':value.role,'state':value.state,
                        'target':{'case_id':str(case.id),'run_id':str(run.id),'document_id':str(projection.document_id),'document_version':projection.document_version,
                            'parse_artifact_id':str(projection.parse_artifact_id) if projection.parse_artifact_id else None,'logical_document_id':str(projection.logical_document_id) if projection.logical_document_id else None,
                            'field_id':str(value.field_assertion_id),'business_revision_id':str(projection.business_revision_id) if projection.business_revision_id else None,
                            'export_snapshot_id':str(picked.id) if picked else None}})
        from app.management.index import summary_fingerprint
        fingerprint=await db.run_sync(lambda sync:summary_fingerprint(sync,run,picked))
        summary_valid=bool(ps and all(p.state=='ready' and p.dependency_fingerprint==fingerprint for p in ps))
        attempt=latest_attempt.get(run.id);initiator=users.get(run.initiated_by);exporter=users.get(picked.exported_by) if picked else None
        reviewer=users.get(UUID(picked.manifest['confirmed_by'])) if picked else None
        output.append({'run_id':str(run.id),'case_id':str(case.id),'case_name':case.name,'case_short_code':case.short_code,
            'workflow_type':run.workflow_type,'target_language':run.target_language,'source_count':len({(p.document_id,p.document_version) for p in ps}),
            'source_types':sorted({p.doc_type for p in ps}),'initiated_by':initiator.display_name if initiator else str(run.initiated_by),'initiated_at':run.created_at,
            'reviewed_by':reviewer.display_name if reviewer else None,'exported_by':exporter.display_name if exporter else None,
            'current_work_status':run.status.value,'current_stage':run.current_stage,'has_successful_snapshot':bool(all_snapshots),
            'displayed_snapshot_id':str(picked.id) if picked else None,'displayed_snapshot_at':picked.exported_at if picked else None,
            'displayed_snapshot_is_latest':bool(picked and picked.id==all_snapshots[0].id),'working_version_differs':bool(picked and picked.manifest['business_revision_id']!=str(run.current_business_revision_id)),
            'latest_export_attempt_id':str(attempt.id) if attempt else None,'latest_export_attempt_status':attempt.status if attempt else None,
            'latest_export_attempt_error_summary':attempt.error_summary if attempt else None,'issue_counts':counts,
            'rules_status':'run' if checks_completed else 'not_run','matched_sources':hits,
            'summary_status':'ready' if summary_valid else 'stale','last_activity_at':run.updated_at,
            'allowed_actions':['view','open_review','ask','delete_case'] if case.owner_user_id==principal.user_id else ['view','open_review','ask'],'disabled_reasons':{}})
    sort=filters.get('sort','activity_desc');key='displayed_snapshot_at' if sort.startswith('exported') else 'initiated_at' if sort.startswith('initiated') else 'last_activity_at'
    output.sort(key=lambda x:(x.get(key) or x['last_activity_at'],x['run_id']),reverse=not sort.endswith('asc'))
    return output


def paginate(rows,cursor,limit,filters):
    from app.api.idempotency import request_digest
    digest=request_digest(filters)
    scope=getattr(rows,'scope',None)
    sort=filters.get("sort","activity_desc")
    key="displayed_snapshot_at" if sort.startswith("exported") else "initiated_at" if sort.startswith("initiated") else "last_activity_at"
    def row_key(row):
        if "run_id" in row:
            moment=row.get(key) or row["last_activity_at"]
            return (moment.isoformat() if hasattr(moment,"isoformat") else str(moment),row["run_id"])
        return (str(row["seq"]).zfill(20),"")
    if cursor:
        try:
            value=json.loads(base64.urlsafe_b64decode(cursor))
            if value["filter"]!=digest:raise ValueError
            anchor=tuple(value["last"])
            rows=[row for row in rows if row_key(row)<anchor] if "run_id" in (rows[0] if rows else {}) and not sort.endswith("asc") else [row for row in rows if row_key(row)>anchor]
        except (ValueError,KeyError) as exc:raise InvalidRequest("管理台游标与当前筛选不一致") from exc
    page=rows[:limit]
    next_cursor=base64.urlsafe_b64encode(json.dumps({"last":row_key(page[-1]),"filter":digest,"scope":scope}).encode()).decode() if len(rows)>limit else None
    return {"items":page,"next_cursor":next_cursor,"has_more":next_cursor is not None,"as_of":datetime.now(UTC).isoformat()}


async def summary(db,run,case,snapshot=None):
    from app.management.index import summary_dependencies,fact_summary
    from app.api.idempotency import request_digest
    dependency=await db.run_sync(lambda sync:summary_dependencies(sync,run,snapshot))
    fingerprint=request_digest(dependency)
    documents=[]
    refs=(await db.execute(select(RunDocument).where(RunDocument.run_id==run.id).order_by(RunDocument.ordinal))).scalars().all()
    for ref in refs:
        source=await source_view(db,ref.document_id,ref.document_version)
        from app.management.versions import run_parse
        frozen_source=next((s for s in snapshot.manifest["source_versions"] if s["document_id"]==str(ref.document_id) and s["document_version"]==ref.document_version),None) if snapshot else None
        if snapshot and not frozen_source:continue
        parse=await db.get(em.ParseArtifact,UUID(frozen_source["parse_artifact_id"])) if frozen_source else await db.run_sync(lambda sync:run_parse(sync,run,ref.document_id,ref.document_version))
        logical=(await db.execute(select(em.LogicalDocument).where(em.LogicalDocument.parse_artifact_id==parse.id).order_by(em.LogicalDocument.ordinal))).scalars().all() if parse else []
        labels="、".join(x.title or x.type_candidate.value for x in logical)
        text=f"{source['filename']}，{labels}，解析{parse.page_count or '未知'}页" if parse else f"{source['filename']}，尚未识别"
        target={"case_id":str(case.id),"run_id":str(run.id),"document_id":str(ref.document_id),"document_version":ref.document_version,"parse_artifact_id":str(parse.id) if parse else None,"export_snapshot_id":str(snapshot.id) if snapshot else None,"business_revision_id":snapshot.manifest["business_revision_id"] if snapshot else str(run.current_business_revision_id) if run.current_business_revision_id else None}
        saved=(await db.execute(select(RunDocumentSummary).where(RunDocumentSummary.run_id==run.id,RunDocumentSummary.document_id==ref.document_id,RunDocumentSummary.document_version==ref.document_version))).scalar_one_or_none()
        if not snapshot and saved and saved.state=="ready" and saved.dependency_fingerprint!=fingerprint:
            from app.management.index import refresh_run_summaries
            await db.run_sync(lambda sync:refresh_run_summaries(sync,run))
        text=saved.summary if not snapshot and saved and saved.state=="ready" and saved.parse_artifact_id==(parse.id if parse else None) and saved.dependency_fingerprint==fingerprint else text
        business_id=UUID(snapshot.manifest["business_revision_id"]) if snapshot else run.current_business_revision_id
        decisions=list((await db.execute(select(em.SectionAssignmentDecision).where(
            em.SectionAssignmentDecision.business_revision_id==business_id,
            em.SectionAssignmentDecision.logical_document_type.is_not(None)).order_by(em.SectionAssignmentDecision.created_at))).scalars())
        if snapshot:decisions=[d for d in decisions if str(d.id) in snapshot.manifest.get("section_assignment_ids",[])]
        sections=list((await db.execute(select(em.DocumentSection).where(em.DocumentSection.parse_artifact_id==parse.id))).scalars()) if parse else []
        section_logical={s.id:s.logical_document_id for s in sections}
        from app.documents.type_catalog import candidate_document_type
        types={x.id:candidate_document_type(x,parse) for x in logical}
        types.update({section_logical[d.section_id]:d.logical_document_type for d in decisions if d.section_id in section_logical})
        summary_ready=bool(parse and (snapshot or saved and saved.state=='ready' and saved.parse_artifact_id==parse.id and saved.dependency_fingerprint==fingerprint))
        field_query=select(em.FieldAssertion).where(em.FieldAssertion.parse_artifact_id==parse.id) if parse else select(em.FieldAssertion).where(False)
        field_query=field_query.where(em.FieldAssertion.id.in_([UUID(value) for value in snapshot.manifest["field_assertion_ids"]])) if snapshot else field_query.join(em.BusinessRevisionField,em.BusinessRevisionField.field_assertion_id==em.FieldAssertion.id).where(em.BusinessRevisionField.business_revision_id==business_id)
        fields=list((await db.execute(field_query)).scalars())
        review_query=select(em.FieldReviewEvent).where(em.FieldReviewEvent.field_assertion_id.in_([field.id for field in fields])).order_by(em.FieldReviewEvent.review_sequence)
        review_query=review_query.where(em.FieldReviewEvent.id.in_([UUID(value) for value in snapshot.manifest["field_review_event_ids"]])) if snapshot else review_query.where(em.FieldReviewEvent.business_revision_id==business_id)
        reviews={event.field_assertion_id:event for event in (await db.execute(review_query)).scalars()}
        if snapshot:text="；".join([text,*fact_summary(fields,reviews),f"冻结业务 {business_id}，译文 {snapshot.manifest['translation_revision_id']}"])
        documents.append({"source":source,"generated_at":snapshot.exported_at if snapshot else saved.created_at if saved else run.updated_at,"dependency_fingerprint":fingerprint,"summary_scope":"snapshot_document" if snapshot else "run_document","parse_artifact_id":str(parse.id) if parse else None,"short_summary":text,"state":"ready" if summary_ready else "stale",
            "logical_documents":[{"logical_document_id":str(x.id),"label":x.title or types.get(x.id,x.type_candidate.value),"doc_type":types.get(x.id,x.type_candidate.value),"summary":"；".join([f"{x.title or types.get(x.id,x.type_candidate.value)}，{sum(section.logical_document_id == x.id for section in sections)}个区段",*fact_summary([field for field in fields if field.logical_document_id==x.id],reviews)]),
                "page_range":f"{x.page_start or '?'}–{x.page_end or '?'}",
                "sections":[{"section_id":str(section.id),"title":section.title or "区段","page_range":f"{section.page_start or '?'}–{section.page_end or '?'}","target":{**target,"logical_document_id":str(x.id),"section_id":str(section.id)}} for section in sections if section.logical_document_id==x.id],
                "generated_at":snapshot.exported_at if snapshot else datetime.now(UTC),"state":"ready" if summary_ready else "stale",
                "dependency_fingerprint":request_digest({"run":fingerprint,"logical_document_id":str(x.id)}),
                "target":{**target,"logical_document_id":str(x.id)}} for x in logical],"source_target":target})
    issue_query=select(rm.Issue).where(rm.Issue.run_id==run.id)
    issue_query=issue_query.where(rm.Issue.id.in_([UUID(x) for x in snapshot.manifest["issue_ids"]])) if snapshot else issue_query.where(rm.Issue.business_revision_id==run.current_business_revision_id)
    issues=(await db.execute(issue_query)).scalars().all()
    if not snapshot:issues=[i for i in issues if i.translation_revision_id in {None,run.current_translation_revision_id}]
    views=[await issue_view(db,x,snapshot.manifest["issue_disposition_ids"] if snapshot else None) for x in issues]
    snapshots=(await db.execute(select(ExportSnapshot).where(ExportSnapshot.run_id==run.id).order_by(ExportSnapshot.exported_at.desc()))).scalars().all()
    stage=f"本次确认稿导出于 {snapshot.exported_at.isoformat()}" if snapshot else run.current_stage
    return {"run_id":str(run.id),"generated_at":snapshot.exported_at if snapshot else datetime.now(UTC),"dependency_fingerprint":fingerprint,"dependencies":dependency,
        "state":"ready" if all(document["state"]=="ready" for document in documents) else "stale",
        "summary":"；".join([case.name,f"{len(refs)} 份原件",f"目标 {snapshot.manifest['target_language'] if snapshot else run.target_language or '无译文'}",f"范围 {snapshot.manifest['translation_scope'] if snapshot else run.task_spec.get('translation_scope','不适用')}",
            f"业务 {snapshot.manifest.get('business_revision_id') if snapshot else run.current_business_revision_id}",f"译文 {snapshot.manifest.get('translation_revision_id') if snapshot else run.current_translation_revision_id}",str(stage),f"疑点 {len(views)}，处置 {','.join(x['status'] for x in views) or '无'}",run.failure_reason or run.pause_explanation or "" if not snapshot else "历史，只读"]),"documents":documents,
        "issues":[{"issue_id":x["issue_id"],"severity":x["severity"],"title":x["rule_name"],"disposition":x["status"],"target":x["a"]["target"] if x["a"] else {"case_id":str(case.id)}} for x in views],
        "snapshots":[{"snapshot_id":str(x.id),"exported_at":x.exported_at,"exported_by":str(x.exported_by),"is_latest":index==0,
            "generated_at":x.exported_at,"state":"ready","dependency_fingerprint":request_digest({"snapshot_id":str(x.id),"manifest":x.manifest}),
            "summary":"；".join([f"交付快照 {x.id}",f"目标 {x.manifest.get('target_language','未知')}，范围 {x.manifest.get('translation_scope','未知')}",
                f"业务 {x.manifest.get('business_revision_id','未知')}，译文 {x.manifest.get('translation_revision_id','未知')}",
                f"{len(x.manifest.get('source_versions',[]))}份冻结原件，{len(x.manifest.get('issue_ids',[]))}个疑点，{len(x.manifest.get('issue_disposition_ids',[]))}个处置",f"格式 {'、'.join(x.manifest.get('output_formats',[]))}","历史，只读"])} for index,x in enumerate(snapshots)]}
