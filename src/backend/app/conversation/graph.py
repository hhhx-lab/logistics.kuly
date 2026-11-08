from __future__ import annotations
from app.languages import requested_language, language_reply

from app.extensions.service import SKILL_ACTION_POLICY

import asyncio
import re
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from uuid import UUID

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from sqlalchemy import or_, select, text

from app.api.dependencies import set_tenant_context
from app.api.errors import ApiError, ServiceUnavailable
from app.config import AdapterKind, ModelRole
from app.conversation.context import assemble_context, requests_prior_constraints
from app.conversation.costs import ConversationCostBudget
from app.conversation.domain import asks_case_specific, capability_conflict, comparison_task_request, general_explanation, general_knowledge_request, is_greeting, is_instruction_attack, run_progress_request, translation_task_request
from app.conversation.evidence import EvidenceClarification, deterministic_answer, field_key, query_evidence, source_versions, verify_answer, verify_natural_answer
from app.conversation.calculation import calculate, calculation_requested
from app.conversation.knowledge import knowledge_sources, knowledge_cards, verify_knowledge_paragraphs
from app.conversation.models import CheckpointRef, Conversation, ConversationTurn, ConversationWorkItem, ConversationSummary, Message, ReferenceSnapshot
from app.api.idempotency import request_digest
from app.conversation.service import GRAPH_VERSION, LEASE_SECONDS, WORK_RETENTION_UNTIL, append_event, finish_answer, load_conversation, load_work, store_work
from app.conversation.state import ConversationState
from app.infra.model_gateway import Failed, InMemoryCallLedger, NeedsReconcile, Succeeded, build_model_gateway
from app.prompts.registry import PromptBudgetExceeded, PromptRenderError, PromptUnavailable, estimate_tokens, get_prompt_registry
from app.prompts.schemas import AnswerDraft, GeneralKnowledgeAnswer, IntentCandidateV3 as IntentCandidateV2, NaturalAnswerDraft, SessionSummary
from app.prompts.schemas import resolve_output_schema
from app.workflows.contracts import ModelBinding
from app.workflows.costs import PricedLedger

ACTION_CATALOG = ["get_consignment_overview", "list_comparison_differences", "get_comparison_evidence", "get_run_timeline", "query_case_facts", "query_provenance", "general_knowledge", "list_issues", "prepare_task", "open_review", "propose_user_memory", "clarify", "out_of_domain"]
READ_ONLY_SKILL_ACTIONS = {"get_consignment_overview", "list_comparison_differences", "get_comparison_evidence", "get_run_timeline", "query_case_facts", "query_provenance", "list_issues", "open_review"}


def answer_fact_groups(facts, *, overview=False, question=""):
    """货物核心证据整行取舍；概览按业务维度轮转，保留不同原值。"""
    groups, lines, totals, paired = [], {}, {}, {}
    for fact in facts:
        line = fact.get("line_item_id") if fact.get("field_key") in {"sku", "description", "quantity"} else None
        if line:
            if line not in lines:
                lines[line] = []
                groups.append(lines[line])
            lines[line].append(fact)
        elif overview and fact.get("document_id") and fact.get("field_key") in {"total_amount", "amount"}:
            # 同份单据的商品合计与应付总额共同保留，不让重复来源挤掉金额口径。
            total = (fact["document_id"], fact["field_key"])
            if total not in totals:
                totals[total] = []
                groups.append(totals[total])
            totals[total].append(fact)
        elif fact.get("paired_source_id"):
            source = fact["paired_source_id"]
            if source not in paired:
                paired[source] = []
                groups.append(paired[source])
            paired[source].append(fact)
        else:
            groups.append([fact])
    if not overview:
        return groups
    buckets = [[] for _ in range(6)]
    for group in groups:
        fact = group[0]
        key, role = fact.get("field_key"), fact.get("role")
        category = (2 if fact.get("line_item_id") and key in {"sku", "description", "quantity"}
            else 0 if key in {"name", "address"} and role in {"seller", "buyer", "shipper", "consignee"}
            else 1 if key in {"port_of_loading", "port_of_discharge", "place_of_receipt", "place_of_delivery"}
            else 2 if key in {"sku", "description", "goods_description", "quantity", "package_count"}
            else 3 if key in {"total_amount", "amount", "freight", "insurance", "currency", "incoterm"}
            else 4 if key in {"invoice_no", "invoice_number", "order_no", "transport_reference", "invoice_date"}
            else 5)
        buckets[category].append(group)
    seen = {}
    ranked = []
    for group in buckets[2]:
        keys = {f.get("field_key") for f in group}
        signature = tuple(sorted((f.get("field_key"), str(f.get("value")), str(f.get("unit"))) for f in group if f.get("field_key") in {"sku", "quantity"}))
        repeat = seen.get(signature, 0)
        seen[signature] = repeat + 1
        ranked.append(((not {"sku", "description", "quantity"}.issubset(keys), repeat), group))
    buckets[2] = [group for _, group in sorted(ranked, key=lambda item: item[0])]
    buckets[0].sort(key=lambda group: group[0].get("field_key") != "name")
    cost_priority = {"total_amount": 0, "amount": 0, "freight": 1, "insurance": 2, "currency": 3, "incoterm": 4}
    requested_costs = {key for key, pattern in (("freight", r"运费|freight"), ("insurance", r"保险|insurance"),
        ("total_amount", r"货值|合计|总额|total|CIF"), ("amount", r"金额|amount")) if re.search(pattern, question, re.I)}
    cost_counts, ranked_costs = {}, []
    for group in buckets[3]:
        key = group[0].get("field_key")
        count = cost_counts.get(key, 0)
        cost_counts[key] = count + 1
        ranked_costs.append(((bool(requested_costs) and key not in requested_costs, count, cost_priority[key]), group))
    buckets[3] = [group for _, group in sorted(ranked_costs, key=lambda item: item[0])]
    balanced = []
    for index in range(max((len(bucket) for bucket in buckets[:5]), default=0)):
        balanced.extend(bucket[index] for bucket in buckets[:5] if index < len(bucket))
    return balanced + buckets[5]


def unasked_record_keys(question):
    """预算不足时，只移除当前未询问的完整记录类别。"""
    keys = []
    if not re.search(r"进度|状态|记录|历史|过程|处理|失败|重试|progress|status|history|record|historique|état|estado|historial|статус|истори", question, re.I):
        keys.append("processing_records")
    if not re.search(r"疑点|风险|问题|异常|修订|忽略|risk|issue|problem|anomal|risque|riesgo|риск|проблем", question, re.I):
        keys.extend(("issues", "more_issues"))
    if not re.search(r"核对|校验|比对|比较|对比|差异|一致|冲突|compar|check|differ|conflict|vérif|verif|сравн|провер", question, re.I):
        keys.extend(("comparison_context", "comparison_results", "comparison_more"))
    return keys


async def frozen_review_cards(db, snapshot):
    from app.documents.models import DocumentVersion

    historical = next((f for f in snapshot.frozen if f.get("kind") == "snapshot"), None)
    direct = [f for f in snapshot.frozen if f.get("kind") in {"document", "section"}]
    scopes = direct or [{**v, "run_id": f.get("run_id"),
        "business_revision_id": f.get("business_revision_id")} for f in snapshot.frozen for v in f.get("source_versions", [])]
    if not scopes:
        return []
    scope = scopes[0]
    if historical:
        fixed = next((v for v in historical["source_versions"] if v["document_id"] == scope["document_id"]
            and v["document_version"] == scope["document_version"]), None)
        if fixed is None:
            return []
        scope = {**scope, **fixed, "run_id": historical["run_id"]}
    version = (await db.execute(select(DocumentVersion).where(DocumentVersion.document_id == UUID(scope["document_id"]),
        DocumentVersion.version == scope["document_version"], DocumentVersion.case_id == snapshot.case_id))).scalar_one_or_none()
    if version is None:
        return []
    target = {"case_id": str(snapshot.case_id), "document_id": scope["document_id"], "document_version": scope["document_version"]}
    for key in ("parse_artifact_id", "logical_document_id", "section_id", "run_id"):
        if scope.get(key):
            target[key] = scope[key]
    if target.get("parse_artifact_id"):
        target["business_revision_id"] = historical["business_revision_id"] if historical else str(snapshot.business_revision_id) if snapshot.business_revision_id else None
    if historical:
        target["export_snapshot_id"] = historical["snapshot_id"]
    return [{"type": "evidence", "cards": [{"card_id": scope["document_id"], "filename": version.filename,
        "logical_document": "关联原件", "location": "打开关联版本", "excerpt": "查看本轮固定的原件与复核信息",
        "status": "historical" if historical else "candidate", "versions": f"原件 v{version.version}", "target": target}]}]


class ConversationCallLedger(InMemoryCallLedger):
    """每次模型响应独立提交；撤权或图后续崩溃不丢失已发生的调用。"""

    def __init__(self, preflight, tenant_id, *, settings=None, turn_id=None, user_id=None, summary_job_id=None):
        super().__init__()
        self.tenant_id = tenant_id
        self._preflight = preflight
        self._loop = asyncio.get_running_loop()
        self.settings, self.turn_id, self.user_id = settings, turn_id, user_id
        self.summary_job_id = summary_job_id

    def append(self, record):
        if self.settings is not None:
            from app.infra.database import create_sync_engine, create_sync_session_factory, TENANT_SETTING
            from app.conversation.models import Conversation
            engine = create_sync_engine(self.settings)
            try:
                with create_sync_session_factory(engine)() as db, db.begin():
                    db.execute(text("SET LOCAL ROLE logistics_worker"))
                    db.execute(text(f"SET LOCAL {TENANT_SETTING} = '{UUID(str(self.tenant_id))}'"))
                    turn = db.get(ConversationTurn, UUID(str(self.turn_id)))
                    conversation = db.get(Conversation, turn.conversation_id) if turn else None
                    if turn is None or conversation is None or conversation.owner_user_id != self.user_id:
                        raise RuntimeError("模型调用对应的本人会话不存在")
                    exists = db.scalar(select(ConversationWorkItem.id).where(ConversationWorkItem.turn_id == turn.id,
                        ConversationWorkItem.kind == "model_call", ConversationWorkItem.payload["call_id"].astext == record.call_id).limit(1))
                    if exists is None:
                        db.add(ConversationWorkItem(tenant_id=turn.tenant_id, conversation_id=turn.conversation_id,
                            turn_id=turn.id, kind="model_call", payload={**record.model_dump(mode="json"), **({"summary_job_id": str(self.summary_job_id)} if self.summary_job_id else {})},
                            expires_at=WORK_RETENTION_UNTIL))
            finally:
                engine.dispose()
        super().append(record)

    def before_send(self):
        check = asyncio.run_coroutine_threadsafe(self._preflight(), self._loop)
        try:
            check.result(timeout=15)
        except BaseException:
            check.cancel()
            raise


