"""确定性关联与比较；不以相似度、行号或缺失值产生冲突。"""
from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from app.api.idempotency import request_digest

RULE_VERSION = "consignment-review@1"
CONFIRMED = {"auto_confirmed", "confirmed"}
UNITS = {"pcs": "piece", "pc": "piece", "pieces": "piece", "件": "piece", "个": "piece",
         "ctn": "carton", "ctns": "carton", "cartons": "carton", "箱": "carton",
         "kgs": "kg", "kilograms": "kg", "公斤": "kg", "千克": "kg", "g": "g",
         "grams": "g", "克": "g", "kg": "kg", "lb": "lb", "lbs": "lb", "pounds": "lb",
         "tonne": "t", "tonnes": "t", "metric ton": "t", "metric tons": "t", "吨": "t", "t": "t"}
UNITS.update({"piece": "piece", "pièce": "piece", "pièces": "piece", "pieza": "piece", "piezas": "piece",
    "шт": "piece", "штука": "piece", "штуки": "piece", "штук": "piece",
    "caja": "carton", "cajas": "carton", "carton": "carton", "cartones": "carton",
    "килограмм": "kg", "килограммы": "kg", "кг": "kg", "kilogramme": "kg", "kilogrammes": "kg",
    "kilogramo": "kg", "kilogramos": "kg"})
MASS_TO_KG={"kg":Decimal(1),"g":Decimal("0.001"),"lb":Decimal("0.45359237"),"t":Decimal(1000)}


def canonical(value):
    return " ".join(unicodedata.normalize("NFKC", value or "").strip().casefold().split())


def identifier(value):
    # 编号只消除显示空格，不把标点、零或字母混同。
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value or "")).upper()


def line_identity(line):
    sku = identifier(getattr(line, "sku", None))
    lot = identifier(getattr(line, "lot_no", None))
    spec = canonical(getattr(line, "specification", None))
    return (sku, lot, spec) if sku else None


def match_candidates(lines):
    by_identity = {}
    for line in lines:
        key = line_identity(line)
        if key: by_identity.setdefault(key[:2], {}).setdefault(str(line.logical_document_id), []).append(line)
    result = []
    for identity, documents in by_identity.items():
        ids = sorted(documents)
        for i, left in enumerate(ids):
            for right in ids[i + 1:]:
                l, r = documents[left], documents[right]
                if len(l) > 1 and len(r) > 1: continue
                specifications = {canonical(getattr(line, "specification", None)) for line in l + r
                    if canonical(getattr(line, "specification", None))}
                if len(specifications) > 1: continue
                result.append((l, r, {"sku": identity[0], "lot_no": identity[1], "specification": next(iter(specifications), "")}))
    return result


def measure(line, kind):
    measurements = [m for m in (getattr(line, "measurements", None) or []) if m.get("quantity_kind") == kind]
    if len(measurements) > 1: return None, None
    if measurements:
        m = measurements[0]
        try: value = Decimal(str(m["value"])) if m.get("value") is not None else None
        except (InvalidOperation,ValueError): value = None
        if value is not None and not value.is_finite():value=None
        return value, UNITS.get(canonical(m.get("unit")), canonical(m.get("unit"))) or None
    column = {"quantity": ("quantity", "quantity_unit"), "package_count": ("package_count", "package_unit"),
              "net_weight": ("net_weight", "net_weight_unit"), "gross_weight": ("gross_weight", "gross_weight_unit"),
              "chargeable_weight": ("chargeable_weight", "chargeable_weight_unit")}.get(kind)
    if not column: return None, None
    value = getattr(line, column[0], None)
    unit = getattr(line, column[1], None)
    if kind in {"net_weight", "gross_weight"}: unit = unit or getattr(line, "weight_unit", None)
    if value is not None and (not isinstance(value,Decimal) or not value.is_finite()):value=None
    return value, UNITS.get(canonical(unit), canonical(unit)) or None


def comparison_kinds(left, right):
    kinds = ["quantity"]
    for kind in ("net_weight", "gross_weight", "chargeable_weight"):
        values = [measure(line, kind) for line in left + right]
        if values and all(value is not None and unit for value, unit in values):
            kinds.append(kind)
    return kinds


