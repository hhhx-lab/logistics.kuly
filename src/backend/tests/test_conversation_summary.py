"""G03/G04语义摘要：真实PostgreSQL回滚夹具、HTTP MockTransport，无付费调用。"""
import json
from contextlib import asynccontextmanager
from datetime import UTC,datetime,timedelta
from decimal import Decimal
from uuid import UUID,uuid4

import httpx
import pytest
from sqlalchemy import delete,select,text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import AdapterKind,ModelPrice,ModelRole,Settings,get_settings
from app.conversation.costs import ConversationCostBudget
from app.conversation.context import assemble_context
from app.conversation.graph import ConversationCallLedger,ConversationNodes,fresh_state
from app.conversation.models import Conversation,ConversationTurn,ConversationSummary,ConversationWorkItem,Message,ReferenceSnapshot
from app.conversation.service import GRAPH_VERSION
from app.conversation.summary import SummaryJob
from app.identity.models import User
from app.identity.service import Principal
from app.infra.database import create_database_engine,create_session_factory,tenant_transaction
from app.infra.model_gateway import DefaultModelGateway,OpenAICompatibleAdapter
from app.infra.recovery import recover_expired_turn
from app.prompts.registry import get_prompt_registry,estimate_tokens
from app.prompts.schemas import SessionSummary
from app.workflows.costs import PricedLedger
from app.workflows.contracts import ModelBinding


@pytest.fixture
async def summary_db():
    settings=get_settings().model_copy(update={"model_adapter":AdapterKind.OPENAI_COMPATIBLE,"redis_url":None,
        "chat_model_base_url":"https://summary.test","chat_model_api_key":"summary-test-key",
        "chat_model_name":"grok-4.6","chat_model_protocol":"openai_chat_completions",
        "chat_model_timeout_seconds":10,"chat_model_max_output_tokens":2000,"chat_model_context_window_tokens":30000,
        "chat_model_max_concurrency":1,"chat_model_temperature":0,"chat_input_token_budget":26000,
        "response_token_reserve":2000,"retrieved_context_token_limit":1000,"session_summary_token_limit":2000})
    engine=create_database_engine(settings)
    async with engine.connect() as conn:
        outer=await conn.begin()
        db=AsyncSession(bind=conn,expire_on_commit=False,join_transaction_mode="create_savepoint")
        await db.execute(text("SET LOCAL ROLE logistics_app"))
        user=(await db.execute(select(User).where(User.account=="demo"))).scalar_one()
        await db.execute(text(f"SET LOCAL app.tenant_id = '{user.tenant_id}'"))
        principal=Principal(user.id,user.tenant_id,user.display_name,tuple(user.permissions),uuid4(),"")
        try:yield db,principal,settings
        finally:
            await db.close();await outer.rollback()
    await engine.dispose()


async def history(db,principal,settings,count=45,first_text="以后把滞箱费称为箱期费，解释时先给适用场景，再列容易混淆的词。",snapshot_frozen=None):
    conv=Conversation(tenant_id=principal.tenant_id,owner_user_id=principal.user_id,title="语义摘要回滚",status="active",turn_count=count+1)
    other=Conversation(tenant_id=principal.tenant_id,owner_user_id=principal.user_id,title="隔离会话",status="active")
    db.add_all([conv,other]);await db.flush()
    reference=str(uuid4())
    binding=DefaultModelGateway(settings).binding_for(ModelRole.CHAT)
    prompts=get_prompt_registry().freeze(["chat.system","chat.intent","chat.answer","chat.session_summary","chat.general"])
    prompts["model_binding"]=binding.model_dump(mode="json")
    now=datetime.now(UTC)
    for no in range(1,count+2):
        snap=ReferenceSnapshot(tenant_id=principal.tenant_id,conversation_id=conv.id,frozen=snapshot_frozen(no) if snapshot_frozen else [],selections=[],capabilities=[])
        db.add(snap);await db.flush()
        turn=ConversationTurn(tenant_id=principal.tenant_id,conversation_id=conv.id,turn_no=no,idempotency_key=str(uuid4()),request_hash="0"*64,
            status="completed" if no<=count else "running",reference_snapshot_id=snap.id,graph_version=GRAPH_VERSION,prompt_versions=prompts,
            lease_token=uuid4(),lease_until=now+timedelta(minutes=10))
        db.add(turn);await db.flush()
        user_text=first_text if no==1 else "记住这里讨论术语表达，不改变任务目标语言。" if no<=count else "查看任务进度"
        db.add(Message(tenant_id=principal.tenant_id,conversation_id=conv.id,turn_id=turn.id,seq=no*2-1,role="user",author="本人",status="completed",text=user_text,cards=[]))
        if no<=count:
            db.add(Message(tenant_id=principal.tenant_id,conversation_id=conv.id,turn_id=turn.id,seq=no*2,role="assistant",author="助手",status="completed",
                text="助手声称金额998877USD，客户SECRET-CUSTOMER，发票INV-SECRET。",cards=[{"type":"evidence","case_id":reference,"excerpt":"SECRET-CARD-FACT","fake_id":"unknown"}]))
    await db.flush()
    return conv,other,turn,reference