def skill_material(capabilities):
    return [{"name": c.get("name", "本人 Skill"), "version": c["extension_version"],
        "instructions": c.get("instructions", ""), "result_format": c.get("result_format", ""),
        "parameters": c.get("user_parameters", {})} for c in capabilities if c["kind"] in {"user_skill", "builtin_skill"} and c.get("instructions")]


def render_material(spec, material, **values):
    # 旧冻结提示词仍按原变量回放，新版才接受低信任 Skill 资料。
    if any(v.name == "skill_material" for v in spec.variables):
        values["skill_material"] = material
    return spec.render(**values)


def fresh_state(conversation_id, turn_id) -> ConversationState:
    return {"conversation_id": str(conversation_id), "turn_id": str(turn_id), "active_case_id": None,
        "reference_snapshot_id": None, "intent_candidate_id": None, "tool_result_ids": [], "evidence_ids": [],
        "draft_id": None, "decision": "plan", "error_class": None, "plan_rounds": 0, "tool_calls": 0}


def build_conversation_graph(nodes) -> StateGraph:
    graph = StateGraph(ConversationState)
    for name in ("scope_and_freeze", "fast_path", "intent_and_plan", "execute_tools", "clarify", "compose_answer", "verify_and_emit"):
        graph.add_node(name, getattr(nodes, name))
    graph.add_edge(START, "scope_and_freeze")
    graph.add_edge("scope_and_freeze", "fast_path")
    graph.add_conditional_edges("fast_path", lambda s: s["decision"], {"plan": "intent_and_plan", "tools": "execute_tools", "clarify": "clarify"})
    graph.add_conditional_edges("intent_and_plan", lambda s: s["decision"], {"tools": "execute_tools", "clarify": "clarify"})
    graph.add_conditional_edges("execute_tools", lambda s: s["decision"], {"answer": "compose_answer", "clarify": "clarify", "replan": "intent_and_plan", "done": END})
    graph.add_edge("compose_answer", "verify_and_emit")
    graph.add_edge("verify_and_emit", END)
    graph.add_edge("clarify", END)
    return graph


def _summary_references(value):
    if not isinstance(value, dict):
        return []
    allowed = {"case_id", "document_id", "run_id", "parse_artifact_id", "logical_document_id", "section_id", "business_revision_id", "translation_revision_id", "export_snapshot_id", "snapshot_id"}
    refs = [str(item) for key, item in value.items() if key in allowed and item]
    if value.get("kind") in {"case", "document", "run", "section", "snapshot"} and value.get("id"):
        refs.append(str(value["id"]))
    for item in value.values():
        if isinstance(item, dict): refs.extend(_summary_references(item))
        elif isinstance(item, list):
            for child in item: refs.extend(_summary_references(child))
    return list(dict.fromkeys(refs))


def _session_summary_material(message):
    """保留自由用户要求，助手只给受控类别；卡片/助手事实不进入语义摘要。"""
    topics = {"task_draft": "曾讨论待启动任务", "service": "曾讨论任务状态", "evidence": "曾讨论来源查看",
        "memory_suggestion": "曾讨论待确认偏好", "issues": "曾讨论待核疑点", "export": "曾讨论交付历史"}
    refs = _summary_references({"references": message.references, "cards": message.cards})
    valid_refs = []
    for value in refs:
        try:
            valid_refs.append(str(UUID(value)))
        except (ValueError, TypeError, AttributeError):
            continue
    text = message.text if message.role == "user" else "；".join(dict.fromkeys(topics[card.get("type")] for card in message.cards or [] if card.get("type") in topics)) or "曾有助手答复，其事实不作摘要输入"
    return {"message_id": str(message.id), "role": message.role, "text": text,
        "classification": "用户要求或声称（待核，低信任）" if message.role == "user" else "讨论主题或待办（无事实结论）",
        "references": list(dict.fromkeys(valid_refs))}


def _summary_navigation(frozen, inherited=None):
    """系统冻结来源的版本导航；不接受模型返回的版本或单据文本。"""
    keys = {"case_id", "document_id", "parse_artifact_id", "logical_document_id", "section_id", "run_id",
        "business_revision_id", "translation_revision_id", "export_snapshot_id", "snapshot_id"}
    versions = {"document_version", "business_revision_version", "translation_revision_version", "snapshot_version"}
    items = []
    for value in frozen or []:
        if not isinstance(value, dict):
            continue
        entry = dict(inherited or {})
        for key in keys:
            if value.get(key):
                try: entry[key] = str(UUID(str(value[key])))
                except (ValueError, TypeError, AttributeError): pass
        for key in versions:
            if isinstance(value.get(key), int) and not isinstance(value[key], bool) and value[key] > 0:
                entry[key] = value[key]
        if value.get("kind") in {"case", "document", "run", "section", "snapshot"}:
            target_key = {"case": "case_id", "document": "document_id", "run": "run_id", "section": "section_id", "snapshot": "snapshot_id"}[value["kind"]]
            try: entry[target_key] = str(UUID(str(value["id"])))
            except (KeyError, ValueError, TypeError, AttributeError): pass
        children = value.get("source_versions")
        if children:
            items.extend(_summary_navigation(children, entry))
        elif entry:
            items.append(entry)
    return items


