from uuid import uuid4

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.extensions import service as extensions
from app.extensions.mcp import McpFailure, seal_credential, validate_arguments, validate_endpoint
from app.identity.models import User
from app.identity.seed import TENANT_A
from app.identity.service import Principal
from app.infra.database import create_database_engine, create_session_factory, tenant_transaction
from app.preferences import service as preferences
from app.api.errors import ServiceUnavailable, VersionConflict


async def test_preferences_preview_confirmation_and_version_cas():
    engine = create_database_engine(get_settings())
    factory = create_session_factory(engine)
    try:
        async with factory() as db, tenant_transaction(db, TENANT_A):
            user = (await db.execute(select(User).where(User.account == "demo"))).scalar_one()
            principal = Principal(user.id, TENANT_A, user.display_name, tuple(user.permissions), uuid4(), "")
            existing = await preferences.get_preferences(db, principal)
            expected = existing.version if existing else 0
            body = preferences.PreferencesBody(interface_language="zh-CN", timezone="Asia/Shanghai", panel_width=640, motion="system", expected_version=expected)
            saved = await preferences.save_preferences(db, principal, body, str(uuid4()))
            assert saved.version == expected+1
            bad = preferences.MemoryBody(category="presentation", content="API_KEY=sk-sensitive-secret-value")
            assert preferences.preview_memory(bad)["rejected_reason"]
            term = preferences.MemoryBody(category="answer_language", content="解释商业发票时优先使用中文")
            assert preferences.preview_memory(term)["rejected_reason"] is None
            bad_condition = preferences.MemoryBody(category="presentation", content="先列风险", applies_when="仅用于这票发票")
            assert preferences.preview_memory(bad_condition)["rejected_field"] == "applies_when"
            body = preferences.MemoryBody(category="answer_language", content="中文")
            before = await preferences.list_memories(db, principal)
            suggestion = await preferences.propose_memory(db, principal, body)
            assert (await preferences.list_memories(db, principal))["memory_version"] == before["memory_version"]
            confirmed = await preferences.resolve_suggestion(db, principal, suggestion.id, str(uuid4()))
            assert confirmed.source == "suggestion"
            assert (await preferences.list_memories(db, principal))["memory_version"] == before["memory_version"]+1
            await preferences.edit_memory(db, principal, confirmed.id, preferences.DeleteMemoryBody(expected_version=1), str(uuid4()), delete=True)
            await db.rollback()
    finally:
        await engine.dispose()


def test_skill_import_shape_errors_and_semantic_defaults():
    import json
    from app.api.errors import InvalidRequest
    principal = Principal(uuid4(), uuid4(), "校验", ("run_start",), uuid4(), "")
    for patch in ({"allowed_actions": None}, {"allowed_actions": "query_case_facts"},
                  {"parameters": [{"name": {}, "type": "string"}]},
                  {"parameters": [{"name": "scope", "type": "enum", "options": "all"}]},
                  {"parameters": [{"name": "scope", "type": "string", "required": "false"}]}):
        with pytest.raises(InvalidRequest):
            extensions.preview_import(json.dumps(patch))
    definition = extensions.parse_skill({"name": "边界", "identifier": "bounds", "parameters": [
        {"name": "scope", "type": "enum", "options": ["all"], "default_value": "unknown"},
        {"name": "detailed", "type": "boolean", "default_value": "false"}]})
    errors = extensions.skill_errors(definition, principal)
    assert {error["field"] for error in errors} == {"parameters.0.default_value", "parameters.1.default_value"}
    definition["parameters"][0]["default_value"] = "all"
    definition["parameters"][1]["default_value"] = False
    assert extensions.skill_errors(definition, principal) == []
    preview = extensions.preview_import('{"scripts": ["do-not-run"], "instructions": "解释用途"}')
    assert any(row["verdict"] == "不支持脚本" for row in preview["diff"])