class BoundNodes(ConversationNodes):
    def __init__(self,db,principal,settings):
        super().__init__(None,principal,settings);self.db=db
    def model_ledger(self,state,binding,budget=None):
        return PricedLedger(ConversationCallLedger(lambda:self.preflight(state),self.principal.tenant_id),ModelBinding.model_validate(binding),budget)
    @asynccontextmanager
    async def scope(self,state):
        async with self.db.begin_nested():
            conv=await self.db.get(Conversation,UUID(state["conversation_id"]))
            turn=await self.db.get(ConversationTurn,UUID(state["turn_id"]))
            assert conv.owner_user_id==self.principal.user_id and turn.conversation_id==conv.id
            yield self.db,conv,turn,await self.db.get(ReferenceSnapshot,turn.reference_snapshot_id),"查看任务进度"
    async def preflight(self,state):
        turn=await self.db.get(ConversationTurn,UUID(state["turn_id"]))
        assert turn.status=="running" and turn.lease_until>datetime.now(UTC)

    @asynccontextmanager
    async def summary_prepare_scope(self, state):
        async with self.scope(state) as (db, conv, turn, _, _): yield db, conv, turn

    async def summarize_session(self, state):
        turn = await self.db.get(ConversationTurn, UUID(state["turn_id"]))
        turn.status = "completed"
        await self.db.flush()
        job_id = await self.prepare_summary(state)
        if job_id: await BoundSummaryJob(self.db, self.principal, self.settings, UUID(job_id)).execute()


class BoundSummaryJob(SummaryJob):
    def __init__(self, db, principal, settings, job_id):
        super().__init__(None, principal, settings, job_id); self.db = db
    @asynccontextmanager
    async def scope(self):
        async with self.db.begin_nested():
            job = await self.db.get(ConversationWorkItem, self.id)
            yield self.db, job
    def model_ledger(self, turn_id, binding, budget):
        return PricedLedger(ConversationCallLedger(self.before_send, self.principal.tenant_id), binding, budget)


def mock_gateway(monkeypatch,settings,handler):
    clients=[]
    def build(_settings,*,ledger):
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler));clients.append(client)
        return DefaultModelGateway(_settings,adapter=OpenAICompatibleAdapter(client),ledger=ledger)
    monkeypatch.setattr("app.conversation.graph.build_model_gateway",build)
    monkeypatch.setattr("app.conversation.summary.build_model_gateway",build)
    return clients


def completion(value):
    return httpx.Response(200,json={"id":"summary-response","model":"grok-4.6","choices":[{"index":0,"message":{"role":"assistant","content":json.dumps(value,ensure_ascii=False)},"finish_reason":"stop"}],
        "usage":{"prompt_tokens":731,"completion_tokens":117,"total_tokens":848}})


