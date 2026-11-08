"""
Prompt 的结构化输出 schema。

这些模型只描述「模型可以说什么」，不代表服务端会采信：来源 ID 归属、证据版本、
动作白名单与权限仍由调用方逐项核验。字段一律无默认值，使严格 JSON Schema 下
「全部字段必填、可空用 null」与 Pydantic 校验口径一致。

schema 一经某个 Prompt 版本引用就不可原位改字段；需要变化时新增 `@2` 并由新 Prompt 版本引用。
"""


from __future__ import annotations
from app.languages import LanguageCode

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    # forbid 让生成的 schema 带 additionalProperties=false，同时拒绝模型夹带额外字段
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---- chat.intent@1 -------------------------------------------------------

IntentAction = Literal[
    # 与 Agent 方案 3.3 的内部受控服务一一对应；模型只能从中挑选，参数由服务端按认证主体重建
    "get_run_timeline",
    "list_issues",
    "query_case_facts",
    "query_provenance",
    "prepare_task",
    "open_review",
    "search_authorized_cases",
    "propose_user_memory",
    "clarify",
    "out_of_domain",
]


class IntentCandidate(_Strict):
    action: IntentAction
    workflow_type: Literal["read_index", "translate_review"] | None
    # 缺目标语言必须为 null 并追问；不允许模型从记忆里代填
    target_language: LanguageCode | None
    referenced_labels: list[str]
    clarification_question: str | None
    rationale: str = Field(max_length=300)


class IntentCandidateV2(IntentCandidate):
    action: Literal[
        "get_run_timeline", "list_issues", "query_case_facts", "query_provenance",
        "prepare_task", "open_review", "search_authorized_cases", "propose_user_memory",
        "clarify", "out_of_domain", "general_knowledge",
    ]


class IntentCandidateV3(IntentCandidateV2):
    action: Literal[
        "get_run_timeline", "list_issues", "query_case_facts", "query_provenance",
        "get_consignment_overview", "list_comparison_differences", "get_comparison_evidence",
        "prepare_task", "open_review", "search_authorized_cases", "propose_user_memory",
        "clarify", "out_of_domain", "general_knowledge",
    ]
    workflow_type: Literal["read_index", "translate_review", "consignment_check"] | None


# ---- chat.answer@1 -------------------------------------------------------


class AnswerParagraph(_Strict):
    text: str = Field(max_length=2000)
    # 「原件写的值」「人工采用值」「目标译文」必须分开标注，前端据此给不同标签
    claim_kind: Literal[
        "source_value",
        "adopted_value",
        "translation",
        "computed",
        "run_status",
        "general_explanation",
        "not_covered",
    ]
    evidence_ids: list[str]
    # 模型自报的立场；服务端逐段核验后另存核验结果，二者不混用
    status: Literal["asserted", "cannot_confirm", "not_covered"]


class AnswerDraft(_Strict):
    paragraphs: list[AnswerParagraph]


NATURAL_CLAIM_KINDS = {"source_value", "adopted_value", "translation", "computed", "run_status", "general_explanation", "not_covered", "issue_reason"}
NATURAL_STATUSES = {"asserted", "cannot_confirm", "not_covered"}


def normalize_natural_metadata(value):
    """模型展示分类不授予事实权限；未知非空分类采用保守显示。"""
    if not isinstance(value, dict):
        return value
    result = dict(value)
    for key, allowed, fallback in (("claim_kind", NATURAL_CLAIM_KINDS, "general_explanation"),
        ("status", NATURAL_STATUSES, "cannot_confirm")):
        item = result.get(key)
        if isinstance(item, str) and item.strip() and item not in allowed:
            result[key] = fallback
    return result


class NaturalAnswerParagraph(AnswerParagraph):
    # 受保护值由服务端替换，模型只组织解释与证据占位符。
    text: str = Field(max_length=2000)
    claim_kind: Literal["source_value", "adopted_value", "translation", "computed", "run_status", "general_explanation", "not_covered", "issue_reason"]

    @model_validator(mode="before")
    @classmethod
    def normalize_display_classification(cls, value):
        return normalize_natural_metadata(value)


class NaturalAnswerDraft(_Strict):
    paragraphs: list[NaturalAnswerParagraph] = Field(min_length=1, max_length=12)


# ---- chat.session_summary@1 ----------------------------------------------


class SessionSummary(_Strict):
    summary: str = Field(max_length=4000)
    # 只存用户明确提出的约束与对象 ID，摘要绝不作为单据事实证据
    open_constraints: list[str]
    referenced_ids: list[str]


# ---- document.extract@1 --------------------------------------------------


class ExtractedField(_Strict):
    field_key: str
    role: str
    raw_value: str
    source_segment_ids: list[str]
    confidence_label: Literal["high", "medium", "low"]
    ambiguity: str | None


class ExtractedCell(_Strict):
    column_key: str
    raw_value: str
    source_segment_ids: list[str]


