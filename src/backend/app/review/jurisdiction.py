"""受控试行包：只检查材料提供情况，不给出法律或海关放行结论。"""
from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

CERTIFICATE_PACK = "cn-eu-fresh-fruit-certificate@2026-07-06.1"
MATERIAL_PACK = "cn-eu-consumer-materials@2026-10-03.1"
EU_COUNTRIES = frozenset("AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE".split())
SOURCES = [
    {"url":"https://publications.europa.eu/resource/celex/02016R2031-20250105.ENG", "version":"2025-01-05", "provisions":["Art2(1)(b)","Art72(1)","Art73","Art47","Art48"]},
    {"url":"https://publications.europa.eu/resource/celex/02019R2072-20260706.ENG", "version":"2026-07-06", "provisions":["Art11(1)-(3)","Annex XI Part A §5 Fruits: Malus L.","Annex XI Part C"]},
]
CERTIFICATE_TITLE = re.compile(r"^\s*(?:phytosanitary\s+certificate(?:\s+for\s+(?:export|re-export))?|植物检疫证书|植物检疫证明书)\s*(?:$|(?:no\.?|number|编号|#|:|：)\s*\S+)",re.I|re.M)
EXEMPT_GOODS = {"bananas":r"\bbananas?\b|\bMusa\b|香蕉", "pineapples":r"\bpineapples?\b|\bAnanas comosus\b|菠萝|凤梨",
                "coconuts":r"\bcoconuts?\b|\bCocos nucifera\b|椰子", "durians":r"\bdurians?\b|\bDurio zibethinus\b|榴莲",
                "dates":r"\bdates\b|\bPhoenix dactylifera\b|椰枣"}
INCOMPLETE_FLAGS = frozenset({"page_needs_ocr","ocr_unavailable","image_region_not_transcribed","unknown_block_type","docx_textbox_not_extracted",
    "docx_drawing_not_extracted","docx_embedded_image_not_ocr","docx_alt_chunk_not_extracted","docx_notes_not_extracted","fake_parser_output",
    "image_blur_suspected","image_glare_suspected","image_edge_content","image_resolution_low"})


class CertificateSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    document_id: UUID
    document_version: int = Field(ge=1)


class CertificateInputs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    origin_country: str | None = Field(default=None, max_length=2, description="用户确认的原产国 ISO 二字码，不从装货港推断")
    dispatch_country: str | None = Field(default=None, max_length=2, description="用户确认的发运国 ISO 二字码")
    destination_country: str | None = Field(default=None, max_length=2, description="用户确认的进口目的国 ISO 二字码")
    eu_plant_health_territory: bool | None = Field(default=None, description="确认属于欧盟植物卫生适用领土，海外排除领土不可按国名推断")
    movement: Literal["ordinary_import","transit_or_special"] | None = None
    commodity: Literal["fresh_apples","pineapples","coconuts","durians","bananas","dates","other"] | None = None
    stated_code: str | None = Field(default=None, max_length=10, description="原件注明的 HS080810 或 CN08081010/08081080；不自动归类")
    import_date: date | None = None
    material_set_complete: bool | None = Field(default=None, description="本任务冻结原件已包括全部本票证书材料；仅据此判断本次材料缺证")
    certificate_source: CertificateSource | None = Field(default=None, description="冻结参与原件集合中的 document_id/document_version")
    certificate_same_consignment: bool | None = None
    declaration_reason: str = Field(default="", max_length=1000, description="用户确认线路/货物及证书同票或完整材料的依据，不冒充法律审定")


MaterialKind = Literal["commercial_invoice", "packing_list", "transport_document"]


