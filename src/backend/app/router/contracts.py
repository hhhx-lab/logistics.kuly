from typing import Annotated, Literal
from app.languages import LanguageCode

from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator
from app.review.jurisdiction import CERTIFICATE_PACK, MATERIAL_PACK, CertificateInputs, ConsumerMaterialInputs, JurisdictionInputs
from app.config import OCRBinding


class SourceVersion(BaseModel):
    model_config = ConfigDict(frozen=True)

    document_id: UUID
    document_version: int = Field(ge=1)


class SelectedSectionRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    document_id: UUID
    document_version: int = Field(ge=1)
    parse_artifact_id: UUID
    logical_document_id: UUID
    section_id: UUID


class DocumentTypeInput(SourceVersion):
    model_config = ConfigDict(frozen=True, extra="forbid")
    document_type: str | None = None
    cargo_profile_ids: tuple[str, ...] = Field(default=(), max_length=16)

    @field_validator("document_type")
    @classmethod
    def validate_document_type(cls, value):
        from app.documents.type_catalog import is_document_type_code
        if value is not None and (not is_document_type_code(value) or value == "unknown"):
            raise ValueError("单据类型必须来自已发布目录；自动识别请留空")
        return value

    @field_validator("cargo_profile_ids")
    @classmethod
    def validate_cargo_profiles(cls, value):
        from app.documents.type_catalog import normalize_cargo_profiles
        return tuple(normalize_cargo_profiles(list(value)))


class BaseTaskSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: UUID
    workflow_version: int = Field(ge=1)
    source_versions: tuple[SourceVersion, ...]
    initiated_by: UUID
    created_at: AwareDatetime
    idempotency_key: str = Field(min_length=1)
    ocr_binding: OCRBinding | None = None
    document_type_inputs: tuple[DocumentTypeInput, ...] = ()

    @model_validator(mode="after")
    def validate_type_inputs(self):
        sources = {(value.document_id, value.document_version) for value in self.source_versions}
        choices = [(value.document_id, value.document_version) for value in self.document_type_inputs]
        if len(set(choices)) != len(choices) or any(value not in sources for value in choices):
            raise ValueError("单据类型声明必须属于本任务原件且不可重复")
        return self

    @field_validator("source_versions")
    @classmethod
    def require_source(cls, value: tuple[SourceVersion, ...]) -> tuple[SourceVersion, ...]:
        if not value:
            raise ValueError("任务至少需要一份已确认的原件版本")
        if len({item.document_id for item in value}) != len(value):
            raise ValueError("同一任务不能重复包含相同原件")
        return value


class ReadIndexTaskSpec(BaseTaskSpec):
    workflow_type: Literal["read_index"] = "read_index"
    index_scope: Literal["selected_documents", "case"] = "selected_documents"


class ConsignmentTaskSpec(BaseTaskSpec):
    workflow_type: Literal["consignment_check"] = "consignment_check"
    scope_revision_id: UUID
    comparison_rule_version: Literal["consignment-review@1"] = "consignment-review@1"
    previous_batch_id: UUID | None = None


class TranslateReviewTaskSpec(BaseTaskSpec):
    workflow_type: Literal["translate_review"] = "translate_review"
    target_language: LanguageCode
    translation_scope: Literal["all", "goods_description", "selected_sections"] = "all"
    selected_section_refs: tuple[SelectedSectionRef, ...] = ()
    # goods_description 在启动时冻结语义选择器版本，解析后按它写 ResolvedTranslationScope
    scope_selector_version: str | None = None
    output_formats: tuple[Literal["docx", "pdf"], ...] = ("docx", "pdf")
    glossary_version: str | None = None
    rule_pack_version: str
    jurisdiction_inputs: JurisdictionInputs | None = None

    @model_validator(mode="after")
    def validate_scope_and_outputs(self) -> "TranslateReviewTaskSpec":
        if self.translation_scope == "selected_sections" and not self.selected_section_refs:
            raise ValueError("指定区段翻译必须选择已解析的区段")
        if self.translation_scope != "selected_sections" and self.selected_section_refs:
            raise ValueError("非指定区段翻译不能携带区段引用")
        if (self.translation_scope == "goods_description") != (self.scope_selector_version is not None):
            raise ValueError("仅货描翻译必须且只能冻结货描选择器版本")
        if len(set(self.selected_section_refs)) != len(self.selected_section_refs):
            raise ValueError("翻译区段不能重复")
        source_keys = {(item.document_id, item.document_version) for item in self.source_versions}
        if self.jurisdiction_inputs is not None:
            expected = ConsumerMaterialInputs if self.rule_pack_version == MATERIAL_PACK else CertificateInputs if self.rule_pack_version == CERTIFICATE_PACK else None
            if expected is None or not isinstance(self.jurisdiction_inputs, expected):
                raise ValueError("材料检查参数必须与冻结规则版本一致")
            materials = self.jurisdiction_inputs.materials if isinstance(self.jurisdiction_inputs, ConsumerMaterialInputs) else []
            refs = [material.source for material in materials]
            if isinstance(self.jurisdiction_inputs, CertificateInputs) and self.jurisdiction_inputs.certificate_source:
                refs.append(self.jurisdiction_inputs.certificate_source)
            if any((ref.document_id, ref.document_version) not in source_keys for ref in refs):
                raise ValueError("材料来源必须属于冻结原件版本集合")
        if any((ref.document_id, ref.document_version) not in source_keys for ref in self.selected_section_refs):
            raise ValueError("翻译区段必须属于本任务冻结的原件版本")
        if not self.output_formats or len(set(self.output_formats)) != len(self.output_formats):
            raise ValueError("输出格式至少一种且不能重复")
        return self


class TranslateConsignmentTaskSpec(TranslateReviewTaskSpec):
    scope_revision_id: UUID
    comparison_source_versions: tuple[SourceVersion, ...]
    comparison_rule_version: Literal["consignment-review@1"] = "consignment-review@1"

    @model_validator(mode="after")
    def validate_comparison(self):
        keys = {(v.document_id, v.document_version) for v in self.comparison_source_versions}
        if len(keys) != len(self.comparison_source_versions) or not keys:
            raise ValueError("核对材料不可为空或重复")
        if not {(v.document_id, v.document_version) for v in self.source_versions}.issubset(keys):
            raise ValueError("翻译原件必须包含在核对范围内")
        return self


TaskSpec = Annotated[
    ReadIndexTaskSpec | TranslateReviewTaskSpec | ConsignmentTaskSpec,
    Field(discriminator="workflow_type"),
]