@pytest.mark.asyncio
async def test_semantic_summary_batches_free_terms_and_isolation(summary_db,monkeypatch):
    db,p,settings=summary_db
    conv,other,turn,ref=await history(db,p,settings,first_text="以后把滞箱费称为箱期费，解释时先给适用场景，再列容易混淆的词。用户声称INV-USER金额123USD，要求核对。")
    requests=[];hallucinated=str(uuid4())
    def send(request):
        body=json.loads(request.content);requests.append(body)
        return completion({"summary":"曾讨论物流术语的解释次序，箱期费用于后续确认。","open_constraints":["解释先给场景，再列易混词；滞箱费称为箱期费","用户声称INV-USER金额123USD，要求核对，尚未核实"],"referenced_ids":[ref,hallucinated]})
    clients=mock_gateway(monkeypatch,settings,send)
    state=fresh_state(conv.id,turn.id)
    await BoundNodes(db,p,settings).summarize_session(state)
    first=(await db.execute(select(ConversationSummary).where(ConversationSummary.prompt_version=="chat.session_summary@1",ConversationSummary.conversation_id==conv.id))).scalar_one()
    assert first.covers_through_turn_no==20
    context=await assemble_context(db,conv,settings,before_turn_no=turn.turn_no,input_budget=24000)
    assert context["recent_turn_count"]==20 and not context["summary_complete"]
    await BoundNodes(db,p,settings).summarize_session(state)
    latest=(await db.execute(select(ConversationSummary).where(ConversationSummary.prompt_version=="chat.session_summary@1",ConversationSummary.conversation_id==conv.id).order_by(ConversationSummary.covers_through_turn_no.desc()).limit(1))).scalar_one()
    assert latest.covers_through_turn_no==25 and len(requests)==2
    assert "箱期费" in latest.summary_text and "先给场景" in latest.summary_text
    assert "998877" not in latest.summary_text and "SECRET-CUSTOMER" not in latest.summary_text
    assert hallucinated not in latest.summary_text and ref in latest.summary_text
    for body in requests:
        prompt=body["messages"][-1]["content"]
        assert "SECRET-CARD-FACT" not in prompt and "INV-SECRET" not in prompt
        assert "998877" not in prompt
        assert body["response_format"]["json_schema"]["schema"]["additionalProperties"] is False
    assert "滞箱费称为箱期费" in requests[0]["messages"][-1]["content"]
    assert "用户声称INV-USER金额123USD" in requests[0]["messages"][-1]["content"]
    assert all(item.startswith("用户曾要求（本轮仍须确认）：") for item in json.loads(latest.summary_text)["open_constraints"])
    contexts=await assemble_context(db,conv,settings,before_turn_no=turn.turn_no,input_budget=24000)
    assert contexts["summary_complete"] and contexts["summary_requires_current_confirmation"]
    isolated=await assemble_context(db,other,settings)
    assert isolated["summary"] is None and isolated["messages"]==[]
    await BoundNodes(db,p,settings).summarize_session(state)
    assert len(requests)==2 and all(client.is_closed for client in clients)
    work=list((await db.execute(select(ConversationWorkItem).where(ConversationWorkItem.conversation_id==conv.id,ConversationWorkItem.kind=="summary_finished"))).scalars())
    assert sorted(len(item.payload["source_turn_ids"]) for item in work)==[5,20]
    assert all(item.payload["output_schema"]=="SessionSummary@1" and item.payload["actual_input_tokens"]==731 for item in work)
    assert all(item.expires_at.year==9999 for item in work)
    assert all(set(item.payload["prompt_versions"])=={"chat.system","chat.session_summary"} and len(item.payload["input_fingerprints"])==2 for item in work)
    records=list((await db.execute(select(ConversationWorkItem).where(ConversationWorkItem.conversation_id==conv.id,ConversationWorkItem.kind=="model_call"))).scalars())
    for record in records:
        params=record.payload["params"]
        assert params["actual_input_tokens"]==731
        assert params["input_estimation_delta"]==731-params["estimated_input_tokens"]
        assert "summary-test-key" not in str(record.payload)
        assert params["cost_status"]=="unknown" and record.payload["cost_cny"] is None


