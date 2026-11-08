"""独立持久摘要作业；前台回合与图检查点不等待模型摘要。"""
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select

from app.api.dependencies import set_tenant_context
from app.api.errors import ApiError
from app.conversation.costs import ConversationCostBudget
from app.conversation.graph import ConversationCallLedger, _session_summary_material
from app.conversation.models import ConversationSummary, ConversationTurn, ConversationWorkItem, Message
from app.conversation.service import WORK_RETENTION_UNTIL, load_conversation
from app.identity.models import User
from app.identity.service import Principal
from app.infra.model_gateway import NeedsReconcile, Succeeded, build_model_gateway, model_call_timeout
from app.prompts.registry import estimate_tokens, get_prompt_registry
from app.prompts.schemas import SessionSummary
from app.workflows.contracts import ModelBinding
from app.workflows.costs import PricedLedger
from app.api.idempotency import request_digest


class SummaryJob:
    def __init__(self, factory, principal, settings, job_id):
        self.factory, self.principal, self.settings, self.id = factory, principal, settings, job_id
        self.token = str(uuid4())

    def model_ledger(self, turn_id, binding, budget):
        base = ConversationCallLedger(self.before_send, self.principal.tenant_id, settings=self.settings,
            turn_id=turn_id, user_id=self.principal.user_id, summary_job_id=self.id)
        return PricedLedger(base, binding, budget)

    @asynccontextmanager
    async def scope(self):
        async with self.factory() as db, db.begin():
            await set_tenant_context(db, self.principal)
            job = (await db.execute(select(ConversationWorkItem).where(ConversationWorkItem.id == self.id).with_for_update())).scalar_one_or_none()
            if job is None or job.tenant_id != self.principal.tenant_id or job.payload.get("owner_user_id") != str(self.principal.user_id):
                raise ApiError("摘要作业不可用", code="SUMMARY_STALE", status_code=409)
            yield db, job

    async def validate(self, db, job):
        user = await db.get(User, self.principal.user_id)
        if user is None or not user.is_active or user.tenant_id != job.tenant_id:
            raise ApiError("摘要授权已失效", code="SUMMARY_STALE", status_code=403)
        conversation = await load_conversation(db, self.principal, self.settings, job.conversation_id)
        if conversation.status != "active" or conversation.archived_at:
            raise ApiError("摘要会话已关闭", code="SUMMARY_STALE", status_code=409)
        payload = job.payload
        source_ids = [UUID(value) for value in payload["source_turn_ids"]]
        turns = list((await db.execute(select(ConversationTurn).where(ConversationTurn.conversation_id == job.conversation_id,
            ConversationTurn.id.in_(source_ids), ConversationTurn.status == "completed",
            ~ConversationTurn.id.in_(select(ConversationWorkItem.turn_id).where(ConversationWorkItem.conversation_id == job.conversation_id,
                ConversationWorkItem.kind == "domain_rejected"))))).scalars())
        if {turn.id for turn in turns} != set(source_ids):
            raise ApiError("摘要来源回合已改变", code="SUMMARY_STALE", status_code=409)
        if payload["previous_summary_id"]:
            prior = await db.get(ConversationSummary, UUID(payload["previous_summary_id"]))
            if prior is None or prior.conversation_id != job.conversation_id or prior.summary_text != payload["previous_summary"]:
                raise ApiError("前摘要已改变", code="SUMMARY_STALE", status_code=409)
        from app.documents.retention import load_case_for_read
        for case_id in {item["case_id"] for item in payload["source_reference_versions"] if item.get("case_id")}:
            await load_case_for_read(db, UUID(case_id), user_id=self.principal.user_id, settings=self.settings)
        rows = list((await db.execute(select(Message).where(Message.conversation_id == job.conversation_id,
            Message.id.in_([UUID(value) for value in payload["source_message_ids"]]), Message.status == "completed").order_by(Message.seq))).scalars())
        material = [_session_summary_material(row) for row in rows]
        identity = {key: payload[key] for key in ("conversation_id", "previous_summary_id", "source_turn_ids", "source_message_ids", "source_reference_versions")}
        if request_digest({**identity, "messages": material}) != payload["source_hash"]:
            raise ApiError("摘要来源已改变", code="SUMMARY_STALE", status_code=409)
        return conversation

    async def before_send(self):
        async with self.scope() as (db, job):
            if job.payload.get("lease_token") != self.token or job.payload.get("status") != "running":
                raise ApiError("摘要租约已失效", code="SUMMARY_STALE", status_code=409)
            if datetime.fromisoformat(job.payload["lease_until"]) <= datetime.now(UTC):
                raise ApiError("摘要租约已过期", code="SUMMARY_STALE", status_code=409)
            await self.validate(db, job)
            job.payload = {**job.payload, "submission_started": True}

    async def execute(self):
        async with self.scope() as (db, job):
            payload = dict(job.payload)
            if job.kind != "summary_pending" or payload.get("status") not in {"queued", "running"}:
                return
            if payload.get("status") == "running":
                if datetime.fromisoformat(payload["lease_until"]) > datetime.now(UTC): return
                if payload.get("submission_started"):
                    job.payload = {**payload, "status": "acceptance_unknown"}; return
            active = (await db.execute(select(ConversationTurn.id).where(ConversationTurn.conversation_id == job.conversation_id,
                ConversationTurn.status.in_(["pending", "running"])).limit(1))).scalar_one_or_none()
            if active is not None:
                from app.workflows.models import QueueOutbox
                outbox = (await db.execute(select(QueueOutbox).where(QueueOutbox.dedupe_key == "summary:" + payload["source_hash"]).with_for_update())).scalar_one_or_none()
                if outbox:
                    outbox.status = "pending"
                    outbox.next_attempt_at = datetime.now(UTC) + timedelta(seconds=2)
                return
            try: await self.validate(db, job)
            except ApiError:
                job.kind = "summary_finished"; job.payload = {**payload, "status": "restricted"}; return
            job.payload = payload = {**payload, "status": "running", "lease_token": self.token,
                "lease_until": (datetime.now(UTC) + timedelta(seconds=model_call_timeout(
                    self.settings, ModelBinding.model_validate(payload["binding"])) + 30)).isoformat()}
            turn = await db.get(ConversationTurn, job.turn_id)
            binding = ModelBinding.model_validate(payload["binding"])
            registry = get_prompt_registry()
            system = registry.get_frozen("chat.system", **payload["prompt_versions"]["chat.system"]).render(tool_catalog=[])
            spec = registry.get_frozen("chat.session_summary", **payload["prompt_versions"]["chat.session_summary"])
            request = replace(spec.render(previous_summary=payload["previous_summary"], turns=payload["material"], max_chars=payload["max_chars"]), max_output_tokens=payload["max_output_tokens"])
            if [system.input_fingerprint, request.input_fingerprint] != payload["input_fingerprints"]:
                job.kind = "summary_finished"; job.payload = {**payload, "status": "input_changed"}; return
            records = list((await db.execute(select(ConversationWorkItem.payload).where(ConversationWorkItem.turn_id == turn.id,
                ConversationWorkItem.kind == "model_call", ConversationWorkItem.payload["summary_job_id"].astext == str(job.id)))).scalars())
            budget = ConversationCostBudget(settings=self.settings, binding=binding, records=records, frozen_limit=payload["chat_max_model_cost_cny"])
        ledger = self.model_ledger(turn.id, binding, budget)
        gateway = build_model_gateway(self.settings, ledger=ledger)
        try:
            result = await gateway.structured_generate(binding, [system, request], SessionSummary, payload["request_key"], budget=budget)
        except Exception:
            async with self.scope() as (_, job):
                if job.payload.get("lease_token") == self.token:
                    job.payload = {**job.payload, "status": "acceptance_unknown" if job.payload.get("submission_started") else "queued", "lease_until": datetime.now(UTC).isoformat()}
            return
        finally: await gateway.aclose()
        async with self.scope() as (db, job):
            if job.payload.get("lease_token") != self.token: return
            for record in ledger.ledger.records:
                exists = (await db.execute(select(ConversationWorkItem.id).where(ConversationWorkItem.kind == "model_call",
                    ConversationWorkItem.payload["call_id"].astext == record.call_id))).scalar_one_or_none()
                if exists is None:
                    db.add(ConversationWorkItem(tenant_id=job.tenant_id, conversation_id=job.conversation_id, turn_id=job.turn_id,
                        kind="model_call", payload={**record.model_dump(mode="json"), "summary_job_id":str(job.id)}, expires_at=WORK_RETENTION_UNTIL))
            if job.payload.get("status") != "running" or datetime.fromisoformat(job.payload["lease_until"]) <= datetime.now(UTC):
                job.payload = {**job.payload, "status": "acceptance_unknown"}
                return
            if isinstance(result, NeedsReconcile):
                job.payload = {**job.payload, "status": "acceptance_unknown"}; return
            job.kind = "summary_finished"
            if not isinstance(result, Succeeded) or result.is_fake:
                job.payload = {**job.payload, "status": "failed"}; return
            try: conversation = await self.validate(db, job)
            except ApiError:
                job.payload = {**job.payload, "status": "restricted"}; return
            value = result.value.model_dump(mode="json")
            allowed = {ref for item in payload["material"] for ref in item["references"]}
            if payload["previous_summary"]:
                try: allowed.update(json.loads(payload["previous_summary"]).get("referenced_ids", []))
                except (ValueError, TypeError): pass
            value["summary"] = "历史讨论与待核事项（均未核实，不作为本次任务或单据事实）：" + value["summary"]
            value["open_constraints"] = ["用户曾要求（本轮仍须确认）：" + item for item in value["open_constraints"]]
            value["referenced_ids"] = [item for item in value["referenced_ids"] if item in allowed]
            value["source_reference_versions"] = payload["source_reference_versions"]
            value["summary_scope"] = "knowledge_unverified" if payload.get("knowledge_safe") else "case_discussion_unverified"
            text = json.dumps(value, ensure_ascii=False)
            tokens = estimate_tokens(text)
            from app.conversation.safety import contains_internal_information
            if tokens > payload["summary_token_limit"] or contains_internal_information(text):
                job.payload = {**job.payload, "status": "output_rejected"}; return
            db.add(ConversationSummary(tenant_id=job.tenant_id, conversation_id=conversation.id,
                covers_through_turn_no=payload["covers_through"], source_message_hash=payload["source_hash"],
                prompt_version="chat.session_summary@1", summary_text=text, token_count=tokens))
            job.payload = {**job.payload, "status": "succeeded", "summary_estimated_tokens": tokens,
                "actual_input_tokens": sum(r.usage.prompt_tokens for r in result.records if r.usage and r.usage.prompt_tokens is not None) if any(r.usage and r.usage.prompt_tokens is not None for r in result.records) else None,
                "actual_output_tokens": sum(r.usage.completion_tokens for r in result.records if r.usage and r.usage.completion_tokens is not None) if any(r.usage and r.usage.completion_tokens is not None for r in result.records) else None}


