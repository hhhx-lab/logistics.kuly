from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select

from app.api import idempotency
from app.api.errors import ApiError, InvalidRequest, NotFoundOrForbidden, VersionConflict
from app.api.idempotency import request_digest
from app.conversation.tools import BUILTINS
from app.extensions.mcp import McpFailure, StreamableHttpClient, master_key, open_credential, public_tool_policy, public_tool_registry, seal_credential, tool_category, validate_arguments, validate_endpoint, validate_public_arguments
from app.extensions.models import CredentialEnvelope, ExtensionCall, McpChallenge, McpConnection, McpConnectionTest, McpConnectionVersion, McpToolGrant, McpToolSnapshot, Skill, SkillVersion

ACTIONS = {"query_case_facts": ("查看单证字段", "read_only"), "query_provenance": ("查看字段来源", "read_only"),
    "get_consignment_overview": ("查看同票概览", "read_only"), "list_comparison_differences": ("查看同票差异", "read_only"),
    "get_comparison_evidence": ("查看同票比较证据", "read_only"), "consignment_check": ("同票单证核对", "controlled_write"),
    "get_run_timeline": ("查询任务进度", "read_only"), "list_issues": ("查看疑点", "read_only"),
    "open_review": ("打开复核入口", "read_only"), "read_index": ("只读识别", "controlled_write"),
    "translate_review": ("翻译复核", "controlled_write")}
SKILL_ACTION_POLICY = {
    "readonly_actions": [action for action, (_, group) in ACTIONS.items() if group == "read_only"],
    "max_readonly_steps": 4,
    "max_business_steps": 1,
    "allow_mixed": False,
}



class SkillBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = Field(min_length=1, max_length=160)
    identifier: str = Field(min_length=1, max_length=80)
    purpose: str = Field(default="", max_length=1000)
    when_to_use: str = Field(default="", max_length=1000)
    when_not_to_use: str = Field(default="", max_length=1000)
    parameters: list[dict] = Field(default_factory=list, max_length=20)
    allowed_actions: list[str] = Field(default_factory=list, max_length=4)
    result_format: str = Field(default="", max_length=1000)
    instructions: str = Field(default="", max_length=6000)

    @field_validator("parameters")
    @classmethod
    def parameter_shapes(cls, parameters):
        for parameter in parameters:
            for key in ("name", "type", "description"):
                if key in parameter and not isinstance(parameter[key], str):
                    raise ValueError("参数名称、类型和说明必须是文本")
            if "required" in parameter and type(parameter["required"]) is not bool:
                raise ValueError("参数必填标记必须是布尔值")
            options = parameter.get("options")
            if options is not None and (not isinstance(options, list) or len(options) > 64 or
                    any(not isinstance(value, str) or len(value) > 200 for value in options)):
                raise ValueError("枚举候选必须是至多64个短文本")
            default = parameter.get("default_value")
            if default is not None and type(default) not in {str, bool}:
                raise ValueError("默认值必须是文本或布尔值")
        return parameters


def parse_skill(body):
    try:
        return SkillBody.model_validate(body).model_dump()
    except ValidationError as exc:
        raise InvalidRequest("Skill字段结构不合法", details={"fields": [
            {"field": ".".join(str(part) for part in item["loc"]), "message": item["msg"]}
            for item in exc.errors()]}) from exc


def stale(message="所选能力版本或授权已变化，请重新选择。"):
    return ApiError(message, code="CAPABILITY_STALE", status_code=409)


def skill_errors(definition, principal):
    errors = []
    if not re.fullmatch(r"[a-z][a-z0-9_-]{1,79}", definition["identifier"]):
        errors.append({"field": "identifier", "message": "标识使用小写字母、数字、下划线或连字符"})
    if definition["identifier"] in {"read_index", "translate_review", "consignment_check"}:
        errors.append({"field": "identifier", "message": "不能占用内置能力标识"})
    readonly = set(SKILL_ACTION_POLICY["readonly_actions"])
    declared = set(definition["allowed_actions"])
    if (declared.issubset(readonly) and len(declared) > SKILL_ACTION_POLICY["max_readonly_steps"]) or (not declared.issubset(readonly) and len(declared) > SKILL_ACTION_POLICY["max_business_steps"]):
        errors.append({"field":"allowed_actions", "message":"当前仅支持一个业务动作或最多四个只读动作；不支持多写入及读写混合编排"})
    for action in definition["allowed_actions"]:
        if action not in ACTIONS:
            errors.append({"field": "allowed_actions", "message": "含未注册业务动作：" + action})
        elif ACTIONS[action][1] == "controlled_write" and "run_start" not in principal.permissions:
            errors.append({"field": "allowed_actions", "message": "无权发起该业务动作"})
    names = set()
    for index, parameter in enumerate(definition["parameters"]):
        name = parameter.get("name", "")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,59}", name) or name in names:
            errors.append({"field": f"parameters.{index}.name", "message": "参数名不合法或重复"})
        else:
            names.add(name)
        kind = parameter.get("type")
        if kind not in ("string", "enum", "boolean"):
            errors.append({"field": f"parameters.{index}.type", "message": "参数类型不合法"})
        options = parameter.get("options")
        valid_options = isinstance(options, list) and bool(options) and all(isinstance(value, str) and value.strip() for value in options)
        if kind == "enum" and not valid_options:
            errors.append({"field": f"parameters.{index}.options", "message": "枚举参数需要非空文本候选值"})
        default = parameter.get("default_value")
        if default is not None and default != "":
            if ((kind == "boolean" and type(default) is not bool) or
                    (kind in ("string", "enum") and not isinstance(default, str)) or
                    (kind == "enum" and (not valid_options or default not in options))):
                errors.append({"field": f"parameters.{index}.default_value", "message": "默认值不符合参数类型或候选范围"})
    return errors


