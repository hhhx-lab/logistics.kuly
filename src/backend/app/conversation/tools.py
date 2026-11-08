from __future__ import annotations

from uuid import NAMESPACE_URL, UUID, uuid5

from app.api.errors import ApiError
from app.workflows.commands import normalize_parameters, workflow_capability
from app.workflows.registry import WORKFLOWS, get_workflow

BUILTINS = {str(uuid5(NAMESPACE_URL, f"logistics-document-agent:workflow:{name}@{version}")): (name, version)
    for name, version in WORKFLOWS}


def stale():
    return ApiError("所选能力或参数已变化，请重新选择。", code="CAPABILITY_STALE", status_code=409)


ISSUE_SKILL_ID = str(uuid5(NAMESPACE_URL,"logistics-document-agent:skill:document-issue-review@1"))
from app.review.issue_skill import skill_instructions
ISSUE_INSTRUCTIONS = skill_instructions()
ISSUE_PURPOSE = "按授权材料区分识别、关键字段、翻译及结构问题，说明风险与处理建议。"


async def available_capabilities(db, principal, settings, q=""):
    items = []
    for extension_id, (workflow, version) in BUILTINS.items():
        definition = get_workflow(workflow, version)
        if not set(definition.required_permissions).issubset(principal.permissions):
            continue
        if q and q.lower() not in (definition.title + workflow).lower():
            continue
        ready, reason = workflow_capability(settings, workflow, version)
        items.append({"kind": "builtin_skill", "extension_id": extension_id, "extension_version": version,
            "name": definition.title, "purpose": definition.title + "；" + "→".join(definition.stages),
            "permission": "受控写入", "connection_status": "可用" if ready else "不可用", "unavailable_reason": reason,
            "workflow_type": workflow, "workflow_version": version,
            "parameter_schema": definition.parameter_model.model_json_schema(),
            "required_permissions": list(definition.required_permissions), "cost_level": definition.cost_level,
            "accepted_mime": list(definition.input_mime), "output_types": list(definition.output_types), "ui_contract": definition.ui_contract(),
            "group": "内置 Skill"})
    if "case_view" in principal.permissions and (not q or q.lower() in ("疑点判断 document-issue-review").lower()):
        items.append({"kind":"builtin_skill","extension_id":ISSUE_SKILL_ID,"extension_version":1,"name":"疑点判断与处理建议","purpose":ISSUE_PURPOSE,"permission":"只读","connection_status":"可用","unavailable_reason":None,"workflow_type":None,"workflow_version":None,"parameter_schema":{"type":"object","properties":{}},"required_permissions":["case_view"],"cost_level":"none","accepted_mime":[],"output_types":["issue_guidance"],"ui_contract":None,"group":"内置 Skill"})
    from app.extensions.service import available
    items.extend(await available(db, principal, settings, q))
    return items


async def validate_capabilities(db, principal, settings, selections):
    frozen = []
    for selection in selections:
        if selection.kind != "builtin_skill":
            # 非内置能力必须由扩展注册与授权服务核验，尚未注册的 ID 不能成为新工具。
            try:
                from app.extensions.service import validate_selections
            except ImportError as exc:
                raise stale() from exc
            frozen.extend(await validate_selections(db, principal, settings, [selection]))
            continue
        if str(selection.extension_id)==ISSUE_SKILL_ID:
            if selection.extension_version!=1 or selection.tool_id or selection.tool_schema_hash or selection.user_parameters or "case_view" not in principal.permissions:raise stale()
            frozen.append({**selection.model_dump(mode="json"),"instructions":ISSUE_INSTRUCTIONS,"allowed_actions":["list_issues"],"result_format":"中文疑点分类、证据、影响和处理建议"})
            continue
        key = BUILTINS.get(str(selection.extension_id))
        if not key or selection.extension_version != key[1] or selection.tool_id or selection.tool_schema_hash:
            raise stale()
        workflow, version = key
        definition = get_workflow(workflow, version)
        if not set(definition.required_permissions).issubset(principal.permissions) or not workflow_capability(settings, workflow, version)[0]:
            raise stale()
        try:
            parameters, _, _ = normalize_parameters(workflow, selection.user_parameters, version=version)
        except Exception as exc:
            raise stale() from exc
        frozen.append({**selection.model_dump(mode="json"), "workflow_type": workflow, "workflow_version": version, "user_parameters": parameters})
    if sum(bool(c.get("workflow_type") or c.get("allowed_actions") or c["kind"] == "mcp_tool") for c in frozen) > 1:
        raise ApiError("本轮请选择一个实际动作能力；纯解释 Skill 可以组合。", code="CAPABILITY_STALE", status_code=409)
    return frozen
