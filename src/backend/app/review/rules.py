"""core-document@1：确定性核对；口径缺失与法规未覆盖显式留六态记录。"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Literal

RuleStatus = Literal["passed", "failed", "not_applicable", "not_covered", "undetermined", "not_run"]
RULE_PACK = "core-document@1"
RULE_PACK_V2 = "core-document@2"
RULE_PACK_V3 = "core-document@3"


@dataclass(frozen=True)
class RuleResult:
    rule_id: str
    status: RuleStatus
    explanation: str
    source_segment_ids: tuple[str, ...] = ()
    input_field_ids: tuple[str, ...] = ()
    calculation: dict = field(default_factory=dict)
    severity: str = "yellow"
    blocks_export: bool = False

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.__dict__, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def decimal_value(value) -> Decimal | None:
    if value is None or isinstance(value, (float, bool)):
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


def evaluate_rules(*, documents: list[dict], fields: list[dict], lines: list[dict], rule_pack: str, line_matches: list[dict] | None = None,
                   jurisdiction_inputs: dict | None = None, source_rows: list[dict] | None = None, rule_inputs: list[dict] | None = None,
                   reviewed_publications: list[dict] | None = None) -> list[RuleResult]:
    from app.review.jurisdiction import CERTIFICATE_PACK, MATERIAL_PACK, evaluate_certificate, evaluate_consumer_materials
    if rule_pack not in {RULE_PACK,RULE_PACK_V2,RULE_PACK_V3,CERTIFICATE_PACK,MATERIAL_PACK}:
        raise ValueError("未注册的规则包")
    base_rule_pack = RULE_PACK_V3 if rule_pack == MATERIAL_PACK else rule_pack
    results: list[RuleResult] = []
    for doc in documents:
        pages = doc.get("page_count")
        seen = set(doc.get("seen_pages", []))
        missing = sorted(set(range(1, pages + 1)) - seen) if pages else []
        flags = doc.get("quality_flags", [])
        failed = not doc.get("segment_count") or bool(missing) or any(x in flags for x in ("page_needs_ocr", "ocr_unavailable"))
        status: RuleStatus = "failed" if failed else "undetermined" if not pages or doc.get("is_synthetic") else "passed"
        results.append(RuleResult("material.coverage", status,
            "缺少可核验文本或解析页覆盖不足" if failed else "源页数未知或假解析，不能确认覆盖" if status == "undetermined" else "已核验已知原件页数与解析页覆盖",
            tuple(doc.get("source_segment_ids", [])), calculation={"parse_artifact_id":doc["id"], "page_count": pages, "seen_pages": sorted(seen), "missing_pages": missing},
            severity="red" if failed else "yellow", blocks_export=failed))
        if flags:
            quality_labels = {"image_blur_suspected":"疑似模糊，核对文字或重拍", "image_glare_suspected":"疑似反光／过曝，核对遮挡或重拍",
                "image_edge_content":"内容接近裁边，检查缺边或补件", "image_resolution_low":"分辨率偏低，核对小字或重拍"}
            descriptions = [quality_labels[flag] for flag in flags if flag in quality_labels]
            results.append(RuleResult("material.quality", "undetermined", "存在解析质量疑点，需人工查看" + ("：" + "；".join(descriptions) if descriptions else ""), tuple(doc.get("source_segment_ids", [])), calculation={"quality_flags":flags}))
    for line in lines:
        refs = tuple(line.get("source_segment_ids", []))
        q, p, amount = (decimal_value(line.get(k)) for k in ("quantity", "unit_price", "amount"))
        if line.get("document_type") == "commercial_invoice":
            if any(v is None for v in (q, p, amount)) or not line.get("currency"):
                results.append(RuleResult("invoice.line_arithmetic", "undetermined", "行数量、单价、金额或币种不完整", refs))
            else:
                computed = (q * p).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                difference = abs(computed - amount)
                failed = difference > Decimal("0.01")
                results.append(RuleResult("invoice.line_arithmetic", "failed" if failed else "passed", "同币种行数量×单价与行金额核对", refs,
                    calculation={"quantity":str(q), "unit_price":str(p), "amount":str(amount), "currency":line["currency"], "computed":str(computed), "difference":str(difference), "tolerance":"0.01", "rounding":"ROUND_HALF_UP"},severity="red", blocks_export=failed))
        net, gross = decimal_value(line.get("net_weight")), decimal_value(line.get("gross_weight"))
        if line.get("document_type") == "packing_list":
            if net is None or gross is None or not line.get("weight_unit"):
                results.append(RuleResult("packing.weight", "undetermined", "同一箱单行毛净重或共同单位缺失", refs))
            else:
                failed = net > gross
                results.append(RuleResult("packing.weight", "failed" if failed else "passed", "同箱单行、同单位净重不得大于毛重", refs,
                    calculation={"net_weight":str(net),"gross_weight":str(gross),"unit":line["weight_unit"]},severity="red",blocks_export=failed))
    # 同主体同字段重复候选冲突才提示；不同单证角色不能硬合并。
    groups: dict[tuple, list[dict]] = {}
    for f in fields:
        groups.setdefault((f.get("logical_document_id"), f.get("field_key"), f.get("role")), []).append(f)
    for group in groups.values():
        values = {f.get("value") for f in group if f.get("value") is not None}
        if len(values) > 1:
            results.append(RuleResult("field.duplicate_candidates", "undetermined", "同角色字段存在多个候选，需核对是否属于不同编号、金额口径或源位置；尚未证实原件冲突", tuple(dict.fromkeys(i for f in group for i in f.get("source_segment_ids", []))), tuple(f["id"] for f in group), calculation={"values":sorted(values)}))
    results.extend(general_consistency_rules(fields,lines,party_mapping=base_rule_pack not in {RULE_PACK_V2,RULE_PACK_V3}))
    results.extend(matched_measurement_rules(fields,lines,line_matches or []))
    results.append(RuleResult("jurisdiction.hs_certificate", "not_covered", "未加载经审定生效的HS及目的国证书规则包；法规未评估" if rule_pack!=CERTIFICATE_PACK else "HS归类及其他目的国证书规则未覆盖；试行包仅另行检查鲜苹果植物检疫证书材料"))
    if not lines:
        results.append(RuleResult("line.arithmetic", "not_run", "当前解析业务版未形成结构化行，行算术未运行"))
    results.extend(required_and_format_rules(documents,fields,lines))
    if base_rule_pack in {RULE_PACK_V2,RULE_PACK_V3}:
        from app.review.core_v2 import lexical_rules,packing_total_rules
        results.extend(lexical_rules(fields,lines,source_rows or []))
        results.extend(packing_total_rules(documents,fields,lines,source_rows or []))
    if base_rule_pack == RULE_PACK_V3:
        from app.review.core_v3 import identity_rules, date_sequence_rules
        results.extend(identity_rules(fields, lines, source_rows or []))
        from app.review.hs_risks import hs_rules
        results.extend(hs_rules(lines, source_rows or [], line_matches or []))
        from app.review.controlled_rules import party_mapping_rules, controlled_packing_rules
        results = [result for result in results if result.rule_id != "party.role_mapping"]
        results.extend(party_mapping_rules(fields, source_rows or [], rule_inputs or []))
        results.extend(controlled_packing_rules(documents, lines, source_rows or [], rule_inputs or []))
        date_fields = [dict(row) for row in fields]
        for decision in rule_inputs or []:
            if decision["kind"] != "date_transport":
                continue
            p = decision["payload"]
            for row in date_fields:
                if row.get("id") in p.get("field_assertion_ids", []):
                    row.update(date_basis="local_calendar", timezone=p["timezone"], event_scope=decision["scope_key"], date_basis_confirmed_by=decision["decided_by"])
        results.extend(date_sequence_rules(date_fields, source_rows or []))
    if reviewed_publications:
        from app.review.publications import description_candidates
        results.extend(description_candidates(lines, source_rows or [], reviewed_publications, jurisdiction_inputs))
    if rule_pack == CERTIFICATE_PACK:
        results.append(evaluate_certificate(jurisdiction_inputs,source_rows or [],documents))
    if rule_pack == MATERIAL_PACK:
        results.extend(evaluate_consumer_materials(jurisdiction_inputs, source_rows or [], documents))
    return results


def matched_measurement_rules(fields,lines,matches):
    """Only human-confirmed one-to-one complete goods lines are comparable."""
    by_id={line.get('id'):line for line in lines if line.get('id')}
    references={}
    for row in fields:
        if row.get('field_key') in {'transport_reference','bill_of_lading_no','booking_no'} and row.get('value'):
            references.setdefault(row.get('logical_document_id'),set()).add(str(row['value']).strip())
    edges=[edge for edge in matches if edge.get('state')=='confirmed']
    degrees=Counter(value for edge in edges for value in (edge.get('left_line_id'),edge.get('right_line_id')))
    results=[]
    for edge in edges:
        left=by_id.get(edge.get('left_line_id'));right=by_id.get(edge.get('right_line_id'))
        if not left or not right:continue
        refs=tuple(dict.fromkeys(left.get('source_segment_ids',[])+right.get('source_segment_ids',[])))
        basis=edge.get('basis') or {}
        if isinstance(basis,str):
            try:basis=json.loads(basis)
            except (ValueError,TypeError):basis={}
        if not isinstance(basis,dict):basis={}
        left_ref=references.get(left.get('logical_document_id'),set());right_ref=references.get(right.get('logical_document_id'),set())
        proven=(left.get('logical_document_id')!=right.get('logical_document_id') and len(left_ref)==1 and left_ref==right_ref
            and edge.get('decided_by') and edge.get('relation')=='one_to_one'
            and degrees.get(left.get('id'))==degrees.get(right.get('id'))==1
            and basis.get('same_goods') is True and basis.get('complete') is True and basis.get('scope_key')
            and basis.get('role')=='goods' and basis.get('packaging_level')
            and left.get('source_segment_ids') and right.get('source_segment_ids'))
        for key,unit_key in [('quantity','quantity_unit'),('package_count','package_unit'),('net_weight','weight_unit'),('gross_weight','weight_unit')]:
            if left.get(key) is None and right.get(key) is None:continue
            a,b=decimal_value(left.get(key)),decimal_value(right.get(key))
            unit=left.get(unit_key);same_unit=bool(unit) and unit==right.get(unit_key)
            context={'left_line_id':left['id'],'right_line_id':right['id'],'line_match_id':edge.get('id'),'basis':basis,'transport_reference':sorted(left_ref),'unit':unit,'left':str(a) if a is not None else None,'right':str(b) if b is not None else None,'tolerance':'0'}
            if not proven or not same_unit or a is None or b is None:
                results.append(RuleResult('cross_document.'+key,'undetermined','匹配、同批次/货品/角色/层级/完整口径或共同单位未证实，不能判数值冲突',refs,calculation=context))
            else:
                failed=a!=b
                results.append(RuleResult('cross_document.'+key,'failed' if failed else 'passed','人工确认一对一、同运输参考与完整同口径的'+key+'核对',refs,calculation=context,severity='red',blocks_export=failed))
    if not results:results.append(RuleResult('cross_document.measurement','undetermined','没有可核验的人工确认行匹配，不按同票或行号推断数量重量关系'))
    return results


def required_and_format_rules(documents,fields,lines):
    """首版交付模板的最低结构要求；未提取不是已证实原件缺失。"""
    results=[]
    logicals=[logical for doc in documents for logical in doc.get("logical_documents",[doc])]
    required={"commercial_invoice":("invoice_no",("seller","buyer")),"packing_list":("packing_list_no",("shipper","consignee")),"bill_of_lading":("bill_of_lading_no",("shipper","consignee"))}
    for doc in logicals:
        kind=doc.get("document_type");logical_id=doc.get("logical_document_id")
        context={"logical_document_id":logical_id,"document_type":kind,"rule_basis":"first-delivery-template@1"}
        if kind not in required:
            status="not_applicable" if kind=="supporting" else "undetermined"
            results.append(RuleResult("document.required_fields",status,"其他佐证材料不强套必填要求" if status=="not_applicable" else "单据用途或类型未确认，必填要求无法判断",tuple(doc.get("source_segment_ids",[])),calculation=context))
            continue
        if not logical_id:
            results.append(RuleResult("document.required_fields","undetermined","尚无逻辑单据与字段来源的明确对应，不能判断必填覆盖",tuple(doc.get("source_segment_ids",[])),calculation=context))
            continue
        rows=[row for row in fields if row.get("logical_document_id")==logical_id and str(row.get("value") or "").strip() and row.get("source_segment_ids")]
        number,roles=required[kind];missing=[]
        if not any(row.get("field_key") in {number,"document_no"} for row in rows):missing.append("单据编号")
        for role in roles:
            if not any(row.get("field_key") in {"name",role+"_name"} and row.get("role")==role for row in rows):missing.append(role+"名称")
        item_rows=[row for row in lines if row.get("logical_document_id")==logical_id]
        if not any(row.get("field_key") in {"goods_description","description_of_goods","commodity_description"} for row in rows) and not any(row.get("description") and row.get("source_segment_ids") for row in item_rows):missing.append("货描")
        sources=tuple(dict.fromkeys(doc.get("source_segment_ids",[])+[sid for row in rows for sid in row.get("source_segment_ids",[])]))
        results.append(RuleResult("document.required_fields","undetermined" if missing else "passed",
            "必需候选未提取完整，请定位原件补充或裁定；不据此宣称原件缺字段" if missing else "当前模板最低编号、主体和货描均有原文候选，仍须人工确认",
            sources,tuple(row["id"] for row in rows if row.get("id")),calculation={**context,"missing_candidates":missing},severity="red" if missing else "yellow",blocks_export=bool(missing)))
    if not logicals:results.append(RuleResult("document.required_fields","not_run","尚无逻辑单据输入，必填校验未运行"))
    formatted=0
    for row in fields:
        key=row.get("field_key");value=str(row.get("value") or "").strip()
        if key not in {"email","container_no"} or not value:continue
        formatted+=1;source_ids=tuple(row.get("source_segment_ids",[]))
        valid=bool(re.fullmatch(r"[^\s@]+@[^\s@.]+(?:\.[^\s@.]+)+",value)) if key=="email" else bool(re.fullmatch(r"[A-Z]{3}[UJZ]\d{7}",value))
        status="undetermined" if not source_ids or not valid else "passed"
        results.append(RuleResult("field.format."+key,status,"邮箱词面形式" if key=="email" else "柜号ISO 6346基本词面形式（不验证校验位或注册归属）",source_ids,(row["id"],) if row.get("id") else (),
            calculation={"raw_value":value,"shape_valid":valid,"rule_basis":"email-basic@1" if key=="email" else "iso6346-shape@1","candidate_only":status=="undetermined"}))
    if not formatted:results.append(RuleResult("field.format","not_run","尚无有值邮箱或柜号候选，形式校验未运行"))
    return results


def general_consistency_rules(fields,lines,*,party_mapping=True):
    results=[]
    def refs(rows):return tuple(dict.fromkeys(i for row in rows for i in row.get("source_segment_ids",[])))
    def ids(rows):return tuple(row["id"] for row in rows if row.get("id"))
    by_document={}
    for row in fields:by_document.setdefault(row.get("logical_document_id"),[]).append(row)
    totals=0
    for document_id,rows in by_document.items():
        total_rows=[row for row in rows if row.get("field_key") in {"total_amount","invoice_total","total"}]
        currency_rows=[row for row in rows if row.get("field_key")=="currency"]
        currency_values={row.get("value") for row in currency_rows}
        item_rows=[row for row in lines if row.get("logical_document_id")==document_id and row.get("document_type")=="commercial_invoice"]
        if not total_rows:continue
        totals+=1
        amounts=[decimal_value(row.get("amount")) for row in item_rows]
        raw_total=total_rows[0].get("numeric_value")
        total=decimal_value(raw_total if raw_total is not None else total_rows[0].get("value"))
        fee_rows=[row for row in rows if row.get("field_key") in {"freight","insurance","tax","discount"}]
        scope_rows=[row.get("value","").lower() for row in rows if row.get("field_key") in {"total_basis","amount_basis"}]
        known_basis=not fee_rows or any(value in {"goods_only","goods_value","货值合计"} for value in scope_rows)
        complete=bool(item_rows) and all(value is not None for value in amounts)
        same_currency=len(currency_values)==1 and all(row.get("currency") in currency_values for row in item_rows)
        if len(total_rows)!=1 or total is None or not complete or not same_currency or not known_basis:
            results.append(RuleResult("invoice.total","undetermined","总额或完整行金额、币种/费用口径不一致，不能直接相加比较",refs(rows+item_rows),ids(total_rows),calculation={"logical_document_id":document_id,"complete_lines":complete,"same_currency":same_currency,"known_basis":known_basis}))
        else:
            computed=sum(amounts,Decimal("0"));difference=abs(computed-total);failed=difference>Decimal("0.01")
            results.append(RuleResult("invoice.total","failed" if failed else "passed","同币种明确货值口径下的行金额合计与总额核对",refs(total_rows+item_rows),ids(total_rows),
                calculation={"logical_document_id":document_id,"line_amounts":[str(value) for value in amounts],"computed":str(computed),"total":str(total),"difference":str(difference),"tolerance":"0.01"},severity="red",blocks_export=failed))
    if not totals:results.append(RuleResult("invoice.total","undetermined","尚无可比较的总额及完整行金额"))
    # 运输参考、柜号/封条、港口和相同事件日期只在同一明确运输组中核对。
    shipment_keys={}
    for doc,rows in by_document.items():
        values={str(row.get("value")) for row in rows if row.get("field_key") in {"transport_reference","bill_of_lading_no","booking_no"} and row.get("value")}
        if len(values)==1:shipment_keys[doc]=next(iter(values))
    comparable=("container_no","seal_no","port_of_loading","port_of_discharge","vessel_name","voyage_no","on_board_date","departure_date","arrival_date")
    comparisons=0
    for field_key in comparable:
        groups={}
        for row in fields:
            if row.get("field_key")!=field_key or not row.get("value"):continue
            shipment=row.get("shipment_group") or shipment_keys.get(row.get("logical_document_id"))
            if shipment:groups.setdefault((shipment,row.get("role")),[]).append(row)
        for group in groups.values():
            if len({row.get("logical_document_id") for row in group})<2:continue
            comparisons+=1;values={str(row["value"]).strip() for row in group};failed=len(values)>1
            results.append(RuleResult("cross_document."+field_key,"failed" if failed else "passed","同运输参考、同角色的"+field_key+"核对",refs(group),ids(group),calculation={"values":sorted(values)},severity="red",blocks_export=failed))
    if not comparisons:results.append(RuleResult("cross_document.transport","undetermined","跨单证尚无共同运输参考或明确运输组，不猜批次对应"))
    if not party_mapping:
        results.append(RuleResult('party.role_mapping','not_covered','首版尚无受控同主体映射输入，未实施跨角色主体一致性核对；各角色分别保存'))
        return results
    # @1 历史行为保留；@2 不接受非受控实体映射。
    parties={}
    for row in fields:
        if row.get("field_key") not in {"name","address","company_no","seller_name","buyer_name","shipper_name","consignee_name"}:continue
        role=row.get("role");entity=row.get("entity_group")
        if entity:parties.setdefault((entity,"address" if "address" in row["field_key"] else "company_no" if "company_no" in row["field_key"] else "name"),[]).append(row)
    party_checks=0
    for group in parties.values():
        if len(group)<2:continue
        party_checks+=1;values={str(row.get("value","")).strip() for row in group};failed=len(values)>1
        results.append(RuleResult("party.confirmed_mapping","failed" if failed else "passed","已确认同主体映射的角色字段核对",refs(group),ids(group),calculation={"values":sorted(values)},severity="red",blocks_export=failed))
    if not party_checks:results.append(RuleResult("party.role_mapping","undetermined","没有已确认同主体映射，Seller/Buyer/Shipper/Consignee各自保留"))
    return results