def preview_import(text, *, mcp=False):
    if len(text.encode()) > 32768:
        raise InvalidRequest("导入文本超出大小限制")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        raw = {"instructions": text} if not mcp else {}
    if not isinstance(raw, dict):
        raise InvalidRequest("说明必须是对象或文本")
    if mcp:
        draft = {k: raw[k] for k in ("name", "endpoint", "auth_type") if k in raw}
        return {"draft": draft, "diff": [{"item": "传输", "verdict": "可用" if raw.get("transport", "streamable_http") == "streamable_http" else "不支持的传输", "detail": "只接受 Streamable HTTP 连接；预览不访问网络"}]}
    draft = {k: v for k, v in raw.items() if k in SkillBody.model_fields}
    parse_skill({"name": "导入的Skill", "identifier": "import_draft", **draft})
    diff = [{"item": action, "verdict": "可用" if action in ACTIONS else "未知动作", "detail": "按服务端注册动作核对"} for action in raw.get("allowed_actions", [])]
    if any(k in raw for k in ("scripts", "command", "exec", "code")):
        diff.append({"item": "可执行内容", "verdict": "不支持脚本", "detail": "导入仅解析说明，不执行代码或命令"})
    diff.append({"item": "说明", "verdict": "低信任指令", "detail": "文字说明不会新增工具或修改业务门禁"})
    return {"draft": draft, "diff": diff}


async def load_skill(db, principal, skill_id, *, write=False):
    query = select(Skill).where(Skill.id == skill_id, Skill.owner_user_id == principal.user_id, Skill.tenant_id == principal.tenant_id)
    if write:
        query = query.with_for_update().execution_options(populate_existing=True)
    skill = (await db.execute(query)).scalar_one_or_none()
    if skill is None:
        raise NotFoundOrForbidden()
    return skill


async def skill_version(db, skill, version, *, write=False):
    query = select(SkillVersion).where(SkillVersion.skill_id == skill.id, SkillVersion.version == version)
    if write:
        query = query.with_for_update().execution_options(populate_existing=True)
    result = (await db.execute(query)).scalar_one_or_none()
    if result is None:
        raise NotFoundOrForbidden()
    return result


def skill_view(version):
    public_status = {"validated": "available", "enabled": "available", "validation_failed": "failed"}.get(version.status, version.status)
    return {**version.definition, "version": version.version, "expected_version": version.edit_version, "status": public_status,
        "validation_errors": version.validation_errors, "validated_at": version.validated_at, "ever_enabled": version.ever_enabled}


async def create_skill(db, principal, body, key):
    definition = parse_skill(body)
    if definition["identifier"] in {"read_index", "translate_review", "consignment_check"}:
        raise InvalidRequest("不能编辑或占用内置能力")
    outcome = await idempotency.begin(db, tenant_id=principal.tenant_id, actor_id=principal.user_id, scope="POST /skills", key=key, payload=definition)
    if outcome.replayed:
        skill = await load_skill(db, principal, UUID(outcome.record.result_ref["skill_id"]))
        return skill, await skill_version(db, skill, 1)
    exists = (await db.execute(select(Skill).where(Skill.owner_user_id == principal.user_id, Skill.identifier == definition["identifier"]))).scalar_one_or_none()
    if exists:
        raise InvalidRequest("该标识已存在")
    skill = Skill(tenant_id=principal.tenant_id, owner_user_id=principal.user_id, identifier=definition["identifier"], status="draft", next_version=1)
    db.add(skill)
    await db.flush()
    version = SkillVersion(tenant_id=principal.tenant_id, skill_id=skill.id, version=1, edit_version=1, status="draft", definition=definition,
        validation_errors=skill_errors(definition, principal), ever_enabled=False)
    db.add(version)
    await db.flush()
    idempotency.complete(outcome.record, {"skill_id": str(skill.id)})
    return skill, version


async def change_skill(db, principal, skill_id, command, key, *, version_no=None, body=None):
    skill = await load_skill(db, principal, skill_id, write=True)
    outcome = await idempotency.begin(db, tenant_id=principal.tenant_id, actor_id=principal.user_id,
        scope=f"{command} /skills/{skill_id}/{version_no or ''}", key=key, payload=body or {})
    if outcome.replayed:
        return skill, await skill_version(db, skill, outcome.record.result_ref.get("version", skill.next_version))
    if skill.status == "archived" and command != "archive":
        raise InvalidRequest("Skill已归档，不能修改或重新启用，请新建Skill")
    version = await skill_version(db, skill, version_no or skill.next_version, write=True)
    if command == "new":
        skill.next_version += 1
        version = SkillVersion(tenant_id=principal.tenant_id, skill_id=skill.id, version=skill.next_version, edit_version=1,
            status="draft", definition=dict(version.definition), validation_errors=[], ever_enabled=False)
        db.add(version)
    elif command == "patch":
        if version.ever_enabled:
            raise VersionConflict("曾启用的版本不可修改，请新建草稿")
        if body.get("expected_version", version.edit_version) != version.edit_version:
            raise VersionConflict(current=skill_view(version))
        definition = parse_skill({**version.definition, **body})
        if definition["identifier"] != skill.identifier:
            raise InvalidRequest("版本不能修改能力标识")
        version.definition, version.status, version.validated_at = definition, "draft", None
        version.edit_version += 1
        version.validation_errors = skill_errors(definition, principal)
    elif command in {"validate", "enable"}:
        version.validation_errors = skill_errors(version.definition, principal)
        version.validated_at = datetime.now(UTC)
        version.status = "validation_failed" if version.validation_errors else "validated"
        if command == "enable":
            if version.validation_errors:
                raise InvalidRequest("能力验证未通过", details={"fields": version.validation_errors})
            skill.enabled_version, skill.status = version.version, "enabled"
            version.status, version.ever_enabled = "enabled", True
    elif command in {"disable", "archive"}:
        skill.enabled_version = None
        skill.status = "archived" if command == "archive" else "disabled"
    await db.flush()
    idempotency.complete(outcome.record, {"skill_id": str(skill.id), "version": version.version})
    return skill, version


