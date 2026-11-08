import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.api.errors import ApiError
from app.api.idempotency import request_digest
from app.config import Settings, get_settings
from app.conversation.contracts import CapabilitySelection
from app.extensions import mcp, service

SCHEMA = {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}
ENDPOINT = "https://reference.example/mcp"
TOOL = {"name": "public_lookup", "inputSchema": SCHEMA, "annotations": {"readOnlyHint": True}}


def configured(parameters=None):
    entry = {"endpoint": ENDPOINT, "tool_id": TOOL["name"], "schema_hash": request_digest(SCHEMA), "parameters": parameters or {"topic": ["UNLOCODE"]}}
    return get_settings().model_copy(update={"mcp_allowed_hosts": "reference.example,mcp.deepwiki.com", "mcp_public_tool_registry_json": json.dumps([entry])})


class Rows:
    def __init__(self, values):self.values = values
    def scalars(self):return iter(self.values)
    def scalar_one_or_none(self):return self.values[0] if self.values else None


class Database:
    def __init__(self, tool, grant):self.tool, self.grant, self.added = tool, grant, []
    async def execute(self, statement):
        sql = str(statement)
        return Rows([self.tool] if "mcp_tool_snapshots" in sql else [self.grant] if "mcp_tool_grants" in sql and self.grant else [])
    def add(self, value):self.added.append(value)
    async def flush(self):pass


