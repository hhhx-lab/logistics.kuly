"""抽取输入契约：明确字段与角色，有界片段输入；源值规范化不补全事实。"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import re
import unicodedata

from app.prompts.registry import estimate_tokens, PromptBudgetExceeded

ORIGIN_FIELD_KEYS = frozenset({"origin", "origin_place", "place_of_origin"})

_COMMON = [
    ("document_no","document","单据编号"),("document_date","document","单据日期"),
    ("transport_reference","transport","运输参考号"),("container_no","transport","柜号"),
    ("seal_no","transport","封条号"),("vessel_name","transport","船名"),("voyage_no","transport","航次"),
    ("port_of_loading","transport","装货港"),("port_of_discharge","transport","卸货港"),
    ("origin","transport","原件明确注明的起运地／出发地，不从主体地址、接货地、装货港、交货地或原产国推断"),
    ("place_of_receipt","transport","承运人接货地"),("place_of_delivery","transport","交货地"),
    ("country_of_origin","goods","原产国"),("goods_description","goods","货物描述"),
    ("hs_code","goods","原件注明的HS编码，字符串保留前导零"),
    ("quantity","goods","数量"),("quantity_unit","goods","数量单位"),
    ("package_count","goods","包装数"),("package_unit","goods","箱、件、托盘等包装层级"),
    ("net_weight","goods","净重"),("gross_weight","goods","毛重"),("weight_unit","goods","重量单位"),
    ("currency","amounts","币种"),("unit_price","amounts","单价"),("total_amount","amounts","原件注明总额"),
    ("incoterm","transport","贸易术语及地点"),("freight","amounts","运费"),("insurance","amounts","保险费"),
]
LINE_COLUMNS=("description","quantity","quantity_unit","unit_price","amount","currency","package_count","package_unit","net_weight","gross_weight","weight_unit","hs_code")
LINE_COLUMNS += ("sku", "specification", "lot_no", "source_item_no", "package_marks", "package_range",
    "country_of_origin", "net_weight_unit", "gross_weight_unit", "chargeable_weight", "chargeable_weight_unit",
    "statutory_quantity_1", "statutory_quantity_1_unit", "statutory_quantity_2", "statutory_quantity_2_unit",
    "declared_quantity", "declared_quantity_unit", "received_quantity", "received_quantity_unit")

LINE_LABELS = {
    "sku": ("sku", "item code", "product code", "商品编号", "货号", "référence article", "código de producto", "артикул"),
    "description": ("description", "goods description", "品名", "货物名称", "désignation", "descripción", "наименование"),
    "specification": ("specification", "model", "规格型号", "规格", "modèle", "especificación", "модель"),
    "lot_no": ("lot", "lot no", "batch no", "批号", "numéro de lot", "número de lote", "номер партии"),
    "source_item_no": ("item no", "line no", "序号", "项号", "n° article", "número de artículo", "номер позиции"),
    "quantity": ("qty", "quantity", "数量", "quantité", "cantidad", "количество"),
    "quantity_unit": ("unit", "uom", "单位", "unité", "unidad", "единица"),
    "unit_price": ("unit price", "单价", "prix unitaire", "precio unitario", "цена за единицу"),
    "amount": ("amount", "line amount", "金额", "montant", "importe", "сумма"),
    "package_count": ("packages", "cartons", "包装数", "箱数", "nombre de colis", "bultos", "количество мест"),
    "package_range": ("carton nos", "package range", "箱号范围", "n° colis", "números de bulto", "номера мест"),
    "package_marks": ("marks", "shipping marks", "唛头", "marques", "marcas", "маркировка"),
    "country_of_origin": ("country of origin", "原产国", "pays d'origine", "país de origen", "страна происхождения"),
    "net_weight": ("net weight", "净重", "poids net", "peso neto", "вес нетто"),
    "gross_weight": ("gross weight", "毛重", "poids brut", "peso bruto", "вес брутто"),
    "chargeable_weight": ("chargeable weight", "计费重量", "poids taxable", "peso facturable", "оплачиваемый вес"),
    "hs_code": ("hs code", "commodity code", "商品编码", "税则号", "code sh", "código sa", "код тн вэд"),
    "statutory_quantity_1": ("法定第一数量", "第一法定数量"),
    "statutory_quantity_2": ("法定第二数量", "第二法定数量"),
    "declared_quantity": ("declared quantity", "申报数量", "quantité déclarée", "cantidad declarada", "заявленное количество"),
    "received_quantity": ("received quantity", "签收数量", "quantité reçue", "cantidad recibida", "полученное количество"),
}

_COMMON_COLUMN_ALIASES = {
    "sku": ("référence",),
    "description": ("product", "producto", "produit", "中文品名"),
    "specification": ("型号",), "lot_no": ("batch", "lote", "批次"),
    "source_item_no": ("line", "n°", "no", "nº"),
    "quantity": ("goods quantity",), "quantity_unit": ("goods unit",),
    "package_unit": ("package type", "package unit"),
    "net_weight": ("net kg",), "gross_weight": ("gross kg",),
    "unit_price": ("unit price eur", "prix eur", "precio eur", "单价eur"),
    "amount": ("amount eur", "montant eur", "importe eur", "金额eur"),
}
LINE_LABELS.update({key: tuple(dict.fromkeys((*LINE_LABELS.get(key, ()), *aliases)))
    for key, aliases in _COMMON_COLUMN_ALIASES.items()})

_MEASUREMENTS = {"quantity": "quantity_unit", "package_count": "package_unit", "net_weight": "net_weight_unit",
    "gross_weight": "gross_weight_unit", "chargeable_weight": "chargeable_weight_unit",
    "statutory_quantity_1": "statutory_quantity_1_unit", "statutory_quantity_2": "statutory_quantity_2_unit",
    "declared_quantity": "declared_quantity_unit", "received_quantity": "received_quantity_unit"}


def field_schema(document_type: str, cargo_profiles=()) -> dict:
    from app.documents.type_catalog import DOCUMENT_TYPES, CARGO_TYPES, ROLE_LABELS, normalize_type_code, CATALOG_VERSION
    profile=DOCUMENT_TYPES.get(normalize_type_code(document_type),DOCUMENT_TYPES["unknown"])
    extra=list(profile.fields)
    focus=list(profile.review_focus)
    for code in cargo_profiles:
        cargo=CARGO_TYPES.get(code)
        if cargo:
            extra.extend(cargo.fields);focus.append(cargo.review_focus)
    entries=list(_COMMON)+extra
    for role in profile.roles:
        entries.extend((key,role,ROLE_LABELS.get(role,role)+meaning) for key,meaning in (
            ("name","名称"),("address","地址"),("phone","电话"),("email","邮箱"),("company_no","主体编号"),
            ("tax_no","税号"),("contact_name","联系人")))
    deduplicated={(key,role):meaning for key,role,meaning in entries}
    return {"catalog_version": CATALOG_VERSION, "profile_version": profile.version, "document_type":profile.code,
        "fields":[{"field_key":key,"role":role,"meaning":meaning} for (key,role),meaning in deduplicated.items()],
        "line_columns":list(LINE_COLUMNS), "line_column_labels": LINE_LABELS, "review_focus":focus, "constraints":[
        "只输出有直接原文依据的允许field_key和角色；不存在的字段不输出，不推断或补全。",
        "raw_value必须是所引source_segment_ids原文连续子串，保留大小写、前导零、单位与标点。",
        "字段中的数量/金额含单位时仍保留原始字符串；币种和单位另有原文才输出。",
        "明细行按真实行结构输出cells及每格source IDs，不按列表序号猜行，不编汇总、费用口径。",
        "续页重复表头、小计和总计保留为原文，不输出为货物明细行；table_header_source_segment_ids只是原文列语义，不是货物值。",
        "SKU、规格、批号、项号、箱号范围、原产国独立输出，不根据货描或发运国家推断；quantity不混入法定第一/第二数量、申报数量或签收数量。",
        "净重、毛重和计费重量单位各自引原文；公用weight_unit只有原表明确共用该单位才输出，不能凭惯例填KG。",
        "角色以Seller/Buyer/Shipper/Consignee等实际标签为准；买方不默认收货人，卖方不默认发货人。",
        "货物领域只规定关注信息，不能推断危险品、受控物项、证书义务或补出批准、签字和合规结论。",
    ]}


def compact_extraction_input(segments: list[dict]) -> list[dict]:
    """同一表格行的表头语义只发送一次，所有原值与来源ID仍逐格保留。"""
    seen = set()
    result = []
    for segment in segments:
        value = dict(segment)
        row = (value.get("section_id"), value.get("table_key"), value.get("page_no"), value.get("table_row"))
        if value.get("table_row") is not None:
            if row in seen:
                value.pop("table_header_context", None)
            seen.add(row)
        result.append(value)
    return result


def _label(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text).replace("’", "'")).casefold().strip(" .:：")


_PARTY_LABELS = {
    "seller": ("seller", "vendor", "卖方", "售货方", "vendeur", "vendedor", "продавец"),
    "buyer": ("buyer", "purchaser", "买方", "购货方", "acheteur", "comprador", "покупатель"),
    "shipper": ("shipper", "consignor", "发货人", "寄件人", "expéditeur", "expedidor", "remitente", "отправитель", "грузоотправитель"),
    "consignee": ("consignee", "收货人", "收件人", "destinataire", "destinatario", "получатель", "грузополучатель"),
    "notify_party": ("notify party", "通知方", "partie à notifier", "parte a notificar", "уведомляемая сторона"),
    "carrier": ("carrier", "承运人", "transporteur", "transportista", "перевозчик"),
    "forwarder": ("forwarder", "freight forwarder", "货运代理", "货代", "transitaire", "transitario", "экспедитор"),
    "exporter": ("exporter", "出口商", "exportateur", "exportador", "экспортер", "экспортёр"),
    "importer": ("importer", "进口商", "importateur", "importador", "импортер", "импортёр"),
    "manufacturer": ("manufacturer", "生产商", "制造商", "fabricant", "fabricante", "производитель"),
    "issuer": ("issuer", "签发机构", "émetteur", "emisor", "эмитент"),
    "insurer": ("insurer", "保险人", "assureur", "asegurador", "страховщик"),
    "insured": ("insured", "被保险人", "assuré", "asegurado", "застрахованный"),
    "warehouse": ("warehouse", "仓库", "entrepôt", "almacén", "склад"),
    "deliverer": ("deliverer", "交付方", "livreur", "entregador"),
    "receiver": ("receiver", "签收方", "réceptionnaire", "receptor"),
    "signatory": ("signatory", "签署人", "签字人", "signataire", "firmante", "подписант"),
    "applicant": ("applicant", "申请人", "demandeur", "solicitante", "заявитель"),
    "end_user": ("end user", "最终用户", "utilisateur final", "usuario final", "конечный пользователь"),
    "declarant": ("declarant", "申报人", "déclarant", "declarante", "декларант"),
    "owner": ("owner", "所有人", "propriétaire", "propietario", "владелец"),
    "supplier": ("supplier", "供应商", "fournisseur", "proveedor", "поставщик"),
    "laboratory": ("laboratory", "检验机构", "实验室", "laboratoire", "laboratorio", "лаборатория"),
}
PARTY_FIELD_KEYS = frozenset({"name", "address", "phone", "email", "company_no", "tax_no", "contact_name"})
_PARTY_ROLE_ALIASES = {_label(label): role for role, labels in _PARTY_LABELS.items() for label in labels}
_SUMMARY_UNITS = {
    "usd", "eur", "cny", "rmb", "gbp", "jpy", "rub", "руб", "€", "$", "¥", "£",
    "kg", "kgs", "g", "t", "lb", "lbs", "pcs", "ctns", "carton", "cartons", "box", "boxes", "crate", "crates",
    "package", "packages", "piece", "pieces", "unit", "units", "bag", "bags", "pallet", "pallets",
    "件", "箱", "千克", "公斤", "克", "吨", "袋", "托盘", "包",
    "caisse", "caisses", "colis", "palette", "palettes", "pièce", "pièces", "unité", "unités",
    "caja", "cajas", "bulto", "bultos", "palé", "palés", "palet", "palets", "pieza", "piezas", "unidad", "unidades", "saco", "sacos",
    "коробка", "коробки", "коробок", "ящик", "ящики", "ящиков", "место", "места", "мест", "шт", "шт.",
    "штука", "штуки", "штук", "паллет", "поддон", "поддоны", "кг", "г", "т",
}
_SUMMARY_UNIT_PATTERN = "(?:" + "|".join(re.escape(unit) for unit in sorted(_SUMMARY_UNITS, key=len, reverse=True)) + ")"
_SUMMARY_VALUE_PATTERN = r"(?:(?=[-+\d.,()\s]*\d)[-+\d.,()\s]+(?:\s*" + _SUMMARY_UNIT_PATTERN + r")?|" + _SUMMARY_UNIT_PATTERN + ")"


def party_role_labels(text: str) -> list[dict]:
    """只认完整角色标签，组合标题逐个保留原角色，不读正文句子或地名。"""
    parts = re.split(r"\s+(?:and|et|y|и)\s+|[/／|&＋+、;；]|[与及]", text.strip().strip(":："), flags=re.I)
    if not parts or any(_label(part) not in _PARTY_ROLE_ALIASES for part in parts):
        return []
    return [{"role": _PARTY_ROLE_ALIASES[_label(part)], "label": part.strip().strip(":：")} for part in parts]


def annotate_party_roles(segments: list[dict]) -> list[dict]:
    """角色只来自紧邻原文标题或同表同列角色表头，不跨表/页/区段传播。"""
    material = [dict(segment) for segment in segments]
    pending = None
    active_table = None
    table_context = []
    scope = None
    for segment in material:
        segment.pop("party_role_context", None)
        segment.pop("party_role_evidence", None)
        current_scope = (segment.get("section_id"), segment.get("page_no"))
        if current_scope != scope:
            pending, active_table, table_context = None, None, []
            scope = current_scope
        if segment.get("structure") != "table_cell":
            labels = party_role_labels(segment["text"])
            pending = [{**label, "source_segment_id": str(segment["id"])} for label in labels] or None
            active_table, table_context = None, []
            continue
        table = (*current_scope, segment.get("table_key"))
        if table != active_table:
            table_context = pending if segment.get("table_key") is not None and pending else []
            pending, active_table = None, table
        if table_context:
            segment["party_role_evidence"] = list(table_context)
            segment["party_role_context"] = sorted({entry["role"] for entry in table_context})

    tables = {}
    for segment in material:
        if segment.get("structure") == "table_cell" and segment.get("table_key") is not None and segment.get("table_row") is not None:
            table = (segment.get("section_id"), segment.get("page_no"), segment.get("table_key"))
            tables.setdefault(table, {}).setdefault(segment["table_row"], []).append(segment)
    for rows in tables.values():
        columns = {}
        for _, row in sorted(rows.items()):
            nonempty = [segment for segment in row if segment["text"].strip()]
            labels = [(segment, party_role_labels(segment["text"])) for segment in nonempty]
            # Seller | Buyer 并列表头只给本列，不能把整表都标为第一个主体。
            if len(labels) >= 2 and all(values for _, values in labels):
                columns = {segment.get("table_col"): [{**value, "source_segment_id": str(segment["id"])} for value in values]
                    for segment, values in labels if segment.get("table_col") is not None}
                continue
            for segment in row:
                evidence = columns.get(segment.get("table_col"))
                if evidence:
                    segment["party_role_evidence"] = list(evidence)
                    segment["party_role_context"] = sorted({entry["role"] for entry in evidence})
    return material


def resolve_party_role(role: str, raw_value: str, source_segment_ids: list[str], material_by_id: dict[str, dict], allowed_roles: set[str]) -> str | None:
    """明确源角色校正候选；多角色原标签按类型允许值解歧，不以模型覆盖原文。"""
    contexts = []
    for identifier in source_segment_ids:
        segment = material_by_id.get(identifier)
        if segment is None or raw_value not in segment["text"]:
            continue
        roles = set(segment.get("party_role_context") or [])
        if roles:
            contexts.append(roles & allowed_roles)
    if not contexts:
        return role
    supported = set.intersection(*contexts)
    if len(supported) == 1:
        return next(iter(supported))
    return role if role in supported else None


def annotate_extraction_structure(segments: list[dict]) -> list[dict]:
    """为抽取标记可证实的表头/汇总，保留全部原件片段及位置。"""
    material = [dict(segment) for segment in segments]
    rows = {}
    for segment in material:
        if segment.get("structure") == "table_cell" and segment.get("table_row") is not None:
            scope = (segment.get("section_id"), segment.get("table_key"))
            rows.setdefault((scope, segment.get("page_no"), segment["table_row"]), []).append(segment)
    aliases = {_label(label): column for column, labels in LINE_LABELS.items() for label in labels}
    summaries = {"subtotal", "sub total", "total", "grand total", "小计", "合计", "总计", "sous-total", "sous total", "total général", "итого", "всего"}
    for segment in material:
        if segment.get("structure") != "table_cell" and re.fullmatch(
            r"(?:" + "|".join(re.escape(label) for label in summaries) + r")[\s:：]+" + _SUMMARY_VALUE_PATTERN, _label(segment["text"])):
            segment["extraction_row_kind"] = "summary"
    headers = {}
    for (scope, _, _), row in rows.items():
        row.sort(key=lambda s: s.get("table_col") if s.get("table_col") is not None else 0)
        columns = {aliases[_label(s["text"])] for s in row if _label(s["text"]) in aliases}
        labels = [_label(s["text"]) for s in row if s["text"].strip()]
        # 至少两个独立列标签，并且没有真实数据值，才将整行视作表头。
        is_header = len(columns) >= 2 and all(label in aliases or label in {"currency", "币种", "devise", "moneda", "валюта"} for label in labels)
        header_columns = {segment.get("table_col"): aliases.get(_label(segment["text"])) for segment in headers.get(scope, [])}
        identifying_columns = {"description", "specification", "hs_code", "country_of_origin", "package_marks", "package_range"}
        has_goods_details = any(segment["text"].strip() and header_columns.get(segment.get("table_col")) in identifying_columns for segment in row[1:])
        is_summary = bool(len(labels) >= 2 and labels[0] in summaries and not has_goods_details and
            all(re.fullmatch(_SUMMARY_VALUE_PATTERN, label) for label in labels[1:]))
        if is_header:
            headers[scope] = row
        for segment in row:
            segment["extraction_row_kind"] = "header" if is_header else "summary" if is_summary else "detail"
            if scope in headers:
                segment["table_header_source_segment_ids"] = [str(s["id"]) for s in headers[scope]]
                segment["table_header_context"] = [{"table_col": s.get("table_col"), "text": s["text"], "source_segment_id": str(s["id"])} for s in headers[scope]]
    return annotate_party_roles(material)


def structured_table_lines(material: list[dict], *, require_sku: bool = True):
    """明确表头与独立SKU货物行直接摘格；无结构证明仍交模型候选。"""
    from app.prompts.schemas import ExtractedCell, ExtractedLineItem
    aliases = {_label(label): key for key, labels in LINE_LABELS.items() for label in labels}
    rows = {}
    for segment in material:
        if segment.get("structure") == "table_cell" and segment.get("extraction_row_kind") == "detail":
            rows.setdefault((segment.get("section_id"), segment.get("table_key"), segment.get("page_no"), segment.get("table_row")), []).append(segment)
    result = []
    for row in rows.values():
        context = next((s.get("table_header_context") for s in row if s.get("table_header_context")), None)
        if not context:
            continue
        columns = {h["table_col"]: aliases.get(_label(h["text"])) for h in context}
        headers = {h["table_col"]: h for h in context}
        identity_column = "sku" if "sku" in columns.values() or require_sku else "description"
        if identity_column not in columns.values() or "quantity" not in columns.values():
            continue
        cells = []
        for segment in row:
            key = columns.get(segment.get("table_col"))
            raw = segment["text"].strip()
            if not key or not raw:
                continue
            cells.append(ExtractedCell(column_key=key, raw_value=raw, source_segment_ids=[segment["id"]]))
            if key in {"net_weight", "gross_weight", "chargeable_weight"}:
                header = headers[segment["table_col"]]
                unit = re.search(r"\b(kg|g|lb|t)\b", header["text"], re.I)
                if unit and _MEASUREMENTS[key] not in columns.values():
                    cells.append(ExtractedCell(column_key=_MEASUREMENTS[key], raw_value=unit[1], source_segment_ids=[header["source_segment_id"]]))
            if key in _MEASUREMENTS and normalize_numeric(raw) is None:
                mixed = re.fullmatch(r"([-+]?\d+(?:\.\d+)?)\s*([^\d]+)", raw)
                if mixed:
                    cells[-1] = ExtractedCell(column_key=key, raw_value=mixed[1], source_segment_ids=[segment["id"]])
                    unit_key = _MEASUREMENTS[key]
                    if unit_key not in columns.values():
                        cells.append(ExtractedCell(column_key=unit_key, raw_value=mixed[2].strip(), source_segment_ids=[segment["id"]]))
        keys = [cell.column_key for cell in cells]
        if len(keys) != len(set(keys)) or not {identity_column, "quantity"}.issubset(keys):
            continue
        result.append(ExtractedLineItem(row_source_segment_ids=[s["id"] for s in row], cells=cells))
    return result


def merge_table_candidates(model_lines, table_lines):
    # 同一明确表格行只保留直接摘格结果，不能把模型漏列/重复行再作为新货物。
    covered = [set(line.row_source_segment_ids) for line in table_lines]
    return [*table_lines, *(line for line in model_lines
        if not any(set(line.row_source_segment_ids) & ids for ids in covered))]


def build_line_values(cells, row_source_ids: list[str], source_map: dict[str, str], structure_map: dict[str, dict] | None = None) -> dict | None:
    """仅保存逐格通过原文核验的行；多计量口径始终附真实源ID。"""
    if not row_source_ids or len(row_source_ids) != len(set(row_source_ids)) or not set(row_source_ids) <= source_map.keys():
        return None
    if structure_map and any(structure_map.get(key, {}).get("extraction_row_kind") in {"header", "summary"} for key in row_source_ids):
        return None
    values, sources = {}, {}
    for cell in cells:
        key, raw, refs = cell.column_key, cell.raw_value, cell.source_segment_ids
        row_refs = set(refs).issubset(row_source_ids)
        header_refs = set().union(*(set(structure_map.get(id, {}).get("table_header_source_segment_ids", [])) for id in row_source_ids)) if structure_map else set()
        explicit_header_unit = key in set(_MEASUREMENTS.values()) and bool(refs) and set(refs).issubset(header_refs)
        if key not in LINE_COLUMNS or key in values or not (row_refs or explicit_header_unit) or not source_value_present(raw, refs, source_map):
            return None
        values[key], sources[key] = raw, list(refs)
    if not values:
        return None
    numeric_keys = {"quantity", "unit_price", "amount", "package_count", "net_weight", "gross_weight", "chargeable_weight"}
    unsupported_scalars = set(_MEASUREMENTS) - numeric_keys
    unsupported_scalars |= {f"{key}_unit" for key in unsupported_scalars}
    result = {key: raw for key, raw in values.items() if key not in numeric_keys | unsupported_scalars}
    result.update({key: value for key in numeric_keys if key in values and
        (value := normalize_numeric(values[key])) is not None and abs(value) < Decimal("1e14") and value.as_tuple().exponent >= -6})
    # SQL兼容列不能静默截断单位或四舍五入；原格与精确计量值仍在证据/measurements里。
    for key, limit in {"quantity_unit": 16, "package_unit": 16, "net_weight_unit": 16,
            "gross_weight_unit": 16, "chargeable_weight_unit": 16, "weight_unit": 8, "currency": 8}.items():
        if key in result and len(result[key]) > limit:
            del result[key]
    result["cell_sources"] = sources
    result["measurements"] = []
    for kind, unit_key in _MEASUREMENTS.items():
        if kind not in values:
            continue
        if unit_key not in values and kind in {"net_weight", "gross_weight"} and "weight_unit" in values:
            unit_key = "weight_unit"
        number = normalize_numeric(values[kind])
        result["measurements"].append({"quantity_kind": kind, "raw_value": values[kind],
            "value": str(number) if number is not None else None, "unit": values.get(unit_key),
            "value_source_segment_ids": sources[kind], "unit_source_segment_ids": sources.get(unit_key, [])})
    return result


def line_evidence_fingerprint(values: dict[str, str], cell_sources: dict[str, list[str]], source_map: dict[str, str]) -> tuple | None:
    """只识别同一来源格的重复候选；同值异行、同段多次出现均不合并。"""
    if not values or set(values) != set(cell_sources):
        return None
    entries = []
    for column, raw in sorted(values.items()):
        refs = sorted(set(cell_sources[column]))
        if not isinstance(raw, str) or not raw or not refs or any(ref not in source_map for ref in refs):
            return None
        # 同一聚合源段若含两条相同商品行，没有字符偏移就不能证明是同一行。
        pattern = r"(?<!\w)" + re.escape(raw) + (r"(?![\w.,])" if normalize_numeric(raw) is not None else r"(?!\w)")
        if sum(len(re.findall(pattern, source_map[ref])) for ref in refs) != 1:
            return None
        entries.append((column, raw, tuple(refs)))
    return tuple(entries)


def normalize_evidence_lines(lines, source_map: dict[str, str], table_rows: dict[str, tuple] | None = None):
    """比较投影排除证实重复的模型记录，原始LineItem及来源永不删除。"""
    kept, aliases, seen = [], {}, {}
    proven = {}
    physical = {}
    if table_rows:
        for line in lines:
            refs = getattr(line, "source_segment_ids", [])
            row_keys = {table_rows[ref] for ref in refs if ref in table_rows}
            if refs and all(ref in table_rows for ref in refs) and len(row_keys) == 1:
                key = (str(line.parse_artifact_id), str(line.logical_document_id), next(iter(row_keys)))
                physical.setdefault(key, []).append(line)
        for key, candidates in physical.items():
            richer = max(candidates, key=lambda line: len(line.cell_sources or {}))
            def raw_cells(line):
                measures = {m["quantity_kind"]: m.get("raw_value") for m in (line.measurements or [])}
                return {column: measures.get(column, getattr(line, column, None)) for column in (line.cell_sources or {})}
            chosen = raw_cells(richer)
            def same_value(left, right):
                lnum, rnum = normalize_numeric(str(left)), normalize_numeric(str(right))
                return lnum == rnum if lnum is not None and rnum is not None else left == right
            # 同一物理行的漏列候选可归并；若已有值互相矛盾，保留全部候选待确认。
            if any(m.get("extraction_method") == "structured_table/1" for m in (richer.measurements or [])) or all(
                    all(column not in chosen or same_value(value, chosen[column]) for column, value in raw_cells(candidate).items())
                    for candidate in candidates):
                proven[key] = richer
    for line in sorted(lines, key=lambda value: (str(value.logical_document_id), value.line_no, str(value.id))):
        refs = getattr(line, "source_segment_ids", [])
        row_keys = {table_rows[ref] for ref in refs if ref in table_rows} if table_rows else set()
        if refs and table_rows and all(ref in table_rows for ref in refs) and len(row_keys) == 1:
            replacement = proven.get((str(line.parse_artifact_id), str(line.logical_document_id), next(iter(row_keys))))
            if replacement is not None and replacement.id != line.id:
                aliases[str(line.id)] = str(replacement.id)
                continue
        refs = getattr(line, "cell_sources", None) or {}
        measurements = {item.get("quantity_kind"): item for item in (getattr(line, "measurements", None) or [])}
        values = {}
        for column in refs:
            measure = measurements.get(column)
            raw = measure.get("raw_value") if measure else getattr(line, column, None)
            if isinstance(raw, Decimal):
                # 兼容列已失去原小数格式时不猜原值、不合并。
                raw = None
            if not isinstance(raw, str):
                break
            values[column] = raw
        fingerprint = line_evidence_fingerprint(values, refs, source_map)
        key = (str(line.parse_artifact_id), str(line.logical_document_id), fingerprint) if fingerprint else None
        if key is not None and key in seen:
            aliases[str(line.id)] = str(seen[key].id)
        else:
            kept.append(line)
            if key is not None:
                seen[key] = line
    return kept, aliases


def extraction_batches(segments: list[dict], *, max_input_tokens: int = 6500, max_segments: int = 40, serialized: bool = False):
    batch=[];tokens=0
    groups=[]
    for segment in segments:
        key = (segment.get("section_id"), segment.get("table_key"), segment.get("page_no"), segment.get("table_row")) if segment.get("structure") == "table_cell" and segment.get("table_row") is not None else None
        if key is not None and groups and groups[-1][0] == key:
            groups[-1][1].append(segment)
        else:
            groups.append((key, [segment]))
    for _, group in groups:
        cost = (estimate_tokens(json.dumps(group, ensure_ascii=False)) + 8 if serialized else
            sum(estimate_tokens(segment["text"]) + estimate_tokens(str(segment.get("table_header_context", []))) + 120 for segment in group))
        if cost>max_input_tokens or len(group)>max_segments:
            raise PromptBudgetExceeded("单个来源片段或表格行超出抽取预算，须先按结构拆分")
        if batch and (len(batch)+len(group)>max_segments or tokens+cost>max_input_tokens):
            yield batch;batch=[];tokens=0
        batch.extend(group);tokens+=cost
    if batch:yield batch


def bounded_extraction_batches(segments: list[dict], *, max_input_tokens: int):
    """保持已落账批次编号，仅拆分完整序列化超限的批次。"""
    for index, batch in enumerate(extraction_batches(segments, max_input_tokens=max_input_tokens)):
        if estimate_tokens(json.dumps(batch, ensure_ascii=False)) <= max_input_tokens:
            yield str(index), batch
        else:
            for child, part in enumerate(extraction_batches(batch, max_input_tokens=max_input_tokens, serialized=True)):
                yield f"{index}.{child}", part


def normalize_numeric(raw: str) -> Decimal | None:
    """仅解析完整数字词面；不猜金额币种、小数分隔约定或缺失数量。"""
    value=raw.strip()
    if not value or any(ch not in "0123456789.+-" for ch in value):return None
    try:result=Decimal(value)
    except InvalidOperation:return None
    return result if result.is_finite() else None


def source_value_present(raw_value: str, source_segment_ids: list[str], source_map: dict[str,str]) -> bool:
    return bool(raw_value and source_segment_ids) and all(value in source_map for value in source_segment_ids) and any(raw_value in source_map[value] for value in source_segment_ids)

_FIELD_LABELS = {key:meaning for key,_,meaning in _COMMON}
_FIELD_LABELS.update({key:"起运地" for key in ORIGIN_FIELD_KEYS})
_FIELD_LABELS.update({"invoice_no":"发票号","invoice_date":"发票日期","packing_list_no":"箱单号",
    "packing_list_date":"箱单日期","bill_of_lading_no":"提单号","on_board_date":"装船日期",
    "departure_date":"离港日期","arrival_date":"到港日期",
    "name":"名称","address":"地址","phone":"电话","email":"邮箱","company_no":"主体编号"})

def field_label(key: str, role: str | None = None) -> str:
    from app.documents.type_catalog import DOCUMENT_PROFILES, CARGO_PROFILES, ROLE_LABELS
    labels={k:label for p in (*DOCUMENT_PROFILES,*CARGO_PROFILES) for k,_,label in p.fields}
    label=_FIELD_LABELS.get(key,labels.get(key,key))
    if key in {"name","address","phone","email","company_no","tax_no","contact_name"}:
        label={"tax_no":"税号","contact_name":"联系人"}.get(key,label)
        return ROLE_LABELS.get(role or "","未明确主体")+label
    return label