async def load_connection(db, principal, connection_id, *, write=False):
    query = select(McpConnection).where(McpConnection.id == connection_id, McpConnection.owner_user_id == principal.user_id,
        McpConnection.tenant_id == principal.tenant_id)
    if write:
        query = query.with_for_update().execution_options(populate_existing=True)
    connection = (await db.execute(query)).scalar_one_or_none()
    if connection is None:
        raise NotFoundOrForbidden()
    return connection


async def connection_version(db, connection, version, *, write=False):
    query = select(McpConnectionVersion).where(McpConnectionVersion.connection_id == connection.id, McpConnectionVersion.version == version)
    if write:
        query = query.with_for_update().execution_options(populate_existing=True)
    row = (await db.execute(query)).scalar_one_or_none()
    if row is None:
        raise NotFoundOrForbidden()
    return row


async def tools_view(db, principal, version, settings=None):
    if settings is None:
        from app.config import get_settings
        settings = get_settings()
    tools = list((await db.execute(select(McpToolSnapshot).where(McpToolSnapshot.connection_version_id == version.id))).scalars())
    grants = list((await db.execute(select(McpToolGrant).where(McpToolGrant.connection_version_id == version.id,
        McpToolGrant.subject_user_id == principal.user_id, McpToolGrant.revoked_at.is_(None)))).scalars())
    by_tool = {g.tool_id: g for g in grants}
    policies = {}
    for tool in tools:
        try:policies[tool.tool_id] = public_tool_policy(settings, version.endpoint, tool.tool_id, tool.schema_hash, tool.input_schema)
        except McpFailure:policies[tool.tool_id] = None
    return [{"tool_id": t.tool_id, "name": t.tool_id, "description": t.description, "schema_hash": t.schema_hash, "input_schema": t.input_schema,
        "category": t.category, "scope": "公开资料", "data_level": "public", "input_schema_summary": json.dumps(t.input_schema, ensure_ascii=False)[:1500],
        "previous_schema_hash": t.previous_schema_hash, "previous_schema_summary": json.dumps(t.previous_schema, ensure_ascii=False)[:1500] if t.previous_schema else None,
        "granted": t.tool_id in by_tool and by_tool[t.tool_id].schema_hash == t.schema_hash and by_tool[t.tool_id].data_level == "public" and by_tool[t.tool_id].scope in {"public_repository", "public_registry@1"} and t.category == "read_only" and policies[t.tool_id] is not None,
        "public_scope_ready": policies[t.tool_id] is not None, "public_parameters": policies[t.tool_id],
        "unavailable_reason": None if policies[t.tool_id] is not None else "维护者尚未发布有效的公开参数范围",
        "grant_id": str(by_tool[t.tool_id].id) if t.tool_id in by_tool else None, "schema_changed": t.schema_changed} for t in tools]


async def connection_view(db, principal, connection, version):
    from urllib.parse import urlsplit
    test = (await db.execute(select(McpConnectionTest).where(McpConnectionTest.connection_version_id == version.id)
        .order_by(McpConnectionTest.created_at.desc()).limit(1))).scalar_one_or_none()
    public_status = {"validated": "available", "enabled": "available", "connect_failed": "failed"}.get(version.status, version.status)
    return {"version": version.version, "expected_version": version.edit_version, "status": public_status, "name": connection.name,
        "endpoint": version.endpoint, "endpoint_summary": "https://" + str(urlsplit(version.endpoint).hostname),
        "template": "streamable_http", "template_label": "Streamable HTTP", "auth_type": version.auth_type,
        "credential_set": version.credential_envelope_id is not None, "tested_at": version.tested_at,
        "test_stages": test.stages if test else [], "error_class": version.error_class, "tools": await tools_view(db, principal, version)}


async def credential_for(db, principal, settings, version):
    if not version.credential_envelope_id:
        return None
    envelope = await db.get(CredentialEnvelope, version.credential_envelope_id)
    if envelope is None or envelope.owner_user_id != principal.user_id or envelope.tenant_id != principal.tenant_id:
        raise McpFailure("credential_unavailable")
    return open_credential(settings, principal, envelope)


async def create_connection(db, principal, settings, body, key):
    name, endpoint, auth_type = body.get("name", ""), body.get("endpoint", ""), body.get("auth_type", "none")
    if not name or len(name) > 160 or auth_type not in {"none", "bearer"}:
        raise InvalidRequest("连接名称或认证方式不合法")
    try:
        endpoint = await validate_endpoint(endpoint, settings)
    except McpFailure as exc:
        raise InvalidRequest("连接端点未通过主机与地址校验", details={"error_class": exc.kind}) from exc
    # 请求摘要不保存明文密钥，密钥仅交给信封加密。
    payload = {"name": name, "endpoint": endpoint, "auth_type": auth_type,
        "credential_hash": request_digest(body.get("credential")) if body.get("credential") else None}
    outcome = await idempotency.begin(db, tenant_id=principal.tenant_id, actor_id=principal.user_id, scope="POST /mcp-connections", key=key, payload=payload)
    if outcome.replayed:
        connection = await load_connection(db, principal, UUID(outcome.record.result_ref["connection_id"]))
        return connection, await connection_version(db, connection, 1)
    credential_id = None
    if auth_type == "bearer":
        if not body.get("credential"):
            raise InvalidRequest("Bearer 认证需要凭证")
        envelope = seal_credential(settings, principal, body["credential"])
        db.add(envelope)
        await db.flush()
        credential_id = envelope.id
    elif body.get("credential"):
        raise InvalidRequest("无认证连接不接收凭证")
    connection = McpConnection(tenant_id=principal.tenant_id, owner_user_id=principal.user_id, name=name, status="draft", next_version=1)
    db.add(connection)
    await db.flush()
    version = McpConnectionVersion(tenant_id=principal.tenant_id, connection_id=connection.id, version=1, edit_version=1,
        status="draft", endpoint=endpoint, auth_type=auth_type, credential_envelope_id=credential_id, ever_enabled=False)
    db.add(version)
    await db.flush()
    idempotency.complete(outcome.record, {"connection_id": str(connection.id)})
    return connection, version