def fixture(monkeypatch, settings, *, grant=True, remote_tool=None, response_override=None):
    connection = SimpleNamespace(id=uuid4(), status="enabled", enabled_version=1, name="Public fixture")
    version = SimpleNamespace(id=uuid4(), version=1, endpoint=ENDPOINT)
    tool = SimpleNamespace(tool_id=TOOL["name"], schema_hash=request_digest(SCHEMA), input_schema=SCHEMA, schema_changed=False, category="read_only", description="public", previous_schema_hash=None, previous_schema=None)
    permission = SimpleNamespace(id=uuid4(), tool_id=tool.tool_id, schema_hash=tool.schema_hash, scope="public_registry@1", data_level="public") if grant else None
    db = Database(tool, permission)
    principal = SimpleNamespace(tenant_id=uuid4(), user_id=uuid4())
    requests = []
    async def load_connection(*a, **kw):return connection
    async def load_version(*a, **kw):return version
    async def credentials(*a, **kw):return None
    async def resolve(endpoint, settings):return endpoint, ["8.8.8.8"]
    def handle(request):
        body = json.loads(request.content);requests.append(body)
        if response_override:
            response = response_override(body)
            if response is not None:return response
        if body["method"] == "notifications/initialized":return httpx.Response(202)
        result = {"protocolVersion": "2025-03-26"} if body["method"] == "initialize" else {"tools": [remote_tool or TOOL]} if body["method"] == "tools/list" else {"content": [{"type": "text", "text": "public reference"}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    constructor = mcp.StreamableHttpClient
    monkeypatch.setattr(service, "load_connection", load_connection)
    monkeypatch.setattr(service, "connection_version", load_version)
    monkeypatch.setattr(service, "credential_for", credentials)
    monkeypatch.setattr(mcp, "endpoint_and_addresses", resolve)
    monkeypatch.setattr(service, "StreamableHttpClient", lambda e,s,c:constructor(e,s,c,client=client))
    selection = CapabilitySelection(kind="mcp_tool", extension_id=connection.id, extension_version=1, tool_id=tool.tool_id, tool_schema_hash=tool.schema_hash, user_parameters={"topic": "UNLOCODE"})
    return db, principal, selection, connection, requests, client


async def test_registered_non_deepwiki_public_call_and_candidate_enum(monkeypatch):
    settings = configured();db, principal, selected, connection, requests, client = fixture(monkeypatch, settings)
    try:
        outcome, error, audit = await service.call_selected_mcp(db, principal, settings, selected.model_dump())
        assert error is None and outcome["content"][0]["text"] == "public reference"
        assert audit.status == "succeeded"
        assert [r["method"] for r in requests] == ["initialize", "notifications/initialized", "tools/list", "tools/call"]
        assert requests[-1]["params"]["arguments"] == {"topic": "UNLOCODE"}
        views = await service.tools_view(db, principal, SimpleNamespace(id=uuid4(), endpoint=ENDPOINT), settings)
        assert views[0]["public_parameters"] == {"topic": ["UNLOCODE"]} and views[0]["granted"]
        async def version(*a,**k):return SimpleNamespace(id=uuid4(),version=1,endpoint=ENDPOINT)
        monkeypatch.setattr(service,"connection_version",version)
        class CandidateDB(Database):
            async def execute(self, statement):
                sql=str(statement)
                if "FROM skills" in sql:return Rows([])
                if "FROM mcp_connections" in sql:return Rows([connection])
                return await super().execute(statement)
        candidates=await service.available(CandidateDB(db.tool,db.grant),principal,settings)
        assert candidates[0]["parameters"][0]["type"] == "enum"
        assert candidates[0]["parameters"][0]["options"] == ["UNLOCODE"]
    finally:await client.aclose()


@pytest.mark.parametrize("case",["unpublished","private_value","extra_text","missing_grant","changed_grant"])
async def test_scope_and_grant_rejected_before_any_http(monkeypatch,case):
    settings=configured();db,p,s,c,requests,client=fixture(monkeypatch,settings,grant=case!="missing_grant")
    if case=="unpublished":settings=settings.model_copy(update={"mcp_public_tool_registry_json":None})
    if case=="private_value":s=s.model_copy(update={"user_parameters":{"topic":"customer secret"}})
    if case=="extra_text":s=s.model_copy(update={"user_parameters":{"topic":"UNLOCODE","case_text":"customer secret"}})
    if case=="changed_grant":db.grant.schema_hash="0"*64
    try:
        with pytest.raises(ApiError):await service.call_selected_mcp(db,p,settings,s.model_dump())
        assert requests==[] and db.added==[]
    finally:await client.aclose()


@pytest.mark.parametrize("change",["schema","readonly"])
async def test_remote_drift_blocks_tools_call(monkeypatch,change):
    remote=deepcopy(TOOL)
    if change=="schema":remote["inputSchema"]={**SCHEMA,"description":"drift"}
    else:remote["annotations"]={"readOnlyHint":False}
    settings=configured();db,p,s,c,requests,client=fixture(monkeypatch,settings,remote_tool=remote)
    try:
        _,error,audit=await service.call_selected_mcp(db,p,settings,s.model_dump())
        assert error in {"schema_changed","data_scope_not_supported"} and audit.status=="failed"
        assert "tools/call" not in [r["method"] for r in requests]
        assert c.status=="needs_recheck" and c.enabled_version is None
    finally:await client.aclose()


@pytest.mark.parametrize("tool", [None, "invalid", {"name": ""}, {"name": "missing_schema"}, {**TOOL, "annotations": []}, {**TOOL, "description": None}])
async def test_malformed_remote_tool_is_classified_without_http(tool):
    client = mcp.StreamableHttpClient(ENDPOINT, Settings(_env_file=None))
    async def request(method, params=None, **kwargs):
        if method == "initialize":return {"protocolVersion": "2025-03-26"}
        if method == "tools/list":return {"tools": [tool]}
    client.request = request
    with pytest.raises(mcp.McpFailure):await client.discover()
    assert client.stages[-1]["stage"] == "schema" and client.stages[-1]["ok"] is False


@pytest.mark.parametrize('result', [
    None, {}, {'content':None}, {'content':'text'}, {'content':[None]}, {'content':[{'type':'text','text':None}]},
    {'content':[], 'isError':'false'}, {'content':[], 'isError':[]}, {'content':[], 'structuredContent':[]},
    {'content':[{'type':'image','data':'AA==','mimeType':None}]}, {'content':[{'type':'audio','mimeType':'audio/wav'}]},
    {'content':[{'type':'resource_link','uri':'https://reference.example','name':None}]},
    {'content':[{'type':'resource','resource':{'uri':'fixture:1','text':None}}]},
    {'content':[{'type':'resource','resource':{'uri':'fixture:1','text':'x','blob':'AA=='}}]},
    {'content':[{'type':'text','text':'x','annotations':[]}]}, {'content':[{'type':'unknown'}]}, {'content':[{'type':[]}]},
])
async def test_invalid_call_content_records_protocol_failure_and_disables(monkeypatch,result):
    settings=Settings(_env_file=None,mcp_public_tool_registry_json=json.dumps([{
        'endpoint':ENDPOINT,'tool_id':TOOL['name'],'schema_hash':request_digest(SCHEMA),'parameters':{'topic':['UNLOCODE']}}]))
    def override(body):
        if body['method']=='tools/call':return httpx.Response(200,json={'jsonrpc':'2.0','id':body['id'],'result':result})
    db,p,selected,connection,requests,http_client=fixture(monkeypatch,settings,response_override=override)
    adapter=mcp.StreamableHttpClient(ENDPOINT,settings,client=http_client)
    monkeypatch.setattr(service,'StreamableHttpClient',lambda *args:adapter)
    try:
        outcome,error,audit=await service.call_selected_mcp(db,p,settings,selected.model_dump())
        assert outcome is None and error=='invalid_response' and audit.status=='failed'
        assert connection.status=='needs_recheck' and connection.enabled_version is None
        assert adapter.stages[-1]['stage']=='tools/call' and adapter.stages[-1]['ok'] is False
        assert adapter.stages[-1]['error_class']=='invalid_response'
    finally:await http_client.aclose()


@pytest.mark.parametrize('result', [
    {'content':[]}, {'content':[],'structuredContent':{'values':[1,{'value':'public'}]},'isError':False},
    {'content':[{'type':'text','text':'public'}, {'type':'image','data':'AA==','mimeType':'image/png'},
        {'type':'audio','data':'AA==','mimeType':'audio/wav'}, {'type':'resource_link','uri':'fixture:1','name':'public'},
        {'type':'resource','resource':{'uri':'fixture:1','text':'public'}},
        {'type':'resource','resource':{'uri':'fixture:2','blob':'AA==','mimeType':'application/pdf'}}]},
    {'content':[{'type':'text','text':'business failure'}],'isError':True},
])
async def test_valid_call_content_and_business_error_keep_connection(monkeypatch,result):
    settings=Settings(_env_file=None,mcp_public_tool_registry_json=json.dumps([{
        'endpoint':ENDPOINT,'tool_id':TOOL['name'],'schema_hash':request_digest(SCHEMA),'parameters':{'topic':['UNLOCODE']}}]))
    def override(body):
        if body['method']=='tools/call':return httpx.Response(200,json={'jsonrpc':'2.0','id':body['id'],'result':result})
    db,p,selected,connection,requests,http_client=fixture(monkeypatch,settings,response_override=override)
    adapter=mcp.StreamableHttpClient(ENDPOINT,settings,client=http_client)
    monkeypatch.setattr(service,'StreamableHttpClient',lambda *args:adapter)
    try:
        outcome,error,audit=await service.call_selected_mcp(db,p,settings,selected.model_dump())
        failed=result.get('isError') is True
        assert error==('tool_failed' if failed else None)
        assert audit.status==('failed' if failed else 'succeeded')
        assert adapter.stages[-1]['ok'] is (not failed)
        assert connection.status=='enabled' and connection.enabled_version==1
        if not failed:assert outcome==result
    finally:await http_client.aclose()


@pytest.mark.parametrize('method,result,error', [
    ('initialize',{},'invalid_initialize'), ('initialize',{'protocolVersion':'2025-03-26','capabilities':[]},'invalid_initialize'),
    ('tools/list',{},'invalid_tools'), ('tools/list',{'tools':None},'invalid_tools'),
])
async def test_discovery_validation_is_in_failed_stage(monkeypatch,method,result,error):
    def override(body):
        if body['method']==method:return httpx.Response(200,json={'jsonrpc':'2.0','id':body['id'],'result':result})
    _,_,_,_,requests,http_client=fixture(monkeypatch,Settings(_env_file=None),response_override=override)
    adapter=mcp.StreamableHttpClient(ENDPOINT,Settings(_env_file=None),client=http_client)
    try:
        with pytest.raises(mcp.McpFailure,match=error):await adapter.discover()
        assert adapter.stages[-1]['stage']==method and adapter.stages[-1]['ok'] is False
        assert adapter.stages[-1]['error_class']==error
    finally:await http_client.aclose()


@pytest.mark.parametrize('payload,error', [
    ({'jsonrpc':'2.0','id':True,'result':{}},'invalid_response'),
    ({'jsonrpc':'2.0','id':1},'invalid_response'),
    ({'jsonrpc':'2.0','id':1,'result':{},'error':{'code':1,'message':'error'}},'invalid_response'),
    ({'jsonrpc':'2.0','id':1,'error':None},'invalid_response'),
    ({'jsonrpc':'2.0','id':1,'error':{'code':True,'message':'error'}},'invalid_response'),
    ({'jsonrpc':'2.0','id':1,'error':{'code':1,'message':None}},'invalid_response'),
    ({'jsonrpc':'2.0','id':1,'error':{'code':-32603,'message':'business failure'}},'rpc_error'),
])
async def test_rpc_envelope_shapes(monkeypatch,payload,error):
    def override(body):return httpx.Response(200,json=payload)
    _,_,_,_,requests,http_client=fixture(monkeypatch,Settings(_env_file=None),response_override=override)
    adapter=mcp.StreamableHttpClient(ENDPOINT,Settings(_env_file=None),client=http_client)
    try:
        with pytest.raises(mcp.McpFailure,match=error):await adapter.request('initialize')
    finally:await http_client.aclose()


@pytest.mark.parametrize('source', [
    '{"value":NaN}', '{"value":1e999}', '{"value":'+('1'*4500)+'}', '['*40+'0'+']'*40,
    '['*12000+'0'+']'*12000, '{invalid',
])
@pytest.mark.parametrize('sse',[False,True])
async def test_invalid_nested_json_is_protocol_failure(monkeypatch,source,sse):
    def override(body):
        content='{"jsonrpc":"2.0","id":'+str(body['id'])+',"result":'+source+'}'
        if sse:content='data: '+content+'\n\n'
        return httpx.Response(200,content=content,headers={'content-type':'text/event-stream' if sse else 'application/json'})
    _,_,_,_,requests,http_client=fixture(monkeypatch,Settings(_env_file=None),response_override=override)
    adapter=mcp.StreamableHttpClient(ENDPOINT,Settings(_env_file=None),client=http_client)
    try:
        with pytest.raises(mcp.McpFailure,match='invalid_response'):await adapter.discover()
        assert adapter.stages[-1]['stage']=='initialize' and adapter.stages[-1]['ok'] is False
    finally:await http_client.aclose()


@pytest.mark.parametrize("schema", [
    {"type": []}, {"properties": []}, {"required": "topic"}, {"required": [None]},
    {"enum": 1}, {"items": None}, {"anyOf": None}, {"oneOf": {}},
    {"maxLength": "10"}, {"minimum": None}, {"pattern": "["},
    {"additionalProperties": None}, {"additionalProperties": []}, {"additionalProperties": {"type": []}},
    {"minimum": float("nan")}, {"maximum": float("inf")}, {"enum": [float("-inf")]},
])
def test_malformed_remote_schema_is_classified(schema):
    with pytest.raises(mcp.McpFailure):mcp.validate_schema(schema)


@pytest.mark.parametrize("error", ["http_401", "ConnectError", "credential_unavailable", "invalid_tools", "tool_failed", "invalid_arguments", "data_scope_not_supported", "rpc_error"])
async def test_actual_mcp_failure_updates_new_call_availability(monkeypatch, error):
    entry = {"endpoint": ENDPOINT, "tool_id": TOOL["name"], "schema_hash": request_digest(SCHEMA), "parameters": {"topic": ["UNLOCODE"]}}
    settings = Settings(_env_file=None, mcp_public_tool_registry_json=json.dumps([entry]))
    db, principal, selected, connection, requests, client = fixture(monkeypatch, settings)
    version = SimpleNamespace(id=uuid4(), version=1, endpoint=ENDPOINT, status="enabled", error_class=None)
    async def load_version(*args, **kwargs):return version
    class FailedClient:
        def __init__(self, *args):pass
        async def discover(self):
            if error == "invalid_tools":raise mcp.McpFailure(error)
            return [TOOL]
        async def call(self, *args):raise mcp.McpFailure(error)
    monkeypatch.setattr(service, "connection_version", load_version)
    monkeypatch.setattr(service, "StreamableHttpClient", FailedClient)
    if error == "credential_unavailable":
        async def credentials(*args, **kwargs):raise mcp.McpFailure(error)
        monkeypatch.setattr(service, "credential_for", credentials)
    try:
        _, failure, audit = await service.call_selected_mcp(db, principal, settings, selected.model_dump())
        assert failure == error and audit.status == "failed" and requests == []
        if error in {"tool_failed", "invalid_arguments", "data_scope_not_supported", "rpc_error"}:
            assert connection.status == "enabled" and connection.enabled_version == 1
            assert version.error_class is None
        else:
            assert connection.enabled_version is None
            assert connection.status == ("needs_recheck" if error == "invalid_tools" else "connect_failed")
            assert version.error_class == error
            with pytest.raises(ApiError):await service.validate_selections(db, principal, settings, [selected])
        assert len(db.added) == 1
    finally:await client.aclose()


def test_additional_properties_schema_and_finite_numeric_arguments():
    for additional in (True, False, {"type": "string"}):
        mcp.validate_schema({"type": "object", "additionalProperties": additional})
    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(mcp.McpFailure, match="invalid_arguments"):
            mcp.validate_arguments({"type": "number", "minimum": 0, "maximum": 10}, value)


async def test_credential_failure_records_connection_test_without_http(monkeypatch):
    settings = Settings(_env_file=None)
    connection = SimpleNamespace(status="enabled", enabled_version=1)
    version = SimpleNamespace(id=uuid4(), endpoint=ENDPOINT, status="enabled", error_class=None)
    db = Database(None, None)
    principal = SimpleNamespace(tenant_id=uuid4())
    async def credentials(*args, **kwargs):raise mcp.McpFailure("credential_unavailable")
    monkeypatch.setattr(service, "credential_for", credentials)
    failure = await service.discover_connection(db, principal, settings, connection, version)
    assert failure == "credential_unavailable" and connection.enabled_version is None
    assert version.status == "connect_failed" and db.added[0].stages == [] and db.added[0].success is False


def test_corrupt_stored_credential_is_classified():
    import base64
    principal = SimpleNamespace(tenant_id=uuid4(), user_id=uuid4())
    settings = Settings(_env_file=None, mcp_credential_master_key=base64.urlsafe_b64encode(b"x" * 32).decode())
    envelope = mcp.seal_credential(settings, principal, "synthetic-token")
    envelope.ciphertext = base64.urlsafe_b64encode(b"invalid ciphertext").decode()
    with pytest.raises(mcp.McpFailure, match="credential_unavailable"):mcp.open_credential(settings, principal, envelope)


@pytest.mark.parametrize("raw",['{','{}','[{}]',json.dumps([{ "endpoint": ENDPOINT,"tool_id":"public_lookup","schema_hash":request_digest(SCHEMA),"parameters":{"topic":[{"text":"anything"}]}}])])
async def test_invalid_registry_explicitly_disables(monkeypatch,raw):
    settings=configured().model_copy(update={"mcp_public_tool_registry_json":raw})
    with pytest.raises(mcp.McpFailure,match="public_registry_invalid"):mcp.public_tool_registry(settings)
    class SkillDB:
        async def execute(self, statement):
            assert "FROM skills" in str(statement)
            return Rows([SimpleNamespace(id=uuid4(),enabled_version=1)])
    async def version(*a,**kw):return SimpleNamespace(version=1,definition={"name":"still available","purpose":"Skill","allowed_actions":[],"parameters":[]})
    monkeypatch.setattr(service,"skill_version",version)
    db=SkillDB();principal=SimpleNamespace(user_id=uuid4(),permissions=("run_start",))
    candidates=await service.available(db,principal,settings)
    assert len(candidates)==1 and candidates[0]["kind"]=="user_skill"
    from app.conversation.tools import available_capabilities
    assert {item["kind"] for item in await available_capabilities(db,principal,settings)}=={"builtin_skill","user_skill"}
    assert service.public_registry_warnings(settings)[0]["code"]=="MCP_PUBLIC_REGISTRY_INVALID"
    assert service.templates(settings)["items"][0]["public_registry_ready"] is False


def test_deepwiki_default_and_explicit_finite_override():
    schema={"type":"object","required":["repoName"],"properties":{"repoName":{"type":"string","description":'GitHub repository in owner/repo format (e.g. "facebook/react").'}}}
    endpoint,tool,digest=mcp.DEEPWIKI_PUBLIC_KEY
    settings=get_settings().model_copy(update={"mcp_public_tool_registry_json":None})
    mcp.validate_public_arguments(settings,endpoint,tool,digest,schema,{"repoName":"langchain-ai/langgraph"})
    with pytest.raises(mcp.McpFailure):mcp.validate_public_arguments(settings,endpoint,tool,digest,schema,{"repoName":"private/customer"})
    override={"endpoint":endpoint,"tool_id":tool,"schema_hash":digest,"parameters":{"repoName":["facebook/react"]}}
    settings=settings.model_copy(update={"mcp_public_tool_registry_json":json.dumps([override])})
    mcp.validate_public_arguments(settings,endpoint,tool,digest,schema,{"repoName":"facebook/react"})
    with pytest.raises(mcp.McpFailure):mcp.validate_public_arguments(settings,endpoint,tool,digest,schema,{"repoName":"langchain-ai/langgraph"})


async def test_registered_private_host_still_rejected():
    settings=configured().model_copy(update={"mcp_allowed_hosts":"127.0.0.1"})
    with pytest.raises(mcp.McpFailure):await mcp.validate_endpoint("https://127.0.0.1/mcp",settings)


async def test_real_database_registered_connection_lifecycle_rolls_back(monkeypatch):
    from sqlalchemy import select
    from app.identity.models import User
    from app.identity.seed import TENANT_A
    from app.identity.service import Principal
    from app.infra.database import create_database_engine, create_session_factory, tenant_transaction
    settings=configured();requests=[]
    async def resolve(endpoint, settings):return endpoint,["8.8.8.8"]
    def handle(request):
        body=json.loads(request.content);requests.append(body)
        if body["method"]=="notifications/initialized":return httpx.Response(202)
        result={"protocolVersion":"2025-03-26"} if body["method"]=="initialize" else {"tools":[TOOL]} if body["method"]=="tools/list" else {"content":[{"type":"text","text":"public fixture"}]}
        return httpx.Response(200,json={"jsonrpc":"2.0","id":body["id"],"result":result})
    client=httpx.AsyncClient(transport=httpx.MockTransport(handle));constructor=mcp.StreamableHttpClient
    monkeypatch.setattr(mcp,"endpoint_and_addresses",resolve)
    monkeypatch.setattr(service,"StreamableHttpClient",lambda e,s,c:constructor(e,s,c,client=client))
    engine=create_database_engine(settings)
    try:
        async with create_session_factory(engine)() as db,tenant_transaction(db,TENANT_A):
            user=(await db.execute(select(User).where(User.account=="demo"))).scalar_one()
            principal=Principal(user.id,TENANT_A,user.display_name,tuple(user.permissions),uuid4(),"")
            connection,version=await service.create_connection(db,principal,settings,{"name":"Public rollback fixture","endpoint":ENDPOINT,"auth_type":"none"},str(uuid4()))
            await service.change_connection(db,principal,settings,connection.id,"test",str(uuid4()),version_no=1)
            views=await service.tools_view(db,principal,version,settings)
            assert views[0]["category"]=="read_only" and views[0]["public_scope_ready"] and not views[0]["granted"]
            await service.change_connection(db,principal,settings,connection.id,"grant",str(uuid4()),version_no=1,body={"tool_ids":[TOOL["name"]]})
            await service.change_connection(db,principal,settings,connection.id,"enable",str(uuid4()),version_no=1)
            candidate=next(item for item in await service.available(db,principal,settings) if item["extension_id"]==str(connection.id))
            assert candidate["parameters"][0]["options"]==["UNLOCODE"]
            selected=CapabilitySelection(kind="mcp_tool",extension_id=connection.id,extension_version=1,tool_id=TOOL["name"],tool_schema_hash=request_digest(SCHEMA),user_parameters={"topic":"UNLOCODE"})
            result,error,audit=await service.call_selected_mcp(db,principal,settings,selected.model_dump())
            assert error is None and audit.status=="succeeded" and result["content"]
            assert len([r for r in requests if r["method"]=="tools/call"])==1
            await service.change_connection(db,principal,settings,connection.id,"disable",str(uuid4()))
            before=len(requests)
            with pytest.raises(ApiError):await service.call_selected_mcp(db,principal,settings,selected.model_dump())
            assert len(requests)==before
            await db.rollback()
    finally:
        await client.aclose();await engine.dispose()


@pytest.mark.parametrize("shared_connection", [False, True])
async def test_real_database_credentials_detach_archive_and_rollback(monkeypatch, shared_connection):
    import base64
    from pydantic import SecretStr
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import AsyncSession
    from app.api.errors import InvalidRequest, NotFoundOrForbidden
    from app.extensions.models import CredentialEnvelope, ExtensionCall, McpConnection, McpConnectionVersion, McpToolGrant
    from app.identity.models import User
    from app.identity.service import Principal
    from app.infra.database import create_database_engine

    settings = get_settings().model_copy(update={"mcp_credential_master_key": SecretStr(base64.urlsafe_b64encode(b"r" * 32).decode())})
    async def endpoint(value, settings):return value
    monkeypatch.setattr(service, "validate_endpoint", endpoint)
    engine = create_database_engine(settings)
    connection_id = None
    try:
        async with engine.connect() as connection:
            outer = await connection.begin()
            db = AsyncSession(bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint")
            try:
                await db.execute(text("SET LOCAL ROLE logistics_app"))
                user = (await db.execute(select(User).where(User.account == "demo"))).scalar_one()
                await db.execute(text(f"SET LOCAL app.tenant_id = '{user.tenant_id}'"))
                principal = Principal(user.id, user.tenant_id, user.display_name, tuple(user.permissions), uuid4(), "")
                created, first = await service.create_connection(db, principal, settings,
                    {"name": "Credential rollback fixture", "endpoint": ENDPOINT, "auth_type": "bearer", "credential": "synthetic-one"}, str(uuid4()))
                connection_id = created.id
                old_id = first.credential_envelope_id
                await service.change_connection(db, principal, settings, created.id, "patch", str(uuid4()), version_no=1,
                    body={"credential": "synthetic-two", "expected_version": 1})
                assert await db.get(CredentialEnvelope, old_id) is None
                shared_id = first.credential_envelope_id
                _, second = await service.change_connection(db, principal, settings, created.id, "new", str(uuid4()))
                await service.change_connection(db, principal, settings, created.id, "patch", str(uuid4()), version_no=2,
                    body={"credential": "synthetic-three", "expected_version": 1})
                assert await db.get(CredentialEnvelope, shared_id) is not None and first.credential_envelope_id == shared_id
                detached_id = second.credential_envelope_id
                await service.change_connection(db, principal, settings, created.id, "patch", str(uuid4()), version_no=2,
                    body={"auth_type": "none", "expected_version": 2})
                assert second.credential_envelope_id is None and await db.get(CredentialEnvelope, detached_id) is None
                async with db.begin_nested():
                    with pytest.raises(InvalidRequest):
                        await service.change_connection(db, principal, settings, created.id, "patch", str(uuid4()), version_no=2,
                            body={"auth_type": "none", "credential": "synthetic-refused", "expected_version": 3})
                    assert second.credential_envelope_id is None
                other = None
                if shared_connection:
                    other = McpConnection(tenant_id=user.tenant_id, owner_user_id=user.id, name="Shared credential rollback", status="enabled", enabled_version=1)
                    db.add(other);await db.flush()
                    db.add(McpConnectionVersion(tenant_id=user.tenant_id, connection_id=other.id, version=1,
                        endpoint=ENDPOINT, auth_type="bearer", credential_envelope_id=shared_id, status="enabled", ever_enabled=True))
                first.ever_enabled = True
                grant = McpToolGrant(tenant_id=user.tenant_id, connection_version_id=first.id, tool_id=TOOL["name"],
                    subject_user_id=user.id, schema_hash=request_digest(SCHEMA), scope="public_registry@1", data_level="public")
                audit = ExtensionCall(tenant_id=user.tenant_id, extension_id=created.id, owner_user_id=user.id,
                    extension_version=1, name=created.name, action=TOOL["name"], status="succeeded", result_redacted="synthetic audit")
                db.add_all([grant, audit]);await db.flush()
                outsider = Principal(uuid4(), user.tenant_id, "other", (), uuid4(), "")
                with pytest.raises(NotFoundOrForbidden):
                    await service.change_connection(db, outsider, settings, created.id, "archive", str(uuid4()))
                key = str(uuid4())
                await service.change_connection(db, principal, settings, created.id, "archive", key)
                await service.change_connection(db, principal, settings, created.id, "archive", key)
                assert created.status == "archived" and created.enabled_version is None and grant.revoked_at is not None
                assert first.credential_envelope_id is None and second.credential_envelope_id is None
                assert first.status == "archived" and second.status == "archived"
                assert await db.get(ExtensionCall, audit.id) is audit
                assert (await db.get(CredentialEnvelope, shared_id) is not None) == shared_connection
                if other:
                    assert other.status == "enabled" and other.enabled_version == 1
                for command in ("new", "patch", "test", "grant", "enable", "disable"):
                    with pytest.raises(InvalidRequest):
                        await service.change_connection(db, principal, settings, created.id, command, str(uuid4()), body={})
                if other:
                    await service.change_connection(db, principal, settings, other.id, "archive", str(uuid4()))
                    assert await db.get(CredentialEnvelope, shared_id) is None
                await db.flush()
            finally:
                await db.close();await outer.rollback()
        async with engine.connect() as connection:
            async with connection.begin():
                await connection.execute(text("SET LOCAL ROLE logistics_app"))
                await connection.execute(text(f"SET LOCAL app.tenant_id = '{user.tenant_id}'"))
                assert (await connection.execute(select(McpConnection.id).where(McpConnection.id == connection_id))).scalar_one_or_none() is None
    finally:
        await engine.dispose()


@pytest.mark.parametrize("command", ["disable", "archive"])
async def test_real_database_stale_identity_cannot_call_disabled_connection(monkeypatch, command):
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import AsyncSession
    from app.extensions.models import McpConnection, McpConnectionVersion, McpToolGrant, McpToolSnapshot
    from app.identity.models import User
    from app.identity.service import Principal
    from app.infra.database import create_database_engine

    settings = configured()
    engine = create_database_engine(settings)
    requests = []
    class ForbiddenClient:
        def __init__(self, *args):
            requests.append("client_created")
            raise AssertionError("失效连接不得请求远端")
    monkeypatch.setattr(service, "StreamableHttpClient", ForbiddenClient)
    try:
        async with engine.connect() as conn:
            outer = await conn.begin()
            first = AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
            second = AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
            try:
                await first.execute(text("SET LOCAL ROLE logistics_app"))
                user = (await first.execute(select(User).where(User.account == "demo"))).scalar_one()
                await first.execute(text(f"SET LOCAL app.tenant_id = '{user.tenant_id}'"))
                principal = Principal(user.id, user.tenant_id, user.display_name, tuple(user.permissions), uuid4(), "")
                connection = McpConnection(tenant_id=user.tenant_id, owner_user_id=user.id, name="Stale identity rollback", status="enabled", enabled_version=1)
                first.add(connection);await first.flush()
                version = McpConnectionVersion(tenant_id=user.tenant_id, connection_id=connection.id, version=1,
                    endpoint=ENDPOINT, auth_type="none", status="enabled", ever_enabled=True)
                first.add(version);await first.flush()
                first.add_all([
                    McpToolSnapshot(tenant_id=user.tenant_id, connection_version_id=version.id, tool_id=TOOL["name"],
                        description="synthetic tool", input_schema=SCHEMA, schema_hash=request_digest(SCHEMA), category="read_only"),
                    McpToolGrant(tenant_id=user.tenant_id, connection_version_id=version.id, tool_id=TOOL["name"],
                        subject_user_id=user.id, schema_hash=request_digest(SCHEMA), scope="public_registry@1", data_level="public"),
                ])
                await first.commit()
                selected = CapabilitySelection(kind="mcp_tool", extension_id=connection.id, extension_version=1,
                    tool_id=TOOL["name"], tool_schema_hash=request_digest(SCHEMA), user_parameters={"topic": "UNLOCODE"})
                await service.validate_selections(first, principal, settings, [selected])
                await first.commit()
                await service.change_connection(second, principal, settings, connection.id, command, str(uuid4()))
                await second.commit()
                assert connection.status == "enabled" and connection.enabled_version == 1
                with pytest.raises(ApiError):await service.call_selected_mcp(first, principal, settings, selected.model_dump())
                assert connection.status == ("archived" if command == "archive" else "disabled")
                assert connection.enabled_version is None and requests == []
                if command == "archive":
                    await service.connection_version(first, connection, 1, write=True)
                    assert version.status == "archived"
            finally:
                await second.close();await first.close();await outer.rollback()
    finally:
        await engine.dispose()


def test_boolean_registry_values_are_strict_and_remote_annotation_not_permission():
    schema={"type":"object","properties":{"detailed":{"type":"boolean"}},"required":["detailed"]}
    entry={"endpoint":ENDPOINT,"tool_id":"boolean_lookup","schema_hash":request_digest(schema),"parameters":{"detailed":[False]}}
    settings=get_settings().model_copy(update={"mcp_public_tool_registry_json":json.dumps([entry])})
    mcp.validate_public_arguments(settings,ENDPOINT,"boolean_lookup",request_digest(schema),schema,{"detailed":False})
    for value in [True,0,"false"]:
        with pytest.raises(mcp.McpFailure):mcp.validate_public_arguments(settings,ENDPOINT,"boolean_lookup",request_digest(schema),schema,{"detailed":value})
    unregistered={"name":"anything","inputSchema":SCHEMA,"annotations":{"readOnlyHint":True}}
    assert mcp.tool_category(unregistered,ENDPOINT,settings)=="read_only"
    assert mcp.public_tool_policy(settings,ENDPOINT,"anything",request_digest(SCHEMA),SCHEMA) is None


async def test_invalid_registry_blocks_discovery_before_http(monkeypatch):
    settings=configured().model_copy(update={"mcp_public_tool_registry_json":"invalid"})
    db,p,s,c,requests,client=fixture(monkeypatch,settings)
    version=SimpleNamespace(id=uuid4(),endpoint=ENDPOINT,tested_at=None,error_class=None,status="draft")
    try:
        error=await service.discover_connection(db,p,settings,c,version)
        assert error=="public_registry_invalid" and version.status=="connect_failed"
        assert requests==[] and c.enabled_version is None
    finally:await client.aclose()


async def test_discovery_readonly_downgrade_revokes_enabled_connection(monkeypatch):
    remote=deepcopy(TOOL);remote["annotations"]={"readOnlyHint":False}
    settings=configured();db,p,s,c,requests,client=fixture(monkeypatch,settings,remote_tool=remote)
    version=SimpleNamespace(id=uuid4(),endpoint=ENDPOINT,tested_at=None,error_class=None,status="enabled")
    try:
        assert await service.discover_connection(db,p,settings,c,version) is None
        assert db.tool.category=="write" and c.enabled_version is None and c.status=="needs_recheck"
        assert db.grant.revoked_at is not None
        assert "tools/call" not in [request["method"] for request in requests]
    finally:await client.aclose()


async def test_registry_readiness_is_redacted_and_degraded(monkeypatch):
    from contextlib import asynccontextmanager
    from app.ops.service import readiness_probe
    class ProbeDB:
        @asynccontextmanager
        async def begin_nested(self):yield
        async def execute(self,*a,**kw):raise RuntimeError('probe fixture')
    class OfflineClient:
        def __init__(self,*a,**kw):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*a):pass
        async def get(self,*a,**kw):raise httpx.ConnectError('probe fixture')
    monkeypatch.setattr('app.ops.service.httpx.AsyncClient',OfflineClient)
    settings=configured().model_copy(update={"mcp_public_tool_registry_json":"invalid private registry content","checkpoint_database_url":None,"redis_url":None,"s3_endpoint_url":None,"s3_public_endpoint_url":None,"mineru_base_url":None,"mineru_api_key":None})
    result=await readiness_probe(ProbeDB(),settings)
    component=result["components"]["mcp_templates"]
    assert component["ok"] is False and component["public_registry_ready"] is False
    assert component["reason"]=="MCP公开工具注册配置无效，调用禁用"
    assert result["status"]=="degraded" and 'invalid private registry content' not in json.dumps(result,default=str)


async def test_invalid_registry_routes_keep_skill_items_and_root_warning(monkeypatch):
    from contextlib import asynccontextmanager
    from app.api.routers import conversation as crouter, extensions as erouter
    from app.conversation import tools
    class RouteDB:
        @asynccontextmanager
        async def begin(self):yield
    async def context(*a):pass
    async def capabilities(*a,**k):return [{"kind":"builtin_skill","name":"Skill fixture"}]
    async def rows(*a,**k):return [{"kind":"user_skill","name":"Skill fixture"}]
    monkeypatch.setattr(crouter,"set_tenant_context",context)
    monkeypatch.setattr(erouter,"set_tenant_context",context)
    monkeypatch.setattr(tools,"available_capabilities",capabilities)
    monkeypatch.setattr(service,"extension_rows",rows)
    settings=configured().model_copy(update={"mcp_public_tool_registry_json":"invalid"})
    for result in [await crouter.capabilities(None,RouteDB(),settings),await erouter.list_extensions(None,RouteDB(),settings)]:
        assert result["items"] and result["warnings"]==service.public_registry_warnings(settings)
        assert all(item["kind"]!="mcp_tool" for item in result["items"])


async def test_unpublished_readonly_classification_never_grants_scope(monkeypatch):
    from app.api.errors import InvalidRequest
    settings=configured().model_copy(update={"mcp_public_tool_registry_json":None})
    db,p,s,c,requests,client=fixture(monkeypatch,settings)
    version=SimpleNamespace(id=uuid4(),version=1,endpoint=ENDPOINT,tested_at=True)
    async def load_version(*a,**kw):return version
    monkeypatch.setattr(service,"connection_version",load_version)
    async def begin(*a,**kw):return SimpleNamespace(replayed=False)
    monkeypatch.setattr(service.idempotency,"begin",begin)
    try:
        assert mcp.tool_category(TOOL,ENDPOINT,settings)=="read_only"
        view=(await service.tools_view(db,p,version,settings))[0]
        assert view["category"]=="read_only" and view["public_scope_ready"] is False and view["granted"] is False
        with pytest.raises(InvalidRequest):await service.change_connection(db,p,settings,c.id,"grant",str(uuid4()),version_no=1,body={"tool_ids":[TOOL["name"]]})
        with pytest.raises(ApiError):await service.call_selected_mcp(db,p,settings,s.model_dump())
        assert requests==[]
        invalid=settings.model_copy(update={"mcp_public_tool_registry_json":"invalid"})
        assert mcp.tool_category(TOOL,ENDPOINT,invalid)=="read_only"
        with pytest.raises(ApiError):await service.call_selected_mcp(db,p,invalid,s.model_dump())
        assert requests==[]
    finally:await client.aclose()
