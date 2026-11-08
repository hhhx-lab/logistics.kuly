from __future__ import annotations
from app.languages import language_name

from uuid import UUID

import re
from sqlalchemy import and_, case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import NotFoundOrForbidden
from app.conversation.models import ReferenceSnapshot
from app.documents.evidence_models import FieldAssertion, ParseArtifact, SourceAnchor, SourceSegment
from app.documents.models import DocumentVersion
from app.prompts.schemas import AnswerDraft, AnswerParagraph


class EvidenceClarification(ValueError):
    pass

FIELD_ALIASES = {"金额": "amount", "总额": "total_amount", "总金额": "total_amount", "发票号": "invoice_number",
    "提单号": "bill_of_lading_number", "毛重": "gross_weight", "净重": "net_weight", "数量": "quantity",
    "收货人": "consignee", "收件人": "consignee", "发货人": "shipper", "寄件人": "shipper", "买方": "buyer", "卖方": "seller", "单号": "document_number",
    "地址":"address", "邮箱":"email", "电话":"phone", "货物名称":"description",
    "品名":"description", "商品":"description", "货品":"description", "货描":"description", "货物":"description"}
FIELD_VARIANTS = {"invoice_number": {"invoice_number", "invoice_no", "invoice_id"},
    "bill_of_lading_number": {"bill_of_lading_number", "bill_of_lading_no", "bl_no", "b_l_no", "bl_number"},
    "document_number": {"invoice_number", "invoice_no", "bill_of_lading_number", "bill_of_lading_no", "bl_no", "document_no", "document_number"},
    "total_amount": {"total_amount", "invoice_total", "amount_total", "total"},
    "consignee": {"consignee", "consignee_name", "recipient", "recipient_name"},
    "shipper": {"shipper", "shipper_name", "sender", "sender_name"},
    "buyer": {"buyer", "buyer_name"}, "seller": {"seller", "seller_name"},
    "description": {"description", "goods_description"}}

# 检索词只用于查找授权原文，不作为字段值或角色的判定依据。
EVIDENCE_TERMS = {
    "description": ("description", "goods", "cargo", "product", "marchandise", "désignation", "produit", "descripción", "mercancía", "producto", "наименование", "товар", "груз"),
    "quantity": ("quantity", "qty", "quantité", "cantidad", "количество"),
    "gross_weight": ("gross weight", "poids brut", "peso bruto", "вес брутто"),
    "net_weight": ("net weight", "poids net", "peso neto", "вес нетто"),
    "seller": ("seller", "vendeur", "vendedor", "продавец"),
    "buyer": ("buyer", "acheteur", "comprador", "покупатель"),
    "shipper": ("shipper", "sender", "expéditeur", "expedidor", "отправитель"),
    "consignee": ("consignee", "recipient", "destinataire", "destinatario", "получатель"),
    "address": ("address", "adresse", "dirección", "domicilio", "адрес"),
    "phone": ("phone", "telephone", "téléphone", "teléfono", "телефон"),
    "email": ("email", "e-mail", "courriel", "correo", "электронная почта"),
    "invoice_number": ("invoice number", "invoice no", "numéro de facture", "número de factura", "номер счета"),
    "total_amount": ("total amount", "invoice total", "montant total", "importe total", "итого"),
}


FIELD_LABELS = {"invoice_number": "发票号", "invoice_no": "发票号", "invoice_id": "发票号", "bill_of_lading_number": "提单号", "bill_of_lading_no": "提单号", "bl_no": "提单号", "b_l_no": "提单号", "bl_number": "提单号", "document_no": "单证号", "document_number": "单证号", "total_amount": "总金额", "amount_total": "总金额", "invoice_total": "总金额", "total": "总金额", "amount": "金额", "quantity": "数量", "gross_weight": "毛重", "net_weight": "净重", "consignee": "收货人", "consignee_name": "收货人", "recipient": "收件人", "recipient_name": "收件人", "shipper": "发货人", "shipper_name": "发货人", "seller": "卖方", "seller_name": "卖方", "buyer": "买方", "buyer_name": "买方", "name": "名称", "address": "地址", "date": "日期", "currency": "币种", "description": "货物描述"}
ROLE_LABELS = {"seller": "卖方", "buyer": "买方", "shipper": "发货人", "consignee": "收货人", "notify_party": "通知方"}
STATE_LABELS = {"candidate": "待核对", "confirmed": "已确认", "conflict": "来源有冲突", "undetermined": "尚无法确定"}


def is_case_overview(question):
    return field_key(question) == "description" or bool(re.search(
        r"介绍|概览|概况|总结|概述|具体内容|全文|什么(?:货|内容)|overview|summari[sz]e|"
        r"品名|货描|货物名称|商品名称|(?:货物|商品|货品).{0,6}(?:是什么|有哪些|名称|描述)|"
        r"what.{0,20}(?:goods|cargo|products)|(?:read|describe).{0,30}(?:document|file)|这票.{0,8}运的是什么", question, re.I))


def render_fact_value(fact):
    value = fact.get("value")
    if value is None:
        value = (fact.get("card") or {}).get("excerpt", fact["canonical_text"])
    if fact["claim_kind"] in {"source_value", "adopted_value", "computed"} and fact.get("unit"):
        value = value_with_unit(value,fact["unit"])
    if fact["claim_kind"] == "computed":
        value = str(value) + "（程序计算）"
        if fact.get("comparison"):
            value += "；" + fact["comparison"]
    return str(value)


def value_with_unit(value,unit):
    text=str(value)
    if not unit or re.search(r"(?:^|\s|\d)"+re.escape(unit)+r"\s*$",text,re.I):return text
    return text+" "+unit


def fact_suffix_position(text, position, fact, *, complete=True):
    """只移除占位符后重复的同一单位；流式未决后缀暂留。"""
    unit = str(fact.get("unit") or "")
    if not unit or fact.get("claim_kind") not in {"source_value", "adopted_value", "computed"}:
        return position
    tail = text[position:]
    suffix = tail.lstrip(" \t")
    spaces = len(tail) - len(suffix)
    if not complete and unit.casefold().startswith(suffix.casefold()):
        return len(text)
    if not suffix.casefold().startswith(unit.casefold()):
        return position
    after = suffix[len(unit):]
    if unit[-1].isascii() and unit[-1].isalnum() and after and after[0].isascii() and after[0].isalnum():
        return position
    return position + spaces + len(unit)


def replace_fact_placeholders(text, facts):
    parts, position = [], 0
    for match in re.finditer(r"\[\[([^\[\]]+)\]\]", text):
        fact = facts[match[1]]
        parts.extend((text[position:match.start()], render_fact_value(fact)))
        position = fact_suffix_position(text, match.end(), fact)
    return "".join(parts) + text[position:]


