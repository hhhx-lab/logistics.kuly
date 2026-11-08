"""持久回合消费者：消息受理与派发通过同一 Outbox 事务关联。"""
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4
import asyncio

from sqlalchemy import select, text

from app.config import get_settings
from app.conversation.models import Conversation, ConversationTurn
from app.conversation.graph import run_turn
from app.identity.models import User
from app.identity.service import Principal
from app.infra.database import create_database_engine, create_session_factory, create_sync_engine, create_sync_session_factory, sync_tenant_transaction
from app.workflows.worker import queue


@queue.task(name="document_agent.turn")
def execute_turn(turn_id: str):
    settings = get_settings()
    engine = create_sync_engine(settings)
    token = uuid4()
    try:
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE logistics_worker"))
            tenant = connection.execute(text("SELECT worker_tenant_of_turn(:id)"), {"id": UUID(turn_id)}).scalar_one_or_none()
        if tenant is None:
            return
        with create_sync_session_factory(engine)() as db, sync_tenant_transaction(db, tenant):
            probe = db.get(ConversationTurn, UUID(turn_id))
            if probe is None:
                return
            conversation = db.get(Conversation, probe.conversation_id)
            if conversation.case_id:
                from app.documents.models import Case
                db.execute(select(Case).where(Case.id == conversation.case_id).with_for_update())
            conversation = db.scalar(select(Conversation).where(Conversation.id == probe.conversation_id).with_for_update())
            turn = db.scalar(select(ConversationTurn).where(ConversationTurn.id == UUID(turn_id)).with_for_update())
            if turn is None or turn.status not in {"pending", "running"}:
                return
            now = datetime.now(UTC)
            if turn.status == "running" and turn.lease_until and turn.lease_until > now:
                return
            user = db.get(User, conversation.owner_user_id)
            if user is None or not user.is_active or conversation.status != "active" or conversation.archived_at or conversation.deleted_at:
                turn.status, turn.error_class = "interrupted", "AUTHORIZATION_CHANGED"
                turn.lease_token, turn.lease_until = None, None
                return
            turn.status = "running"
            turn.lease_token, turn.lease_until = token, now + timedelta(seconds=120)
            principal = Principal(user.id, tenant, user.display_name, tuple(user.permissions), uuid4(), "")
            conversation_id = conversation.id
        async def execute():
            async_engine = create_database_engine(settings)
            try:
                await run_turn(create_session_factory(async_engine), principal, settings, conversation_id, UUID(turn_id), lease_token=token)
            finally:
                await async_engine.dispose()
        with asyncio.Runner() as runner:
            runner.run(execute())
    finally:
        engine.dispose()


@queue.task(name="document_agent.summary")
def execute_summary(job_id: str, turn_id: str):
    settings = get_settings()
    engine = create_sync_engine(settings)
    try:
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE logistics_worker"))
            tenant = connection.execute(text("SELECT worker_tenant_of_turn(:id)"), {"id": UUID(turn_id)}).scalar_one_or_none()
        if tenant is None:
            return
        with create_sync_session_factory(engine)() as db, sync_tenant_transaction(db, tenant):
            from app.conversation.models import ConversationWorkItem
            job = db.get(ConversationWorkItem, UUID(job_id))
            turn = db.get(ConversationTurn, UUID(turn_id))
            if job is None or turn is None or job.turn_id != turn.id or job.conversation_id != turn.conversation_id:
                return
            conversation = db.get(Conversation, turn.conversation_id)
            user = db.get(User, conversation.owner_user_id)
            if user is None:
                return
            principal = Principal(user.id, tenant, user.display_name, tuple(user.permissions), uuid4(), "")
        async def execute():
            from app.conversation.summary import SummaryJob
            async_engine = create_database_engine(settings)
            try:
                await SummaryJob(create_session_factory(async_engine), principal, settings, UUID(job_id)).execute()
            finally:
                await async_engine.dispose()
        with asyncio.Runner() as runner:
            runner.run(execute())
    finally:
        engine.dispose()