async def discover_connection(db, principal, settings, connection, version):
    client = None
    error = None
    try:
        public_tool_registry(settings)
        client = StreamableHttpClient(version.endpoint, settings, await credential_for(db, principal, settings, version))
        tools = await client.discover()
        existing = {t.tool_id: t for t in (await db.execute(select(McpToolSnapshot).where(McpToolSnapshot.connection_version_id == version.id))).scalars()}
        for tool in tools:
            schema = tool["inputSchema"]
            digest = request_digest(schema)
            row = existing.pop(tool["name"], None)
            previous_category = row.category if row is not None else None
            if row is None:
                row = McpToolSnapshot(tenant_id=principal.tenant_id, connection_version_id=version.id, tool_id=tool["name"],
                    description=tool.get("description", "")[:4000], input_schema=schema, schema_hash=digest, category=tool_category(tool, version.endpoint, settings), schema_changed=False)
                db.add(row)
            elif row.schema_hash != digest:
                row.previous_schema_hash, row.previous_schema = row.schema_hash, row.input_schema
                row.input_schema, row.schema_hash, row.schema_changed = schema, digest, True
                connection.enabled_version, connection.status = None, "needs_recheck"
                for grant in (await db.execute(select(McpToolGrant).where(McpToolGrant.connection_version_id == version.id, McpToolGrant.tool_id == row.tool_id,
                    McpToolGrant.revoked_at.is_(None)))).scalars():
                    grant.revoked_at = datetime.now(UTC)
            row.category = tool_category(tool, version.endpoint, settings)
            if previous_category == "read_only" and row.category != "read_only":
                connection.enabled_version, connection.status = None, "needs_recheck"
                for grant in (await db.execute(select(McpToolGrant).where(McpToolGrant.connection_version_id == version.id,
                    McpToolGrant.tool_id == row.tool_id, McpToolGrant.revoked_at.is_(None)))).scalars():
                    grant.revoked_at = datetime.now(UTC)
        for row in existing.values():
            row.schema_changed = True
            connection.enabled_version, connection.status = None, "needs_recheck"
        version.tested_at = datetime.now(UTC)
        version.error_class = None
        version.status = "validated"
    except McpFailure as exc:
        error = exc.kind
        version.error_class, version.status = error, "connect_failed"
        connection.enabled_version, connection.status = None, "connect_failed"
    db.add(McpConnectionTest(tenant_id=principal.tenant_id, connection_version_id=version.id, stages=client.stages if client else [], success=error is None, error_class=error))
    await db.flush()
    return error


async def delete_unreferenced_credentials(db, principal, credential_ids):
    if not credential_ids:
        return
    await db.flush()
    envelopes = (await db.execute(select(CredentialEnvelope).where(
        CredentialEnvelope.id.in_(credential_ids), CredentialEnvelope.tenant_id == principal.tenant_id,
        CredentialEnvelope.owner_user_id == principal.user_id).order_by(CredentialEnvelope.id).with_for_update())).scalars()
    for envelope in envelopes:
        referenced = (await db.execute(select(McpConnectionVersion.id).where(
            McpConnectionVersion.credential_envelope_id == envelope.id).limit(1))).scalar_one_or_none()
        if referenced is None:
            await db.delete(envelope)


