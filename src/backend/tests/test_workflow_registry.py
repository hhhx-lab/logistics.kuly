from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.api.errors import InvalidRequest
from app.workflows.commands import normalize_parameters
from app.workflows.registry import WORKFLOWS, WorkflowVersionUnavailable, get_workflow


def test_registered_contracts_preserve_parameter_and_read_artifact_boundaries():
    read = get_workflow("read_index", 1)
    translate = get_workflow("translate_review", 1)
    assert normalize_parameters("read_index", {}) == ({"index_scope": "selected_documents", "document_type_inputs": []}, [], [])
    parameters, missing, questions = normalize_parameters("translate_review", {})
    assert parameters["target_language"] is None and missing == ["target_language"] and questions
    assert normalize_parameters("translate_review", {"target_language": "en"})[1] == []
    assert not read.artifact_policy()["export_snapshot_required"]
    assert read.review_policy(None, None)["exportable"] is False
    assert translate.artifact_policy()["export_snapshot_required"]
    for definition in WORKFLOWS.values():
        assert definition.ui_contract()["parameter_schema"] == definition.parameter_model.model_json_schema()
        assert definition.graph_version == f"{definition.workflow_type}@{definition.version}"
    for workflow_type, version in (("not_published", 1), ("read_index", 2)):
        with pytest.raises(InvalidRequest):
            normalize_parameters(workflow_type, {}, version=version)
        with pytest.raises(WorkflowVersionUnavailable):
            get_workflow(workflow_type, version)


def test_registered_read_graph_executes_only_declared_services():
    calls = []
    definition = get_workflow("read_index", 1)

    class Nodes:
        def __getattr__(self, name):
            if name == "run_awaiting_input":
                return lambda state: False
            if name not in definition.step_services:
                raise AssertionError(name)
            def execute(state):
                calls.append(name)
                return {}
            return execute

    definition.build_graph(Nodes(), checkpointer=InMemorySaver()).invoke(
        {"run_id": str(uuid4()), "execution_epoch": 1, "workflow_version": 1},
        {"configurable": {"thread_id": str(uuid4())}}, durability="sync")
    assert tuple(calls) == definition.step_services


def test_worker_unavailable_fixed_version_pauses_without_fallback(monkeypatch):
    import app.workflows.worker as worker
    from app.workflows.models import RunStatus
    run = SimpleNamespace(id=uuid4(), workflow_type="read_index", workflow_version=999)
    closed = []
    events = []

    class Worker:
        def __init__(self, run_id, epoch):
            self.run_id = run_id
        @contextmanager
        def transaction(self):
            yield SimpleNamespace(scalar=lambda query: SimpleNamespace(purpose="initial") if "execution_epochs" in str(query) else None)
        def locked(self, db):
            return run
        def close(self):
            closed.append(True)

    monkeypatch.setattr(worker, "DocumentWorker", Worker)
    monkeypatch.setattr(worker, "append_event", lambda db, run, event, message: events.append(message))
    monkeypatch.setattr("app.conversation.deferred.enqueue_run_continuations", lambda db, run: None)
    with pytest.raises(WorkflowVersionUnavailable):
        worker.execute_run.run(str(uuid4()), 1)
    assert run.status is RunStatus.PAUSED_RECONCILE
    assert "不能自动更换图" in run.failure_reason
    assert events == [run.failure_reason] and closed == [True]


async def test_builtin_skill_version_parameters_and_permissions_are_frozen(monkeypatch):
    from app.api.errors import ApiError
    from app.conversation import tools
    from app.conversation.contracts import CapabilitySelection
    from app.identity.service import Principal
    principal = Principal(uuid4(), uuid4(), "校验", ("run_start",), uuid4(), "")
    calls = []
    def ready(settings, workflow, version=1):
        calls.append((workflow, version))
        return True, None
    async def no_extensions(*args):
        return []
    monkeypatch.setattr(tools, "workflow_capability", ready)
    monkeypatch.setattr("app.extensions.service.available", no_extensions)
    items = await tools.available_capabilities(None, principal, None)
    translate = next(item for item in items if item["workflow_type"] == "translate_review")
    selection = CapabilitySelection(kind="builtin_skill", extension_id=translate["extension_id"], extension_version=translate["extension_version"],
        user_parameters={"target_language": "en", "translation_scope": "goods_description", "output_formats": ["docx"]})
    frozen = (await tools.validate_capabilities(None, principal, None, [selection]))[0]
    assert frozen["workflow_version"] == translate["workflow_version"]
    assert frozen["user_parameters"]["scope_selector_version"] == "cargo_description@1"
    assert frozen["user_parameters"]["output_formats"] == ["docx"]
    for bad in (selection.model_copy(update={"extension_version": 999}), selection.model_copy(update={"extension_id": uuid4()})):
        with pytest.raises(ApiError, match="所选能力"):
            await tools.validate_capabilities(None, principal, None, [bad])
    denied = Principal(principal.user_id, principal.tenant_id, "校验", (), principal.session_id, "")
    assert await tools.available_capabilities(None, denied, None) == []
    with pytest.raises(ApiError):
        await tools.validate_capabilities(None, denied, None, [selection])
    assert ("translate_review", translate["workflow_version"]) in calls


async def test_builtin_repository_preserves_real_unavailability(monkeypatch):
    from app.extensions.service import extension_rows
    from app.conversation import tools
    async def capabilities(*args):
        return [{"kind": "builtin_skill", "extension_id": str(uuid4()), "extension_version": 2,
            "name": "翻译", "purpose": "翻译工作流", "permission": "受控写入", "connection_status": "不可用", "unavailable_reason": "模型尚未配置"}]
    class Db:
        async def execute(self, query):
            return SimpleNamespace(scalars=lambda: [])
    monkeypatch.setattr(tools, "available_capabilities", capabilities)
    row = (await extension_rows(Db(), SimpleNamespace(user_id=uuid4()), None))[0]
    assert row["row_state"] == "connect_failed" and row["enabled_version"] is None
    assert row["version"] == 2 and row["error_class"] == "模型尚未配置"
