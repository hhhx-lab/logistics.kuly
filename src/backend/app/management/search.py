"""M01与C08共享SQL筛选：授权、同逻辑单据交集、冻结成功快照投影。"""
from datetime import UTC,datetime,time,timedelta,date
from zoneinfo import ZoneInfo
from uuid import UUID
from sqlalchemy import and_,or_,exists,select,func,tuple_
from sqlalchemy.orm import aliased
from sqlalchemy.dialects.postgresql import array
from app.api.errors import InvalidRequest
from app.documents.models import Case,CaseAccessGrant,CaseStatus
from app.exports.models import ExportSnapshot,ReviewConfirmation,ExportAttempt
from app.identity.models import User
from app.management.models import SearchProjection,SearchFieldValue
from app.workflows.models import Run
from app.documents.extraction import ORIGIN_FIELD_KEYS

ROLE_NAMES={"origin":ORIGIN_FIELD_KEYS,"receipt":{"receipt","place_of_receipt"},
    "pol":{"pol","port_of_loading","loading_port"},"country":{"country","country_of_origin"},
    "consignee":{"consignee"},"buyer":{"buyer"},"notify":{"notify","notify_party"}}


def contains(column,value):
    return column.ilike('%'+value.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%',escape='\\')


def many(filters,key):
    value=filters.get(key,[])
    return value if isinstance(value,list) else [value]


def local_bounds(filters,timezone):
    start=end=None
    try:
        if filters.get("local_date_from"):start=datetime.combine(date.fromisoformat(filters["local_date_from"]),time.min,ZoneInfo(timezone)).astimezone(UTC)
        if filters.get("local_date_to"):end=datetime.combine(date.fromisoformat(filters["local_date_to"])+timedelta(days=1),time.min,ZoneInfo(timezone)).astimezone(UTC)
    except ValueError as exc:raise InvalidRequest("日期必须使用YYYY-MM-DD") from exc
    if start and end and start>=end:raise InvalidRequest("开始日期不能晚于结束日期")
    return start,end


def _field_condition(projection,filters,*,role=None,value=None,q=None):
    field=aliased(SearchFieldValue)
    conditions=[field.projection_id==projection.id]
    if role:conditions.append(or_(field.role.in_(ROLE_NAMES.get(role,{role})),field.field_key.in_(ROLE_NAMES.get(role,{role}))))
    if value:conditions.append(contains(field.normalized_value,value))
    if q:conditions.append(contains(field.normalized_value,q))
    if filters.get('only_confirmed') in {True,'true'}:conditions.append(field.state=='confirmed')
    return exists(select(field.id).where(*conditions))


def projection_conditions(projection,filters,timezone):
    conditions=[]
    if many(filters,'doc_type'):conditions.append(projection.doc_type.in_(many(filters,'doc_type')))
    for dimension in ('origin','recipient'):
        if filters.get(dimension+'_value'):
            conditions.append(_field_condition(projection,filters,role=filters.get(dimension+'_role'),value=filters[dimension+'_value']))
    q=(filters.get('q') or '').strip()
    if q:conditions.append(or_(contains(projection.filename,q),contains(projection.summary,q),_field_condition(projection,filters,q=q),
        exists(select(Case.id).where(Case.id==projection.case_id,or_(contains(Case.name,q),contains(Case.short_code,q))).correlate(projection))))
    if filters.get('date_type')=='uploaded':
        start,end=local_bounds(filters,timezone)
        if start:conditions.append(projection.uploaded_at>=start)
        if end:conditions.append(projection.uploaded_at<end)
    if filters.get('actor_role')=='uploader' and filters.get('actor_value'):
        conditions.append(exists(select(User.id).where(User.id==projection.uploaded_by,contains(User.display_name,filters['actor_value']))))
    return conditions


def candidate_query(principal,filters,timezone):
    """返回授权Run+投影Snapshot候选；C08可复用同Run集合而不另推断字段语义。"""
    now=datetime.now(UTC);p=SearchProjection
    latest=select(ExportSnapshot.id).where(ExportSnapshot.run_id==Run.id).order_by(ExportSnapshot.exported_at.desc(),ExportSnapshot.id.desc()).limit(1).correlate(Run).scalar_subquery()
    has_snapshot=exists(select(ExportSnapshot.id).where(ExportSnapshot.run_id==Run.id).correlate(Run))
    grant=exists(select(CaseAccessGrant.id).where(CaseAccessGrant.case_id==Case.id,CaseAccessGrant.subject_user_id==principal.user_id,
        or_(CaseAccessGrant.expires_at.is_(None),CaseAccessGrant.expires_at>now),
        CaseAccessGrant.capabilities.has_any(array(['read','view','write']))))
    query=select(p.run_id,p.snapshot_id).join(Run,Run.id==p.run_id).join(Case,Case.id==p.case_id).outerjoin(ExportSnapshot,ExportSnapshot.id==p.snapshot_id)
    query=query.where(Case.status==CaseStatus.ACTIVE,Case.retention_expires_at>now,or_(Case.owner_user_id==principal.user_id,grant))
    for key,column in (("case_id",Case.id),("run_id",Run.id),("snapshot_id",p.snapshot_id)):
        if filters.get(key):
            try:identifier=UUID(str(filters[key]))
            except (ValueError,TypeError,AttributeError) as exc:raise InvalidRequest(f"{key}必须是有效对象ID") from exc
            query=query.where(column==identifier)
    tab=filters.get('tab','all')
    states={'awaiting':['awaiting_review','ready_to_export'],'processing':['queued','processing','awaiting_input','exporting'],
        'failed':['failed_retryable','failed_final','cancelled','paused_reconcile'],'indexed':['indexed']}
    if tab=='exported':query=query.where(has_snapshot,p.snapshot_id.is_not(None))
    elif tab=='failed':
        last_attempt_status=select(ExportAttempt.status).where(ExportAttempt.run_id==Run.id).order_by(
            ExportAttempt.created_at.desc(),ExportAttempt.id.desc()).limit(1).correlate(Run).scalar_subquery()
        query=query.where(or_(Run.status.in_(states[tab]),last_attempt_status.in_(['failed_retryable','failed_final','paused_reconcile'])),p.snapshot_id.is_(None))
    elif tab in states:query=query.where(Run.status.in_(states[tab]),p.snapshot_id.is_(None))
    elif tab=='all':query=query.where(or_(and_(has_snapshot,p.snapshot_id.is_not(None)),and_(~has_snapshot,p.snapshot_id.is_(None))))
    else:raise InvalidRequest('不支持此管理台状态页')
    if tab in {'all','exported'} and filters.get('version_scope')!='include_history' and not filters.get('snapshot_id'):query=query.where(or_(p.snapshot_id.is_(None),p.snapshot_id==latest))
    if many(filters,'language'):query=query.where(Run.target_language.in_(many(filters,'language')))
    if many(filters,'format'):
        formats=func.coalesce(ExportSnapshot.manifest['output_formats'],Run.task_spec['output_formats'])
        query=query.where(or_(*[formats.contains([value]) for value in many(filters,'format')]))
    conditions=projection_conditions(p,filters,timezone)
    if filters.get('match_scope','same_logical_document')=='any_in_case':
        for index,condition in enumerate(conditions):
            # 重建各条件的相关子查询，确保跨文件仅限同Run及同展示快照。
            other=aliased(SearchProjection)
            rebuilt=projection_conditions(other,filters,timezone)[index]
            query=query.where(exists(select(other.id).where(other.run_id==p.run_id,other.snapshot_id.is_not_distinct_from(p.snapshot_id),rebuilt)))
    else:query=query.where(*conditions)
    actor=(filters.get('actor_value') or '').strip();role=filters.get('actor_role','initiator')
    if actor and role!='uploader':
        actor_id=Run.initiated_by
        if role=='exporter':actor_id=ExportSnapshot.exported_by
        elif role=='reviewer':actor_id=select(ReviewConfirmation.confirmed_by).where(ReviewConfirmation.run_id==Run.id,
            ReviewConfirmation.business_revision_id==p.business_revision_id).order_by(ReviewConfirmation.confirmed_at.desc()).limit(1).correlate(Run,p).scalar_subquery()
        query=query.where(exists(select(User.id).where(User.id==actor_id,contains(User.display_name,actor))))
    if filters.get('date_type','activity')!='uploaded':
        start,end=local_bounds(filters,timezone)
        moment=Run.created_at if filters.get('date_type')=='initiated' else ExportSnapshot.exported_at if filters.get('date_type')=='exported' else Run.updated_at
        if filters.get('date_type')=='reviewed':moment=select(ReviewConfirmation.confirmed_at).where(ReviewConfirmation.run_id==Run.id,ReviewConfirmation.business_revision_id==p.business_revision_id).order_by(ReviewConfirmation.confirmed_at.desc()).limit(1).correlate(Run,p).scalar_subquery()
        if start:query=query.where(moment>=start)
        if end:query=query.where(moment<end)
    return query.distinct()


def ordered_candidates(principal,filters,timezone):
    """每Run只选最近命中的成功快照，再在SQL中稳定排序；不改变字段交集。"""
    matched=candidate_query(principal,filters,timezone).subquery()
    ranked=select(matched.c.run_id,matched.c.snapshot_id,func.row_number().over(
        partition_by=matched.c.run_id,
        order_by=(ExportSnapshot.exported_at.desc().nulls_last(),matched.c.snapshot_id.desc().nulls_last())).label('position'))\
        .outerjoin(ExportSnapshot,ExportSnapshot.id==matched.c.snapshot_id).subquery()
    sort=filters.get('sort','activity_desc')
    if sort not in {'activity_desc','activity_asc','initiated_desc','initiated_asc','exported_desc','exported_asc'}:
        raise InvalidRequest('不支持此管理台排序')
    moment=func.coalesce(ExportSnapshot.exported_at,Run.updated_at) if sort.startswith('exported') else Run.created_at if sort.startswith('initiated') else Run.updated_at
    return select(ranked.c.run_id,ranked.c.snapshot_id,moment.label('sort_at'),
        Run.updated_at,Run.status,Run.current_business_revision_id,Run.current_translation_revision_id,
        Case.review_state_version,Case.case_version).join(Run,Run.id==ranked.c.run_id)\
        .join(Case,Case.id==Run.case_id).outerjoin(ExportSnapshot,ExportSnapshot.id==ranked.c.snapshot_id).where(ranked.c.position==1)


def candidate_page(query,filters,*,anchor=None,limit=None):
    candidates=query.subquery()
    order=(candidates.c.sort_at,candidates.c.run_id)
    ascending=filters.get('sort','activity_desc').endswith('asc')
    page=select(candidates)
    if anchor is not None:
        page=page.where(tuple_(*order)>tuple_(*anchor) if ascending else tuple_(*order)<tuple_(*anchor))
    page=page.order_by(*[column.asc() if ascending else column.desc() for column in order])
    return page.limit(limit) if limit is not None else page