@pytest.mark.asyncio
async def test_summary_acceptance_unknown_restart_and_new_binding_do_not_resend(summary_db,monkeypatch):
    db,p,settings=summary_db
    conv,other,turn,ref=await history(db,p,settings,count=23)
    attempts=[]
    def disconnect(request):
        attempts.append(request);raise httpx.ReadTimeout("lost summary receipt",request=request)
    clients=mock_gateway(monkeypatch,settings,disconnect)
    state=fresh_state(conv.id,turn.id)
    await BoundNodes(db,p,settings).summarize_session(state)
    pending=(await db.execute(select(ConversationWorkItem).where(ConversationWorkItem.kind=="summary_pending",ConversationWorkItem.conversation_id==conv.id))).scalar_one()
    assert pending.payload["status"]=="acceptance_unknown" and len(attempts)==1
    frozen=dict(pending.payload["binding"])
    pending_id,other_id=pending.id,other.id
    # 新节点实例只读数据库受理真值，不重用上一实例的内存状态。
    db.expire_all()
    await BoundNodes(db,p,settings).summarize_session(state)
    turn=await db.get(ConversationTurn,UUID(state["turn_id"]))
    turn.prompt_versions={**turn.prompt_versions,"model_binding":{**frozen,"model_name":"changed-model"}}
    await db.flush()
    await BoundNodes(db,p,settings).summarize_session(state)
    assert len(attempts)==1
    pending=await db.get(ConversationWorkItem,pending_id)
    assert pending.payload["binding"]==frozen
    calls=list((await db.execute(select(ConversationWorkItem).where(ConversationWorkItem.conversation_id==UUID(state["conversation_id"]),ConversationWorkItem.kind=="model_call"))).scalars())
    assert len(calls)==1 and calls[0].payload["outcome"]=="needs_reconcile"
    assert calls[0].payload["params"]["cost_status"]=="unknown"
    turn.status="running"
    turn.lease_until=datetime.now(UTC)-timedelta(seconds=1);await db.flush()
    recovered=await db.run_sync(lambda sync:recover_expired_turn(sync,turn,datetime.now(UTC)))
    assert recovered and turn.status=="pending"
    assert pending.payload["status"]=="acceptance_unknown"
    await db.flush()
    assert all(client.is_closed for client in clients)
    other=await db.get(Conversation,other_id)
    assert (await assemble_context(db,other,settings))["summary"] is None


@pytest.mark.asyncio
async def test_oversize_summary_prompt_and_gateway_exception_are_conservative(summary_db,monkeypatch):
    db,p,settings=summary_db
    conv,other,turn,ref=await history(db,p,settings,count=22,first_text="任意自由要求"*12000)
    attempts=[]
    def unexpected(request):attempts.append(request);raise RuntimeError("gateway crash")
    clients=mock_gateway(monkeypatch,settings,unexpected)
    state=fresh_state(conv.id,turn.id)
    assert await BoundNodes(db,p,settings).scope_and_freeze(state)=={"active_case_id":None,"reference_snapshot_id":str(turn.reference_snapshot_id)}
    assert attempts==[]
    assert not list((await db.execute(select(ConversationWorkItem).where(ConversationWorkItem.kind=="summary_pending",ConversationWorkItem.conversation_id==conv.id))).scalars())
    first=(await db.execute(select(Message).where(Message.conversation_id==conv.id,Message.role=="user").order_by(Message.seq).limit(1))).scalar_one()
    first.text="以后术语解释先举例再给定义";await db.flush()
    await BoundNodes(db,p,settings).summarize_session(state)
    assert len(attempts)==1 and all(client.is_closed for client in clients)
    await BoundNodes(db,p,settings).summarize_session(state)
    assert len(attempts)==1
    pending=(await db.execute(select(ConversationWorkItem).where(ConversationWorkItem.kind=="summary_pending",ConversationWorkItem.conversation_id==conv.id))).scalar_one()
    assert pending.payload["status"]=="acceptance_unknown"


@pytest.mark.asyncio
async def test_summary_budget_trims_complete_turns_and_handles_turn_number_gaps(summary_db,monkeypatch):
    db,p,settings=summary_db
    conv,other,turn,ref=await history(db,p,settings,count=22)
    second=(await db.execute(select(Message).join(ConversationTurn,ConversationTurn.id==Message.turn_id).where(
        Message.conversation_id==conv.id,ConversationTurn.turn_no==2,Message.role=="user"))).scalar_one()
    second.text="自由讨论"*12000;await db.flush()
    bodies=[]
    def send(request):
        bodies.append(json.loads(request.content));return completion({"summary":"讨论解释次序","open_constraints":["术语先解释场景"],"referenced_ids":[]})
    mock_gateway(monkeypatch,settings,send)
    state=fresh_state(conv.id,turn.id)
    await BoundNodes(db,p,settings).summarize_session(state)
    finished=(await db.execute(select(ConversationWorkItem).where(ConversationWorkItem.conversation_id==conv.id,ConversationWorkItem.kind=="summary_finished"))).scalar_one()
    assert len(finished.payload["source_turn_ids"])==1 and len(finished.payload["source_message_ids"])==2
    prompt=bodies[0]["messages"][-1]["content"]
    assert "自由讨论自由讨论" not in prompt and "曾讨论来源查看" in prompt
    # 第2轮中断后不属于已完成旧轮，覆盖无需依赖轮号连续。
    archived=await db.get(ConversationTurn,second.turn_id);archived.status="interrupted";await db.flush()
    context=await assemble_context(db,conv,settings,before_turn_no=turn.turn_no,input_budget=24000)
    assert context["summary_complete"]