class ExtractedLineItem(_Strict):
    row_source_segment_ids: list[str]
    cells: list[ExtractedCell]


class ExtractionResult(_Strict):
    fields: list[ExtractedField]
    line_items: list[ExtractedLineItem]


# ---- document.translate@1 ------------------------------------------------


class TranslatedSegment(_Strict):
    source_segment_ids: list[str]
    translated_text: str
    protected_token_ids: list[str]


class TranslationResult(_Strict):
    target_language: LanguageCode
    segments: list[TranslatedSegment]


# ---- document.vision_region@1 --------------------------------------------


class VisionTranscription(_Strict):
    transcription: str
    legible: bool
    uncertain_spans: list[str]


# ---- document.semantic_qa@1 ----------------------------------------------


class SemanticFinding(_Strict):
    source_segment_ids: list[str]
    translation_segment_ids: list[str]
    category: Literal["role_mistranslation", "meaning_reversal", "omission", "other"]
    explanation: str = Field(max_length=500)


class SemanticQaResult(_Strict):
    findings: list[SemanticFinding]


class GuidedSemanticFinding(SemanticFinding):
    recommendation: str = Field(max_length=600)
    impact: str = Field(max_length=400)
    replacement_text: str | None = Field(max_length=50000)


class GuidedSemanticQaResult(_Strict):
    findings: list[GuidedSemanticFinding]


class CategorizedSemanticFinding(GuidedSemanticFinding):
    category: Literal["role_mistranslation", "meaning_reversal", "terminology", "omission", "source_uncertain", "source_omission", "field_missing", "protected_value_changed", "layout", "other"]


class CategorizedSemanticQaResult(_Strict):
    findings: list[CategorizedSemanticFinding]


class IssueAssessmentResult(_Strict):
    explanation: str = Field(min_length=1, max_length=1600)
    recommendation: str = Field(min_length=1, max_length=1600)
    source_segment_ids: list[str] = Field(max_length=40)
    recommended_action: Literal["revise-translation", "ignore", "supplement-upload", "human-review"]


class SegmentRevisionResult(_Strict):
    translated_text: str = Field(min_length=1, max_length=50000)
    explanation: str = Field(max_length=800)


# ---- 注册表 --------------------------------------------------------------

class GeneralKnowledgeAnswer(_Strict):
    domain: Literal["logistics", "out_of_domain"]
    paragraphs: list[str] = Field(min_length=1, max_length=6)


class SourcedKnowledgeAnswer(GeneralKnowledgeAnswer):
    source_ids: list[str] = Field(min_length=0, max_length=6)

class StreamKnowledgeAnswer(_Strict):
    domain: Literal["logistics", "related_material", "out_of_domain"]
    source_ids: list[str] = Field(max_length=6)
    paragraphs: list[str] = Field(min_length=1, max_length=6)


class StreamNaturalAnswer(_Strict):
    domain: Literal["logistics", "related_material", "out_of_domain"]
    paragraphs: list[NaturalAnswerParagraph] = Field(min_length=1, max_length=12)


# None 表示「显式声明无结构化输出」：system 段本身不产生输出，text 为自由文本。
# 与「漏填 schema」区分开，启动校验只放行这里登记过的标识。
NO_OUTPUT = "none@1"
TEXT_OUTPUT = "text@1"



class UiLocalizationResult(_Strict):
    entries: dict[str, str]

OUTPUT_SCHEMAS: dict[str, type[BaseModel] | None] = {
    "GeneralKnowledgeAnswer@3": StreamKnowledgeAnswer,
    "NaturalAnswerDraft@2": StreamNaturalAnswer,
    "UiLocalizationResult@1": UiLocalizationResult,
    NO_OUTPUT: None,
    TEXT_OUTPUT: None,
    "IntentCandidate@1": IntentCandidate,
    "IntentCandidate@2": IntentCandidateV2,
    "IntentCandidate@3": IntentCandidateV3,
    "AnswerDraft@1": AnswerDraft,
    "AnswerDraft@2": NaturalAnswerDraft,
    "GeneralKnowledgeAnswer@1": GeneralKnowledgeAnswer,
    "GeneralKnowledgeAnswer@2": SourcedKnowledgeAnswer,
    "SessionSummary@1": SessionSummary,
    "ExtractionResult@1": ExtractionResult,
    "TranslationResult@1": TranslationResult,
    "VisionTranscription@1": VisionTranscription,
    "SemanticQaResult@1": SemanticQaResult,
    "SemanticQaResult@2": GuidedSemanticQaResult,
    "SemanticQaResult@3": CategorizedSemanticQaResult,
    "IssueAssessmentResult@1": IssueAssessmentResult,
    "SegmentRevisionResult@1": SegmentRevisionResult,
}


def resolve_output_schema(schema_id: str) -> type[BaseModel] | None:
    if schema_id not in OUTPUT_SCHEMAS:
        raise KeyError(schema_id)
    return OUTPUT_SCHEMAS[schema_id]