async def change_connection(db, principal, settings, connection_id, command, key, *, version_no=None, body=None):
    connection = await load_connection(db, principal, connection_id, write=True)
    digest_body = dict(body or {})
    if digest_body.get("credential"):
        digest_body["credential"] = request_digest(digest_body["credential"])
    outcome = await idempotency.begin(db, tenant_id=principal.tenant_id, actor_id=principal.user_id,
        scope=f"{command} /mcp-connections/{connection_id}/{version_no or ''}", key=key, payload=digest_body)
    if outcome.replayed:
        return connection, await connection_version(db, connection, outcome.record.result_ref.get("version", connection.next_version))
    if connection.status == "archived" and command != "archive":
        raise InvalidRequest("连接已归档，不能修改或重新启用，请新建连接")
    version = await connection_version(db, connection, version_no or connection.next_version, write=True)
    detached_credentials = set()
    if command == "new":
        connection.next_version += 1
        version = McpConnectionVersion(tenant_id=principal.tenant_id, connection_id=connection.id, version=connection.next_version, edit_version=1,
            endpoint=version.endpoint, auth_type=version.auth_type, credential_envelope_id=version.credential_envelope_id, status="draft", ever_enabled=False)
        db.add(version)
    elif command == "patch":
        if version.ever_enabled:
            raise VersionConflict("曾启用版本不可修改，请新建连接版本")
        if body.get("expected_version", version.edit_version) != version.edit_version:
            raise VersionConflict(current=await connection_view(db, principal, connection, version))
        try:
            version.endpoint = await validate_endpoint(body.get("endpoint", version.endpoint), settings)
        except McpFailure as exc:
            raise InvalidRequest("端点未通过地址校验", details={"error_class": exc.kind}) from exc
        auth = body.get("auth_type", version.auth_type)
        if auth not in {"none", "bearer"}:
            raise InvalidRequest("认证方式不支持")
        if auth == "none" and body.get("credential"):
            raise InvalidRequest("无认证连接不接收凭证")
        if version.credential_envelope_id and (body.get("credential") or auth == "none"):
            detached_credentials.add(version.credential_envelope_id)
        if body.get("credential"):
            envelope = seal_credential(settings, principal, body["credential"])
            db.add(envelope)
            await db.flush()
            version.credential_envelope_id = envelope.id
        if auth == "none":
            version.credential_envelope_id = None
        elif not version.credential_envelope_id:
            raise InvalidRequest("Bearer 认证需要配置凭证")
        version.auth_type, version.status, version.tested_at = auth, "draft", None
        version.edit_version += 1
        connection.enabled_version, connection.status = None, "needs_retest"
    elif command == "test":
        await discover_connection(db, principal, settings, connection, version)
    elif command == "grant":
        wanted = set(body.get("tool_ids", []))
        for grant in (await db.execute(select(McpToolGrant).where(McpToolGrant.connection_version_id == version.id,
            McpToolGrant.subject_user_id == principal.user_id, McpToolGrant.revoked_at.is_(None)))).scalars():
            if grant.tool_id not in wanted:
                grant.revoked_at = datetime.now(UTC)
        for tool_id in wanted:
            tool = (await db.execute(select(McpToolSnapshot).where(McpToolSnapshot.connection_version_id == version.id,
                McpToolSnapshot.tool_id == tool_id))).scalar_one_or_none()
            try:published = tool is not None and public_tool_policy(settings, version.endpoint, tool.tool_id, tool.schema_hash, tool.input_schema) is not None
            except McpFailure:published = False
            if tool is None or tool.category != "read_only" or not version.tested_at or not published:
                raise InvalidRequest("只能授权已测试且已发布公开参数范围的只读工具")
            old = (await db.execute(select(McpToolGrant).where(McpToolGrant.connection_version_id == version.id,
                McpToolGrant.tool_id == tool_id, McpToolGrant.subject_user_id == principal.user_id, McpToolGrant.revoked_at.is_(None)))).scalar_one_or_none()
            if old is None or old.schema_hash != tool.schema_hash or old.data_level != "public" or old.scope not in {"public_repository", "public_registry@1"}:
                if old:
                    old.revoked_at = datetime.now(UTC)
                db.add(McpToolGrant(tenant_id=principal.tenant_id, connection_version_id=version.id, tool_id=tool_id,
                    subject_user_id=principal.user_id, schema_hash=tool.schema_hash, scope="public_registry@1", data_level="public"))
            tool.schema_changed = False
    elif command == "enable":
        error = await discover_connection(db, principal, settings, connection, version)
        grants = await tools_view(db, principal, version, settings)
        if error or not any(t["granted"] and not t["schema_changed"] and t["category"] == "read_only" for t in grants):
            connection.status = "needs_recheck"
        else:
            connection.enabled_version, connection.status = version.version, "enabled"
            version.status, version.ever_enabled = "enabled", True
    elif command in {"disable", "archive"}:
        connection.enabled_version = None
        connection.status = "archived" if command == "archive" else "disabled"
        if command == "archive":
            versions = list((await db.execute(select(McpConnectionVersion).where(
                McpConnectionVersion.connection_id == connection.id).order_by(McpConnectionVersion.id).with_for_update())).scalars())
            for row in versions:
                if row.credential_envelope_id:
                    detached_credentials.add(row.credential_envelope_id)
                    row.credential_envelope_id = None
                row.status = "archived"
            for grant in (await db.execute(select(McpToolGrant).where(
                McpToolGrant.connection_version_id.in_([row.id for row in versions]),
                McpToolGrant.revoked_at.is_(None)).with_for_update())).scalars():
                grant.revoked_at = datetime.now(UTC)
    await delete_unreferenced_credentials(db, principal, detached_credentials)
    await db.flush()
    idempotency.complete(outcome.record, {"connection_id": str(connection.id), "version": version.version})
    return connection, version


def public_registry_warnings(settings):
    try:public_tool_registry(settings)
    except McpFailure:
        return [{"code": "MCP_PUBLIC_REGISTRY_INVALID", "message": "MCP公开工具注册配置无效，MCP调用禁用；Skill仍可使用"}]
    return []


async def available(db, principal, settings, q=""):
    items = []
    skills = list((await db.execute(select(Skill).where(Skill.owner_user_id == principal.user_id,
        Skill.status == "enabled", Skill.enabled_version.is_not(None)))).scalars())
    for skill in skills:
        version = await skill_version(db, skill, skill.enabled_version)
        if q and q.lower() not in (version.definition["name"]+version.definition["purpose"]).lower():
            continue
        items.append({"kind": "user_skill", "extension_id": str(skill.id), "extension_version": version.version,
            "name": version.definition["name"], "purpose": version.definition["purpose"], "permission": "受控写入" if any(a in {"read_index", "translate_review", "consignment_check"} for a in version.definition["allowed_actions"]) else "只读",
            "connection_status": "可用", "group": "已启用 Skill", "parameters": version.definition["parameters"]})
    if public_registry_warnings(settings):
        return items
    connections = list((await db.execute(select(McpConnection).where(McpConnection.owner_user_id == principal.user_id,
        McpConnection.status == "enabled", McpConnection.enabled_version.is_not(None)))).scalars())
    for connection in connections:
        version = await connection_version(db, connection, connection.enabled_version)
        for tool in await tools_view(db, principal, version, settings):
            if not tool["granted"] or tool["schema_changed"] or (q and q.lower() not in (connection.name+tool["name"]).lower()):
                continue
            items.append({"kind": "mcp_tool", "extension_id": str(connection.id), "extension_version": version.version,
                "tool_id": tool["tool_id"], "tool_schema_hash": tool["schema_hash"], "name": connection.name+" · "+tool["name"],
                "purpose": tool["description"], "permission": "只读", "connection_status": "可用", "group": "可用 MCP 工具",
                "parameters": [{"name": name, "type": "boolean" if spec.get("type") == "boolean" else "enum",
                    "options": tool["public_parameters"][name], "required": True,
                    "description": spec.get("description", ""), "default_value": None}
                    for name, spec in tool["input_schema"].get("properties", {}).items()]})
    return items