@pytest.mark.asyncio
async def test_summary_invalid_output_and_output_budget_never_create_summary(summary_db,monkeypatch):
    db,p,settings=summary_db
    conv,other,turn,ref=await history(db,p,settings,count=21)
    responses=[]
    def invalid(request):
        responses.append(request);return completion({"summary":"讨论术语","open_constraints":[],"referenced_ids":[],"field_fact":"伪造字段"})
    clients=mock_gateway(monkeypatch,settings,invalid)
    state=fresh_state(conv.id,turn.id)
    await BoundNodes(db,p,settings).summarize_session(state)
    assert len(responses)==2 and all(client.is_closed for client in clients)
    assert not list((await db.execute(select(ConversationSummary).where(ConversationSummary.conversation_id==conv.id,ConversationSummary.prompt_version=="chat.session_summary@1"))).scalars())
    calls=list((await db.execute(select(ConversationWorkItem).where(ConversationWorkItem.conversation_id==conv.id,ConversationWorkItem.kind=="model_call"))).scalars())
    assert len(calls)==2 and all(item.payload["outcome"]=="invalid_output" for item in calls)
    await BoundNodes(db,p,settings).summarize_session(state)
    assert len(responses)==2
    next_conv,_,next_turn,_=await history(db,p,settings,count=21)
    mock_gateway(monkeypatch,settings,lambda request:completion({"summary":"待核讨论"*900,"open_constraints":[],"referenced_ids":[]}))
    await BoundNodes(db,p,settings).summarize_session(fresh_state(next_conv.id,next_turn.id))
    finished=(await db.execute(select(ConversationWorkItem).where(ConversationWorkItem.conversation_id==next_conv.id,ConversationWorkItem.kind=="summary_finished"))).scalar_one()
    assert finished.payload["status"]=="output_rejected"


@pytest.mark.asyncio
async def test_summary_frozen_prompt_mismatch_and_absent_usage_are_conservative(summary_db,monkeypatch):
    db,p,settings=summary_db
    conv,other,turn,ref=await history(db,p,settings,count=21)
    prompts=dict(turn.prompt_versions)
    turn.prompt_versions={**prompts,"chat.session_summary":{**prompts["chat.session_summary"],"sha256":"0"*64}}
    await db.flush()
    bodies=[]
    def no_usage(request):
        bodies.append(request)
        response=completion({"summary":"历史术语讨论","open_constraints":[],"referenced_ids":[]})
        payload=json.loads(response.content);payload.pop("usage");return httpx.Response(200,json=payload)
    mock_gateway(monkeypatch,settings,no_usage)
    state=fresh_state(conv.id,turn.id)
    assert await BoundNodes(db,p,settings).scope_and_freeze(state)
    assert bodies==[]
    turn.prompt_versions=prompts;await db.flush()
    await BoundNodes(db,p,settings).summarize_session(state)
    finished=(await db.execute(select(ConversationWorkItem).where(ConversationWorkItem.conversation_id==conv.id,ConversationWorkItem.kind=="summary_finished"))).scalar_one()
    assert finished.payload["actual_input_tokens"] is None and finished.payload["actual_output_tokens"] is None
    call=(await db.execute(select(ConversationWorkItem).where(ConversationWorkItem.conversation_id==conv.id,ConversationWorkItem.kind=="model_call"))).scalar_one()
    assert call.payload["usage"] is None and call.payload["params"]["actual_input_tokens"] is None
    assert call.payload["params"]["input_estimation_delta"] is None and call.payload["cost_cny"] is None