class ConversationNodes:
    def __init__(self, session_factory, principal, settings, lease_token=None):
        self.factory, self.principal, self.settings = session_factory, principal, settings
        self.lease_token = lease_token

    def model_ledger(self, state, binding, budget=None):
        base = ConversationCallLedger(lambda: self.preflight(state), self.principal.tenant_id,
            settings=self.settings, turn_id=state["turn_id"], user_id=self.principal.user_id)
        return PricedLedger(base, ModelBinding.model_validate(binding), budget)

    async def model_budget(self, db, turn, binding):
        records = list((await db.execute(select(ConversationWorkItem.payload).where(
            ConversationWorkItem.tenant_id == turn.tenant_id, ConversationWorkItem.turn_id == turn.id,
            ConversationWorkItem.kind == "model_call", ConversationWorkItem.payload["summary_job_id"].astext.is_(None)))).scalars())
        return ConversationCostBudget(settings=self.settings, binding=ModelBinding.model_validate(binding), records=records,
            frozen_limit=turn.prompt_versions.get("chat_max_model_cost_cny"))

    async def store_call_records(self, db, turn, ledger):
        for record in ledger.ledger.records:
            exists = (await db.execute(select(ConversationWorkItem.id).where(ConversationWorkItem.turn_id == turn.id,
                ConversationWorkItem.kind == "model_call", ConversationWorkItem.payload["call_id"].astext == record.call_id).limit(1))).scalar_one_or_none()
            if exists is None:
                item_id = await store_work(db, turn, "model_call", record.model_dump(mode="json"))
                item = await db.get(ConversationWorkItem, UUID(item_id))
                item.expires_at = WORK_RETENTION_UNTIL

    def input_budget(self, binding):
        frozen = ModelBinding.model_validate(binding) if binding else None
        configured = self.settings.chat_input_token_budget - self.settings.response_token_reserve
        return max(0, min(configured, frozen.context_window_tokens - max(frozen.max_output_tokens, self.settings.response_token_reserve))) if frozen else max(0, configured)

    async def preflight(self, state):
        from app.identity.models import User

        async with self.factory() as db, db.begin():
            await set_tenant_context(db, self.principal)
            user = await db.get(User, self.principal.user_id)
            if user is None or user.tenant_id != self.principal.tenant_id or not user.is_active:
                raise ApiError("用户授权已失效。", code="AUTHORIZATION_CHANGED", status_code=403)
            conversation = await load_conversation(db, self.principal, self.settings, UUID(state["conversation_id"]))
            turn = (await db.execute(select(ConversationTurn).where(ConversationTurn.id == UUID(state["turn_id"]),
                ConversationTurn.conversation_id == conversation.id))).scalar_one_or_none()
            if (turn is None or conversation.status != "active" or turn.status != "running"
                or turn.graph_version != GRAPH_VERSION or turn.lease_until is None or turn.lease_until <= datetime.now(UTC)
                or (self.lease_token is not None and turn.lease_token != self.lease_token)):
                raise ApiError("本轮执行授权已失效。", code="TURN_INTERRUPTED", status_code=409)
            snapshot = await db.get(ReferenceSnapshot, turn.reference_snapshot_id)
            if (snapshot is None or snapshot.tenant_id != self.principal.tenant_id
                or snapshot.conversation_id != conversation.id or snapshot.case_id != conversation.case_id):
                raise ApiError("本轮引用不可用。", code="REFERENCE_STALE", status_code=409)

    @asynccontextmanager
    async def scope(self, state):
        async with self.factory() as db, db.begin():
            await set_tenant_context(db, self.principal)
            from app.identity.models import User
            user = await db.get(User, self.principal.user_id)
            if user is None or not user.is_active or user.tenant_id != self.principal.tenant_id:
                raise ApiError("用户授权已失效", code="AUTHORIZATION_CHANGED", status_code=403)
            conversation = await load_conversation(db, self.principal, self.settings, UUID(state["conversation_id"]), write=True)
            turn = (await db.execute(select(ConversationTurn).where(ConversationTurn.id == UUID(state["turn_id"]),
                ConversationTurn.conversation_id == conversation.id).with_for_update())).scalar_one_or_none()
            if turn is None or (self.lease_token and turn.lease_token != self.lease_token) or turn.status not in {"pending", "running"} or conversation.status != "active" or turn.graph_version != GRAPH_VERSION or (turn.status == "running" and (not turn.lease_until or turn.lease_until <= datetime.now(UTC))):
                raise ApiError("本轮已结束或图版本不可恢复。", code="TURN_INTERRUPTED", status_code=409)
            turn.status = "running"
            turn.lease_until = datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS)
            snapshot = await db.get(ReferenceSnapshot, turn.reference_snapshot_id)
            if snapshot is None or snapshot.tenant_id != self.principal.tenant_id or snapshot.conversation_id != conversation.id:
                raise ApiError("本轮引用不可用。", code="REFERENCE_STALE", status_code=409)
            question = (await db.execute(select(Message.text).where(Message.turn_id == turn.id, Message.role == "user"))).scalar_one()
            yield db, conversation, turn, snapshot, question

    async def scope_and_freeze(self, state):
        async with self.scope(state) as (_, conversation, _, snapshot, _):
            return {"active_case_id": str(conversation.case_id) if conversation.case_id else None,
                "reference_snapshot_id": str(snapshot.id)}

    async def answer_context(self, db, conversation, turn, *, knowledge_only=False):
        context = await assemble_context(db, conversation, self.settings, before_turn_no=turn.turn_no,
            input_budget=self.input_budget(turn.prompt_versions.get("model_binding")), knowledge_only=knowledge_only)
        from app.workflows.models import Run, TaskDraft
        spec = dict(turn.prompt_versions.get("current_task_spec", {}))
        if "current_task_spec" not in turn.prompt_versions and conversation.active_run_id:
            run = await db.get(Run, conversation.active_run_id)
            if run and run.case_id == conversation.case_id:
                spec = dict(run.task_spec)
        elif "current_task_spec" not in turn.prompt_versions and conversation.active_task_draft_id:
            draft = await db.get(TaskDraft, conversation.active_task_draft_id)
            if draft and draft.case_id == conversation.case_id:
                spec = {"workflow_type": draft.workflow_type, "workflow_version": draft.workflow_version,
                    "parameters": draft.draft_parameters, "status": "not_started"}
        if knowledge_only:
            spec = {key: value for key, value in spec.items() if key in {"workflow_type", "workflow_version", "target_language", "status"}}
        memory = [{"category": "default_answer_language", "content": turn.prompt_versions.get("interface_language", "zh-CN")}]
        memory.extend({"category": item["category"], "content": item["content"], "applies_when": item.get("applies_when")}
            for item in turn.prompt_versions.get("confirmed_preferences", []) if item["category"] != "target_language")
        return context, spec, memory

    def fit_answer_request(self, spec, system, binding, values):
        """以最终渲染输入核预算，移除整个旧轮/完整证据，不截事实。"""
        import copy
        import json
        overhead = 32
        if binding:
            from app.infra.model_gateway import capabilities_for, strict_json_schema, JSON_TEXT_INSTRUCTION
            caps = capabilities_for(ModelBinding.model_validate(binding))
            if not caps.json_schema:
                schema = strict_json_schema(resolve_output_schema(spec.output_schema))
                overhead += estimate_tokens(JSON_TEXT_INSTRUCTION + json.dumps(schema, ensure_ascii=False))
        values = copy.deepcopy(values)
        from app.conversation.evidence import is_case_overview
        groups = answer_fact_groups(values.get("fact_cards", []), overview=is_case_overview(values.get("question", "")), question=values.get("question", ""))
        if "fact_cards" in values:
            values["fact_cards"] = [fact for group in groups for fact in group]
        removable_records = unasked_record_keys(values.get("question", ""))
        while True:
            try:
                request = spec.render(**values)
                if system.estimated_input_tokens + request.estimated_input_tokens + overhead <= self.input_budget(binding):
                    return request
            except PromptBudgetExceeded:
                pass
            if values.get("context", {}).get("turns"):
                from types import SimpleNamespace
                from app.conversation.context import expression_constraints
                removed = values["context"]["turns"].pop(0)
                notes = expression_constraints([SimpleNamespace(**m) for m in removed])
                if notes:
                    previous = values["context"].get("summary") or ""
                    values["context"]["summary"] = previous + "；本会话因预算压缩的低信任表达约束：" + "；".join(notes)
            elif any(key in values.get("case_records", {}) for key in removable_records):
                for key in removable_records:
                    values["case_records"].pop(key, None)
            elif len(groups) > 1:
                # 冲突各方与货物整行都不拆开；完整低优先级证据最后取舍。
                drop = next((i for i in range(len(groups) - 1, -1, -1)
                    if all(fact.get("status") != "conflict" for fact in groups[i])), None)
                if drop is None:
                    raise ServiceUnavailable("冲突证据超过对话预算，请缩小关联范围")
                groups.pop(drop)
                values["fact_cards"] = [fact for group in groups for fact in group]
            elif values.get("context", {}).get("summary"):
                raise ServiceUnavailable("本会话约束与本轮要求超过预算，请缩小问题范围或重新确认要求")
            else:
                raise ServiceUnavailable("当前问题、偏好或任务要求超过对话预算，请缩小问题范围")

    async def prepare_summary(self, state):
        """冻结有界同会话旧轮后提交；摘要失败不终止正常问答，未知受理不重投。"""
        import json
        from dataclasses import replace
        async with self.summary_prepare_scope(state) as (db, conversation, turn):
            binding = turn.prompt_versions.get("model_binding")
            if not binding or self.settings.model_adapter != AdapterKind.OPENAI_COMPATIBLE:
                return
            context = await assemble_context(db, conversation, self.settings, before_turn_no=turn.turn_no,
                input_budget=self.input_budget(binding))
            cutoff = context["direct_from_turn_no"]
            prior = (await db.execute(select(ConversationSummary).where(
                ConversationSummary.tenant_id == conversation.tenant_id, ConversationSummary.conversation_id == conversation.id,
                ConversationSummary.prompt_version == "chat.session_summary@1", ConversationSummary.covers_through_turn_no < turn.turn_no)
                .order_by(ConversationSummary.covers_through_turn_no.desc(), ConversationSummary.created_at.desc(), ConversationSummary.id.desc()).limit(1))).scalar_one_or_none()
            through = prior.covers_through_turn_no if prior else 0
            archive = list((await db.execute(select(ConversationTurn).where(
                ConversationTurn.tenant_id == conversation.tenant_id, ConversationTurn.conversation_id == conversation.id,
                ConversationTurn.status == "completed", ConversationTurn.turn_no > through, ConversationTurn.turn_no < cutoff,
                ~ConversationTurn.id.in_(select(ConversationWorkItem.turn_id).where(ConversationWorkItem.conversation_id == conversation.id,
                    ConversationWorkItem.kind == "domain_rejected")))
                .order_by(ConversationTurn.turn_no).limit(20))).scalars())
            if not archive:
                return
            # 同一批次身份独立于下一轮的新绑定。未知结果保留受理真值，不用新配置重投。
            archive_ids = [str(item.id) for item in archive]
            existing = (await db.execute(select(ConversationWorkItem.id).where(
                ConversationWorkItem.tenant_id == conversation.tenant_id, ConversationWorkItem.conversation_id == conversation.id,
                ConversationWorkItem.kind.in_(["summary_pending", "summary_finished"]),
                or_(*(ConversationWorkItem.payload["source_turn_ids"].contains([item]) for item in archive_ids))).limit(1))).scalar_one_or_none()
            if existing is not None:
                return
            messages = list((await db.execute(select(Message).where(Message.tenant_id == conversation.tenant_id,
                Message.conversation_id == conversation.id, Message.turn_id.in_([item.id for item in archive]),
                Message.status == "completed").order_by(Message.seq))).scalars())
            snapshots = list((await db.execute(select(ReferenceSnapshot).where(ReferenceSnapshot.tenant_id == conversation.tenant_id,
                ReferenceSnapshot.conversation_id == conversation.id, ReferenceSnapshot.id.in_([item.reference_snapshot_id for item in archive])))).scalars())
            frozen_navigation = {item.id: _summary_navigation(item.frozen,
                {"case_id": str(item.case_id)} if item.case_id else {}) for item in snapshots}
            registry = get_prompt_registry()
            try:
                system = registry.get_frozen("chat.system", **turn.prompt_versions["chat.system"]).render(tool_catalog=[])
                spec = registry.get_frozen("chat.session_summary", **turn.prompt_versions["chat.session_summary"])
                schema = resolve_output_schema(spec.output_schema)
                if schema is not SessionSummary:
                    return
            except (KeyError, PromptRenderError, PromptUnavailable):
                return
            request = None
            while archive:
                allowed_turns = {item.id for item in archive}
                selected = [item for item in messages if item.turn_id in allowed_turns]
                material = [_session_summary_material(item) for item in selected]
                previous_navigation = []
                if prior:
                    try: previous_navigation = _summary_navigation(json.loads(prior.summary_text).get("source_reference_versions", []))
                    except (ValueError, TypeError, AttributeError): pass
                navigation = previous_navigation + [entry for item in archive for entry in frozen_navigation.get(item.reference_snapshot_id, [])]
                navigation = list({json.dumps(entry, sort_keys=True): entry for entry in navigation}.values())
                navigation_tokens = estimate_tokens(json.dumps(navigation, ensure_ascii=False))
                if navigation_tokens + 128 >= self.settings.session_summary_token_limit:
                    archive.pop()
                    continue
                try:
                    candidate = spec.render(previous_summary=prior.summary_text if prior else "", turns=material,
                        max_chars=min(4000, max(1, self.settings.session_summary_token_limit)))
                except PromptBudgetExceeded:
                    archive.pop()
                    continue
                except PromptRenderError:
                    return
                if system.estimated_input_tokens + candidate.estimated_input_tokens + 24 > self.input_budget(binding):
                    archive.pop()
                    continue
                request = replace(candidate, max_output_tokens=min(candidate.max_output_tokens,
                    ModelBinding.model_validate(binding).max_output_tokens, self.settings.session_summary_token_limit - navigation_tokens - 128))
                break
            if request is None or not archive:
                return
            source_identity = {"conversation_id": str(conversation.id), "previous_summary_id": str(prior.id) if prior else None,
                "source_turn_ids": [str(item.id) for item in archive], "source_message_ids": [str(item.id) for item in selected],
                "source_reference_versions": navigation}
            digest = request_digest({**source_identity, "messages": material})
            # 兼容先前版本的pending：其来源消息相同即使hash含旧配置也不能再次受理。
            existing = (await db.execute(select(ConversationWorkItem.id).where(
                ConversationWorkItem.tenant_id == conversation.tenant_id, ConversationWorkItem.conversation_id == conversation.id,
                ConversationWorkItem.kind.in_(["summary_pending", "summary_finished"]),
                or_(*(ConversationWorkItem.payload["source_message_ids"].contains([item]) for item in source_identity["source_message_ids"]))).limit(1))).scalar_one_or_none()
            if existing is not None:
                return
            frozen_prompt_versions = {key: turn.prompt_versions[key] for key in ("chat.system", "chat.session_summary")}
            pending_id = await store_work(db, turn, "summary_pending", {**source_identity, "source_hash": digest,
                "covers_through": archive[-1].turn_no, "request_key": "summary:" + digest,
                "binding": binding, "prompt_versions": frozen_prompt_versions, "output_schema": request.output_schema,
                "input_fingerprints": [system.input_fingerprint, request.input_fingerprint],
                "status": "pending", "max_output_tokens": request.max_output_tokens})
            pending = await db.get(ConversationWorkItem, UUID(pending_id))
            pending.payload = {**pending.payload, "source_reference_versions": navigation}
            pending.expires_at = WORK_RETENTION_UNTIL
            pending.payload = {**pending.payload, "status": "queued", "material": material,
                "previous_summary": prior.summary_text if prior else "", "max_chars": min(4000, max(1, self.settings.session_summary_token_limit)),
                "knowledge_safe": (not prior or json.loads(prior.summary_text).get("summary_scope") == "knowledge_unverified")
                    and not conversation.case_id and not navigation and all(not snap.frozen for snap in snapshots)
                    and all(not asks_case_specific(item["text"]) and not is_instruction_attack(item["text"])
                        and not re.search(r"发票号|客户|地址|密钥|token|\d{5,}", item["text"], re.I) for item in material if item["role"] == "user"),
                "owner_user_id": str(self.principal.user_id), "chat_max_model_cost_cny": turn.prompt_versions.get("chat_max_model_cost_cny"),
                "summary_token_limit": self.settings.session_summary_token_limit}
            from app.workflows.models import QueueOutbox
            db.add(QueueOutbox(tenant_id=turn.tenant_id, topic="summary", dedupe_key="summary:" + digest,
                payload={"job_id": pending_id, "turn_id": str(turn.id)}))
            return pending_id

    @asynccontextmanager
    async def summary_prepare_scope(self, state):
        async with self.factory() as db, db.begin():
            await set_tenant_context(db, self.principal)
            conversation = await load_conversation(db, self.principal, self.settings, UUID(state["conversation_id"]), write=True)
            turn = await db.get(ConversationTurn, UUID(state["turn_id"]))
            if conversation.status != "active" or turn is None or turn.conversation_id != conversation.id or turn.status != "completed":
                raise ApiError("摘要来源回合尚未完成", code="SUMMARY_NOT_READY", status_code=409)
            yield db, conversation, turn

    async def candidate(self, state, action, workflow=None, language=None, clarification=None, workflow_version=None):
        async with self.scope(state) as (db, _, turn, _, _):
            payload = IntentCandidateV2(action=action, workflow_type=workflow,
                target_language=language, referenced_labels=[], clarification_question=clarification, rationale="受控路由").model_dump(mode="json")
            if workflow_version is not None:
                payload["registered_workflow_version"] = workflow_version
            item_id = await store_work(db, turn, "intent", payload)
            return {"intent_candidate_id": item_id, "decision": "clarify" if action in {"clarify", "out_of_domain"} else "tools"}

    async def fast_path(self, state):
        async with self.scope(state) as (db, conversation, turn, snapshot, question):
            capabilities = snapshot.capabilities
            q = question.strip()
            has_sources = bool(source_versions(snapshot.frozen))
            needs_constraint_confirmation = requests_prior_constraints(q)
            draft_language_reply = False
            if conversation.active_task_draft_id and language_reply(q) is not None:
                from app.workflows.models import TaskDraft
                draft = await db.get(TaskDraft, conversation.active_task_draft_id)
                draft_language_reply = bool(draft and draft.workflow_type == "translate_review" and not draft.started_run_id and not draft.discarded_at)
        if is_instruction_attack(q):
            return await self.candidate(state, "out_of_domain")
        if not capabilities and is_greeting(q):
            return await self.candidate(state, "clarify", clarification="你好！可以直接问我物流、贸易和单证知识，也可以上传单据做识别、翻译与校验。")
        if needs_constraint_confirmation and (asks_case_specific(q) or any(word in q for word in ("翻译", "导出", "任务", "修改字段", "放行"))):
            return await self.candidate(state, "clarify", clarification="请确认这次要沿用的要求、格式或术语。历史摘要只用于理解讨论，不能替代本轮任务要求；确认后再处理，已冻结任务不会因此改变。")
        if has_sources and (any(x in q for x in ("为什么红", "为何红", "红色", "为什么阻断", "风险原因", "疑点原因", "哪些风险"))
                or re.search(r"(?:为什么|为何|原因).{0,8}(?:标红|红框|阻断)|(?:标红|红框|阻断).{0,8}(?:为什么|为何|原因)", q)):
            return await self.candidate(state, "list_issues")
        if draft_language_reply:
            return await self.candidate(state, "prepare_task", workflow="translate_review", language=language_reply(q), workflow_version=draft.workflow_version)
        if calculation_requested(q) and not capabilities and not comparison_task_request(q):
            return await self.candidate(state, "query_case_facts")
        if capabilities:
            selected = next((c for c in capabilities if c.get("workflow_type") or c.get("allowed_actions") or c["kind"] == "mcp_tool"), None)
            if selected is None:
                return await self.candidate(state, "query_case_facts" if has_sources else "general_knowledge")
            if selected["kind"] == "mcp_tool":
                return await self.candidate(state, "query_case_facts")
            if len(set(selected.get("allowed_actions", []))) > 1 and not set(selected["allowed_actions"]).issubset(READ_ONLY_SKILL_ACTIONS):
                return await self.candidate(state, "clarify", clarification="此Skill声明了多个业务步骤，当前执行器还不支持这组步骤顺序。请改选单个动作或分别发起任务；本轮未执行这些步骤。")
            conflict = capability_conflict(q, selected)
            if conflict:
                return await self.candidate(state, "clarify", clarification=conflict)
            action = "prepare_task" if selected.get("workflow_type") else next((a for a in selected.get("allowed_actions", []) if a in ACTION_CATALOG), "clarify")
            if selected.get("workflow_type") not in {None, "read_index", "translate_review", "consignment_check"}:
                return await self.candidate(state, "clarify", clarification="该工作流尚未发布对应对话意图与专属任务界面，请从已发布入口发起。")
            language = (selected.get("user_parameters") or {}).get("target_language")
            if selected.get("workflow_type") == "translate_review" and not language:
                language = requested_language(q)
            return await self.candidate(state, action, workflow=selected.get("workflow_type"),
                language=language, workflow_version=selected.get("workflow_version"))
        if "记住" in q:
            if re.search(r"(?:不要|不用|无需|不必|别).{0,4}记住", q):
                return await self.candidate(state, "clarify", clarification="本次不会提出长期偏好建议，请说明当前要处理的单证问题。")
            return await self.candidate(state, "propose_user_memory")
        if has_sources and comparison_task_request(q):
            if translation_task_request(q):
                return await self.candidate(state, "prepare_task", workflow="translate_review", language=requested_language(q), workflow_version=2)
            return await self.candidate(state, "prepare_task", workflow="consignment_check", workflow_version=1)
        if not translation_task_request(q) and not asks_case_specific(q) and (general_explanation(q) or general_knowledge_request(q)
                or ("翻译" in q and any(word in q for word in ("注意", "哪些", "流程", "如何", "怎么")))):
            return await self.candidate(state, "general_knowledge")
        if any(x in q for x in ("结束服务", "结束会话")):
            return await self.candidate(state, "clarify", clarification="请使用顶栏结束服务按钮，确认后本会话转为只读。")
        if any(x in q for x in ("修改", "放行", "导出", "裁定", "确认无误", "打开复核", "查看校验")):
            return await self.candidate(state, "open_review")
        if run_progress_request(q):
            return await self.candidate(state, "get_run_timeline")
        if has_sources and any(x in q for x in ("译文", "翻译结果", "怎么译")):
            return await self.candidate(state, "query_case_facts")
        if any(x in q for x in ("不要翻译", "不用翻译", "无需翻译", "不需要翻译")):
            if "识别" in q:
                return await self.candidate(state, "prepare_task", workflow="read_index")
            return await self.candidate(state, "clarify", clarification="已理解本次不做翻译，请说明要识别原件、查看字段还是询问物流知识。")
        if translation_task_request(q):
            language = requested_language(q)
            return await self.candidate(state, "prepare_task", workflow="translate_review", language=language)
        if re.search(r"^(?:请|帮我)?(?:识别|解析|提取)(?:一下|这|文件|单据|附件)", q):
            return await self.candidate(state, "prepare_task", workflow="read_index")
        if has_sources and (field_key(q) or any(x in q for x in ("内容", "写了", "这份", "这张", "这票", "来源"))):
            return await self.candidate(state, "query_case_facts")
        if has_sources:
            if re.search(r"^(?:帮我|请)?(?:处理(?:一下)?|看看|看一下)(?:文件|单据|附件)?[。！\s]*$", q):
                return await self.candidate(state, "clarify", clarification="你希望查看文件内容、仅识别单据，还是翻译并校验？翻译任务还需要指定目标语言。")
            return await self.candidate(state, "query_case_facts")
        return await self.candidate(state, "general_knowledge")

    async def intent_and_plan(self, state):
        if state.get("plan_rounds", 0) >= self.settings.chat_max_plan_rounds:
            return await self.candidate(state, "clarify", clarification="请说明要查看的文件、字段或任务。")
        async with self.scope(state) as (db, conversation, turn, snapshot, question):
            previous = list((await db.execute(select(ConversationWorkItem).where(ConversationWorkItem.turn_id == turn.id,
                ConversationWorkItem.kind == "intent").order_by(ConversationWorkItem.created_at))).scalars())
            if previous:
                previous_candidate = IntentCandidateV2.model_validate({key: value for key, value in previous[-1].payload.items() if key != "registered_workflow_version"})
                return {"intent_candidate_id": str(previous[-1].id),
                    "decision": "clarify" if previous_candidate.action in {"clarify", "out_of_domain"} else "tools", "plan_rounds": 1}
            inflight = list((await db.execute(select(ConversationWorkItem).where(ConversationWorkItem.turn_id == turn.id,
                ConversationWorkItem.kind.in_(["model_pending", "model_finished"])))).scalars())
            if inflight:
                raise ApiError("外部意图请求受理状态不明，本轮不能重复提交。", code="MODEL_ACCEPTANCE_UNKNOWN", status_code=409)
            frozen_binding = turn.prompt_versions.get("model_binding")
            labels = [s.get("label") or s["id"] for s in snapshot.selections]
            context = await assemble_context(db, conversation, self.settings, before_turn_no=turn.turn_no, input_budget=self.input_budget(turn.prompt_versions.get("model_binding")))
            registry = get_prompt_registry()
            intent_spec = registry.get_frozen("chat.intent", **turn.prompt_versions["chat.intent"])
            catalog = ACTION_CATALOG if intent_spec.version >= 3 else [a for a in ACTION_CATALOG if a != "general_knowledge"]
            intent_schema = resolve_output_schema(intent_spec.output_schema)
            system = registry.get_frozen("chat.system", **turn.prompt_versions["chat.system"]).render(tool_catalog=catalog)
            intent = render_material(intent_spec, skill_material(snapshot.capabilities), action_catalog=catalog,
                authorized_labels=labels, question=question + "\n本会话语境（不能作为单证事实）：" + str({"summary": context.get("summary"), "messages": context["messages"]}))
        if system.estimated_input_tokens + intent.estimated_input_tokens > self.input_budget(frozen_binding):
            return await self.candidate(state, "clarify", clarification="本轮上下文超过模型输入预算，请缩短问题或明确本次要求。")
        if not frozen_binding or self.settings.model_adapter != AdapterKind.OPENAI_COMPATIBLE:
            return await self.candidate(state, "clarify", clarification="请明确要查看哪个字段、查询进度，或发起翻译任务。")
        async with self.scope(state) as (db, _, turn, _, _):
            pending_id = await store_work(db, turn, "model_pending", {"request_key": "turn:" + state["turn_id"] + ":intent"})
            budget = await self.model_budget(db, turn, frozen_binding)
            await append_event(db, turn, "tool_started", {"name": "intent_and_plan", "message": "正在理解问题并选择处理方式"})
        ledger = self.model_ledger(state, frozen_binding, budget)
        gateway = build_model_gateway(self.settings, ledger=ledger)
        try:
            result = await gateway.structured_generate(ModelBinding.model_validate(frozen_binding), [system, intent], intent_schema,
                "turn:" + state["turn_id"] + ":intent:" + str(state.get("plan_rounds", 0)), budget=budget)
        finally:
            await gateway.aclose()
        async with self.scope(state) as (db, _, turn, _, _):
            pending = await db.get(ConversationWorkItem, UUID(pending_id))
            if not isinstance(result, NeedsReconcile):
                pending.kind = "model_finished"
            await self.store_call_records(db, turn, ledger)
            await append_event(db, turn, "tool_finished", {"name": "intent_and_plan", "status": "succeeded" if isinstance(result, Succeeded) and not result.is_fake else "failed"})
        if isinstance(result, NeedsReconcile):
            raise ApiError("意图模型响应中断，请求受理待核查；请勿重复提交同一问题。", code="MODEL_ACCEPTANCE_UNKNOWN", status_code=409)
        if not isinstance(result, Succeeded) or result.is_fake:
            code = "MODEL_AUTH_FAILED" if getattr(result, "kind", None) == "auth" else "MODEL_INTENT_FAILED"
            raise ServiceUnavailable("意图模型未返回有效结果，原问题已保存。", code=code)
        candidate = result.value
        if candidate.action not in ACTION_CATALOG or any(label not in labels for label in candidate.referenced_labels):
            return await self.candidate(state, "clarify", clarification="请重新选择有权访问的文件或任务。")
        async with self.scope(state) as (db, _, turn, _, _):
            item = await store_work(db, turn, "intent", candidate.model_dump(mode="json"))
        return {"intent_candidate_id": item, "plan_rounds": state.get("plan_rounds", 0) + 1,
            "decision": "clarify" if candidate.action in {"clarify", "out_of_domain"} else "tools"}

    async def execute_tools(self, state):
        async with self.scope(state) as (_, _, _, snapshot, _):
            selected = next((c for c in snapshot.capabilities if c["kind"] == "user_skill" and c.get("allowed_actions")), None)
            actions = list(dict.fromkeys(selected["allowed_actions"])) if selected else []
        if len(actions) <= 1:
            return await self.execute_tool(state)
        if not set(actions).issubset(READ_ONLY_SKILL_ACTIONS) or len(actions) > min(SKILL_ACTION_POLICY["max_readonly_steps"], self.settings.chat_max_tool_calls):
            return await self.candidate(state, "clarify", clarification="所选Skill步骤包含多个启动动作、混合写入或超过本轮预算；本轮未执行，请拆分为明确任务。")
        result_ids, evidence_ids = [], []
        for index, action in enumerate(actions):
            try:
                result = await self.execute_tool({**state, "tool_calls": index}, action_override=action)
            except ApiError as exc:
                async with self.scope(state) as (db, _, turn, snapshot, _):
                    await self.skill_step_result(db, turn, selected, action, "failed", error_class=exc.code)
                    result_ids.append(await store_work(db, turn, "tool_result", {"facts": [], "action": action, "status": "failed"}))
                    await store_work(db, turn, "safe_notice", {"text": f"Skill第{index + 1}步未完成：{exc.message}。后续步骤未执行。", "cards": []})
                break
            result_ids.extend(result["tool_result_ids"])
            evidence_ids.extend(result["evidence_ids"])
            if result.get("step_status") == "failed":
                break
        return {"tool_result_ids": list(dict.fromkeys(result_ids)), "evidence_ids": list(dict.fromkeys(evidence_ids)),
            "tool_calls": min(len(actions), index + 1), "decision": "answer"}

    async def skill_step_result(self, db, turn, selected, action, status, *, error_class=None):
        from app.extensions.models import ExtensionCall
        from app.extensions.audit import audit_context
        prior = (await db.execute(select(ExtensionCall).where(ExtensionCall.parent_turn_id == turn.id,
            ExtensionCall.extension_id == UUID(selected["extension_id"]), ExtensionCall.action == action))).scalar_one_or_none()
        if prior is not None:
            return
        db.add(ExtensionCall(tenant_id=turn.tenant_id, extension_id=UUID(selected["extension_id"]), owner_user_id=self.principal.user_id,
            extension_version=selected["extension_version"], conversation_id=turn.conversation_id, parent_turn_id=turn.id,
            name=selected.get("name", "本人 Skill"), action=action, status=status, error_class=error_class, **await audit_context(db, turn.id, selected),
            result_redacted="受控只读步骤已完成" if status == "succeeded" else "步骤未完成，后续步骤停止"))
        await store_work(db, turn, "safe_notice", {"text": "", "cards": [{"type": "tool_call", "name": selected.get("name", "本人 Skill"),
            "version": selected["extension_version"], "purpose": action, "status": status,
            "params_redacted": "固定引用与声明参数", "external_source": "本人受控Skill步骤"}]})

    async def execute_tool(self, state, *, action_override=None):
        if state.get("tool_calls", 0) >= self.settings.chat_max_tool_calls:
            return await self.candidate(state, "clarify", clarification="本轮查询达到上限，请缩小范围后继续。")
        async with self.scope(state) as (db, conversation, turn, snapshot, question):
            previous = list((await db.execute(select(ConversationWorkItem).where(ConversationWorkItem.turn_id == turn.id,
                ConversationWorkItem.kind == "tool_result",
                *([ConversationWorkItem.payload["action"].astext == action_override] if action_override else [])).order_by(ConversationWorkItem.created_at))).scalars())
            if previous:
                return {"tool_result_ids": [str(previous[-1].id)], "evidence_ids": [f["evidence_id"] for f in previous[-1].payload.get("facts", [])],
                    "decision": "answer", "tool_calls": state.get("tool_calls", 0) + 1, "step_status": previous[-1].payload.get("status", "succeeded")}
            intent = dict(await load_work(db, turn, state["intent_candidate_id"], "intent"))
            workflow_version = intent.pop("registered_workflow_version", 2 if intent.get("workflow_type") == "translate_review" else 1)
            candidate = IntentCandidateV2.model_validate(intent)
            selected = next((c for c in snapshot.capabilities if c["kind"] == "user_skill" and c.get("allowed_actions")), None)
            if action_override:
                from app.conversation.contracts import CapabilitySelection
                from app.extensions.service import validate_selections
                await validate_selections(db, self.principal, self.settings, [CapabilitySelection.model_validate(
                    {key: selected[key] for key in CapabilitySelection.model_fields if key in selected})], write=True)
                candidate = candidate.model_copy(update={"action": action_override})
            await append_event(db, turn, "tool_started", {"name": candidate.action})
            facts = []
            cards = []
            step_status = "succeeded"
            mcp = next((c for c in snapshot.capabilities if c["kind"] == "mcp_tool"), None)
            if mcp:
                from app.extensions.service import call_selected_mcp
                result, error, call = await call_selected_mcp(db, self.principal, self.settings, mcp,
                    conversation_id=conversation.id, parent_turn_id=turn.id)
                text = "外部只读工具调用未完成，请检查连接与授权。" if error else "已读取维护者发布范围的公开资料。以下为外部参考资料，不能作为本票字段或规则结论的证据。"
                if result:
                    import json
                    text += "\n" + json.dumps(result.get("content", []), ensure_ascii=False)[:6000]
                await store_work(db, turn, "safe_notice", {"text": text, "cards": [{"type": "tool_call", "name": mcp["tool_id"],
                    "version": mcp["extension_version"], "purpose": "读取已发布公开资料", "status": "failed" if error else "succeeded", "params_redacted": "固定公开参数允许值", "external_source": "MCP外部参考"}]})
            elif candidate.action == "get_run_timeline":
                from app.workflows.commands import load_run
                from app.workflows.models import RunEvent
                run_id = next((f.get("run_id") for f in snapshot.frozen if f.get("run_id")), None) or conversation.active_run_id
                if not run_id:
                    text = "本会话尚未启动任务。"
                else:
                    run, _ = await load_run(db, self.principal, self.settings, UUID(str(run_id)))
                    event = (await db.execute(select(RunEvent).where(RunEvent.run_id == run.id).order_by(RunEvent.seq.desc()).limit(1))).scalar_one_or_none()
                    text = f"任务状态：{run.status.value}；当前阶段：{run.current_stage}。" + (f" {event.message}" if event else "")
                    cards = [{"type": "run_link", "run_id": str(run.id)}]
                await store_work(db, turn, "safe_notice", {"text": text, "cards": cards})
            elif candidate.action == "propose_user_memory":
                import re
                from app.preferences.service import MemoryBody, preview_memory, propose_memory
                content = question.strip()
                category = ("target_language" if re.search(r"目标语言|译本|默认.{0,4}翻译|以后.{0,4}翻译|常用.{0,4}翻译", question) else
                    "answer_language" if requested_language(question) else
                    "terminology" if re.search(r"术语|称呼|译作|译为|terminology", question, re.I) else
                    "presentation" if re.search(r"风险|清单|列表|表格|先.{0,6}再|简明|详细|DOCX|PDF|呈现|格式", question, re.I) else None)
                if not category or len(content) > 120:
                    notice = {"text": "请用120字以内说明要记住的回答语言、常用目标语言、呈现方式或术语偏好；确认后才生效。", "cards": []}
                else:
                    body = MemoryBody(category=category, content=content)
                    rejected = preview_memory(body)["rejected_reason"]
                    if rejected:
                        notice = {"text": rejected, "cards": []}
                    else:
                        suggestion = await propose_memory(db, self.principal, body, conversation_id=conversation.id, parent_turn_id=turn.id)
                        notice = {"text": "已提出长期偏好建议，请确认后生效。", "cards": [{"type": "memory_suggestion",
                            "suggestion_id": str(suggestion.id), "category": category, "content": content}]}
                await store_work(db, turn, "safe_notice", notice)
            elif candidate.action == "open_review":
                cards = await frozen_review_cards(db, snapshot)
                await store_work(db, turn, "safe_notice", {"text": "点击来源卡打开本轮关联版本，核对原文、字段及疑点；修订或导出仍需页面权限与版本检查。" if cards else "请先上传或用@关联这票资料，再打开对应复核页面。", "cards": cards})
            elif candidate.action in {"list_issues", "list_comparison_differences"}:
                from app.conversation.evidence import frozen_issue_rows
                rows = await frozen_issue_rows(db, snapshot, comparison_only=candidate.action == "list_comparison_differences")
                more_issues = len(rows) > 30
                for issue, view in rows[:30]:
                    if view["a"]:
                        cards.append({"type": "issue", "issue_id": view["issue_id"], "severity": view["severity"], "category": view["category"],
                            "rule_name": view["rule_name"], "a": view["a"], "b": view["b"], "pending_action": view["status"]})
                    label = "当前阻断项" if view["blocking_delivery"] else "已处置项" if view["status"] not in {"open", "unresolved"} else "提示项"
                    reason = label + "：" + issue.title + "；已记录原因：" + issue.description
                    facts.append({"evidence_id": "issue:" + str(issue.id), "claim_kind": "issue_reason", "canonical_text": reason,
                        "value": reason + "；处理状态：" + view["status"] + "；推荐处理：" + view["guidance"]["recommendation"] + "；可能影响：" + view["guidance"]["impact"],
                        "target": {"case_id": str(snapshot.case_id), "issue_id": str(issue.id), "run_id": str(issue.run_id),
                            "business_revision_id": str(issue.business_revision_id)}, "source_targets": issue.sources, "card": None})
                if more_issues:
                    await store_work(db, turn, "safe_notice", {"text": "本次仅展示关联范围内前30项疑点，更多项目请进入复核页面。", "cards": []})
                await store_work(db, turn, "safe_notice", {"text": "" if facts else "当前冻结范围没有已记录疑点；这不代表所有规则已评估或已放行。", "cards": cards})
            elif candidate.action == "general_knowledge":
                filenames = [s.get("label", "") for s in snapshot.selections]
                if asks_case_specific(question) or any(name and name in question for name in filenames):
                    await store_work(db, turn, "safe_notice", {"text": "这个问题指向关联单据，请明确要核对的字段或来源；一般知识不能替代本票核验。", "cards": []})
                else:
                    explanation = general_explanation(question)
                    import re
                    if (any(item["category"] != "target_language" for item in turn.prompt_versions.get("confirmed_preferences", []))
                            or turn.prompt_versions.get("interface_language", "zh-CN") != "zh-CN" or re.search(r"(?:用|使用).{0,4}(?:英文|英语|中文|汉语)|(?:in|用) English", question, re.I)):
                        explanation = None
                    if turn.prompt_versions.get("chat.general", {}).get("version", 1) >= 4 or snapshot.capabilities:
                        explanation = None
                    await store_work(db, turn, "safe_notice" if explanation else "general_request",
                        {"text": explanation, "cards": []} if explanation else {"question": question})
            elif candidate.action in {"query_case_facts", "query_provenance", "get_consignment_overview", "get_comparison_evidence"}:
                explanation = general_explanation(question)
                if turn.prompt_versions.get("chat.general", {}).get("version", 1) >= 4:
                    explanation = None
                versions = source_versions(snapshot.frozen)
                has_parse = any(f.get("parse_artifact_id") or any(v.get("parse_artifact_id") for v in f.get("source_versions", [])) for f in snapshot.frozen)
                pure_skill = bool(snapshot.capabilities) and all(c["kind"] == "user_skill" and not c.get("allowed_actions") for c in snapshot.capabilities)
                translation_notice = next((f["notice"] for f in snapshot.frozen if f["kind"] == "translation_notice"), None)
                if calculation_requested(question) and not versions:
                    try:
                        computed = calculate(question, [], snapshot.case_id)
                        facts = [computed] if computed else []
                    except EvidenceClarification as exc:
                        await store_work(db, turn, "safe_notice", {"text": str(exc), "cards": []})
                elif translation_notice:
                    await store_work(db, turn, "safe_notice", {"text": translation_notice, "cards": []})
                elif pure_skill and not asks_case_specific(question) and not field_key(question):
                    await store_work(db, turn, "general_request", {"question": question})
                elif explanation and not action_override:
                    await store_work(db, turn, "safe_notice", {"text": explanation, "cards": []})
                elif not versions:
                    if action_override or asks_case_specific(question) or field_key(question):
                        await store_work(db, turn, "safe_notice", {"text": "尚无可核验的关联原件，请先上传或用 @ 选择单据后核对该字段。", "cards": []})
                        if action_override:
                            step_status = "failed"
                    else:
                        await store_work(db, turn, "general_request", {"question": question})
                elif not has_parse and versions:
                    if action_override:
                        await store_work(db, turn, "safe_notice", {"text": "该只读步骤需要先完成原件识别。本次Skill组合不自动启动识别，未产生OCR请求。", "cards": []})
                        step_status = "failed"
                    else:
                        from app.conversation.deferred import register_read_continuation
                        await register_read_continuation(db, conversation, turn, snapshot, question, self.principal, self.settings)
                        return {"decision": "done", "tool_calls": state.get("tool_calls", 0) + 1}
                else:
                    from app.conversation.evidence import EvidenceClarification
                    try:
                        facts = await query_evidence(db, snapshot, question, self.settings)
                        computed = calculate(question, facts, snapshot.case_id)
                        if computed:
                            facts.append(computed)
                    except EvidenceClarification as exc:
                        await store_work(db, turn, "safe_notice", {"text": str(exc), "cards": []})
            elif candidate.action == "prepare_task":
                from app.workflows.commands import prepare_draft, start_run
                workflow_capability = next((item for item in snapshot.capabilities if item.get("workflow_type")), None)
                parameters = dict(workflow_capability.get("user_parameters") or {}) if workflow_capability else {}
                if workflow_capability and workflow_capability["workflow_type"] != candidate.workflow_type:
                    from app.conversation.tools import stale
                    raise stale()
                if workflow_capability and workflow_capability["kind"] == "builtin_skill":
                    from app.conversation.contracts import CapabilitySelection
                    from app.conversation.tools import validate_capabilities
                    await validate_capabilities(db, self.principal, self.settings, [CapabilitySelection.model_validate(
                        {key: workflow_capability[key] for key in CapabilitySelection.model_fields if key in workflow_capability})])
                    workflow_version = workflow_capability["workflow_version"]
                if candidate.target_language:
                    parameters["target_language"] = candidate.target_language
                if not conversation.case_id:
                    await store_work(db, turn, "safe_notice", {"text": "请先上传并验收本票文件。", "cards": []})
                elif candidate.workflow_type == "read_index":
                    from app.conversation.deferred import register_read_continuation
                    await register_read_continuation(db, conversation, turn, snapshot, question, self.principal, self.settings,
                        workflow_version=workflow_version, index_scope=parameters.get("index_scope", "selected_documents"))
                    return {"decision": "done", "tool_calls": state.get("tool_calls", 0) + 1}
                else:
                    request_item = (await db.execute(select(ConversationWorkItem).where(ConversationWorkItem.turn_id == turn.id,
                        ConversationWorkItem.kind == "request"))).scalar_one()
                    draft_id = request_item.payload.get("task_draft_id") or conversation.active_task_draft_id
                    if draft_id and not request_item.payload.get("task_draft_id"):
                        from app.workflows.models import TaskDraft
                        prior = await db.get(TaskDraft, UUID(str(draft_id)))
                        if prior is None or prior.started_run_id or prior.discarded_at or (prior.workflow_type, prior.workflow_version) != (candidate.workflow_type, workflow_version):
                            draft_id = None
                    if draft_id:
                        from app.workflows.commands import load_draft
                        draft, _ = await load_draft(db, self.principal, self.settings, UUID(str(draft_id)), write=True)
                        if draft.workflow_type != candidate.workflow_type or draft.workflow_version != workflow_version:
                            from app.conversation.tools import stale
                            raise stale()
                        if parameters:
                            from app.workflows.commands import patch_draft
                            draft = await patch_draft(db, self.principal, self.settings, draft_id=draft.id,
                                expected_draft_version=draft.draft_version, draft_parameters=parameters)
                    else:
                        draft = await prepare_draft(db, self.principal, self.settings, case_id=conversation.case_id,
                            workflow_type=candidate.workflow_type, workflow_version=workflow_version,
                            source_versions=[{"document_id": v["document_id"], "document_version": v["document_version"]} for v in source_versions(snapshot.frozen)],
                            draft_parameters=parameters or ({"target_language": candidate.target_language, "translation_scope": "all"} if candidate.workflow_type == "translate_review" else {}), conversation_id=conversation.id)
                    conversation.active_task_draft_id = draft.id
                    if draft.missing_fields:
                        text = "请补充目标语言或任务参数：" + "；".join(draft.questions or draft.missing_fields)
                        cards = [{"type": "read_prompt", "text": text}]
                    else:
                        run = await start_run(db, self.principal, self.settings, draft_task_id=draft.id,
                            expected_draft_version=draft.draft_version, idempotency_key="turn:" + str(turn.id))
                        conversation.active_run_id = run.id
                        conversation.active_task_draft_id = None
                        if run.workflow_type == "consignment_check":
                            from app.conversation.deferred import register_check_continuation
                            await register_check_continuation(db, conversation, turn, run, question)
                        text = f"已启动{'同票核对' if run.workflow_type == 'consignment_check' else '翻译复核'}任务，当前阶段：{run.current_stage}。"
                        cards = [{"type": "run_link", "run_id": str(run.id)}]
                    await store_work(db, turn, "safe_notice", {"text": text, "cards": cards})
            else:
                await store_work(db, turn, "safe_notice", {"text": "请说明要查看的文件、字段或任务。", "cards": []})
            from app.conversation.evidence import is_case_overview, case_records
            records = await case_records(db, snapshot) if facts and any(f["target"].get("source_segment_id") for f in facts) else {}
            result_id = await store_work(db, turn, "tool_result", {"facts": facts, "action": candidate.action, "status": step_status, "case_records": records})
            if action_override:
                await self.skill_step_result(db, turn, selected, action_override, step_status)
            await append_event(db, turn, "tool_finished", {"name": candidate.action, "status": step_status})
            return {"tool_result_ids": [result_id], "evidence_ids": [f["evidence_id"] for f in facts],
                "tool_calls": state.get("tool_calls", 0) + 1, "decision": "answer", "step_status": step_status}

    async def clarify(self, state):
        async with self.scope(state) as (db, conversation, turn, _, _):
            intent = dict(await load_work(db, turn, state["intent_candidate_id"], "intent")) if state.get("intent_candidate_id") else None
            if intent:
                intent.pop("registered_workflow_version", None)
            candidate = IntentCandidateV2.model_validate(intent) if intent else None
            text = candidate.clarification_question if candidate else None
            if candidate and candidate.action == "out_of_domain":
                await store_work(db, turn, "domain_rejected", {})
                text = "我可以协助物流单证内容、来源、翻译复核与任务进度。请提供本票文件或具体单证问题。"
            await finish_answer(db, conversation, turn, [text or "请说明要查看的文件、字段或任务。"])
        return {}

    async def generate_answer(self, state, binding, prompts, schema, key, budget, *, sources=(), facts=()):
        from app.conversation.preview import AnswerPreview
        async with self.scope(state) as (_, _, turn, _, _):
            frozen = dict(turn.prompt_versions)
        ledger = self.model_ledger(state, binding, budget)
        gateway = build_model_gateway(self.settings, ledger=ledger)
        try:
            if frozen.get("stream_enabled") and prompts[-1].output_schema in {"GeneralKnowledgeAnswer@3", "NaturalAnswerDraft@2"}:
                settings = self.settings.model_copy(update={name: frozen.get(name.removeprefix("chat_"), getattr(self.settings, name))
                    for name in ("chat_stream_max_chars", "chat_stream_event_chars", "chat_stream_pending_chars")})
                async def emit(kind, payload):
                    async with self.scope(state) as (db, _, turn, _, _):
                        await append_event(db, turn, kind, {"preview_id":str(turn.id), **payload})
                preview = AnswerPreview(emit, settings, sources=sources, facts=facts)
                async with preview:
                    result = await gateway.structured_stream(ModelBinding.model_validate(binding), prompts, schema, key,
                        preview.feed, budget=budget, max_bytes=frozen.get("stream_max_bytes", self.settings.chat_stream_max_bytes),
                        event_max_bytes=frozen.get("stream_event_max_bytes", self.settings.chat_stream_event_max_bytes), on_retry=preview.reset)
                    if isinstance(result, Succeeded):
                        await preview.flush()
                    elif preview.started:
                        # 已经知道响应无效时清预览；断流保留明确未核验的中断草稿。
                        if not isinstance(result, NeedsReconcile):
                            await emit("answer_preview_reset", {"revision":1,"reason":"本次输出未完成核验"})
                return result, ledger
            return await gateway.structured_generate(ModelBinding.model_validate(binding), prompts, schema, key, budget=budget), ledger
        finally:
            await gateway.aclose()

    async def compose_answer(self, state):
        async with self.scope(state) as (db, conversation, turn, snapshot, question):
            general = (await db.execute(select(ConversationWorkItem).where(ConversationWorkItem.turn_id == turn.id,
                ConversationWorkItem.kind == "general_request"))).scalar_one_or_none()
            if general:
                prior = (await db.execute(select(ConversationWorkItem).where(ConversationWorkItem.turn_id == turn.id,
                    ConversationWorkItem.kind == "general_answer"))).scalar_one_or_none()
                if prior:
                    prompts = turn.prompt_versions
                    spec = get_prompt_registry().get_frozen("chat.general", **prompts["chat.general"])
                    value = resolve_output_schema(spec.output_schema).model_validate(prior.payload)
                    frozen_sources = (await db.execute(select(ConversationWorkItem).where(ConversationWorkItem.turn_id == turn.id,
                        ConversationWorkItem.kind == "general_sources"))).scalar_one_or_none()
                    text, cards = self.general_output(value, spec, frozen_sources.payload["sources"] if frozen_sources else knowledge_sources(question))
                    if value.domain == "out_of_domain":
                        await store_work(db, turn, "domain_rejected", {})
                    exists = (await db.execute(select(ConversationWorkItem.id).where(ConversationWorkItem.turn_id == turn.id,
                        ConversationWorkItem.kind == "safe_notice", ConversationWorkItem.payload["text"].astext == text))).scalar_one_or_none()
                    if exists is None:
                        await store_work(db, turn, "safe_notice", {"text": text, "cards": cards})
                    return {"draft_id": str(prior.id)}
                pending = (await db.execute(select(ConversationWorkItem.id).where(ConversationWorkItem.turn_id == turn.id,
                    ConversationWorkItem.kind == "general_pending"))).scalar_one_or_none()
                if pending:
                    raise ApiError("通用知识请求受理状态不明，本轮不能重复提交。", code="MODEL_ACCEPTANCE_UNKNOWN", status_code=409)
                registry = get_prompt_registry()
                prompts = turn.prompt_versions
                binding = prompts.get("model_binding")
                context, task_spec, memory = await self.answer_context(db, conversation, turn, knowledge_only=True)
                system = registry.get_frozen("chat.system", **prompts["chat.system"]).render(tool_catalog=[])
                general_spec = registry.get_frozen("chat.general", **prompts["chat.general"])
                sources = knowledge_sources(question)
                await store_work(db, turn, "general_sources", {"sources": sources})
                if general_spec.version >= 4:
                    request = self.fit_answer_request(general_spec, system, binding, {"question": question, "task_spec": task_spec,
                        "context": {"turns": context["turns"], "summary": context["summary"]}, "preferences": memory,
                        "knowledge_sources": sources, "skill_material": skill_material(snapshot.capabilities)})
                else:
                    request = render_material(general_spec, skill_material(snapshot.capabilities), question=question,
                        context=context["messages"], preferences=memory)
                if system.estimated_input_tokens + request.estimated_input_tokens > self.input_budget(binding):
                    raise ServiceUnavailable("当前问题与偏好超过对话预算，请缩短问题")
                pending_id = await store_work(db, turn, "general_pending", {"request_key": f"turn:{turn.id}:general"})
                budget = await self.model_budget(db, turn, binding) if binding else None
            else:
                binding = None
        if general:
            if not binding or self.settings.model_adapter != AdapterKind.OPENAI_COMPATIBLE:
                raise ServiceUnavailable("通用物流知识模型尚不可用")
            result, ledger = await self.generate_answer(state, binding, [system, request],
                resolve_output_schema(general_spec.output_schema), f"turn:{state['turn_id']}:general", budget, sources=sources)
            async with self.scope(state) as (db, _, turn, _, _):
                await self.store_call_records(db, turn, ledger)
                if isinstance(result, NeedsReconcile):
                    raise ApiError("模型服务响应中断，请求受理待核查；请勿重复提交同一问题。", code="MODEL_ACCEPTANCE_UNKNOWN", status_code=409)
                if not isinstance(result, Succeeded) or result.is_fake:
                    if isinstance(result, Failed) and result.kind == "provider_protocol_error":
                        raise ApiError("模型服务返回的回复流格式异常，本轮没有生成可用正文。", code="MODEL_STREAM_PROTOCOL_FAILED", status_code=503)
                    raise ServiceUnavailable("通用知识生成未返回可信结果")
                value = result.value
                item = await store_work(db, turn, "general_answer", value.model_dump(mode="json"))
                await db.delete(await db.get(ConversationWorkItem, UUID(pending_id)))
            # 已知成功候选先提交，再校验发布；复验不重新收费。
            async with self.scope(state) as (db, _, turn, _, _):
                text, cards = self.general_output(value, general_spec, sources)
                if value.domain == "out_of_domain":
                    await store_work(db, turn, "domain_rejected", {})
                await store_work(db, turn, "safe_notice", {"text": text, "cards": cards})
                return {"draft_id": item}
        async with self.scope(state) as (db, conversation, turn, snapshot, question):
            facts, records = [], {}
            for item in state.get("tool_result_ids", []):
                payload = await load_work(db, turn, item, "tool_result")
                facts.extend(payload["facts"])
                records.update(payload.get("case_records", {}))
            previous = (await db.execute(select(ConversationWorkItem).where(ConversationWorkItem.turn_id == turn.id,
                ConversationWorkItem.kind.in_(["answer", "natural_answer"])).order_by(ConversationWorkItem.created_at.desc()).limit(1))).scalar_one_or_none()
            if previous:
                return {"draft_id": str(previous.id)}
            spec = get_prompt_registry().get_frozen("chat.answer", **turn.prompt_versions["chat.answer"])
            binding = turn.prompt_versions.get("model_binding")
            if not facts or spec.version < 2 or not binding or self.settings.model_adapter != AdapterKind.OPENAI_COMPATIBLE:
                if any(f["claim_kind"] == "issue_reason" for f in facts):
                    raise ServiceUnavailable("风险原因的自然解释需要已配置的对话模型，请在复核页查看记录")
                if facts and (skill_material(snapshot.capabilities) or turn.prompt_versions.get("confirmed_preferences")) and spec.version >= 2:
                    raise ServiceUnavailable("表达偏好或说明Skill需要已配置的对话模型，请重试")
                item = await store_work(db, turn, "answer", deterministic_answer(facts).model_dump(mode="json"))
                return {"draft_id": item}
            pending = (await db.execute(select(ConversationWorkItem.id).where(ConversationWorkItem.turn_id == turn.id,
                ConversationWorkItem.kind == "answer_pending"))).scalar_one_or_none()
            if pending:
                raise ApiError("回答请求受理状态不明，本轮不能重复提交。", code="MODEL_ACCEPTANCE_UNKNOWN", status_code=409)
            context, task_spec, memory = await self.answer_context(db, conversation, turn)
            system = get_prompt_registry().get_frozen("chat.system", **turn.prompt_versions["chat.system"]).render(tool_catalog=[])
            material = [{"evidence_id": f["evidence_id"], "claim_kind": f["claim_kind"], "text": f["canonical_text"],
                "status": f.get("card", {}).get("status") if f.get("card") else "computed",
                "field_key": f.get("field_key"), "role": f.get("role"), "value": f.get("value"),
                **({"source_roles": f["source_roles"]} if f.get("source_roles") else {}),
                "filename": (f.get("card") or {}).get("filename"), "document_id": f["target"].get("document_id"),
                "paired_source_id": f["target"].get("source_segment_id"),
                **({"source_label": f["source_label"]} if f.get("source_label") else {}),
                **({"line_item_id": f.get("source_line_id") or f["line_item_id"], "unit": f.get("unit")}
                    if f.get("source_line_id") or f.get("line_item_id") else {})} for f in facts]
            values = {"question": question, "task_spec": task_spec,
                "preferences": memory, "context": {"turns": context["turns"], "summary": context["summary"]},
                "fact_cards": material, "skill_material": skill_material(snapshot.capabilities)}
            if spec.version >= 5:
                values["case_records"] = records
            request = self.fit_answer_request(spec, system, binding, values)
            await store_work(db, turn, "answer_pending", {"request_key": f"turn:{turn.id}:answer"})
            budget = await self.model_budget(db, turn, binding)
        # 来源卡必须先完成版本鉴权再出正文；最终仍逐段重核。
        async with self.scope(state) as (db, _, turn, snapshot, _):
            from app.prompts.schemas import NaturalAnswerParagraph
            cards = []
            for offset in range(0, len(facts), 12):
                batch = facts[offset:offset + 12]
                checked, batch_cards = await verify_natural_answer(db, snapshot, NaturalAnswerDraft(paragraphs=[
                    NaturalAnswerParagraph(text="[[" + f["evidence_id"] + "]]", claim_kind=f["claim_kind"], evidence_ids=[f["evidence_id"]], status="asserted") for f in batch]), batch)
                if any("无法确认" in text or "没有足够证据" in text for text in checked):
                    raise ServiceUnavailable("当前来源核验未通过，请重新关联有效版本")
                cards.extend(batch_cards)
            cards = list({card["card_id"]: card for card in cards}.values())
            if cards:
                await append_event(db, turn, "evidence_card", {"cards": cards})
        result, ledger = await self.generate_answer(state, binding, [system, request],
            resolve_output_schema(spec.output_schema), f"turn:{state['turn_id']}:answer", budget, facts=facts)
        async with self.scope(state) as (db, _, turn, _, _):
            await self.store_call_records(db, turn, ledger)
            if isinstance(result, NeedsReconcile):
                raise ApiError("模型服务响应中断，请求受理待核查；请勿重复提交同一问题。", code="MODEL_ACCEPTANCE_UNKNOWN", status_code=409)
            if not isinstance(result, Succeeded) or result.is_fake:
                if isinstance(result, Failed) and result.kind == "provider_protocol_error":
                    raise ApiError("模型服务返回的回复流格式异常，本轮没有生成可用正文。", code="MODEL_STREAM_PROTOCOL_FAILED", status_code=503)
                raise ServiceUnavailable("本票解释未返回可信结果，请核对来源或重试")
            item = await store_work(db, turn, "natural_answer", result.value.model_dump(mode="json"))
            pending = (await db.execute(select(ConversationWorkItem).where(ConversationWorkItem.turn_id == turn.id,
                ConversationWorkItem.kind == "answer_pending"))).scalar_one_or_none()
            if pending:
                await db.delete(pending)
            return {"draft_id": item}

    def general_output(self, value, spec, sources):
        cards = []
        if spec.version >= 4 and value.domain in {"logistics", "related_material"}:
            try:
                allowed = {source["source_id"] for source in sources}
                cards = knowledge_cards(list(dict.fromkeys(key for key in value.source_ids if key in allowed)), sources)
                paragraphs = verify_knowledge_paragraphs(value.paragraphs, value.source_ids, sources, require_source=spec.version >= 5)
            except ValueError as exc:
                raise ServiceUnavailable("知识答复缺少有效依据，请核对来源；本轮已知结果不重复调用模型") from exc
        else:
            paragraphs = value.paragraphs
        text = "我可以协助物流运输与单证知识，请提供相关问题。" if value.domain not in {"logistics", "related_material"} else "\n\n".join(paragraphs) + "\n\n_一般知识参考；未对具体单票作核验。_"
        return text, cards

    async def verify_and_emit(self, state):
        async with self.scope(state) as (db, conversation, turn, snapshot, _):
            notices = list((await db.execute(select(ConversationWorkItem).where(ConversationWorkItem.turn_id == turn.id,
                ConversationWorkItem.kind == "safe_notice").order_by(ConversationWorkItem.created_at))).scalars())
            texts, cards = [n.payload["text"] for n in notices if n.payload["text"]], [c for n in notices for c in n.payload["cards"]]
            facts = []
            tool_results = []
            for item in state.get("tool_result_ids", []):
                result = await load_work(db, turn, item, "tool_result")
                tool_results.append(result)
                facts.extend(result["facts"])
            output_valid = True
            if facts or not notices:
                draft_item = await db.get(ConversationWorkItem, UUID(state["draft_id"]))
                if draft_item is None or draft_item.turn_id != turn.id:
                    raise ServiceUnavailable("答复绑定已失效")
                if draft_item.kind == "natural_answer" and draft_item.payload.get("domain") == "out_of_domain":
                    verified_texts, evidence_cards = ["我可以协助物流单证及理解关联材料所需的知识，请说明相关问题。"], []
                    await store_work(db, turn, "domain_rejected", {})
                elif draft_item.kind == "natural_answer":
                    payload = {k:v for k,v in draft_item.payload.items() if k != "domain"}
                    verified_texts, evidence_cards = await verify_natural_answer(db, snapshot, NaturalAnswerDraft.model_validate(payload), facts)
                else:
                    draft = AnswerDraft.model_validate(await load_work(db, turn, state["draft_id"], "answer"))
                    verified_texts, evidence_cards = await verify_answer(db, snapshot, draft, facts)
                texts.extend(verified_texts)
                output_valid = not any("无法确认" in text or "没有足够证据" in text for text in verified_texts)
                if evidence_cards:
                    cards.append({"type": "evidence", "cards": evidence_cards})
            from app.extensions.models import ExtensionCall
            from app.extensions.audit import audit_context
            for selected in snapshot.capabilities:
                if selected["kind"] != "user_skill":
                    continue
                if len(set(selected.get("allowed_actions", []))) > 1:
                    continue
                general_result = (await db.execute(select(ConversationWorkItem).where(ConversationWorkItem.turn_id == turn.id,
                    ConversationWorkItem.kind == "general_answer"))).scalar_one_or_none()
                actions = selected.get("allowed_actions", [])
                applied = output_valid and (any(r.get("action") in actions and r.get("status") == "succeeded" for r in tool_results) if actions else
                    (bool(general_result and general_result.payload.get("domain") == "logistics") or bool(facts and draft_item.kind == "natural_answer")))
                skill_status = "succeeded" if applied else "failed"
                db.add(ExtensionCall(tenant_id=turn.tenant_id, extension_id=UUID(selected["extension_id"]), owner_user_id=self.principal.user_id,
                    extension_version=selected["extension_version"], conversation_id=conversation.id, parent_turn_id=turn.id,
                    name=selected.get("name", "本人 Skill"), action="general_explanation" if not selected.get("allowed_actions") else selected["allowed_actions"][0],
                    status=skill_status, **await audit_context(db, turn.id, selected),
                    result_redacted="冻结版本已按受控动作或低信任解释说明应用" if applied else "说明未应用或输出未通过证据校验"))
                cards.append({"type": "tool_call", "name": selected.get("name", "本人 Skill"), "version": selected["extension_version"],
                    "purpose": "应用本人 Skill", "status": skill_status, "params_redacted": "已校验声明参数", "external_source": "本人低信任说明"})
            if any("无法确认" in item or "没有足够证据" in item for item in texts):
                await append_event(db, turn, "answer_preview_reset", {"preview_id":str(turn.id),"revision":1,"reason":"最终来源核验未通过"})
            await finish_answer(db, conversation, turn, texts, cards)
        return {}