async def validate_selections(db, principal, settings, selections, *, write=False):
    frozen = []
    for selection in selections:
        if selection.kind == "user_skill":
            skill = await load_skill(db, principal, selection.extension_id, write=write)
            if skill.status != "enabled" or skill.enabled_version != selection.extension_version:
                raise stale()
            version = await skill_version(db, skill, selection.extension_version, write=write)
            if skill_errors(version.definition, principal):
                raise stale()
            values = dict(selection.user_parameters or {})
            properties = {p["name"]: p for p in version.definition["parameters"]}
            if any(name not in properties for name in values):
                raise stale("参数未在所选 Skill schema 中注册")
            for name, parameter in properties.items():
                if name not in values and parameter.get("default_value") not in (None, ""):
                    values[name] = parameter["default_value"]
                if parameter.get("required") and name not in values:
                    raise stale("所选 Skill 缺少必需参数")
                if name in values:
                    value = values[name]
                    if parameter["type"] == "boolean" and not isinstance(value, bool):
                        raise stale()
                    if parameter["type"] in {"string", "enum"} and not isinstance(value, str):
                        raise stale()
                    if parameter["type"] == "enum" and value not in parameter["options"]:
                        raise stale()
            actions = version.definition["allowed_actions"]
            workflow = next((a for a in actions if a in {"read_index", "translate_review", "consignment_check"}), None)
            frozen.append({**selection.model_dump(mode="json"), "user_parameters": values, "allowed_actions": actions,
                "workflow_type": workflow, "instructions": version.definition["instructions"],
                "name": version.definition["name"], "result_format": version.definition["result_format"]})
        elif selection.kind == "mcp_tool":
            connection = await load_connection(db, principal, selection.extension_id, write=write)
            if connection.status != "enabled" or connection.enabled_version != selection.extension_version:
                raise stale()
            version = await connection_version(db, connection, selection.extension_version, write=write)
            tool_query = select(McpToolSnapshot).where(McpToolSnapshot.connection_version_id == version.id,
                McpToolSnapshot.tool_id == selection.tool_id)
            grant_query = select(McpToolGrant).where(McpToolGrant.connection_version_id == version.id,
                McpToolGrant.tool_id == selection.tool_id, McpToolGrant.subject_user_id == principal.user_id,
                McpToolGrant.revoked_at.is_(None))
            if write:
                tool_query = tool_query.with_for_update().execution_options(populate_existing=True)
                grant_query = grant_query.with_for_update().execution_options(populate_existing=True)
            tool = (await db.execute(tool_query)).scalar_one_or_none()
            grant = (await db.execute(grant_query)).scalar_one_or_none()
            if tool is None or grant is None or tool.schema_changed or tool.category != "read_only" or tool.schema_hash != selection.tool_schema_hash or grant.schema_hash != tool.schema_hash or grant.data_level != "public" or grant.scope not in {"public_repository", "public_registry@1"}:
                raise stale()
            try:
                validate_public_arguments(settings, version.endpoint, tool.tool_id, tool.schema_hash, tool.input_schema, selection.user_parameters or {})
            except McpFailure as exc:
                raise stale("工具参数或公开范围不符合已发布配置") from exc
            frozen.append({**selection.model_dump(mode="json"), "connection_version_id": str(version.id), "allowed_actions": [], "workflow_type": None})
        else:
            raise stale()
    return frozen