@pytest.mark.asyncio
async def test_chat_priced_ledger_survives_response_then_graph_crash(summary_db):
    _,p,settings=summary_db
    engine=create_database_engine(settings);factory=create_session_factory(engine)
    async with factory() as db,tenant_transaction(db,p.tenant_id):
        conv,other,turn,_=await history(db,p,settings,count=0)
        conv_id,other_id,turn_id=conv.id,other.id,turn.id
    try:
        price=ModelPrice(version="verified-test",evidence_reference="mock invoice",base_url="https://summary.test",model_name="grok-4.6",
            protocol="openai_chat_completions",verified=True,billing_basis="all_inclusive_prompt_completion_tokens",
            input_cny_per_million=Decimal(3),output_cny_per_million=Decimal(7))
        binding=DefaultModelGateway(settings).binding_for(ModelRole.CHAT).model_copy(update={"price":price})
        base=ConversationCallLedger(lambda:None,p.tenant_id,settings=settings,turn_id=turn_id,user_id=p.user_id)
        # 此测试只验证已受理后的响应事务；生产发送门禁由既有独立DB用例验证。
        base.before_send=lambda:None
        registry=get_prompt_registry()
        prompts=[registry.get("chat.system").render(tool_catalog=[]),registry.get("chat.session_summary").render(previous_summary="",turns=[],max_chars=1000)]
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request:completion({"summary":"历史讨论","open_constraints":[],"referenced_ids":[]})))
        gateway=DefaultModelGateway(settings,adapter=OpenAICompatibleAdapter(client),ledger=PricedLedger(base,binding,None))
        with pytest.raises(RuntimeError,match="after receipt"):
            await gateway.structured_generate(binding,prompts,SessionSummary,"turn:"+str(turn_id)+":summary")
            raise RuntimeError("graph crash after receipt")
        await gateway.aclose()
        async with factory() as db,tenant_transaction(db,p.tenant_id):
            records=list((await db.execute(select(ConversationWorkItem).where(ConversationWorkItem.turn_id==turn_id,ConversationWorkItem.kind=="model_call"))).scalars())
            assert len(records)==1
            assert records[0].expires_at.year==9999
            row=records[0].payload
            assert Decimal(row["cost_cny"])==Decimal("0.003012") and row["params"]["price_snapshot"]["version"]=="verified-test"
            assert row["usage"]["prompt_tokens"]==731 and row["params"]["cost_status"]=="known"
        # 重新实例化账本，供应商受理未知仍独立保存，不能编造usage或零费用。
        def unknown(request):raise httpx.ReadTimeout("lost receipt",request=request)
        client=httpx.AsyncClient(transport=httpx.MockTransport(unknown))
        gateway=DefaultModelGateway(settings,adapter=OpenAICompatibleAdapter(client),ledger=PricedLedger(base,binding,None))
        await gateway.structured_generate(binding,prompts,SessionSummary,"turn:"+str(turn_id)+":unknown")
        await gateway.aclose()
        async with factory() as db,tenant_transaction(db,p.tenant_id):
            records=list((await db.execute(select(ConversationWorkItem).where(ConversationWorkItem.turn_id==turn_id,ConversationWorkItem.kind=="model_call"))).scalars())
            assert len(records)==2
            unknown_row=next(item.payload for item in records if item.payload["outcome"]=="needs_reconcile")
            assert unknown_row["cost_cny"] is None and unknown_row["usage"] is None
    finally:
        async with factory() as db,tenant_transaction(db,p.tenant_id):
            await db.execute(delete(Conversation).where(Conversation.id.in_([conv_id,other_id])))
        await engine.dispose()


def test_chat_cost_budget_is_independent_and_unknown_cost_blocks(monkeypatch):
    from pydantic import ValidationError
    settings=get_settings().model_copy(update={"run_max_model_cost_cny":Decimal("0.00001")})
    binding=DefaultModelGateway(settings).binding_for(ModelRole.CHAT)
    monkeypatch.setenv("CHAT_MAX_MODEL_COST_CNY","")
    assert Settings(_env_file=None).chat_max_model_cost_cny is None
    with pytest.raises(ValidationError):Settings(_env_file=None,chat_max_model_cost_cny="-1")
    budget=ConversationCostBudget(settings=settings,binding=binding,records=[],frozen_limit=None)
    assert budget.can_start(100)  # Run cap不应用于对话。
    assert not ConversationCostBudget(settings=settings,binding=binding.model_copy(update={"protocol":"openai_chat_completions","price":None}),records=[],frozen_limit="1").can_start(100)
    unknown={"call_id":"unknown","outcome":"needs_reconcile","is_fake":False,"cost_cny":None,"usage":None}
    assert not ConversationCostBudget(settings=settings,binding=binding,records=[unknown],frozen_limit="1").can_start(100)