async def test_skill_enable_disable_invalidates_old_selection():
    from app.conversation.contracts import CapabilitySelection
    from app.api.errors import ApiError
    settings = get_settings()
    engine = create_database_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with factory() as db, tenant_transaction(db, TENANT_A):
            user = (await db.execute(select(User).where(User.account == "demo"))).scalar_one()
            principal = Principal(user.id, TENANT_A, user.display_name, tuple(user.permissions), uuid4(), "")
            skill, version = await extensions.create_skill(db, principal, {"name": "查看来源", "identifier": "sources_"+uuid4().hex,
                "allowed_actions": ["query_provenance"]}, str(uuid4()))
            assert skill.enabled_version is None
            await extensions.change_skill(db, principal, skill.id, "enable", str(uuid4()), version_no=1)
            assert extensions.skill_view(version)["status"] == "available"
            selected = CapabilitySelection(kind="user_skill", extension_id=skill.id, extension_version=1)
            assert (await extensions.validate_selections(db, principal, settings, [selected]))[0]["allowed_actions"] == ["query_provenance"]
            with pytest.raises(VersionConflict):
                await extensions.change_skill(db, principal, skill.id, "patch", str(uuid4()), version_no=1, body={"name": "改名"})
            await extensions.change_skill(db, principal, skill.id, "disable", str(uuid4()))
            with pytest.raises(ApiError) as error:
                await extensions.validate_selections(db, principal, settings, [selected])
            assert error.value.code == "CAPABILITY_STALE"
            await db.rollback()
    finally:
        await engine.dispose()


async def test_mcp_host_and_credential_fail_closed():
    settings = get_settings().model_copy(update={"mcp_allowed_hosts": "", "mcp_credential_master_key": None})
    with pytest.raises(McpFailure):
        await validate_endpoint("https://127.0.0.1/mcp", settings)
    principal = Principal(uuid4(), uuid4(), "测试", (), uuid4(), "")
    with pytest.raises(ServiceUnavailable):
        seal_credential(settings, principal, "secret")
    with pytest.raises(McpFailure):
        validate_arguments({"type": "object", "properties": {"repoName": {"type": "string"}}, "required": ["repoName"]},
            {"repoName": "public/repo", "case_text": "隐藏外发"})


async def test_pure_skill_freezes_parameters_and_rejects_multiple_actions():
    from app.conversation.contracts import CapabilitySelection
    from app.conversation.tools import validate_capabilities
    from app.api.errors import ApiError
    settings = get_settings()
    engine = create_database_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with factory() as db, tenant_transaction(db, TENANT_A):
            user = (await db.execute(select(User).where(User.account == "demo"))).scalar_one()
            principal = Principal(user.id, TENANT_A, user.display_name, tuple(user.permissions), uuid4(), "")
            skill, _ = await extensions.create_skill(db, principal, {"name": "解释结构", "identifier": "explain_"+uuid4().hex,
                "instructions": "按用途、风险两段解释", "result_format": "两段", "parameters": [{"name": "detailed", "type": "boolean", "required": True}]}, str(uuid4()))
            await extensions.change_skill(db, principal, skill.id, "enable", str(uuid4()), version_no=1)
            selected = CapabilitySelection(kind="user_skill", extension_id=skill.id, extension_version=1, user_parameters={"detailed": False})
            frozen = await validate_capabilities(db, principal, settings, [selected])
            assert frozen[0]["instructions"] == "按用途、风险两段解释"
            assert frozen[0]["result_format"] == "两段" and frozen[0]["user_parameters"]["detailed"] is False
            candidate = next(c for c in await extensions.available(db, principal, settings) if c["extension_id"] == str(skill.id))
            assert candidate["parameters"][0]["required"]
            actions = []
            for action in ("query_case_facts", "list_issues"):
                other, _ = await extensions.create_skill(db, principal, {"name": action, "identifier": "action_"+uuid4().hex, "allowed_actions": [action]}, str(uuid4()))
                await extensions.change_skill(db, principal, other.id, "enable", str(uuid4()), version_no=1)
                actions.append(CapabilitySelection(kind="user_skill", extension_id=other.id, extension_version=1))
            with pytest.raises(ApiError):
                await validate_capabilities(db, principal, settings, actions)
            from app.conversation.contracts import CreateConversationBody, TurnBody
            from app.conversation.graph import ConversationNodes, fresh_state
            from app.conversation.models import ReferenceSnapshot, TurnEvent
            from app.conversation.service import create_conversation, create_turn
            from app.extensions.models import ExtensionCall
            from sqlalchemy import func
            multi, _ = await extensions.create_skill(db, principal, {"name": "查看状态与复核", "identifier": "multi_"+uuid4().hex,
                "allowed_actions": ["get_run_timeline", "list_issues", "open_review"]}, str(uuid4()))
            await extensions.change_skill(db, principal, multi.id, "enable", str(uuid4()), version_no=1)
            conversation = await create_conversation(db, principal, settings, CreateConversationBody(), str(uuid4()))
            turn, _ = await create_turn(db, principal, settings, conversation.id, TurnBody(text="查看状态", capabilities=[
                CapabilitySelection(kind="user_skill", extension_id=multi.id, extension_version=1)]), str(uuid4()))
            from contextlib import asynccontextmanager
            class BoundNodes(ConversationNodes):
                @asynccontextmanager
                async def scope(self, state):
                    yield db, conversation, turn, await db.get(ReferenceSnapshot, turn.reference_snapshot_id), "查看状态"
            nodes = BoundNodes(None, principal, settings)
            state = fresh_state(conversation.id, turn.id)
            routed = await nodes.fast_path(state)
            result = await nodes.execute_tools({**state, **routed})
            replay = await nodes.execute_tools({**state, **routed})
            assert result["tool_result_ids"] == replay["tool_result_ids"] and result["tool_calls"] == 3
            assert (await db.execute(select(func.count()).select_from(ExtensionCall).where(ExtensionCall.parent_turn_id == turn.id))).scalar_one() == 3
            assert (await db.execute(select(func.count()).select_from(TurnEvent).where(TurnEvent.turn_id == turn.id))).scalar_one() == 6
            await db.rollback()
    finally:
        await engine.dispose()