async def call_selected_mcp(db, principal, settings, selection, *, conversation_id=None, parent_turn_id=None):
    from app.extensions.audit import safe_destination, safe_parameter_summary
    started_at = datetime.now(UTC)
    from app.conversation.contracts import CapabilitySelection
    selected = CapabilitySelection.model_validate({k: selection[k] for k in CapabilitySelection.model_fields if k in selection})
    await validate_selections(db, principal, settings, [selected], write=True)
    connection = await load_connection(db, principal, selected.extension_id, write=True)
    version = await connection_version(db, connection, selected.extension_version, write=True)
    outcome, error = None, None
    try:
        client = StreamableHttpClient(version.endpoint, settings, await credential_for(db, principal, settings, version))
        tools = await client.discover()
        tool = next((t for t in tools if t["name"] == selected.tool_id), None)
        if tool is None or request_digest(tool["inputSchema"]) != selected.tool_schema_hash:
            connection.status, connection.enabled_version = "needs_recheck", None
            raise McpFailure("schema_changed")
        arguments = selected.user_parameters or {}
        if tool_category(tool, version.endpoint, settings) != "read_only":
            connection.status, connection.enabled_version = "needs_recheck", None
            raise McpFailure("data_scope_not_supported")
        validate_public_arguments(settings, version.endpoint, selected.tool_id, selected.tool_schema_hash, tool["inputSchema"], arguments)
        outcome = await client.call(selected.tool_id, arguments)
    except McpFailure as exc:
        error = exc.kind
        protocol_error = error in {"schema_changed", "invalid_schema", "unsupported_schema", "invalid_tools", "invalid_tool_name", "invalid_initialize", "invalid_response", "response_too_large", "JSONDecodeError", "UnicodeDecodeError"}
        http_error_type = getattr(httpx, error, None)
        connection_error = error.startswith("http_") or error in {"credential_unavailable", "unsafe_endpoint", "host_not_allowed", "dns_failed", "unsafe_ip", "redirect_rejected"} or (
            isinstance(http_error_type, type) and issubclass(http_error_type, httpx.HTTPError))
        changed_scope = error == "data_scope_not_supported" and connection.status == "needs_recheck"
        if protocol_error or connection_error or changed_scope:
            connection.enabled_version = None
            connection.status = "needs_recheck" if protocol_error or changed_scope else "connect_failed"
            version.status, version.error_class = "connect_failed", error
    call = ExtensionCall(tenant_id=principal.tenant_id, extension_id=connection.id, owner_user_id=principal.user_id,
        extension_version=version.version, conversation_id=conversation_id, parent_turn_id=parent_turn_id,
        name=connection.name, action=selected.tool_id, status="failed" if error else "succeeded",
        result_redacted="工具调用失败" if error else "已发布范围的公开资料已返回；结果仅为外部参考资料", error_class=error,
        tool_schema_hash=selected.tool_schema_hash,
        params_redacted=safe_parameter_summary(selected.user_parameters), destination=safe_destination(version.endpoint),
        started_at=started_at, finished_at=datetime.now(UTC), duration_ms=max(0, int((datetime.now(UTC)-started_at).total_seconds()*1000)))
    db.add(call)
    await db.flush()
    return outcome, error, call


async def extension_rows(db, principal, settings, *, kind=None, row_state=None, q=""):
    from app.conversation.tools import available_capabilities
    def filters(value, allowed, name):
        if value is None:
            return None
        selected = {part.strip() for part in value.split(",")}
        if not selected or not selected.issubset(allowed):
            raise InvalidRequest(f"{name}筛选值不合法")
        return selected
    kinds = filters(kind, {"builtin_skill", "user_skill", "mcp"}, "kind")
    states = filters(row_state, {"enabled", "ready_to_enable", "draft", "validating", "disabled", "archived",
        "validation_failed", "connect_failed", "needs_retest", "needs_recheck", "syncing"}, "row_state")
    public_status = {"enabled": "available", "ready_to_enable": "available", "syncing": "available",
        "validation_failed": "failed", "connect_failed": "failed", "needs_retest": "failed", "needs_recheck": "failed"}
    rows = []
    for builtin in await available_capabilities(db, principal, settings, q):
        if builtin["kind"] != "builtin_skill":
            continue
        ready = builtin["connection_status"] == "可用"
        version = builtin["extension_version"]
        rows.append({"extension_id": builtin["extension_id"], "kind": "builtin_skill", "name": builtin["name"], "status": "available" if ready else "failed",
            "row_state": "enabled" if ready else "connect_failed", "version": version, "enabled_version": version if ready else None,
            "updated_at": datetime.now(UTC), "summary": builtin["purpose"], "error_class": builtin.get("unavailable_reason"),
            "source_label": "项目注册内置流程", "permission": builtin["permission"], "owner": "系统", "read_only": True})
    for skill in (await db.execute(select(Skill).where(Skill.owner_user_id == principal.user_id))).scalars():
        version = await skill_version(db, skill, skill.next_version)
        state = skill.status if skill.status != "draft" else "ready_to_enable" if version.status == "validated" else version.status
        rows.append({"extension_id": str(skill.id), "kind": "user_skill", "name": version.definition["name"], "status": public_status.get(state, state),
            "row_state": state, "version": version.version, "enabled_version": skill.enabled_version, "updated_at": version.created_at,
            "tested_at": version.validated_at, "summary": version.definition["purpose"], "source_label": "本人 Skill 版本",
            "permission": "、".join(version.definition["allowed_actions"]) or "纯解释", "owner": principal.display_name, "read_only": False})
    for connection in (await db.execute(select(McpConnection).where(McpConnection.owner_user_id == principal.user_id))).scalars():
        version = await connection_version(db, connection, connection.next_version)
        tools = await tools_view(db, principal, version, settings)
        state = connection.status if connection.status != "draft" else "ready_to_enable" if version.status == "validated" else version.status
        rows.append({"extension_id": str(connection.id), "kind": "mcp", "name": connection.name, "status": public_status.get(state, state),
            "row_state": state, "version": version.version, "enabled_version": connection.enabled_version, "updated_at": version.created_at,
            "tested_at": version.tested_at, "summary": "远程只读工具", "source_label": "本人 MCP 连接", "permission": "逐工具授权",
            "transport_label": "Streamable HTTP", "granted_tool_count": sum(t["granted"] for t in tools), "discovered_tool_count": len(tools),
            "error_class": version.error_class, "owner": principal.display_name, "read_only": False})
    return [r for r in rows if (kinds is None or r["kind"] in kinds) and (states is None or r["row_state"] in states)
        and (not q or q.lower() in (r["name"]+r["summary"]).lower()) and (states is not None and "archived" in states or r["row_state"] != "archived")]


