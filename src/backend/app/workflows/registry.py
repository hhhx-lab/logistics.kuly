from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable
from pydantic import BaseModel
from app.config import ModelRole
from app.router.contracts import ReadIndexTaskSpec, TranslateReviewTaskSpec, ConsignmentTaskSpec, TranslateConsignmentTaskSpec
from app.workflows.contracts import ReadParameters, TranslateParameters, ConsignmentParameters, TranslateConsignmentParameters

from langgraph.graph import StateGraph

from app.workflows.graphs import build_read_index_graph, build_translate_review_graph


def build_registered_translation_graph(worker):
    from app.workflows.translation import TranslationNodes
    return build_translate_review_graph(TranslationNodes(worker))


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    workflow_type: str
    version: int
    title: str
    stages: tuple[str, ...]
    graph_builder: Callable[..., StateGraph]
    parameter_model: type[BaseModel]
    task_spec_model: type[BaseModel]
    input_mime: tuple[str, ...]
    output_types: tuple[str, ...]
    supported_formats: tuple[str, ...]
    supported_languages: tuple[str, ...]
    required_permissions: tuple[str, ...]
    model_roles: tuple[ModelRole, ...]
    prompt_keys: tuple[str, ...]
    graph_version: str
    cost_level: str
    form_component: str
    result_actions: tuple[str, ...]
    review_required: bool
    step_services: tuple[str, ...]

    async def prepare(self, db, principal, settings, **kwargs):
        from app.workflows.commands import prepare_draft
        return await prepare_draft(db, principal, settings, workflow_type=self.workflow_type, workflow_version=self.version, **kwargs)

    def review_policy(self, db, run, *, can_confirm=True):
        if not self.review_required:
            return {"confirmable": False, "exportable": False, "reason": "识别索引工作流不生成正式译本或导出快照"}
        from app.exports.gate import review_gate
        return review_gate(db, run, can_confirm=can_confirm)

    def artifact_policy(self):
        return {"output_types": list(self.output_types), "export_snapshot_required": self.review_required,
            "bilingual_per_source": self.review_required, "translation_only_export": self.review_required, "case_report": False,
            "verification_policy": "bilingual-docx@1" if self.review_required else "provenance-index@1"}

    def ui_contract(self):
        return {"version": "workflow-form@1", "component": self.form_component,
            "parameter_schema": self.parameter_model.model_json_schema(), "result_actions": list(self.result_actions),
            "review_component": "ReviewPanel" if self.review_required or self.workflow_type == "consignment_check" else "SourceReader"}

    def build_graph(self, worker, *, checkpointer=None):
        return self.graph_builder(worker).compile(checkpointer=checkpointer)


INPUT_MIME = ("application/pdf", "image/png", "image/jpeg", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")


_DEFINITIONS = {
    ("read_index", 1): WorkflowDefinition(
        workflow_type="read_index",
        version=1,
        title="单据识别与索引",
        stages=("文件检查", "解析", "逻辑单据分类", "抽取", "来源核验", "索引"),
        graph_builder=build_read_index_graph,
        parameter_model=ReadParameters, task_spec_model=ReadIndexTaskSpec, input_mime=INPUT_MIME,
        output_types=("index",), supported_formats=(), supported_languages=(), required_permissions=("run_start",),
        model_roles=(ModelRole.EXTRACT,), prompt_keys=("document.extract",), graph_version="read_index@1",
        cost_level="ocr_and_model", form_component="ReadTaskDraft", result_actions=("ask_source", "open_source"), review_required=False,
        step_services=("load_spec", "inspect_sources", "parse_documents", "classify_sections", "extract_fields", "verify_sources", "publish_index"),
    ),
    ("translate_review", 1): WorkflowDefinition(
        workflow_type="translate_review",
        version=1,
        title="单据翻译与校验",
        stages=("文件检查", "解析", "逻辑单据分类", "抽取", "来源核验", "校验", "翻译", "预览", "待复核"),
        graph_builder=build_registered_translation_graph,
        parameter_model=TranslateParameters, task_spec_model=TranslateReviewTaskSpec, input_mime=INPUT_MIME,
        output_types=("docx", "pdf", "source_pdf"), supported_formats=("docx", "pdf"), supported_languages=("zh-CN", "en", "fr", "es", "ru"),
        required_permissions=("run_start",), model_roles=(ModelRole.EXTRACT, ModelRole.TRANSLATE),
        prompt_keys=("document.extract", "document.translate", "document.semantic_qa"), graph_version="translate_review@1",
        cost_level="ocr_and_model", form_component="TranslateTaskDraft", result_actions=("open_review", "confirm_review", "export"), review_required=True,
        step_services=("load_spec", "inspect_sources", "parse_documents", "classify_sections", "extract_fields", "verify_sources", "validate_documents", "translate_segments", "quality_and_preview", "publish_review"),
    ),
}

from dataclasses import replace
from app.workflows.consignment import build_registered_consignment_graph, build_registered_translation_v2_graph

_DEFINITIONS[("consignment_check", 1)] = replace(
    _DEFINITIONS[("read_index", 1)], workflow_type="consignment_check", title="同票单证核对",
    stages=("文件检查", "解析", "逻辑单据分类", "抽取", "来源核验", "关联", "核对"),
    graph_builder=build_registered_consignment_graph, parameter_model=ConsignmentParameters,
    task_spec_model=ConsignmentTaskSpec, graph_version="consignment_check@1", output_types=("comparison",),
    form_component="ConsignmentTaskDraft", result_actions=("open_comparison", "ask_source"),
    step_services=(*_DEFINITIONS[("read_index", 1)].step_services[:-1], "associate_documents", "publish_comparison"),
)
_DEFINITIONS[("translate_review", 2)] = replace(
    _DEFINITIONS[("translate_review", 1)], version=2, title="同票核对与翻译",
    stages=("文件检查", "解析", "逻辑单据分类", "抽取", "来源核验", "关联", "核对", "校验", "翻译", "预览", "待复核"),
    graph_builder=build_registered_translation_v2_graph, parameter_model=TranslateConsignmentParameters,
    task_spec_model=TranslateConsignmentTaskSpec, graph_version="translate_review@2",
    step_services=(*_DEFINITIONS[("translate_review", 1)].step_services[:6], "associate_documents", "publish_comparison", *_DEFINITIONS[("translate_review", 1)].step_services[6:]),
)

WORKFLOWS = MappingProxyType(_DEFINITIONS)


class WorkflowVersionUnavailable(KeyError):
    pass


def get_workflow(workflow_type: str, version: int) -> WorkflowDefinition:
    try:
        return WORKFLOWS[(workflow_type, version)]
    except KeyError as exc:
        raise WorkflowVersionUnavailable((workflow_type, version)) from exc