@pytest.mark.asyncio
async def test_summary_cost_cap_shared_with_repair_and_isolated_from_foreground(summary_db,monkeypatch):
    db,p,settings=summary_db
    price=ModelPrice(version="cap-test",evidence_reference="mock price",base_url="https://summary.test",model_name="grok-4.6",
        protocol="openai_chat_completions",verified=True,billing_basis="all_inclusive_prompt_completion_tokens",input_cny_per_million=Decimal(3),output_cny_per_million=Decimal(7))
    conv,other,turn,ref=await history(db,p,settings,count=21)
    binding=ModelBinding.model_validate(turn.prompt_versions["model_binding"]).model_copy(update={"price":price})
    turn.prompt_versions={**turn.prompt_versions,"model_binding":binding.model_dump(mode="json"),"chat_max_model_cost_cny":"0.211"}
    await db.flush()
    sent=[]
    def invalid(request):
        sent.append(request);return completion({"summary":"无效待核摘要","open_constraints":[],"referenced_ids":[],"extra":"invalid"})
    mock_gateway(monkeypatch,settings,invalid)
    state=fresh_state(conv.id,turn.id)
    # 30000上下文窗口×7元/百万=0.21元预留；首轮可发，0.003012元已发生后修复不再可发。
    await BoundNodes(db,p,settings).summarize_session(state)
    assert len(sent)==1
    records=list((await db.execute(select(ConversationWorkItem).where(ConversationWorkItem.turn_id==turn.id,ConversationWorkItem.kind=="model_call"))).scalars())
    assert len(records)==1 and Decimal(records[0].payload["cost_cny"])==Decimal("0.003012")
    # 新节点及改大运行时设置不能改掉原Turn冻结上限，供下一意图/一般知识调用重新读账本。
    replacement=BoundNodes(db,p,settings.model_copy(update={"chat_max_model_cost_cny":Decimal(100)}))
    resumed_budget=await replacement.model_budget(db,turn,turn.prompt_versions["model_binding"])
    assert resumed_budget.can_start(2000) and resumed_budget.max_cost==Decimal("0.211")
    summary_budget = ConversationCostBudget(settings=settings, binding=binding, records=[r.payload for r in records], frozen_limit="0.211")
    assert not summary_budget.can_start(2000)
    assert resumed_budget.cost_used==Decimal(0)
    assert summary_budget.cost_used==Decimal("0.003012")
    next_conv,_,next_turn,_=await history(db,p,settings,count=21)
    next_turn.prompt_versions={**next_turn.prompt_versions,"model_binding":binding.model_dump(mode="json"),"chat_max_model_cost_cny":"0.209"}
    await db.flush()
    await BoundNodes(db,p,settings).summarize_session(fresh_state(next_conv.id,next_turn.id))
    assert len(sent)==1  # 初次请求也必须先通过保守费用门禁。


@pytest.mark.asyncio
async def test_semantic_summary_preserves_adjacent_fixed_reference_versions(summary_db,monkeypatch):
    db,p,settings=summary_db
    document_id,parse1,parse2,business,translation,snapshot=(str(uuid4()) for _ in range(6))
    def frozen(no):
        return [{"kind":"document","id":document_id,"document_id":document_id,"document_version":no,"parse_artifact_id":parse1 if no==1 else parse2,
            "business_revision_id":business,"translation_revision_id":translation,"snapshot_id":snapshot,"filename":"SECRET-FILENAME","excerpt":"SECRET-CONTENT"}] if no<=2 else []
    conv,other,turn,ref=await history(db,p,settings,count=22,snapshot_frozen=frozen)
    bodies=[]
    def valid(request):
        bodies.append(json.loads(request.content));return completion({"summary":"历史讨论","open_constraints":[],"referenced_ids":[]})
    mock_gateway(monkeypatch,settings,valid)
    await BoundNodes(db,p,settings).summarize_session(fresh_state(conv.id,turn.id))
    saved=(await db.execute(select(ConversationSummary).where(ConversationSummary.conversation_id==conv.id,ConversationSummary.prompt_version=="chat.session_summary@1"))).scalar_one()
    navigation=json.loads(saved.summary_text)["source_reference_versions"]
    assert {entry["document_version"] for entry in navigation}=={1,2}
    assert {entry["parse_artifact_id"] for entry in navigation}=={parse1,parse2}
    assert all(entry["business_revision_id"]==business and entry["translation_revision_id"]==translation and entry["snapshot_id"]==snapshot for entry in navigation)
    assert "SECRET-FILENAME" not in saved.summary_text and "SECRET-CONTENT" not in str(bodies)