def templates(settings):
    ready = master_key(settings) is not None
    try:public_tool_registry(settings);public_ready=True
    except McpFailure:public_ready=False
    return {"items": [{"template": "streamable_http", "label": "Streamable HTTP", "description": "已批准 HTTPS 主机的 MCP 协议连接",
        "public_registry_ready": public_ready, "unavailable_reason": None if public_ready else "MCP公开工具注册配置无效，所有MCP调用禁用",
        "auth_types": [{"id": "none", "label": "无需凭证", "available": True}, {"id": "bearer", "label": "Bearer 凭证", "available": ready,
            "disabled_reason": None if ready else "个人凭证主密钥未配置"}], "credential_store_ready": ready,
        "credential_store_note": "随机数据密钥信封加密" if ready else "未配置主密钥，禁止接收个人凭证"}]}


async def load_challenge(db, principal, settings, challenge_id, *, write=False):
    from app.conversation.service import load_conversation
    query = select(McpChallenge).where(McpChallenge.id == challenge_id, McpChallenge.owner_user_id == principal.user_id,
        McpChallenge.tenant_id == principal.tenant_id)
    if write:
        query = query.with_for_update()
    challenge = (await db.execute(query)).scalar_one_or_none()
    if challenge is None:
        raise NotFoundOrForbidden()
    await load_conversation(db, principal, settings, challenge.conversation_id)
    return challenge


async def challenge_view(db, principal, challenge):
    version = await db.get(McpConnectionVersion, challenge.connection_version_id)
    connection = await load_connection(db, principal, version.connection_id)
    return {"challenge_id": str(challenge.id), "service_name": connection.name, "destination": challenge.destination,
        "tool": challenge.tool_id, "purpose": "调用已授权只读工具", "scope": [str(s.get("source_segment_id") or s.get("document_id") or "公开资料") for s in challenge.frozen_scope],
        "data_level": "case_fragment" if challenge.frozen_scope else "public", "retains_result": True,
        "expires_at": challenge.expires_at, "status": challenge.status}


async def resolve_challenge(db, principal, settings, challenge_id, key, *, reject=False):
    from app.conversation.contracts import CapabilitySelection
    from app.conversation.models import Conversation, ConversationTurn, ReferenceSnapshot
    from app.conversation.service import append_event, append_message, message_view
    challenge = await load_challenge(db, principal, settings, challenge_id, write=True)
    outcome = await idempotency.begin(db, tenant_id=principal.tenant_id, actor_id=principal.user_id,
        scope=f"{'reject' if reject else 'approve'} /mcp-challenges/{challenge_id}", key=key, payload={})
    if outcome.replayed:
        return challenge
    if challenge.status != "pending" or challenge.expires_at < datetime.now(UTC):
        raise InvalidRequest("授权卡已处理或过期")
    conversation = (await db.execute(select(Conversation).where(Conversation.id == challenge.conversation_id).with_for_update())).scalar_one()
    if conversation.status != "active" or conversation.archived_at or conversation.deleted_at:
        raise InvalidRequest("会话已结束，不能批准外部调用")
    version = await db.get(McpConnectionVersion, challenge.connection_version_id)
    connection = await load_connection(db, principal, version.connection_id)
    turn = (await db.execute(select(ConversationTurn).where(ConversationTurn.id == challenge.parent_turn_id).with_for_update())).scalar_one()
    if reject:
        challenge.status = "rejected"
        from app.extensions.audit import safe_destination, safe_parameter_summary
        rejected_at = datetime.now(UTC)
        db.add(ExtensionCall(tenant_id=principal.tenant_id, extension_id=connection.id, owner_user_id=principal.user_id,
            extension_version=version.version, conversation_id=conversation.id, parent_turn_id=turn.id, name=connection.name,
            action=challenge.tool_id, status="rejected", result_redacted="用户拒绝本次外发授权", reject_class="user_rejected", tool_schema_hash=challenge.schema_hash,
            params_redacted=safe_parameter_summary(challenge.arguments), destination=safe_destination(challenge.destination),
            started_at=rejected_at, finished_at=rejected_at, duration_ms=0))
        await append_event(db, turn, "continuation_final", {"status": "cancelled", "text": "已拒绝外部辅助调用。核心任务仍独立运行。"})
    else:
        snapshot = await db.get(ReferenceSnapshot, challenge.reference_snapshot_id)
        if snapshot is None or snapshot.conversation_id != conversation.id or snapshot.id != turn.reference_snapshot_id:
            raise stale("本次授权引用不再可用")
        if challenge.frozen_scope:
            # 当前发布模板仅通过公开仓库协议验收，未发布本票数据外发范围。
            raise InvalidRequest("当前工具模板尚未发布本票资料外发范围")
        from urllib.parse import urlsplit
        if challenge.destination != "https://" + str(urlsplit(version.endpoint).hostname):
            raise stale("服务目标已变化，请重新确认")
        selection = CapabilitySelection(kind="mcp_tool", extension_id=connection.id, extension_version=version.version,
            tool_id=challenge.tool_id, tool_schema_hash=challenge.schema_hash, user_parameters=challenge.arguments)
        result, error, call = await call_selected_mcp(db, principal, settings, selection.model_dump(mode="json"),
            conversation_id=conversation.id, parent_turn_id=turn.id)
        challenge.status = "approved"
        text = "外部只读辅助调用失败。" if error else "已获授权并完成外部只读调用，结果仅作为外部参考资料。"
        if result:
            text += "\n" + json.dumps(result.get("content", []), ensure_ascii=False)[:6000]
        message = await append_message(db, conversation, turn, text=text, parent_turn_id=turn.id,
            cards=[{"type": "tool_call", "name": challenge.tool_id, "version": version.version, "purpose": "外部只读参考",
                "status": "failed" if error else "succeeded", "params_redacted": "已批准的公开资料范围"}])
        await append_event(db, turn, "continuation_final", {"status": "failed" if error else "completed", "message": message_view(message)}, message.id)
    await db.flush()
    idempotency.complete(outcome.record, {"challenge_id": str(challenge.id)})
    return challenge