def recover_summary_jobs(db, moment, batch_size):
    """只恢复尚未外发的作业；租约过期且外发过的作业保持未知。"""
    from app.workflows.models import QueueOutbox
    jobs = list(db.scalars(select(ConversationWorkItem).where(ConversationWorkItem.kind == "summary_pending",
        ConversationWorkItem.payload["owner_user_id"].astext.is_not(None),
        ConversationWorkItem.payload["status"].astext.in_(["queued", "running"]))
        .order_by(ConversationWorkItem.created_at, ConversationWorkItem.id).limit(batch_size).with_for_update(skip_locked=True)))
    for job in jobs:
        payload = job.payload
        if payload.get("lease_until") and datetime.fromisoformat(payload["lease_until"]) > moment: continue
        if payload.get("submission_started"):
            job.payload = {**payload, "status": "acceptance_unknown"}; continue
        key = "summary:" + payload["source_hash"]
        outbox = db.scalar(select(QueueOutbox).where(QueueOutbox.dedupe_key == key).with_for_update())
        if outbox is None:
            db.add(QueueOutbox(tenant_id=job.tenant_id, topic="summary", dedupe_key=key, payload={"job_id":str(job.id), "turn_id":str(job.turn_id)}))
        elif outbox.status == "published" and outbox.published_at and outbox.published_at < moment - timedelta(minutes=5):
            outbox.status, outbox.next_attempt_at = "pending", moment
        job.payload = {**payload, "status": "queued"}