def test_skill_combinations_share_published_execution_policy():
    from types import SimpleNamespace
    policy=extensions.SKILL_ACTION_POLICY
    principal=SimpleNamespace(permissions=("run_start",))
    def errors(actions):
        body={**extensions.parse_skill({"name":"组合","identifier":"combo"}),"allowed_actions":actions}
        return [value for value in extensions.skill_errors(body,principal) if value["field"]=="allowed_actions"]
    assert errors(policy["readonly_actions"][:policy["max_readonly_steps"]])==[]
    assert errors(policy["readonly_actions"])
    assert errors(["read_index"] )==[]
    assert errors(["read_index","translate_review"])
    assert errors(["read_index","query_case_facts"])


def extension_rows_fixture(monkeypatch, rows):
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from app.conversation import tools
    from app.extensions.models import Skill, McpConnection
    skills, connections, versions = [], [], {}
    now = datetime.now(UTC)
    for kind, state, version_state in rows:
        item = SimpleNamespace(id=uuid4(),status=state,next_version=1,enabled_version=1 if state=='enabled' else None,name=kind+state)
        version = SimpleNamespace(version=1,status=version_state,created_at=now,validated_at=now,tested_at=now,error_class=None,
            definition={'name':kind+state,'purpose':'fixture','allowed_actions':[]})
        (skills if kind=='user_skill' else connections).append(item)
        versions[item.id]=version
    class DB:
        async def execute(self, statement):
            values=skills if statement.column_descriptions[0]['entity'] is Skill else connections
            return SimpleNamespace(scalars=lambda:values)
    async def available(*args):
        return [{'kind':'builtin_skill','extension_id':str(uuid4()),'extension_version':1,'name':'builtin fixture',
            'connection_status':'可用','purpose':'fixture','permission':'只读'}]
    async def version(db,item,*args):return versions[item.id]
    async def tool_views(*args):return []
    monkeypatch.setattr(tools,'available_capabilities',available)
    monkeypatch.setattr(extensions,'skill_version',version)
    monkeypatch.setattr(extensions,'connection_version',version)
    monkeypatch.setattr(extensions,'tools_view',tool_views)
    return DB(),SimpleNamespace(user_id=uuid4(),display_name='fixture')


@pytest.mark.parametrize('kind',['user_skill','mcp'])
@pytest.mark.parametrize('state,version_state,status,row_state',[
    ('enabled','enabled','available','enabled'), ('draft','validated','available','ready_to_enable'),
    ('draft','draft','draft','draft'), ('draft','validating','validating','validating'),
    ('disabled','enabled','disabled','disabled'), ('archived','enabled','archived','archived'),
    ('draft','validation_failed','failed','validation_failed'), ('connect_failed','connect_failed','failed','connect_failed'),
    ('needs_recheck','validated','failed','needs_recheck'), ('needs_retest','draft','failed','needs_retest'),
    ('syncing','enabled','available','syncing'),
])
async def test_extension_rows_public_status_validates_dto(monkeypatch,kind,state,version_state,status,row_state):
    from app.api.response_dto import ExtensionListView
    from app.config import Settings
    db,p=extension_rows_fixture(monkeypatch,[(kind,state,version_state)])
    rows=await extensions.extension_rows(db,p,Settings(_env_file=None),kind=kind,row_state=row_state)
    response=ExtensionListView.model_validate({'items':rows,'warnings':[]})
    assert len(response.items)==1 and response.items[0].status==status and response.items[0].row_state==row_state