def field_key(question: str) -> str | None:
    for alias in sorted(FIELD_ALIASES, key=len, reverse=True):
        if alias in question:
            return FIELD_ALIASES[alias]
    for key in FIELD_ALIASES.values():
        if key in question.lower():
            return key
    for key, terms in sorted(EVIDENCE_TERMS.items(), key=lambda item: item[0] == "description"):
        if any(re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", question, re.I) for term in terms):
            return key
    return None


def source_versions(frozen: list[dict]) -> list[dict]:
    versions = {}
    for item in frozen:
        candidates = item.get("source_versions", [])
        if item.get("document_id") and item.get("document_version"):
            candidates = [{"document_id": item["document_id"], "document_version": item["document_version"]}]
        for v in candidates:
            versions[(v["document_id"], v["document_version"])] = v
    return list(versions.values())


def grouped_scopes(frozen: list[dict]) -> dict[str, list[dict]]:
    """显式文件/子区段约束覆盖同文件的广范围；多个子区段保存并集。"""
    direct = [f for f in frozen if f.get("kind") in {"document", "section"}]
    direct_docs = {f["document_id"] for f in direct}
    historical = {v["document_id"]: v for f in frozen if f.get("kind") == "snapshot" for v in f.get("source_versions", [])}
    scopes = direct + [v for f in frozen for v in f.get("source_versions", []) if v["document_id"] not in direct_docs]
    result = {}
    for original in scopes:
        scope = {**original}
        if scope.get("document_id") in historical:
            fixed = historical[scope["document_id"]]
            if scope.get("document_version") != fixed.get("document_version"):
                raise EvidenceClarification("所选区段与历史快照原件版本不同，请重新选择对应版本。")
            scope["parse_artifact_id"] = fixed.get("parse_artifact_id")
        if scope.get("parse_artifact_id"):
            bucket = result.setdefault(str(scope["parse_artifact_id"]), [])
            if scope not in bucket:
                bucket.append(scope)
    return result


def scope_predicate(model, scopes):
    predicates = []
    for scope in scopes:
        parts = []
        if scope.get("logical_document_id"):
            parts.append(model.logical_document_id == UUID(scope["logical_document_id"]))
        if scope.get("section_id"):
            parts.append(model.section_id == UUID(scope["section_id"]))
        if not parts:
            return None
        predicates.append(and_(*parts))
    return or_(*predicates)


def matches_scope(source, scopes):
    return any((not s.get("logical_document_id") or str(source.logical_document_id) == s["logical_document_id"])
        and (not s.get("section_id") or str(source.section_id) == s["section_id"]) for s in scopes)


async def source_row_labels(db, snapshot, parse_id, scopes, segment_ids):
    """只采用有页码与表格键证明的同一行首列原文，不猜邻近段落。"""
    if not segment_ids or not scopes:
        return {}
    query = select(SourceSegment, SourceAnchor).join(SourceAnchor, SourceAnchor.source_segment_id == SourceSegment.id).where(
        SourceSegment.tenant_id == snapshot.tenant_id, SourceSegment.case_id == snapshot.case_id,
        SourceSegment.parse_artifact_id == parse_id, SourceSegment.structure == "table_cell",
        or_(SourceSegment.table_col == 0, SourceSegment.id.in_([UUID(str(i)) for i in segment_ids])),
        SourceAnchor.tenant_id == snapshot.tenant_id, SourceAnchor.case_id == snapshot.case_id,
        SourceAnchor.parse_artifact_id == parse_id)
    restriction = scope_predicate(SourceSegment, scopes)
    if restriction is not None:
        query = query.where(restriction)
    rows = list((await db.execute(query.order_by(SourceSegment.ordinal).limit(3000))).all())
    labels, values = {}, {}
    for segment, anchor in rows:
        table = (anchor.transform or {}).get("table_cell") or {}
        if (anchor.page_no is None or not table.get("table_key") or segment.table_row is None
            or table.get("row_index") != segment.table_row or table.get("col_index") != segment.table_col):
            continue
        key = (segment.logical_document_id, segment.section_id, anchor.page_no, table["table_key"], segment.table_row)
        if segment.table_col == 0 and 0 < len(segment.text.strip()) <= 120 and re.search(r"[A-Za-z\u00c0-\u024f\u0400-\u04ff\u4e00-\u9fff]", segment.text):
            labels.setdefault(key, {})[str(segment.id)] = segment.text
        elif segment.table_col is not None and segment.table_col > 0:
            values.setdefault(str(segment.id), set()).add(key)
    result = {}
    for identifier, keys in values.items():
        candidates = {label_id: value for key in keys for label_id, value in labels.get(key, {}).items()}
        if len(keys) == 1 and len(candidates) == 1:
            label_id, value = next(iter(candidates.items()))
            result[identifier] = {"source_label": value, "source_label_source_id": label_id}
    return result


def relevance_terms(question):
    text = re.sub(r"这票|这份|这张|本票|原件|单据|文件|请问|请|解释|说明|为什么|如何|怎么|是什么|有哪些|译文|翻译结果|内容|分别|查看|来源", " ", question)
    terms = re.findall(r"[A-Za-zÀ-ÿА-Яа-яЁё][A-Za-zÀ-ÿА-Яа-яЁё_-]{2,}|[\u4e00-\u9fff]{2,}", text)
    expanded = []
    for term in terms:
        expanded.append(term)
        if re.fullmatch(r"[\u4e00-\u9fff]+", term) and len(term) > 3:
            expanded.extend(term[i:i + 2] for i in range(len(term) - 1))
    expanded.extend(EVIDENCE_TERMS.get(field_key(question), ()))
    return list(dict.fromkeys(term for term in expanded if len(term) <= 30))[:40]


def relevant_order(column, question):
    terms = relevance_terms(question)
    return sum((case((column.ilike("%" + term + "%"), 1), else_=0) for term in terms), 0)


def relevant_excerpt(text, question, limit=1500):
    positions = [text.lower().find(term.lower()) for term in relevance_terms(question)]
    found = [pos for pos in positions if pos >= 0]
    start = max(0, min(found) - 200) if found else 0
    return ("…" if start else "") + text[start:start + limit] + ("…" if start + limit < len(text) else "")


async def source_party_context(db, snapshot, parse_id, scopes):
    """旧抽取角色仅作候选，明确角色标题按同页同表原文重新核对。"""
    from app.documents.extraction import annotate_party_roles
    query = select(SourceSegment, SourceAnchor).outerjoin(SourceAnchor,
        and_(SourceAnchor.source_segment_id == SourceSegment.id, SourceAnchor.parse_artifact_id == SourceSegment.parse_artifact_id)).where(
        SourceSegment.tenant_id == snapshot.tenant_id, SourceSegment.case_id == snapshot.case_id,
        SourceSegment.parse_artifact_id == parse_id)
    restriction = scope_predicate(SourceSegment, scopes)
    if restriction is not None:
        query = query.where(restriction)
    candidates = {}
    for segment, anchor in (await db.execute(query.order_by(SourceSegment.ordinal).limit(3000))).all():
        table = (anchor.transform or {}).get("table_cell") or {} if anchor else {}
        row = {"id": str(segment.id), "text": segment.text, "structure": segment.structure,
            "section_id": str(segment.section_id), "page_no": anchor.page_no if anchor else None,
            "table_key": table.get("table_key"), "table_row": segment.table_row, "table_col": segment.table_col}
        if row["page_no"] is None:
            row["table_key"] = None
        if segment.structure == "table_cell" and (table.get("row_index") != segment.table_row or table.get("col_index") != segment.table_col):
            row["table_key"] = None
        candidates.setdefault(row["id"], []).append(row)
    material = [rows[0] for rows in candidates.values() if all(row == rows[0] for row in rows)]
    return {row["id"]: row for row in annotate_party_roles(material)}


async def query_evidence(db: AsyncSession, snapshot: ReferenceSnapshot, question: str, settings) -> list[dict]:
    from app.documents.evidence_models import LogicalDocument, LogicalDocumentType
    overview = is_case_overview(question)
    wants_translation = any(word in question for word in ("译文", "翻译结果", "怎么译"))
    requested_types = {kind for term, kind in (("发票", LogicalDocumentType.COMMERCIAL_INVOICE), ("提单", LogicalDocumentType.BILL_OF_LADING),
        ("装箱单", LogicalDocumentType.PACKING_LIST)) if term in question}
    requested_type = next(iter(requested_types)) if len(requested_types) == 1 else None
    snapshots = [f for f in snapshot.frozen if f.get("kind") == "snapshot"]
    allowed = grouped_scopes(snapshot.frozen)
    facts = []
    key = None if overview else field_key(question)
    goods_identity = overview and (field_key(question) == "description" or bool(re.search(r"品名|货描|货物|商品|货品|goods|cargo|products", question, re.I))) and not re.search(r"介绍|概览|概况|总结|概述|overview|summari[sz]e|买方|卖方|买卖|收货|发货|收件|寄件|buyer|seller|consignee|shipper", question, re.I)
    for parse_id, scopes in allowed.items():
        parse = await db.get(ParseArtifact, UUID(parse_id))
        if parse is None or parse.case_id != snapshot.case_id or parse.tenant_id != snapshot.tenant_id or parse.is_synthetic:
            raise NotFoundOrForbidden()
        if any(str(parse.document_id) != scope.get("document_id") or parse.document_version != scope.get("document_version") for scope in scopes):
            raise NotFoundOrForbidden()
        version = (await db.execute(select(DocumentVersion).where(DocumentVersion.document_id == parse.document_id,
            DocumentVersion.version == parse.document_version, DocumentVersion.case_id == snapshot.case_id))).scalar_one_or_none()
        if version is None:
            raise NotFoundOrForbidden()
        fields_query = select(FieldAssertion).where(FieldAssertion.case_id == snapshot.case_id, FieldAssertion.parse_artifact_id == parse.id)
        if goods_identity:
            fields_query = fields_query.where(FieldAssertion.group == "goods")
        if key:
            keys = FIELD_VARIANTS.get(key, {key})
            if key in {"consignee", "shipper", "buyer", "seller"}:
                fields_query = fields_query.where(or_(FieldAssertion.role == key,
                    FieldAssertion.field_key.in_(keys) & FieldAssertion.role.is_(None), FieldAssertion.group == "parties"))
            else:
                fields_query = fields_query.where(FieldAssertion.field_key.in_(keys))
        if requested_type:
            fields_query = fields_query.where(FieldAssertion.logical_document_id.in_(select(LogicalDocument.id).where(
                LogicalDocument.parse_artifact_id == parse.id, LogicalDocument.type_candidate == requested_type)))
        restriction = scope_predicate(FieldAssertion, scopes)
        if restriction is not None:
            fields_query = fields_query.where(restriction)
        if snapshots:
            fixed_field_ids = {identifier for historical in snapshots for identifier in historical.get("field_assertion_ids", [])}
            fields_query = fields_query.where(FieldAssertion.id.in_([UUID(i) for i in fixed_field_ids]))
        if snapshot.business_revision_id:
            from app.documents.evidence_models import BusinessRevisionField
            revision_ids = select(BusinessRevisionField.field_assertion_id).where(BusinessRevisionField.business_revision_id == snapshot.business_revision_id)
            fields_query = fields_query.where(FieldAssertion.id.in_(revision_ids) if overview else
                or_(FieldAssertion.is_human.is_(False), FieldAssertion.id.in_(revision_ids)))
        else:
            fields_query = fields_query.where(FieldAssertion.is_human.is_(False))
        fields = list((await db.execute(fields_query.order_by(FieldAssertion.is_human.desc(), case((FieldAssertion.group == "parties", 0), (FieldAssertion.group == "goods", 1), (FieldAssertion.group == "logistics", 2), else_=3), FieldAssertion.created_at.desc(), FieldAssertion.id).limit(50))).scalars()) if key or overview else []
        if fields and not wants_translation:
            from app.documents.extraction import PARTY_FIELD_KEYS, resolve_party_role
            from app.documents.type_catalog import DOCUMENT_TYPES
            party_context = await source_party_context(db, snapshot, parse.id, scopes) if any(f.field_key in PARTY_FIELD_KEYS for f in fields) else {}
            row_labels = await source_row_labels(db, snapshot, parse.id, scopes, {identifier for f in fields for identifier in f.source_segment_ids or []})
            for f in fields:
                # 来源转写层只用 source_text；规范值与人工采用值不能冒充原文。
                value = (f.normalized_value or f.candidate_value or f.source_text) if f.is_human else f.source_text
                if value is None or not f.source_segment_ids:
                    continue
                role = f.role
                source_roles = {entry["role"] for identifier in f.source_segment_ids for entry in party_context.get(identifier, {}).get("party_role_evidence", [])}
                if f.field_key in PARTY_FIELD_KEYS:
                    logical = await db.get(LogicalDocument, f.logical_document_id)
                    profile = DOCUMENT_TYPES.get(logical.type_candidate.value if logical else "unknown", DOCUMENT_TYPES["unknown"])
                    role = resolve_party_role(role, f.source_text or "", f.source_segment_ids, party_context, set(profile.roles))
                    if role is None or (key in {"consignee", "shipper", "buyer", "seller"} and role != key and key not in source_roles):
                        continue
                segments = list((await db.execute(select(SourceSegment).where(SourceSegment.id.in_([UUID(i) for i in f.source_segment_ids]),
                    SourceSegment.parse_artifact_id == parse.id, SourceSegment.case_id == snapshot.case_id))).scalars())
                if len(segments) != len(f.source_segment_ids):
                    continue
                for segment in segments:
                    target = {"case_id": str(snapshot.case_id), "document_id": str(parse.document_id),
                        "document_version": parse.document_version, "parse_artifact_id": str(parse.id),
                        "logical_document_id": str(f.logical_document_id), "source_segment_id": str(segment.id), "field_id": str(f.id)}
                    if f.section_id:
                        target["section_id"] = str(f.section_id)
                    if snapshot.business_revision_id:
                        target["business_revision_id"] = str(snapshot.business_revision_id)
                    kind = "adopted_value" if f.is_human else "source_value"
                    label = "人工采用值" if f.is_human else "原件写的值"
                    source_label = row_labels.get(str(segment.id), {})
                    field_label = source_label.get("source_label") or FIELD_LABELS.get(f.field_key, "单证字段")
                    role_label = " / ".join(ROLE_LABELS.get(r, r) for r in sorted(source_roles)) if source_roles else ROLE_LABELS.get(role, "")
                    display_label = (role_label + " ") if role_label and role_label != field_label else ""
                    canonical = f"{label}：{display_label}{field_label}为 {value_with_unit(value,f.unit)}（{STATE_LABELS.get(f.state, '待核对')}）。"
                    fact = await _fact(db, segment, version, target, canonical, f.state, value)
                    fact["value"], fact["unit"], fact["field_key"] = str(value), f.unit, f.field_key
                    fact["claim_kind"] = kind
                    fact["role"], fact["group"] = role, f.group
                    role_evidence = party_context.get(str(segment.id), {}).get("party_role_evidence")
                    if role_evidence:
                        fact["source_role_proof"] = role_evidence
                        fact["source_roles"] = sorted(source_roles)
                    fact.update(source_label)
                    fact["card"]["logical_document"] = "复核后采用" if f.is_human else "识别原文"
                    facts.append(fact)
        # 无比较/快照时，未抽取到字段仍可依据原文回答；不绕开冻结明细的证据门禁。
        source_fallback = not any(f.get("field_key") and f["target"].get("parse_artifact_id") == parse_id for f in facts) and not snapshots and not any(f.get("comparison") for f in snapshot.frozen)
        if not wants_translation and (overview or (not key and not fields) or source_fallback):
            query = select(SourceSegment).where(SourceSegment.parse_artifact_id == parse.id, SourceSegment.case_id == snapshot.case_id)
            restriction = scope_predicate(SourceSegment, scopes)
            if restriction is not None:
                query = query.where(restriction)
            if requested_type:
                query = query.where(SourceSegment.logical_document_id.in_(select(LogicalDocument.id).where(LogicalDocument.parse_artifact_id == parse.id, LogicalDocument.type_candidate == requested_type)))
            segments = list((await db.execute(query.order_by(relevant_order(SourceSegment.text, question if field_key(question) else "goods shipment description cargo 货物 运输").desc() if overview else relevant_order(SourceSegment.text, question).desc() if relevance_terms(question) else SourceSegment.ordinal,
                SourceSegment.ordinal).limit(min(2, settings.retrieved_segment_count_limit) if overview and fields else settings.retrieved_segment_count_limit))).scalars())
            for segment in segments:
                target = {"case_id": str(snapshot.case_id), "document_id": str(parse.document_id), "document_version": parse.document_version,
                    "parse_artifact_id": str(parse.id), "logical_document_id": str(segment.logical_document_id),
                    "section_id": str(segment.section_id), "source_segment_id": str(segment.id)}
                excerpt = relevant_excerpt(segment.text, question, limit=600 if overview else 1500)
                facts.append(await _fact(db, segment, version, target, f"原件片段：{excerpt}", "candidate", excerpt))
    if not wants_translation and (overview or key in {"quantity", "gross_weight", "net_weight", "amount"}):
        facts.extend(await frozen_line_facts(db, snapshot, allowed, key=key, requested_type=requested_type))
        if not any(f.get("comparison") for f in snapshot.frozen) and (overview or key == "quantity"):
            table_facts = await frozen_source_table_facts(db, snapshot, allowed, requested_type=requested_type)
            facts.extend(f for f in table_facts if not key or f.get("field_key") == key)
    context = next((f for f in snapshot.frozen if f["kind"] == "translation_context"), None)
    translation_id = context.get("translation_revision_id") if context else next((f.get("translation_revision_id") for f in snapshot.frozen if f.get("translation_revision_id")), None)
    if translation_id and allowed:
        from app.review.models import TranslationRevision, TranslationSegment
        revision = await db.get(TranslationRevision, UUID(translation_id))
        if revision and revision.case_id == snapshot.case_id and not revision.is_synthetic:
            source_query = select(SourceSegment.id).where(SourceSegment.case_id == snapshot.case_id,
                or_(*(and_(SourceSegment.parse_artifact_id == UUID(parse_id), scope_predicate(SourceSegment, scopes))
                    if scope_predicate(SourceSegment, scopes) is not None else SourceSegment.parse_artifact_id == UUID(parse_id) for parse_id, scopes in allowed.items())))
            if requested_type:
                source_query = source_query.where(SourceSegment.logical_document_id.in_(select(LogicalDocument.id).where(LogicalDocument.type_candidate == requested_type)))
            source_ids = [str(i) for i in (await db.execute(source_query)).scalars()]
            if not source_ids:
                return facts
            question_key = "description" if goods_identity else field_key(question)
            preferred_sources = {f["target"].get("source_segment_id") for f in facts
                if f.get("field_key") in FIELD_VARIANTS.get(question_key or "description", {question_key or "description"})
                    or (question_key in {"seller", "buyer", "shipper", "consignee"} and (f.get("role") == question_key or question_key in f.get("source_roles", [])))}
            preferred_sources.discard(None)
            preferred = or_(*(TranslationSegment.source_segment_ids.contains([i]) for i in preferred_sources)) if preferred_sources else None
            translation_filter = preferred if preferred_sources and question_key and not wants_translation else TranslationSegment.id.is_not(None)
            segments = list((await db.execute(select(TranslationSegment).where(TranslationSegment.id.in_([UUID(i) for i in revision.segment_ids]),
                TranslationSegment.parse_artifact_id.in_([UUID(i) for i in allowed]),
                TranslationSegment.source_segment_ids.contained_by(source_ids),
                translation_filter,
                or_(*(TranslationSegment.source_segment_ids.contains([i]) for i in source_ids)))
                .order_by(case((preferred, 1), else_=0).desc() if preferred_sources else TranslationSegment.ordinal * 0,
                    relevant_order(TranslationSegment.translated_text, question if field_key(question) or not overview else "goods cargo 货物 描述 shipment 运输路线").desc() if overview or relevance_terms(question) else TranslationSegment.ordinal,
                    TranslationSegment.ordinal).limit(settings.retrieved_segment_count_limit))).scalars())
            for translated in segments:
                source = await db.get(SourceSegment, UUID(translated.source_segment_ids[0])) if translated.source_segment_ids else None
                if source is None or source.case_id != snapshot.case_id:
                    continue
                scopes = allowed.get(str(translated.parse_artifact_id), [])
                if not matches_scope(source, scopes) or not set(translated.source_segment_ids).issubset(set(source_ids)):
                    continue
                logical = await db.get(LogicalDocument, source.logical_document_id)
                if requested_type and (logical is None or logical.type_candidate != requested_type):
                    continue
                version = (await db.execute(select(DocumentVersion).where(DocumentVersion.document_id == translated.document_id,
                    DocumentVersion.version == translated.document_version))).scalar_one()
                target = {"case_id": str(snapshot.case_id), "document_id": str(translated.document_id), "document_version": translated.document_version,
                    "parse_artifact_id": str(translated.parse_artifact_id), "logical_document_id": str(source.logical_document_id), "section_id": str(source.section_id),
                    "source_segment_id": str(source.id), "run_id": str(translated.run_id), "target_language": translated.target_language,
                    "business_revision_id": str(revision.business_revision_id), "translation_revision_id": str(revision.id), "translation_segment_id": str(translated.id)}
                label = f"目标译文（{language_name(revision.target_language)}，Run {revision.run_id}，译文版 {revision.revision_no}）："
                fact = await _fact(db, source, version, target, label + translated.translated_text, "historical" if (context and context.get("historical")) or translated.state != "valid" else "confirmed", translated.translated_text)
                fact["evidence_id"], fact["claim_kind"] = str(translated.id), "translation"
                fact["card"]["card_id"] = str(translated.id)
                fact["card"]["logical_document"] = "译文"
                fact["value"], fact["label"] = translated.translated_text, label.rstrip("：")
                paired_fact = next((f for f in facts if f["target"].get("source_segment_id") == str(source.id) and f.get("field_key")), None)
                if paired_fact:
                    # 配对译文随原文整行进入模型预算，避免具体译名被通用标题挤掉。
                    fact.update({key: paired_fact[key] for key in ("field_key", "group", "role", "source_roles", "source_role_proof", "source_line_id") if key in paired_fact})
                    if paired_fact.get("line_item_id"):
                        fact["source_line_id"] = paired_fact["line_item_id"]
                facts.append(fact)
    if key and not wants_translation and len({f["target"]["logical_document_id"] for f in facts}) > 1 and not any(w in question for w in ("比较", "各份", "分别", "冲突", "全部", "相差", "差多少", "差值", "合计", "是否一致")):
        raise EvidenceClarification("该字段来自多张逻辑单据，请关联具体单据区段，或明确要求分别比较；不会把它们合成一个值。")
    if key and not wants_translation:
        focused = [f for f in facts if f.get("field_key") in FIELD_VARIANTS.get(key, {key})
            or (key in {"seller", "buyer", "shipper", "consignee"} and (f.get("role") == key or key in f.get("source_roles", [])))]
        if focused:
            facts = focused
    facts = list({f["evidence_id"]: f for f in facts}.values())
    if overview:
        facts=overview_facts(facts)
    return facts


def overview_facts(facts):
    unique={}
    for fact in facts:
        semantic=(fact["target"].get("logical_document_id"),fact.get("field_key"),fact.get("role"),
            fact["claim_kind"],str(fact.get("value")),fact.get("unit"),fact.get("source_line_id") or fact.get("line_item_id"),fact.get("source_label")) if fact.get("field_key") else fact["evidence_id"]
        unique.setdefault(semantic,fact)
    counts={};ranked=[]
    for fact in unique.values():
        key,role=fact.get("field_key"),fact.get("role")
        category=0 if role in {"seller","buyer","shipper","consignee"} and key in {"name","address"} else 1 if key in {"port_of_loading","port_of_discharge","place_of_receipt","place_of_delivery"} else 2 if key in {"sku","description","goods_description","lot_no","quantity","package_count"} else 3 if key in {"total_amount","amount","freight","insurance","currency","incoterm"} else 4 if key in {"invoice_no","invoice_number","order_no","transport_reference","invoice_date"} else 5
        family=(key,role) if key else ("fragment",)
        count=counts.get(family,0);counts[family]=count+1
        ranked.append(((category>=5,count,category),fact))
    return [fact for _,fact in sorted(ranked,key=lambda item:item[0])]


async def frozen_line_facts(db, snapshot, allowed, *, key=None, requested_type=None):
    """货物计量只读冻结批次的行及原格证据，不从核对摘要推算数量。"""
    from app.documents.evidence_models import LineItem, LogicalDocument
    comparison = next((f.get("comparison") for f in snapshot.frozen if f.get("comparison")), None)
    if not comparison:
        return []
    identifiers = comparison.get("manifest", {}).get("line_ids", [])
    query = select(LineItem).where(LineItem.id.in_([UUID(i) for i in identifiers]),
        LineItem.case_id == snapshot.case_id, LineItem.tenant_id == snapshot.tenant_id,
        LineItem.parse_artifact_id.in_([UUID(i) for i in allowed]))
    if requested_type:
        query = query.where(LineItem.logical_document_id.in_(select(LogicalDocument.id).where(LogicalDocument.type_candidate == requested_type)))
    facts = []
    labels = {"quantity": "货物数量", "package_count": "包装件数", "net_weight": "净重", "gross_weight": "毛重", "amount": "货物金额"}
    for line in (await db.execute(query.order_by(LineItem.logical_document_id, LineItem.line_no, LineItem.id).limit(30))).scalars():
        parse = await db.get(ParseArtifact, line.parse_artifact_id)
        scopes = allowed[str(line.parse_artifact_id)]
        if parse is None or parse.is_synthetic or parse.case_id != snapshot.case_id or parse.tenant_id != snapshot.tenant_id:
            raise NotFoundOrForbidden()
        if any(str(parse.document_id) != s.get("document_id") or parse.document_version != s.get("document_version") for s in scopes):
            raise NotFoundOrForbidden()
        version = (await db.execute(select(DocumentVersion).where(DocumentVersion.document_id == parse.document_id,
            DocumentVersion.version == parse.document_version, DocumentVersion.case_id == snapshot.case_id))).scalar_one_or_none()
        if version is None:
            raise NotFoundOrForbidden()
        if key is None:
            for name in ("sku","description","lot_no"):
                ids=(line.cell_sources or {}).get(name,[])
                for source_id in ids:
                    segment=await db.get(SourceSegment,UUID(source_id))
                    if segment is None or segment.case_id!=snapshot.case_id or segment.tenant_id!=snapshot.tenant_id or segment.parse_artifact_id!=parse.id or segment.logical_document_id!=line.logical_document_id or not matches_scope(segment,scopes):continue
                    target={"case_id":str(snapshot.case_id),"document_id":str(parse.document_id),"document_version":parse.document_version,
                        "parse_artifact_id":str(parse.id),"logical_document_id":str(line.logical_document_id),
                        "section_id":str(segment.section_id),"source_segment_id":str(segment.id)}
                    fact=await _fact(db,segment,version,target,"原件货物行内容："+segment.text,"candidate",segment.text)
                    fact.update(field_key=name,group="goods",value=segment.text,source_line_id=str(line.id))
                    facts.append(fact)
        for measurement in line.measurements or []:
            kind = measurement.get("quantity_kind")
            if kind not in labels or (key and kind != key) or measurement.get("raw_value") is None:
                continue
            value_refs = measurement.get("value_source_segment_ids", [])
            unit_refs = measurement.get("unit_source_segment_ids", [])
            refs = set(value_refs + unit_refs)
            if not value_refs or (measurement.get("unit") and not unit_refs):
                continue
            segments = list((await db.execute(select(SourceSegment).where(SourceSegment.id.in_([UUID(i) for i in refs]),
                SourceSegment.case_id == snapshot.case_id, SourceSegment.tenant_id == snapshot.tenant_id,
                SourceSegment.parse_artifact_id == parse.id, SourceSegment.logical_document_id == line.logical_document_id))).scalars())
            if len(segments) != len(refs) or not all(matches_scope(s, scopes) for s in segments):
                continue
            segment = next(s for s in segments if str(s.id) == value_refs[0])
            value, unit = measurement["raw_value"], measurement.get("unit")
            target = {"case_id": str(snapshot.case_id), "document_id": str(parse.document_id), "document_version": parse.document_version,
                "parse_artifact_id": str(parse.id), "logical_document_id": str(line.logical_document_id),
                "section_id": str(segment.section_id), "source_segment_id": str(segment.id)}
            canonical = f"原件货物行 {line.line_no}：{labels[kind]}为 {value_with_unit(value,unit)}。"
            fact = await _fact(db, segment, version, target, canonical, "candidate", canonical)
            identifier = f"line:{line.id}:{kind}"
            fact.update(evidence_id=identifier, value=value, unit=unit, field_key=kind, group="goods",
                line_item_id=str(line.id), measurement_kind=kind, supporting_source_segment_ids=sorted(refs))
            fact["card"].update(card_id=identifier, logical_document="识别原文")
            facts.append(fact)
    return facts


async def frozen_source_table_facts(db, snapshot, allowed, *, requested_type=None):
    """无比较批次时直接摘录冻结原件货物表，不采用历史货物候选。"""
    from app.documents.evidence_models import LogicalDocument
    from app.documents.extraction import annotate_extraction_structure, structured_table_lines
    facts = []
    for parse_id, scopes in allowed.items():
        parse = await db.get(ParseArtifact, UUID(parse_id))
        if (parse is None or parse.is_synthetic or parse.case_id != snapshot.case_id or parse.tenant_id != snapshot.tenant_id
            or any(str(parse.document_id) != s.get("document_id") or parse.document_version != s.get("document_version") for s in scopes)):
            raise NotFoundOrForbidden()
        version = (await db.execute(select(DocumentVersion).where(DocumentVersion.document_id == parse.document_id,
            DocumentVersion.version == parse.document_version, DocumentVersion.case_id == snapshot.case_id))).scalar_one_or_none()
        if version is None:
            raise NotFoundOrForbidden()
        query = select(SourceSegment, SourceAnchor).join(SourceAnchor, SourceAnchor.source_segment_id == SourceSegment.id).where(
            SourceSegment.tenant_id == snapshot.tenant_id, SourceSegment.case_id == snapshot.case_id,
            SourceSegment.parse_artifact_id == parse.id, SourceSegment.structure == "table_cell",
            SourceAnchor.tenant_id == snapshot.tenant_id, SourceAnchor.case_id == snapshot.case_id,
            SourceAnchor.parse_artifact_id == parse.id)
        restriction = scope_predicate(SourceSegment, scopes)
        if restriction is not None:
            query = query.where(restriction)
        if requested_type:
            query = query.where(SourceSegment.logical_document_id.in_(select(LogicalDocument.id).where(
                LogicalDocument.parse_artifact_id == parse.id, LogicalDocument.type_candidate == requested_type)))
        candidates, segments = {}, {}
        for segment, anchor in (await db.execute(query.order_by(SourceSegment.ordinal).limit(3000))).all():
            table = (anchor.transform or {}).get("table_cell") or {}
            if (anchor.page_no is None or not table.get("table_key") or segment.table_row is None or segment.table_col is None
                or table.get("row_index") != segment.table_row or table.get("col_index") != segment.table_col):
                continue
            identifier = str(segment.id)
            # 页内表头与原格必须来自同一表；不能凭位置邻近借用其他页表头。
            material = {"id": identifier, "text": segment.text, "structure": "table_cell", "section_id": str(segment.section_id),
                "table_key": (anchor.page_no, table["table_key"]), "page_no": anchor.page_no,
                "table_row": segment.table_row, "table_col": segment.table_col}
            candidates.setdefault(identifier, []).append(material)
            segments[identifier] = segment
        material = [rows[0] for rows in candidates.values() if all(row == rows[0] for row in rows)]
        # 同物理格有多份不同来源时不任意选一份。
        cell_counts = {}
        for row in material:
            cell = (row["section_id"], row["table_key"], row["table_row"], row["table_col"])
            cell_counts[cell] = cell_counts.get(cell, 0) + 1
        material = [row for row in material if cell_counts[(row["section_id"], row["table_key"], row["table_row"], row["table_col"])] == 1]
        annotated = annotate_extraction_structure(material)
        by_id = {row["id"]: row for row in annotated}
        # 只读问答允许没有SKU的品名/数量表；核对工作流仍采用默认SKU门槛。
        for line in structured_table_lines(annotated, require_sku=False)[:30]:
            first = by_id[line.row_source_segment_ids[0]]
            row_id = f"source-table:{parse_id}:{first['section_id']}:{first['table_key']}:{first['table_row']}"
            headers = sorted(first.get("table_header_source_segment_ids", []))
            cells = {cell.column_key: cell for cell in line.cells}
            for key in ("sku", "description", "quantity"):
                cell = cells.get(key)
                if cell is None or len(cell.source_segment_ids) != 1:
                    continue
                segment = segments[cell.source_segment_ids[0]]
                unit_cell = cells.get("quantity_unit") if key == "quantity" else None
                unit = unit_cell.raw_value if unit_cell else None
                proof = {"header_ids": headers, "row_source_ids": sorted(line.row_source_segment_ids),
                    "value_source_ids": cell.source_segment_ids, "unit_source_ids": unit_cell.source_segment_ids if unit_cell else []}
                target = {"case_id": str(snapshot.case_id), "document_id": str(parse.document_id), "document_version": parse.document_version,
                    "parse_artifact_id": parse_id, "logical_document_id": str(segment.logical_document_id),
                    "section_id": str(segment.section_id), "source_segment_id": str(segment.id)}
                canonical = "原件货物行内容：" + value_with_unit(cell.raw_value, unit)
                fact = await _fact(db, segment, version, target, canonical, "candidate", canonical)
                fact.update(field_key=key, group="goods", value=cell.raw_value, unit=unit,
                    source_line_id=row_id, source_table_proof=proof)
                facts.append(fact)
    return facts


async def frozen_issue_rows(db, snapshot, *, limit=31, comparison_only=False):
    from app.review.models import Issue
    from app.review.commands import issue_view
    scopes = [s for group in grouped_scopes(snapshot.frozen).values() for s in group]
    patterns = [{key: s[key] for key in ("document_id", "document_version", "parse_artifact_id", "logical_document_id", "section_id") if s.get(key)} for s in scopes]
    if not patterns:
        return []
    comparison = next((f["comparison"] for f in snapshot.frozen if f.get("comparison")), None)
    historical = next((f for f in snapshot.frozen if f.get("kind") == "snapshot"), None)
    run_id = next((f.get("run_id") for f in snapshot.frozen if f.get("run_id")), None)
    branches = []
    if snapshot.business_revision_id and not comparison_only:
        branch = and_(Issue.business_revision_id == snapshot.business_revision_id, Issue.comparison_batch_id.is_(None))
        if run_id:
            branch = and_(branch, Issue.run_id == UUID(run_id))
        branches.append(branch)
    if comparison:
        branches.append(Issue.comparison_batch_id == UUID(comparison["batch_id"]))
    if not branches:
        return []
    query = select(Issue).where(Issue.case_id == snapshot.case_id, Issue.tenant_id == snapshot.tenant_id,
        or_(*branches), or_(*(Issue.sources.contains([{"target": t}]) for t in patterns), *(Issue.sources.contains([t]) for t in patterns)))
    issues = list((await db.execute(query.order_by(Issue.blocks_export.desc(), Issue.created_at, Issue.id).limit(limit))).scalars())
    rows = []
    for issue in issues:
        frozen_ids = historical.get("issue_disposition_ids", []) if historical else (
            comparison.get("issue_disposition_ids", []) if issue.comparison_batch_id else next((f.get("issue_disposition_ids") for f in snapshot.frozen if "issue_disposition_ids" in f), None))
        rows.append((issue, await issue_view(db, issue, frozen_ids)))
    return rows


async def case_records(db, snapshot):
    """概览的只读处理记忆限于已冻结Run、业务版及原件区段。"""
    from app.review.commands import issue_view
    from app.review.models import Issue
    from app.workflows.models import Run, RunEvent
    if not snapshot.case_id:
        return {}
    historical = next((f for f in snapshot.frozen if f.get("kind") == "snapshot"), None)
    run_id = next((f.get("run_id") for f in snapshot.frozen if f.get("run_id")), None)
    result = {}
    if run_id:
        run = await db.get(Run, UUID(run_id))
        if run is None or run.case_id != snapshot.case_id or run.tenant_id != snapshot.tenant_id:
            raise NotFoundOrForbidden()
        query = select(RunEvent).where(RunEvent.run_id == run.id)
        if snapshot.created_at:
            query = query.where(RunEvent.occurred_at <= snapshot.created_at)
        if historical:
            from app.exports.models import ExportSnapshot
            saved = await db.get(ExportSnapshot, UUID(historical["snapshot_id"]))
            if saved is None or saved.case_id != snapshot.case_id:
                raise NotFoundOrForbidden()
            query = query.where(RunEvent.occurred_at <= saved.exported_at)
        events = list((await db.execute(query.order_by(RunEvent.seq.desc()).limit(6))).scalars())
        result["processing_records"] = [{"stage": e.stage, "status": e.status, "message": e.message, "occurred_at": e.occurred_at.isoformat()} for e in reversed(events)]
    if snapshot.business_revision_id:
        issues = await frozen_issue_rows(db, snapshot, limit=11)
        result["issues"] = []
        for issue, view in issues[:10]:
            result["issues"].append({"title": issue.title, "description": issue.description, "status": view["status"], "severity": view["severity"]})
        result["more_issues"] = len(issues) > 10
    frozen_comparison = next((f.get("comparison") for f in snapshot.frozen if f.get("comparison")), None)
    if frozen_comparison:
        from app.consignment.models import ComparisonBatch, CaseScopeRevision
        from app.review.models import RuleCheck
        batch = await db.get(ComparisonBatch, UUID(frozen_comparison["batch_id"]))
        if batch is None or batch.case_id != snapshot.case_id or batch.tenant_id != snapshot.tenant_id or batch.input_fingerprint != frozen_comparison["input_fingerprint"]:
            raise NotFoundOrForbidden()
        scope = await db.get(CaseScopeRevision, batch.scope_revision_id)
        result["comparison_context"] = {"batch_id": str(batch.id), "scope_revision_id": str(batch.scope_revision_id),
            "summary": batch.summary, "cargo": scope.payload.get("cargo", []), "legs": scope.payload.get("legs", []),
            "note": "冻结原始核对结论；人工处置不改写来源，未判定不表示通过。"}
        allowed_parses = set(grouped_scopes(snapshot.frozen))
        checks = list((await db.execute(select(RuleCheck).where(RuleCheck.comparison_batch_id == batch.id)
            .order_by(RuleCheck.id))).scalars())
        result["comparison_results"] = []
        frozen_ids = frozen_comparison.get("issue_disposition_ids", historical.get("issue_disposition_ids", []) if historical else [])
        for check in checks:
            segments = list((await db.execute(select(SourceSegment).where(SourceSegment.id.in_([UUID(x) for x in check.source_segment_ids])))).scalars()) if check.source_segment_ids else []
            if not segments or not all(str(s.parse_artifact_id) in allowed_parses for s in segments):
                continue
            issue = (await db.execute(select(Issue).where(Issue.rule_check_id == check.id))).scalar_one_or_none()
            view = await issue_view(db, issue, frozen_ids) if issue else None
            result["comparison_results"].append({"rule_id": check.rule_id, "basis": check.comparison_basis,
                "status": check.status, "description": issue.description if issue else check.explanation,
                "disposition": view["status"] if view else None, "blocks_delivery": view["blocking_delivery"] if view else False})
            if len(result["comparison_results"]) >= 20:
                result["comparison_more"] = True
                break
    return result


async def _fact(db, segment, version, target, canonical, status, excerpt):
    anchor = (await db.execute(select(SourceAnchor).where(SourceAnchor.source_segment_id == segment.id,
        SourceAnchor.parse_artifact_id == segment.parse_artifact_id).limit(1))).scalar_one_or_none()
    location = "段落定位"
    if anchor:
        target["source_anchor_id"] = str(anchor.id)
        if anchor.page_no:
            location = f"第 {anchor.page_no} 页"
    return {"evidence_id": target.get("field_id") or str(segment.id), "canonical_text": canonical, "claim_kind": "source_value", "target": target,
        "card": {"card_id": target.get("field_id") or str(segment.id), "filename": version.filename, "logical_document": "原件",
            "location": location, "excerpt": excerpt, "status": status, "versions": f"原件 v{version.version} · 解析 {segment.parse_artifact_id}", "target": target}}


async def verify_answer(db: AsyncSession, snapshot: ReferenceSnapshot, draft: AnswerDraft, facts: list[dict], *, source_cache=None) -> tuple[list[str], list[dict]]:
    by_id = {f["evidence_id"]: f for f in facts}
    texts, cards = [], []
    if source_cache is None:
        source_cache = {}
    label_cache, table_cache = source_cache.setdefault("labels", {}), source_cache.setdefault("tables", {})
    role_cache = source_cache.setdefault("roles", {})
    used = set()
    for paragraph in draft.paragraphs:
        selected = [by_id[i] for i in paragraph.evidence_ids if i in by_id]
        valid = len(selected) == len(paragraph.evidence_ids) and bool(selected)
        for fact in selected:
            t = fact["target"]
            valid = valid and t.get("case_id") == (str(snapshot.case_id) if snapshot.case_id else None)
            if t.get("source_segment_id"):
                scopes = grouped_scopes(snapshot.frozen).get(t.get("parse_artifact_id"), [])
                segment = await db.get(SourceSegment, UUID(t["source_segment_id"]))
                valid = valid and segment is not None and bool(scopes) and matches_scope(segment, scopes) and segment.case_id == snapshot.case_id and segment.tenant_id == snapshot.tenant_id and str(segment.parse_artifact_id) == t.get("parse_artifact_id")
                if segment:
                    valid = valid and str(segment.logical_document_id) == t.get("logical_document_id")
                    if t.get("section_id"):
                        valid = valid and str(segment.section_id) == t["section_id"]
                    parse = await db.get(ParseArtifact, segment.parse_artifact_id)
                    valid = valid and parse is not None and not parse.is_synthetic and str(parse.document_id) == t.get("document_id") and parse.document_version == t.get("document_version")
                if fact.get("source_label"):
                    parse_id = t.get("parse_artifact_id")
                    cached = label_cache.setdefault(parse_id, {})
                    identifiers = {f["target"]["source_segment_id"] for f in facts if f["target"].get("parse_artifact_id") == parse_id
                        and f["target"].get("source_segment_id") and f["target"]["source_segment_id"] not in cached}
                    if identifiers:
                        labels = await source_row_labels(db, snapshot, UUID(parse_id), scopes, identifiers)
                        cached.update({identifier: labels.get(identifier) for identifier in identifiers})
                    actual = cached.get(t["source_segment_id"])
                    valid = valid and actual == {"source_label": fact["source_label"], "source_label_source_id": fact.get("source_label_source_id")}
                from app.documents.extraction import PARTY_FIELD_KEYS
                if fact.get("source_role_proof") or fact.get("field_key") in PARTY_FIELD_KEYS:
                    from app.documents.extraction import resolve_party_role
                    from app.documents.evidence_models import LogicalDocument
                    from app.documents.type_catalog import DOCUMENT_TYPES
                    parse_id = t.get("parse_artifact_id")
                    if parse_id not in role_cache:
                        role_cache[parse_id] = await source_party_context(db, snapshot, UUID(parse_id), scopes)
                    actual = role_cache[parse_id].get(t["source_segment_id"], {})
                    logical = await db.get(LogicalDocument, segment.logical_document_id) if segment else None
                    profile = DOCUMENT_TYPES.get(logical.type_candidate.value if logical else "unknown", DOCUMENT_TYPES["unknown"])
                    resolved = resolve_party_role(fact.get("role"), segment.text if segment else "", [t["source_segment_id"]], role_cache[parse_id], set(profile.roles))
                    evidence = actual.get("party_role_evidence")
                    if evidence:
                        valid = valid and evidence == fact.get("source_role_proof") and resolved == fact.get("role")
                        valid = valid and sorted({entry["role"] for entry in evidence}) == fact.get("source_roles")
                    else:
                        valid = valid and not fact.get("source_role_proof") and not fact.get("source_roles")
                if fact.get("source_table_proof"):
                    parse_id = t.get("parse_artifact_id")
                    if parse_id not in table_cache:
                        table_cache[parse_id] = {f["evidence_id"]: f for f in await frozen_source_table_facts(db, snapshot, {parse_id: scopes})}
                    actual = table_cache[parse_id].get(fact["evidence_id"])
                    valid = valid and actual is not None and all(actual.get(key) == fact.get(key) for key in
                        ("field_key", "value", "unit", "source_line_id", "source_table_proof", "canonical_text"))
            if fact.get("line_item_id"):
                from app.documents.evidence_models import LineItem
                comparison = next((f.get("comparison") for f in snapshot.frozen if f.get("comparison")), None)
                line = await db.get(LineItem, UUID(fact["line_item_id"]))
                measures = (line.measurements or []) if line is not None else []
                measure = next((m for m in measures if m.get("quantity_kind") == fact["measurement_kind"]), None)
                refs = set(fact.get("supporting_source_segment_ids", []))
                valid = valid and bool(comparison) and fact["line_item_id"] in comparison["manifest"].get("line_ids", [])
                valid = valid and line is not None and line.case_id == snapshot.case_id and line.tenant_id == snapshot.tenant_id
                valid = valid and line is not None and str(line.parse_artifact_id) == t.get("parse_artifact_id") and str(line.logical_document_id) == t.get("logical_document_id")
                valid = valid and measure is not None and measure.get("raw_value") == fact["value"] and measure.get("unit") == fact["unit"]
                valid = valid and measure is not None and refs == set(measure.get("value_source_segment_ids", []) + measure.get("unit_source_segment_ids", []))
                supporting = list((await db.execute(select(SourceSegment).where(SourceSegment.id.in_([UUID(i) for i in refs])))).scalars())
                valid = valid and len(supporting) == len(refs) and all(s.case_id == snapshot.case_id and s.tenant_id == snapshot.tenant_id
                    and str(s.parse_artifact_id) == t.get("parse_artifact_id") and str(s.logical_document_id) == t.get("logical_document_id")
                    and matches_scope(s, grouped_scopes(snapshot.frozen).get(t.get("parse_artifact_id"), [])) for s in supporting)
            if fact["claim_kind"] == "computed":
                from app.conversation.calculation import calculate
                try:
                    replay = calculate(fact.get("calculation_question", ""), [f for f in facts if f["claim_kind"] != "computed"], snapshot.case_id)
                    valid = valid and replay is not None and replay["value"] == fact.get("value") and replay["formula"] == fact.get("formula") and replay["evidence_id"] == fact["evidence_id"]
                except EvidenceClarification:
                    valid = False
                operand_ids = fact.get("operand_evidence_ids", [])
                if any(i not in by_id for i in operand_ids):
                    valid = False
                elif operand_ids:
                    operands = [by_id[i] for i in operand_ids]
                    checked, _ = await verify_answer(db, snapshot, deterministic_answer(operands), operands, source_cache=source_cache)
                    valid = valid and checked == [f["canonical_text"] for f in operands]
        # 关键值由服务端构造；旧版原句校验仍保留以便冻结回合复现。
        if valid and paragraph.claim_kind == selected[0]["claim_kind"] and paragraph.text in [f["canonical_text"] for f in selected]:
            texts.append(paragraph.text)
            for fact in selected:
                if fact.get("card") and fact["evidence_id"] not in used:
                    cards.append(fact["card"])
                    used.add(fact["evidence_id"])
        elif paragraph.claim_kind == "not_covered" and paragraph.status == "not_covered":
            texts.append("该规则或通关结论未评估，请在复核页面查看已发布规则范围。")
        elif paragraph.status == "cannot_confirm":
            texts.append("无法确认这项事实，请在复核页面核对来源。")
        else:
            texts.append("无法确认这项事实，请在复核页面核对来源。")
    # 冲突双方不得因模型只选择其一而消失。
    for fact in facts:
        if (fact.get("card") or {}).get("status") == "conflict" and fact["evidence_id"] not in used:
            texts.append(fact["canonical_text"])
            cards.append(fact["card"])
    return texts or ["当前冻结版本没有足够证据，请在复核页面查看原件。"], cards


async def verify_natural_answer(db, snapshot, draft, facts):
    """模型不能重写关键值：占位符绑定事实，数值/单位与出处由服务端填入。"""
    from app.conversation.safety import guard_text
    by_id = {f["evidence_id"]: f for f in facts}
    texts, cards, used = [], [], set()
    source_cache = {}
    for paragraph in draft.paragraphs:
        ids = re.findall(r"\[\[([^\[\]]+)\]\]", paragraph.text)
        remaining = re.sub(r"\[\[[^\[\]]+\]\]", "", paragraph.text)
        if not ids and not paragraph.evidence_ids and paragraph.claim_kind == "general_explanation":
            texts.append(guard_text(paragraph.text))
            continue
        paraphrase = paragraph.claim_kind == "general_explanation" and not ids and bool(paragraph.evidence_ids)
        declared = list(dict.fromkeys(paragraph.evidence_ids))
        if ("[[" in remaining or "]]" in remaining or (not ids and not paraphrase)
            or not set(ids).issubset(declared) or any(i not in by_id for i in declared)):
            texts.append("无法确认这项解释，请核对对应来源。")
            continue
        selected = [by_id[i] for i in declared]
        ordinary = [f for f in selected if f["claim_kind"] != "issue_reason"]
        checked, evidence_cards = await verify_answer(db, snapshot, deterministic_answer(ordinary), ordinary, source_cache=source_cache) if ordinary else ([], [])
        reasons = [f for f in selected if f["claim_kind"] == "issue_reason"]
        if reasons:
            from app.review.models import Issue
            for fact in reasons:
                issue = await db.get(Issue, UUID(fact["target"]["issue_id"]))
                allowed = [s for group in grouped_scopes(snapshot.frozen).values() for s in group]
                source_allowed = issue is not None and any(all(target.get(k) == scope.get(k) for k in
                    ("document_id", "document_version", "parse_artifact_id", "logical_document_id", "section_id") if scope.get(k))
                    for entry in issue.sources for target in [entry.get("target") or entry] for scope in allowed)
                comparison = next((f["comparison"] for f in snapshot.frozen if f.get("comparison")), None)
                revision_allowed = issue is not None and (issue.business_revision_id == snapshot.business_revision_id or
                    comparison and str(issue.comparison_batch_id) == comparison["batch_id"])
                if issue is None or not source_allowed or issue.case_id != snapshot.case_id or issue.tenant_id != snapshot.tenant_id or not revision_allowed or str(issue.run_id) != fact["target"].get("run_id"):
                    checked.append("无法确认该问题原因，请重新核对版本。")
                else:
                    checked.append(fact["canonical_text"])
        if set(checked) != {f["canonical_text"] for f in selected}:
            texts.extend(checked)
            continue
        text = paragraph.text if paraphrase else replace_fact_placeholders(paragraph.text, by_id)
        texts.append(guard_text(text))
        for fact in selected:
            if fact["claim_kind"] == "computed":
                texts.append("计算依据：" + fact["formula"] + "；" + ("操作数来自本轮冻结证据。" if fact.get("operand_evidence_ids") else "操作数由用户本轮明确输入，未核验为单据事实。"))
        cards.extend(card for card in evidence_cards if card["card_id"] not in used)
        used.update(card["card_id"] for card in evidence_cards)
        used.update(declared)
    for fact in facts:
        if fact.get("card", {}) and fact["card"].get("status") == "conflict" and fact["evidence_id"] not in used:
            checked, evidence_cards = await verify_answer(db, snapshot, deterministic_answer([fact]), [fact], source_cache=source_cache)
            texts.extend(checked)
            cards.extend(evidence_cards)
    return texts or ["没有足够证据组织这项答复，请核对来源。"], cards


def deterministic_answer(facts: list[dict]) -> AnswerDraft:
    return AnswerDraft(paragraphs=[AnswerParagraph(text=f["canonical_text"], claim_kind=f["claim_kind"], evidence_ids=[f["evidence_id"]], status="asserted") for f in facts])