def compare_measurements(left, right, scope, state):
    kind = scope.get("quantity_kind", "quantity")
    lvals, rvals = [measure(x, kind) for x in left], [measure(x, kind) for x in right]
    members = {"left": [{"line_id": str(x.id), "logical_id": str(x.logical_document_id),
                          "value": str(v) if v is not None else None, "unit": u} for x, (v, u) in zip(left, lvals)],
               "right": [{"line_id": str(x.id), "logical_id": str(x.logical_document_id),
                           "value": str(v) if v is not None else None, "unit": u} for x, (v, u) in zip(right, rvals)]}
    base = {"members": members, "scope": scope, "kind": kind}
    all_units={u for v,u in lvals+rvals}
    conversion=bool(all_units) and all_units.issubset(MASS_TO_KG)
    if conversion:
        for side,values in (("left",lvals),("right",rvals)):
            for member,(value,unit) in zip(members[side],values):
                member["normalized_value"]=str(value*MASS_TO_KG[unit]) if value is not None else None
                member["normalized_unit"]="kg"
        lvals=[(v*MASS_TO_KG[u] if v is not None else None,"kg") for v,u in lvals]
        rvals=[(v*MASS_TO_KG[u] if v is not None else None,"kg") for v,u in rvals]
        base["unit_conversion"]={"normal_unit":"kg","exact_factors":{u:str(MASS_TO_KG[u]) for u in sorted(all_units)}}
    missing = next((why for valid, why in [
        (state in CONFIRMED, "Cargo association awaits confirmation"),
        (bool(scope.get("cargo_id")) and bool(scope.get("leg_id")), "Cargo or transport stage is unknown"),
        (scope.get("completeness") == "complete", "Comparison material coverage is not confirmed complete"),
        (all(v is not None and u for v, u in lvals + rvals), "Measurement value or unit is missing"),
        (kind != "package_count" or bool(scope.get("package_level")), "Packaging level is unknown"),
        (len({u for v, u in lvals + rvals}) == 1, "Measurement units are not equivalent"),
        (kind not in {"net_weight", "gross_weight", "chargeable_weight"} or scope.get("absolute_tolerance") is not None,
         "Weight precision and tolerance have not been verified"),
    ] if not valid), None)
    if missing:
        known = all(value is not None and unit for value, unit in lvals + rvals) and len({unit for _,unit in lvals+rvals}) == 1
        if known:
            lv, rv = sum((value for value,_ in lvals), Decimal(0)), sum((value for value,_ in rvals), Decimal(0))
            base.update(left_total=str(lv), right_total=str(rv), delta=str(lv-rv), unit=lvals[0][1])
            missing += f"; selected {kind}: {lv} versus {rv} {lvals[0][1]}, difference {lv-rv}"
        return {**base, "status": "undetermined", "severity": "unknown", "difference_kind": "scope_insufficient", "explanation": missing}
    lv, rv = sum((v for v, u in lvals), Decimal(0)), sum((v for v, u in rvals), Decimal(0))
    try:tolerance = Decimal(str(scope.get("absolute_tolerance") or 0))
    except (InvalidOperation,ValueError):return {**base,"status":"undetermined","severity":"unknown","difference_kind":"scope_insufficient","explanation":"Measurement tolerance is invalid"}
    if not tolerance.is_finite() or tolerance<0:return {**base,"status":"undetermined","severity":"unknown","difference_kind":"scope_insufficient","explanation":"Measurement tolerance is invalid"}
    delta = lv - rv
    failed = abs(delta) > tolerance
    return {**base, "status": "failed" if failed else "passed", "severity": "red" if failed else "unknown",
            "difference_kind": "conflict" if failed else "equivalent", "left_total": str(lv), "right_total": str(rv),
            "delta": str(delta), "tolerance": str(tolerance), "unit": lvals[0][1],
            "explanation": f"{kind}: {lv} {lvals[0][1]} versus {rv} {rvals[0][1]}; difference {delta}; tolerance {tolerance}"}


def compare_fields(left, right, *, kind, state, scope):
    source = [(f.candidate_value or f.source_text) for f in (left, right)]
    adopted = [(f.normalized_value or f.candidate_value or f.source_text) for f in (left, right)]
    out = []
    for basis, values in (("source", source), ("adopted", adopted)):
        applicable = state in CONFIRMED and scope.get("same_semantics") is True
        valid = all(values) and all(f.source_segment_ids for f in (left, right))
        same = identifier(values[0]) == identifier(values[1]) if kind.endswith("reference") or kind.endswith("tax_id") else canonical(values[0]) == canonical(values[1])
        status = "undetermined" if not applicable or not valid else "passed" if same else "failed"
        # 地址与名称差异需要专业或人工判定，不能凭字符串不同证明主体不同。
        strong = kind.endswith("tax_id") or kind.endswith("reference")
        out.append({"basis": basis, "status": status, "severity": "red" if status == "failed" and strong else "yellow" if status == "failed" else "unknown",
                    "difference_kind": "conflict" if status == "failed" else "equivalent" if status == "passed" else "scope_insufficient",
                    "members": {"left": [{"field_id": str(left.id), "logical_id": str(left.logical_document_id), "value": values[0]}],
                                "right": [{"field_id": str(right.id), "logical_id": str(right.logical_document_id), "value": values[1]}]},
                    "scope": scope, "explanation": f"{kind}: {values[0] or '?'} versus {values[1] or '?'}"})
    return out


def stable_key(rule, objects, scope, basis):
    return request_digest({"rule": rule, "objects": sorted(str(x) for x in objects), "scope": scope, "basis": basis})