async def test_extension_rows_multiple_kinds_states_and_archive(monkeypatch):
    from app.api.response_dto import ExtensionListView
    from app.config import Settings
    db,p=extension_rows_fixture(monkeypatch,[('user_skill','enabled','enabled'),('mcp','enabled','enabled'),
        ('mcp','syncing','enabled'),('user_skill','archived','enabled')])
    settings=Settings(_env_file=None)
    rows=await extensions.extension_rows(db,p,settings,kind='builtin_skill,user_skill',row_state='enabled,syncing')
    assert {row['kind'] for row in rows}=={'builtin_skill','user_skill'} and len(rows)==2
    assert len((await extensions.extension_rows(db,p,settings,kind='mcp',row_state='enabled,syncing')))==2
    assert not any(row['row_state']=='archived' for row in await extensions.extension_rows(db,p,settings))
    rows=await extensions.extension_rows(db,p,settings,kind='user_skill',row_state='enabled,archived')
    assert {row['row_state'] for row in rows}=={'enabled','archived'}
    ExtensionListView.model_validate({'items':rows,'warnings':[]})


@pytest.mark.parametrize('query',[{'kind':'mcp_tool'},{'kind':'user_skill,unknown'},{'kind':''},{'kind':'user_skill,'},
    {'row_state':'available'},{'row_state':'enabled,unknown'},{'row_state':''},{'row_state':'enabled,'}])
async def test_extension_rows_invalid_filter_is_422(monkeypatch,query):
    from app.api.errors import InvalidRequest
    from app.config import Settings
    db,p=extension_rows_fixture(monkeypatch,[])
    with pytest.raises(InvalidRequest) as error:await extensions.extension_rows(db,p,Settings(_env_file=None),**query)
    assert error.value.status_code==422


async def test_real_skill_archive_is_terminal_and_idempotent():
    from sqlalchemy import func, text
    from sqlalchemy.ext.asyncio import AsyncSession
    from app.api.errors import ApiError, InvalidRequest
    from app.conversation.contracts import CapabilitySelection
    from app.documents.models import IdempotencyRecord
    from app.extensions.models import Skill, SkillVersion
    settings=get_settings();engine=create_database_engine(settings)
    skill_id=None
    try:
        async with engine.connect() as conn:
            outer=await conn.begin()
            db=AsyncSession(bind=conn,expire_on_commit=False,join_transaction_mode='create_savepoint')
            try:
                user=(await db.execute(select(User).where(User.account=='demo'))).scalar_one()
                tenant_id=user.tenant_id
                await db.execute(text(f"SET LOCAL app.tenant_id = '{tenant_id}'"));await db.execute(text('SET LOCAL ROLE logistics_app'))
                principal=Principal(user.id,tenant_id,user.display_name,tuple(user.permissions),uuid4(),'')
                skill,version=await extensions.create_skill(db,principal,{'name':'归档回滚','identifier':'archive_'+uuid4().hex,
                    'allowed_actions':['get_run_timeline']},str(uuid4()))
                skill_id=skill.id
                enable_key=str(uuid4())
                await extensions.change_skill(db,principal,skill.id,'enable',enable_key,version_no=1)
                archive_key=str(uuid4())
                await extensions.change_skill(db,principal,skill.id,'archive',archive_key)
                before=(await db.execute(select(func.count()).select_from(IdempotencyRecord).where(
                    IdempotencyRecord.scope.like(f'%/skills/{skill.id}/%')))).scalar_one()
                await extensions.change_skill(db,principal,skill.id,'archive',archive_key)
                # 已成功enable的幂等重放只返回旧结果，不重做状态修改。
                await extensions.change_skill(db,principal,skill.id,'enable',enable_key,version_no=1)
                assert skill.status=='archived' and skill.enabled_version is None
                assert (await db.execute(select(func.count()).select_from(IdempotencyRecord).where(
                    IdempotencyRecord.scope.like(f'%/skills/{skill.id}/%')))).scalar_one()==before
                for command in ('new','patch','validate','enable','disable'):
                    with pytest.raises(InvalidRequest) as error:
                        async with db.begin_nested():
                            await extensions.change_skill(db,principal,skill.id,command,str(uuid4()),
                                version_no=1 if command in {'patch','validate','enable'} else None,body={'name':'refused'} if command=='patch' else None)
                    assert error.value.status_code==422
                    current=await extensions.load_skill(db,principal,skill_id,write=True)
                    assert current.status=='archived' and current.enabled_version is None and current.next_version==1
                selection=CapabilitySelection(kind='user_skill',extension_id=skill.id,extension_version=1)
                with pytest.raises(ApiError) as error:await extensions.validate_selections(db,principal,settings,[selection],write=True)
                assert error.value.code=='CAPABILITY_STALE'
                assert (await db.execute(select(func.count()).select_from(SkillVersion).where(SkillVersion.skill_id==skill_id))).scalar_one()==1
            finally:
                await db.close();await outer.rollback()
            async with conn.begin():
                assert (await conn.execute(select(func.count()).select_from(Skill).where(Skill.id==skill_id))).scalar_one()==0
    finally:await engine.dispose()