async def run_turn(session_factory, principal, settings, conversation_id, turn_id, *, checkpointer=None, resume=False, lease_token=None):
    nodes = ConversationNodes(session_factory, principal, settings, lease_token)
    async def heartbeat():
        while True:
            await asyncio.sleep(30)
            async with session_factory() as db, db.begin():
                await set_tenant_context(db, principal)
                turn = (await db.execute(select(ConversationTurn).where(ConversationTurn.id == turn_id).with_for_update())).scalar_one_or_none()
                if turn is None or (lease_token and turn.lease_token != lease_token) or turn.status not in {"pending", "running"}:
                    return
                turn.lease_until = datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS)
    heartbeat_task = asyncio.create_task(heartbeat())
    try:
        async with session_factory() as db, db.begin():
            await set_tenant_context(db, principal)
            conversation = await load_conversation(db, principal, settings, conversation_id)
            ref = (await db.execute(select(CheckpointRef).where(CheckpointRef.conversation_id == conversation.id))).scalar_one()
            thread_id = ref.thread_id
        config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 15}
        if checkpointer is not None:
            await build_conversation_graph(nodes).compile(checkpointer=checkpointer).ainvoke(None if resume else fresh_state(conversation_id, turn_id), config, durability="sync")
        else:
            url = settings.checkpoint_database_url
            if not url:
                raise ServiceUnavailable("对话检查点数据库尚未配置")
            url = url.replace("postgresql+psycopg://", "postgresql://").replace("postgresql+asyncpg://", "postgresql://")
            async with AsyncPostgresSaver.from_conn_string(url) as saver:
                await build_conversation_graph(nodes).compile(checkpointer=saver).ainvoke(None if resume else fresh_state(conversation_id, turn_id), config, durability="sync")
    except Exception as exc:
        async with session_factory() as db, db.begin():
            await set_tenant_context(db, principal)
            conversation = (await db.execute(select(Conversation).where(Conversation.id == conversation_id).with_for_update())).scalar_one_or_none()
            turn = (await db.execute(select(ConversationTurn).where(ConversationTurn.id == turn_id).with_for_update())).scalar_one_or_none()
            if conversation and turn and (not lease_token or turn.lease_token == lease_token) and turn.status in {"pending", "running"}:
                from app.conversation.service import fail_answer
                await fail_answer(db, conversation, turn, exc.code if isinstance(exc, ApiError) else type(exc).__name__)

    finally:
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task

        # 回复已定稿后才排摘要；冻结故障不撤销已保存的回复。
        try:
            await nodes.prepare_summary(fresh_state(conversation_id, turn_id))
        except ApiError as exc:
            if exc.code != "SUMMARY_NOT_READY":
                import logging
                logging.getLogger(__name__).warning("摘要排队未完成 error_class=%s", type(exc).__name__)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("摘要排队未完成 error_class=%s", type(exc).__name__)
