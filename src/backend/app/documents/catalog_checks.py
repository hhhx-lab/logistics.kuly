"""按目录核对源文标签与抽取覆盖；没有法定必填假设。"""
from __future__ import annotations
import re
from decimal import Decimal, InvalidOperation
from app.documents.extraction import field_schema, field_label
from app.review.rules import RuleResult
from app.documents.type_catalog import CATALOG_VERSION, document_type_label

# 只检查原件明示的标签。自由段落中提及某编号不等于存在一个表单字段。
_LABELS = {
    "invoice_no": ("invoice no", "invoice number", "发票号", "发票编号"),
    "packing_list_no": ("packing list no", "箱单号", "装箱单号"),
    "bill_of_lading_no": ("bill of lading no", "b/l no", "提单号"),
    "air_waybill_no": ("air waybill no", "awb no", "空运单号"),
    "waybill_no": ("waybill no", "consignment no", "运单号"),
    "certificate_no": ("certificate no", "certificate number", "证书号", "证书编号"),
    "license_no": ("license no", "licence no", "许可证号"),
    "policy_no": ("policy no", "保险单号"),
    "declaration_no": ("declaration no", "申报编号", "报关单号"),
    "batch_no": ("batch no", "批次号"), "lot_no": ("lot no", "货批号"),
    "un_number": ("un no", "un number", "un/ id no", "un编号"),
    "cas_no": ("cas no", "cas number", "cas编号"),
    "botanical_name": ("botanical name", "植物学名"),
    "end_use": ("end use", "最终用途"),
    "material_grade": ("material grade", "材质牌号", "矿石品位"),
    "temperature_min": ("minimum temperature", "最低温度"),
    "temperature_max": ("maximum temperature", "最高温度"),
    "monetary_instrument": ("monetary instrument", "货币种类"),
}


def labeled_content_sources(labels, sources):
    matches=[]
    for source in sources:
        text=source["text"].strip()
        if any(re.search(r"(?:^|[\n|])\s*"+re.escape(label)+r"\s*(?:[.:：#]|\|)\s*\S",text,re.I) for label in labels):
            matches.append(source);continue
        # MinerU会把表格标签与内容拆成相邻单元格，必须按实际同区段/行/列关系确认。
        if source.get("structure") != "table_cell" or source.get("table_row") is None or source.get("table_col") is None:
            continue
        if not any(text.casefold().rstrip(" .:：#") == label.casefold() for label in labels):
            continue
        values=[value for value in sources if value.get("structure")=="table_cell" and value.get("section_id")==source.get("section_id")
            and value.get("table_row")==source["table_row"] and value.get("table_col")==source["table_col"]+1 and str(value.get("text") or "").strip()]
        if values:matches.extend([source,*values])
    return list({source["id"]:source for source in matches}.values())


def evaluate_catalog_checks(*, logical_documents, fields, source_rows):
    results=[]
    for logical in logical_documents:
        lid=logical["logical_document_id"]
        code=logical["document_type"]
        profiles=logical.get("cargo_profile_ids",[])
        schema=field_schema(code,profiles)
        local_fields=[f for f in fields if f["logical_document_id"]==lid]
        sources=[s for s in source_rows if s["logical_document_id"]==lid]
        observed={f["field_key"] for f in local_fields if str(f.get("value") or "").strip()}
        permitted={f["field_key"] for f in schema["fields"]}
        coverage_gaps=[]
        for key,labels in _LABELS.items():
            if key not in permitted or key in observed:continue
            matches=labeled_content_sources(labels,sources)
            # 超长说明段/一句带很多字段的文本可能仍有未抽取值，提供实际来源供人工判断。
            if matches:coverage_gaps.append((key,matches))
        for key,matches in coverage_gaps:
            results.append(RuleResult("catalog.extraction_coverage", "undetermined",
                f"原件出现“{field_label(key)}”标签及内容，但当前字段未提取到对应值。请定位核对识别结果；可补充材料、提出处理意见，或确认不影响后忽略。",
                tuple(s["id"] for s in matches[:5]), calculation={"catalog_version":CATALOG_VERSION,"logical_document_id":lid,
                    "document_type":code,"cargo_profile_ids":profiles,"field_key":key,"gap":"source_label_not_extracted"},severity="yellow"))
        if not coverage_gaps:
            results.append(RuleResult("catalog.extraction_coverage", "passed",
                f"已按{document_type_label(code)}与所选货物领域核对当前有明确标签的提取覆盖；未作法定必填或合规判断。",
                calculation={"catalog_version":CATALOG_VERSION,"logical_document_id":lid,"document_type":code,
                    "cargo_profile_ids":profiles,"review_focus":schema["review_focus"],"observed_field_count":len(local_fields)}))
        temps={key:[f for f in local_fields if f["field_key"]==key] for key in ("temperature_min","temperature_max","temperature_unit")}
        if all(len(value)==1 for value in temps.values()):
            low,high=(temps[k][0] for k in ("temperature_min","temperature_max"))
            try:
                a,b=Decimal(str(low["value"])),Decimal(str(high["value"]))
                comparable=a.is_finite() and b.is_finite()
            except InvalidOperation:comparable=False
            if comparable and a>b:
                rows=[low,high,temps["temperature_unit"][0]]
                results.append(RuleResult("catalog.temperature_range", "failed", "同一原件明确同单位温度范围的最低值大于最高值，请核对识别或原始记录。",
                    tuple(dict.fromkeys(i for row in rows for i in row.get("source_segment_ids",[]))),tuple(row["id"] for row in rows),
                    calculation={"logical_document_id":lid,"minimum":str(a),"maximum":str(b),"unit":temps["temperature_unit"][0]["value"]},severity="red",blocks_export=True))
    return results