@pytest.mark.asyncio
async def test_new_turn_freezes_chat_cost_limit_without_later_config_read(summary_db):
    from app.conversation.contracts import CreateConversationBody,TurnBody
    from app.conversation.service import create_conversation,create_turn
    db,p,settings=summary_db
    configured=settings.model_copy(update={"chat_max_model_cost_cny":Decimal("0.25")})
    conv=await create_conversation(db,p,configured,CreateConversationBody(),str(uuid4()))
    turn,_=await create_turn(db,p,configured,conv.id,TurnBody(text="查看任务进度"),str(uuid4()))
    assert turn.prompt_versions["chat_max_model_cost_cny"]=="0.25"
    configured=configured.model_copy(update={"chat_max_model_cost_cny":Decimal(100)})
    budget=await BoundNodes(db,p,configured).model_budget(db,turn,turn.prompt_versions["model_binding"])
    assert budget.max_cost==Decimal("0.25")


@pytest.mark.asyncio
async def test_background_summary_validates_sources_and_safe_knowledge_consumption(summary_db, monkeypatch):
    db, p, settings = summary_db
    conv, _, turn, _ = await history(db, p, settings, count=21, first_text="物流术语先给场景再解释")
    turn.status = "completed"
    await db.flush()
    state = fresh_state(conv.id, turn.id)
    job_id = UUID(await BoundNodes(db, p, settings).prepare_summary(state))
    job = await db.get(ConversationWorkItem, job_id)
    assert job.payload["status"] == "queued" and job.expires_at.year == 9999
    sent = []
    def send(request):
        sent.append(request)
        assert datetime.fromisoformat(job.payload["lease_until"]) > datetime.now(UTC) + timedelta(seconds=60)
        return completion({"summary":"讨论物流术语与解释次序", "open_constraints":["先给场景再解释"], "referenced_ids":[]})
    mock_gateway(monkeypatch, settings, send)
    source = await db.get(ConversationTurn, UUID(job.payload["source_turn_ids"][0]))
    source.status = "interrupted"
    await BoundSummaryJob(db, p, settings, job_id).execute()
    assert not sent and job.payload["status"] == "restricted"
    source.status = "completed"
    job.kind, job.payload = "summary_pending", {**job.payload, "status":"queued"}
    await db.flush()
    await BoundSummaryJob(db, p, settings, job_id).execute()
    assert len(sent) == 1 and job.payload["status"] == "succeeded"
    context = await assemble_context(db, conv, settings, knowledge_only=True)
    assert "讨论物流术语与解释次序" in context["summary"]
    summary = (await db.execute(select(ConversationSummary).where(ConversationSummary.conversation_id == conv.id,
        ConversationSummary.prompt_version == "chat.session_summary@1"))).scalar_one()
    value = json.loads(summary.summary_text)
    summary.summary_text = json.dumps({**value, "summary_scope":"case_discussion_unverified"}, ensure_ascii=False)
    await db.flush()
    assert "讨论物流术语与解释次序" not in ((await assemble_context(db, conv, settings, knowledge_only=True))["summary"] or "")


@pytest.mark.asyncio
async def test_summary_active_turn_defers_without_model_and_unknown_lease_never_resends(summary_db, monkeypatch):
    from app.workflows.models import QueueOutbox
    from app.conversation.summary import recover_summary_jobs
    db, p, settings = summary_db
    conv, _, turn, _ = await history(db, p, settings, count=21)
    turn.status = "completed"
    await db.flush()
    job_id = UUID(await BoundNodes(db, p, settings).prepare_summary(fresh_state(conv.id, turn.id)))
    job = await db.get(ConversationWorkItem, job_id)
    outbox = (await db.execute(select(QueueOutbox).where(QueueOutbox.dedupe_key == "summary:" + job.payload["source_hash"]))).scalar_one()
    outbox.status = "published"
    turn.status = "running"
    await db.flush()
    sent = []
    mock_gateway(monkeypatch, settings, lambda request: sent.append(request))
    await BoundSummaryJob(db, p, settings, job_id).execute()
    assert not sent and job.payload["status"] == "queued" and outbox.status == "pending"
    assert outbox.next_attempt_at > datetime.now(UTC)
    turn.status = "completed"
    job.payload = {**job.payload, "status":"running", "submission_started":True,
        "lease_until":(datetime.now(UTC)-timedelta(seconds=1)).isoformat()}
    await db.flush()
    await BoundSummaryJob(db, p, settings, job_id).execute()
    assert job.payload["status"] == "acceptance_unknown" and not sent
    await db.run_sync(lambda sync: recover_summary_jobs(sync, datetime.now(UTC), 10))
    assert job.payload["status"] == "acceptance_unknown" and not sent
