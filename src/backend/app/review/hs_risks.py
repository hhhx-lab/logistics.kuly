"""核对已有原件HS事实，不以词面或同货品确认代替税则归类。"""
from __future__ import annotations

import json
import re
from collections import Counter

from app.review.core_v2 import direct_sources
from app.review.rules import RuleResult


def hs_rules(lines, sources, matches):
    source_by_id = {row["id"]: row for row in sources}
    by_id = {row["id"]: row for row in lines if row.get("id")}
    results = []
    for line in lines:
        if line.get("hs_code") is None:
            continue
        raw = line["hs_code"]
        value = raw.strip() if isinstance(raw, str) else ""
        proven = bool(value and line.get("logical_document_id")) and direct_sources(line, source_by_id, value)
        shape = bool(re.fullmatch(r"[0-9]{6}", value))
        results.append(RuleResult("line.lexical.hs_code", "passed" if proven and shape else "undetermined",
            "原件HS六位数字形式核对；不判断编码存在、货描匹配或归类正确" if proven and shape else "明细HS来源或六位形式不足；扩展码缺国别依据，不判断归类",
            tuple(line.get("source_segment_ids", [])), calculation={"line_id":line.get("id"), "raw_value":raw,
                "direct_source":proven, "assessment":"lexical_only", "candidate_only":True}))

    edges = [edge for edge in matches if edge.get("state") == "confirmed"]
    degrees = Counter(value for edge in edges for value in (edge.get("left_line_id"), edge.get("right_line_id")))
    for edge in edges:
        left, right = by_id.get(edge.get("left_line_id")), by_id.get(edge.get("right_line_id"))
        basis = edge.get("basis") or {}
        if isinstance(basis, str):
            try:
                basis = json.loads(basis)
            except (ValueError, TypeError):
                basis = {}
        if not isinstance(basis, dict):
            basis = {}
        mapping = bool(left and right and left.get("logical_document_id") and right.get("logical_document_id")
            and left["logical_document_id"] != right["logical_document_id"] and edge.get("decided_by")
            and edge.get("relation") == "one_to_one" and degrees[left["id"]] == degrees[right["id"]] == 1
            and basis.get("same_goods") is True and basis.get("complete") is True
            and isinstance(basis.get("scope_key"), str) and basis["scope_key"].strip() and basis.get("role") == "goods"
            and isinstance(basis.get("reason"), str) and basis["reason"].strip())
        facts = []
        for line in (left, right):
            line = line or {}
            raw, description = line.get("hs_code"), line.get("description")
            code = raw.strip() if isinstance(raw, str) else ""
            proven = bool(code and isinstance(description, str) and description.strip()
                and re.fullmatch(r"[0-9]{6}(?:[0-9]{2})?(?:[0-9]{2})?", code)
                and direct_sources(line, source_by_id, code) and direct_sources(line, source_by_id, description.strip()))
            facts.append({"line_id":line.get("id"), "logical_document_id":line.get("logical_document_id"),
                "hs_code":raw, "description":description, "source_segment_ids":line.get("source_segment_ids", []),
                "direct_facts":proven, "six_digit_prefix":code[:6] if proven else None})
        proven = mapping and all(fact["direct_facts"] for fact in facts)
        disagreement = bool(proven and facts[0]["six_digit_prefix"] != facts[1]["six_digit_prefix"])
        status = "failed" if disagreement else "not_covered" if proven else "undetermined"
        explanation = ("人工确认同货品的两侧原件货描与HS事实中，六位编码不同；请复核货描、编码及对应关系，不给归类结论" if disagreement else
            "同货品两侧六位编码未见差异；国别扩展与货描归类未评估，相同编码不能证明归类正确" if proven else
            "同货品一对一确认、完整范围或两侧货描与HS直接来源不足，不能判断事实是否一致")
        results.append(RuleResult("hs.same_goods_source_consistency", status, explanation,
            tuple(dict.fromkeys(sid for fact in facts for sid in fact["source_segment_ids"])),
            calculation={"line_match_id":edge.get("id"), "decided_by":edge.get("decided_by"), "basis":basis,
                "mapping_confirmed":mapping, "facts":facts, "comparison":"six_digit_source_facts_only",
                "country_extensions":"not_assessed", "candidate_only":True}, severity="yellow", blocks_export=False))
    if not edges:
        results.append(RuleResult("hs.same_goods_source_consistency", "undetermined",
            "尚无人工确认同货品行关系，不根据相似货描或行号推断HS一致性"))
    results.append(RuleResult("hs.description_classification", "not_covered",
        "未加载经审定的权威税则及适用日期、目的国/货类边界和专业复核人；货描与编码归类未评估",
        calculation={"authoritative_tariff":"missing", "applicable_date":"unverified", "destination_category_scope":"unverified",
            "professional_reviewer":"missing", "assessment":"not_assessed"}))
    return results