async def test_real_skill_stale_cache_refreshes_commands_validation_and_version():
    from sqlalchemy import func, text
    from sqlalchemy.ext.asyncio import AsyncSession
    from app.api.errors import ApiError, InvalidRequest
    from app.conversation.contracts import CapabilitySelection
    from app.extensions.models import Skill
    settings=get_settings();engine=create_database_engine(settings)
    skill_id=None
    try:
        async with engine.connect() as conn:
            outer=await conn.begin()
            first=AsyncSession(bind=conn,expire_on_commit=False,join_transaction_mode='create_savepoint')
            second=AsyncSession(bind=conn,expire_on_commit=False,join_transaction_mode='create_savepoint')
            validator=AsyncSession(bind=conn,expire_on_commit=False,join_transaction_mode='create_savepoint')
            try:
                user=(await first.execute(select(User).where(User.account=='demo'))).scalar_one()
                await first.execute(text(f"SET LOCAL app.tenant_id = '{user.tenant_id}'"));await first.execute(text('SET LOCAL ROLE logistics_app'))
                principal=Principal(user.id,user.tenant_id,user.display_name,tuple(user.permissions),uuid4(),'')
                skill,version=await extensions.create_skill(first,principal,{'name':'缓存回滚','identifier':'cache_'+uuid4().hex,
                    'allowed_actions':['get_run_timeline']},str(uuid4()))
                skill_id=skill.id
                selection=CapabilitySelection(kind='user_skill',extension_id=skill.id,extension_version=1)
                # 未启用草稿也必须在写锁后刷新edit_version，阻止旧CAS覆盖另一Session修改。
                await first.commit()
                await extensions.change_skill(second,principal,skill_id,'patch',str(uuid4()),version_no=1,body={'expected_version':1,'purpose':'new'})
                await second.commit()
                assert version.edit_version==1
                with pytest.raises(VersionConflict):
                    await extensions.change_skill(first,principal,skill_id,'patch',str(uuid4()),version_no=1,body={'expected_version':1,'purpose':'old'})
                assert version.edit_version==2 and version.definition['purpose']=='new'
                await first.rollback()
                await extensions.change_skill(first,principal,skill_id,'enable',str(uuid4()),version_no=1);await first.commit()
                # 独立Session先缓存enabled，再由另一Session归档。
                await extensions.validate_selections(first,principal,settings,[selection]);await first.commit()
                cached=await extensions.load_skill(validator,principal,skill_id)
                await validator.commit()
                await extensions.change_skill(second,principal,skill_id,'archive',str(uuid4()));await second.commit()
                assert skill.status=='enabled' and skill.enabled_version==1
                with pytest.raises(InvalidRequest):await extensions.change_skill(first,principal,skill_id,'enable',str(uuid4()),version_no=1)
                assert skill.status=='archived' and skill.enabled_version is None
                await first.rollback()
                assert cached.status=='enabled' and cached.enabled_version==1
                with pytest.raises(ApiError) as error:await extensions.validate_selections(validator,principal,settings,[selection],write=True)
                assert error.value.code=='CAPABILITY_STALE'
                assert cached.status=='archived' and cached.enabled_version is None
            finally:
                await validator.close();await second.close();await first.close();await outer.rollback()
            async with conn.begin():
                assert (await conn.execute(select(func.count()).select_from(Skill).where(Skill.id==skill_id))).scalar_one()==0
    finally:await engine.dispose()