class ConsignmentMaterial(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: MaterialKind
    source: CertificateSource
    same_consignment: bool | None = None
    basis: str = Field(default="", max_length=1000, description="核对本票编号或业务关联的依据，不能只按文件名确认")


class ConsumerMaterialInputs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["consumer_material_check"]
    dispatch_country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    destination_country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    eu_customs_territory: bool | None = None
    general_consumer_goods: bool | None = None
    non_dangerous: bool | None = None
    declaration_reason: str = Field(default="", max_length=1000)
    material_set_complete: bool | None = None
    materials: list[ConsignmentMaterial] = Field(default_factory=list, max_length=3)
    required_materials: list[MaterialKind] = Field(default_factory=list, max_length=3, description="用户明确本票业务必需清单，不代表法律义务")
    required_materials_basis: str = Field(default="", max_length=1000, description="合同、委托或业务要求依据；缺依据不产生缺材料阻断")

    @model_validator(mode="after")
    def unique_materials(self):
        if len({material.kind for material in self.materials}) != len(self.materials) or len(set(self.required_materials)) != len(self.required_materials):
            raise ValueError("同类候选与本票必需材料不能重复")
        return self


JurisdictionInputs = ConsumerMaterialInputs | CertificateInputs
MATERIAL_LABELS = {"commercial_invoice": "商业发票", "packing_list": "装箱单", "transport_document": "运输文件"}
MATERIAL_TITLES = {
    "commercial_invoice": re.compile(r"^\s*(?:commercial\s+invoice|商业发票|商業發票)\s*(?:$|(?:no\.?|number|编号|#|:|：)\s*\S+)", re.I | re.M),
    "packing_list": re.compile(r"^\s*(?:packing\s+list|装箱单|裝箱單|箱单)\s*(?:$|(?:no\.?|number|编号|#|:|：)\s*\S+)", re.I | re.M),
    "transport_document": re.compile(r"^\s*(?:bill\s+of\s+lading|sea\s+waybill|air\s+waybill|consignment\s+note|提单|海运单|空运单|航空运单|运输单)\s*(?:$|(?:no\.?|number|编号|#|:|：)\s*\S+)", re.I | re.M),
}
MATERIAL_SOURCES = [
    {"url": "https://trade.ec.europa.eu/access-to-markets/en/content/guide-import-goods", "version": "retrieved-2026-10-03", "provisions": ["Prepare the documents for customs clearance in the EU / Which documents to prepare for customs?"]},
    {"url": "https://trade.ec.europa.eu/access-to-markets/en/content/customs-clearance-documents-and-procedures", "version": "retrieved-2026-10-03", "provisions": ["Entry Summary Declaration", "Member State Import Requirements and Authorities"]},
]


def published_rule_packs() -> list[dict]:
    return [
        {"version":"core-document@3","title":"通用单据核对（ISO币种与柜号校验位）","status":"published","parameter_schema":None,
         "notice":"采用SIX 2026-09-17当前币种表；ISO6346校验位不证明注册归属或真实性；主体映射、完整同层多列表及运输日期需明确确认依据，未知保持无法判断。"},
        {"version":"core-document@2","title":"通用单据核对（词面与明确包装汇总）","status":"published","parameter_schema":None,
         "notice":"词面候选不代表法规有效；包装合计只覆盖明确完整同层表格；同主体映射未实施。"},
        {"version":"core-document@1","title":"通用单据核对（历史版本）","status":"published","parameter_schema":None},
        {"version": MATERIAL_PACK, "title": "中国出口欧盟一般非危险消费品：候选材料核对", "status": "trial_material_check",
         "legal_review_status": "pending", "notice": "仅核本次发票、箱单和运输文件的材料提供与同票确认；缺件默认提示，仅有依据的本票业务必需清单可产生阻断。HS、专项证书及海关合规未评估。日期为项目来源复核窗口，不是法律有效期。",
         "valid_from": "2026-10-03", "valid_before": "2027-01-03", "sources": MATERIAL_SOURCES, "parameter_schema": ConsumerMaterialInputs.model_json_schema()},
        {"version":CERTIFICATE_PACK,"title":"中国鲜苹果进口欧盟：植物检疫证书材料","status":"trial_material_check",
         "legal_review_status":"pending","notice":"只检查材料提供情况；法律审定待完成，证书真实性及海关合规未评估。",
         "valid_from":"2026-07-06","valid_before":"2026-10-15","sources":SOURCES,"parameter_schema":CertificateInputs.model_json_schema()},
    ]


def evaluate_consumer_materials(inputs: dict | None, source_rows: list[dict], documents: list[dict]):
    from app.review.rules import RuleResult

    data = ConsumerMaterialInputs.model_validate(inputs or {"kind": "consumer_material_check"}).model_dump(mode="json")
    context = {"rule_pack_version": MATERIAL_PACK, "legal_review_status": "pending", "sources": MATERIAL_SOURCES,
               "source_review_window": {"from": "2026-10-03", "before": "2027-01-03", "is_legal_validity": False},
               "declared_inputs": data, "assessment_scope": "candidate_materials_and_declared_business_requirements",
               "authenticity": "not_assessed", "customs_compliance": "not_assessed", "special_certificates": "not_covered"}
    all_ids = tuple(row["id"] for row in source_rows)
    def result(rule, status, explanation, refs=all_ids, *, blocked=False, evidence=None):
        return RuleResult(rule, status, explanation, refs, calculation={**context, "evidence": evidence or {}},
                          severity="red" if blocked else "yellow", blocks_export=blocked)

    if datetime.now(UTC).date() >= date(2027, 1, 3):
        return [result("jurisdiction.consumer_materials", "not_covered", "候选材料试行包的项目来源复核窗口已到期，须复核后使用新版本；未评估法律有效性")]
    required = ("dispatch_country", "destination_country", "eu_customs_territory", "general_consumer_goods", "non_dangerous")
    missing = [key for key in required if data[key] is None]
    if missing or not data["declaration_reason"].strip():
        return [result("jurisdiction.consumer_materials", "undetermined", "线路、一般消费品/非危险属性或确认依据不完整；只保留材料候选，不推断专项义务", evidence={"missing_inputs": missing})]
    if (data["dispatch_country"] != "CN" or data["destination_country"] not in EU_COUNTRIES
        or any(data[key] is not True for key in ("eu_customs_territory", "general_consumer_goods", "non_dangerous"))):
        return [result("jurisdiction.consumer_materials", "not_covered", "本试行包仅覆盖中国发运至欧盟海关适用领土的一般非危险消费品申明；其他线路、领土与货类继续通用翻译，专项未覆盖")]
    complete_parse = bool(documents) and all(doc.get("segment_count") and doc.get("page_count")
        and not INCOMPLETE_FLAGS.intersection(doc.get("quality_flags", [])) and not doc.get("is_synthetic")
        and set(range(1, doc["page_count"] + 1)).issubset(set(doc.get("seen_pages", []))) for doc in documents)
    output = []
    for kind, label in MATERIAL_LABELS.items():
        selected = [material for material in data["materials"] if material["kind"] == kind]
        rule = "jurisdiction.consumer_materials." + kind
        if len(selected) > 1:
            output.append(result(rule, "undetermined", f"{label}存在重复材料选择，须明确本次核对的原件版本"))
            continue
        candidates = [row for row in source_rows if MATERIAL_TITLES[kind].search(row["text"])]
        if selected:
            material = selected[0]
            titles = [row for row in candidates if row["document_id"] == material["source"]["document_id"] and row["document_version"] == material["source"]["document_version"]]
            if titles and material["same_consignment"] is True and material["basis"].strip():
                output.append(result(rule, "passed", f"已提供有直接标题来源并明确同票确认依据的{label}候选；不判断签发效力、真实性或法律完整性",
                    tuple(row["id"] for row in titles), evidence={"material_status": "provided", "selected_material": material, "title_source_ids": [row["id"] for row in titles]}))
            else:
                output.append(result(rule, "undetermined", f"所选{label}缺少当前冻结原件标题来源或明确同票确认依据，不能视为已核提供",
                    tuple(row["id"] for row in candidates) or all_ids, evidence={"selected_material": material}))
            continue
        if candidates or data["material_set_complete"] is not True or not complete_parse:
            output.append(result(rule, "undetermined", f"{label}存在未确认的标题候选，或本次材料集合/解析尚不完整；不能判断本次确实未提供",
                tuple(row["id"] for row in candidates) or all_ids, evidence={"candidate_source_ids": [row["id"] for row in candidates], "parse_complete": complete_parse}))
            continue
        blocking = kind in data["required_materials"] and bool(data["required_materials_basis"].strip())
        explanation = (f"本次已确认完整材料中未提供用户明确本票业务必需的{label}；请按已记录的合同/委托依据补充，这不是法定义务判定" if blocking
                       else f"本次已确认完整材料中未发现{label}标题候选；请按本票业务要求核对是否需要，不推定为法定必需")
        output.append(result(rule, "failed" if blocking else "undetermined", explanation, blocked=blocking,
            evidence={"material_status": "missing_from_declared_complete_set", "user_required": kind in data["required_materials"], "business_basis": data["required_materials_basis"]}))
    return output


def evaluate_certificate(inputs: dict | None, source_rows: list[dict], documents: list[dict]):
    from app.review.rules import RuleResult

    data=CertificateInputs.model_validate(inputs or {}).model_dump(mode="json")
    context={"rule_pack_version":CERTIFICATE_PACK,"legal_review_status":"pending","sources":SOURCES,
             "effective_window":{"from":"2026-07-06","before":"2026-10-15"},"declared_inputs":data,
             "assessment_scope":"document_provided_only","authenticity":"not_assessed","customs_compliance":"not_assessed"}
    all_ids=tuple(row["id"] for row in source_rows)
    def result(status, explanation, refs=all_ids, *, blocked=False, evidence=None):
        return RuleResult("jurisdiction.phytosanitary_certificate",status,explanation,refs,
                          calculation={**context,"evidence":evidence or {}},severity="red" if blocked else "yellow",blocks_export=blocked)
    if datetime.now(UTC).date() >= date(2026,10,15):
        return result("not_covered","本试行包依据复核窗口已到期；须核对新生效官方文本后发布新版本，未自动沿用旧证书结论")
    required=("origin_country","dispatch_country","destination_country","eu_plant_health_territory","movement","commodity","import_date")
    missing=[key for key in required if data[key] is None]
    if missing or not data["declaration_reason"].strip():
        return result("undetermined","适用事实或确认依据不完整；不能推断证书要求",evidence={"missing_inputs":missing+( [] if data["declaration_reason"].strip() else ["declaration_reason"] )})
    if (data["origin_country"]!="CN" or data["dispatch_country"]!="CN" or data["destination_country"] not in EU_COUNTRIES
        or data["eu_plant_health_territory"] is not True or data["movement"]!="ordinary_import"
        or not "2026-07-06"<=data["import_date"]<"2026-10-15"):
        return result("not_covered","线路、领土、特殊用途或进口日期超出本试行包；专项法规未评估")
    if data["commodity"] in {"pineapples","coconuts","durians","bananas","dates"}:
        exempt_sources=[row for row in source_rows if re.search(EXEMPT_GOODS[data["commodity"]],row["text"],re.I)]
        if any(re.search(r"\bfresh\s+apples\b|鲜苹果|新鲜苹果",row["text"],re.I) for row in source_rows):
            return result("undetermined","冻结原件同时存在鲜苹果货描，不能按单一五果免证申明豁免混合或矛盾材料")
        if not exempt_sources:
            return result("undetermined","免证水果申明没有当前冻结原件的直接货描来源；不据此作例外结论")
        return result("not_applicable","明确申明为 Annex XI Part C 五果例外；仅本植物检疫证书要求不适用，其他法规未评估",
                      tuple(row["id"] for row in exempt_sources),evidence={"exemption":"Annex XI Part C","commodity":data["commodity"]})
    if data["commodity"]!="fresh_apples":return result("not_covered","货物不在本鲜苹果材料包范围；未作其他货类法规判断")
    if data["stated_code"] is None:return result("undetermined","缺少原件注明的 HS/CN 编码；本包不自动归类")
    if data["stated_code"] not in {"080810","08081010","08081080"}:return result("not_covered","申明编码超出本包，不能自动改写为鲜苹果编码")
    cargo=[row for row in source_rows if re.search(r"\bfresh\s+(?:or\s+chilled\s+)?apples\b|鲜苹果|新鲜苹果",row["text"],re.I)]
    code=[row for row in source_rows if re.search(r"(?<!\d)"+re.escape(data["stated_code"])+r"(?!\d)",row["text"])]
    if not cargo or not code:return result("undetermined","鲜苹果货描或申明编码没有当前冻结原件的直接来源；不得只凭申明判缺证")
    cargo_ids=tuple(dict.fromkeys(row["id"] for row in cargo+code))
    selected=data["certificate_source"]
    if selected:
        cert_rows=[row for row in source_rows if row["document_id"]==str(selected.get("document_id")) and row["document_version"]==selected.get("document_version")]
        titles=[row for row in cert_rows if CERTIFICATE_TITLE.search(row["text"])]
        if not titles or data["certificate_same_consignment"] is not True:
            return result("undetermined","所选材料没有明确植物检疫证书标题来源或同票归属未确认；不能视为已提供",cargo_ids+tuple(row["id"] for row in cert_rows))
        return result("passed","已提供当前冻结原件中的同票植物检疫证书材料；真实性、签发效力、检疫附加要求及海关合规未评估",
                      tuple(dict.fromkeys(cargo_ids+tuple(row["id"] for row in titles))),evidence={"material_status":"provided","certificate_source":selected,"certificate_title_source_ids":[row["id"] for row in titles]})
    complete_parse=bool(documents) and all(doc.get("segment_count") and not INCOMPLETE_FLAGS.intersection(doc.get("quality_flags",[]))
        and not doc.get("is_synthetic") and (not doc.get("page_count") or set(range(1,doc["page_count"]+1)).issubset(set(doc.get("seen_pages",[])))) for doc in documents)
    candidate_titles=[row for row in source_rows if CERTIFICATE_TITLE.search(row["text"])]
    if data["material_set_complete"] is not True or not complete_parse or candidate_titles:
        return result("undetermined","完整材料集合或可靠解析尚未确认，或发现未确认归属的证书候选；不能判缺证",cargo_ids)
    return result("failed","本次已确认完整的冻结材料中缺少中国鲜苹果进口欧盟所需的植物检疫证书；请补充同票材料，未判断主管机关是否签发或海关是否放行",cargo_ids,blocked=True,evidence={"material_status":"missing_from_declared_complete_set"})
