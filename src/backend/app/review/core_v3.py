"""core@3：官方币种快照及ISO6346校验位；来源缺失不判通过。"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from app.review.core_v2 import direct_sources
from app.review.rules import RuleResult

CURRENCY_TABLE = json.loads((Path(__file__).parent / "data" / "iso4217_20260917.json").read_text())
CURRENCY_CODES = frozenset(CURRENCY_TABLE["codes"])
HISTORIC_TABLE = json.loads((Path(__file__).parent / "data" / "iso4217_historic_20260101.json").read_text())
HISTORIC_CODES = frozenset(HISTORIC_TABLE["codes"])
CHECK_DIGIT_SOURCE = "https://www.bic-code.org/check-digit-calculator/"
ISO6346_VALUES = {letter: value for letter, value in zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", (10,12,13,14,15,16,17,18,19,20,21,23,24,25,26,27,28,29,30,31,32,34,35,36,37,38))}


def container_check_digit(prefix: str) -> int | None:
    if not re.fullmatch(r"[A-Z]{3}[UJZ][0-9]{6}", prefix):
        return None
    weighted = sum((int(character) if character.isdigit() else ISO6346_VALUES[character]) * 2 ** position
                   for position, character in enumerate(prefix))
    return (weighted % 11) % 10


def identity_rules(fields, lines, sources):
    source_by_id = {row["id"]: row for row in sources}
    rows = list(fields)
    rows.extend({**line, "id": None, "line_id": line.get("id"), "field_key": "currency", "value": line["currency"]}
                for line in lines if line.get("currency") is not None)
    results = []
    for row in rows:
        key = row.get("field_key")
        if key not in {"container_no", "currency"}:
            continue
        value = row.get("value")
        value = value.strip() if isinstance(value, str) else ""
        proven = bool(value) and direct_sources(row, source_by_id, value)
        context = {"raw_value": value, "direct_source": proven, "line_id": row.get("line_id")}
        if key == "currency":
            listed = value in CURRENCY_CODES
            special = value in {"XXX", "XTS"}
            historic = value in HISTORIC_CODES and not listed
            status = "undetermined" if not proven or not re.fullmatch("[A-Z]{3}", value) or special or historic else "passed" if listed else "failed"
            scope = "SIX官方ISO4217当前代码表校验；不判断历史生效日、汇率或交易适用性"
            context.update({"rule_basis": "iso4217-list-one@2026-09-17", "listed": listed,
                            "source_url": CURRENCY_TABLE["source_url"], "source_sha256": CURRENCY_TABLE["source_sha256"],
                            "source_publish_date": CURRENCY_TABLE["source_publish_date"], "historical_validity": "not_assessed", "historic_code": historic,
                            "historic_source_url": HISTORIC_TABLE["source_url"], "historic_source_sha256": HISTORIC_TABLE["source_sha256"],
                            "minor_units": CURRENCY_TABLE["codes"].get(value, {}).get("minor_units")})
            if historic:
                scope += "；已退出当前表的历史代码，单据日期及当时适用性未核实，不能判无效"
            if special:
                scope += "；XXX为无币种、XTS为测试代码，不能作为交易币种通过"
            rule_id = "field.currency.iso4217"
        else:
            shape = bool(re.fullmatch(r"[A-Z]{3}[UJZ][0-9]{7}", value))
            expected = container_check_digit(value[:10]) if shape else None
            correct = shape and int(value[-1]) == expected
            status = "undetermined" if not proven or not shape else "passed" if correct else "failed"
            scope = "ISO6346校验位核对；不证明BIC注册归属、柜体存在或号码真实性"
            context.update({"rule_basis": "iso6346-check-digit@3", "source_url": CHECK_DIGIT_SOURCE,
                            "algorithm": "letter-values-skip-multiples-of-11; powers-of-two-left-to-right; mod11; remainder10-to0",
                            "expected_check_digit": expected, "observed_check_digit": int(value[-1]) if shape else None,
                            "shape_valid": shape, "registration": "not_assessed", "authenticity": "not_assessed"})
            rule_id = "field.container.iso6346_check_digit"
        results.append(RuleResult(rule_id, status, scope + ("；来源不足或用途特殊，须核对原件" if status == "undetermined" else ""),
                                  tuple(row.get("source_segment_ids", [])), (row["id"],) if row.get("id") else (),
                                  calculation=context, severity="red" if status == "failed" else "yellow", blocks_export=status == "failed"))
    return results


def date_sequence_rules(fields, sources):
    """仅受控同运输事件范围、同日历与时区输入可以比较先后。"""
    by_id = {row["id"]: row for row in sources}
    groups = {}
    for row in fields:
        if row.get("field_key") in {"departure_date", "arrival_date"}:
            groups.setdefault(row.get("logical_document_id"), []).append(row)
    results = []
    for lid, rows in groups.items():
        departure = [row for row in rows if row.get("field_key") == "departure_date"]
        arrival = [row for row in rows if row.get("field_key") == "arrival_date"]
        proven = len(departure) == len(arrival) == 1 and bool(lid)
        values = []
        for row in rows:
            raw = row.get("value", "")
            try:
                parsed = date.fromisoformat(raw) if isinstance(raw, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw) else None
            except ValueError:
                parsed = None
            proven = proven and parsed is not None and direct_sources(row, by_id, raw)
            values.append(parsed)
        proven = proven and all(row.get("date_basis") == "local_calendar" and row.get("timezone")
                                and row.get("event_scope") and row.get("date_basis_confirmed_by") for row in rows)
        proven = proven and len({row.get("timezone") for row in rows}) == 1 and len({row.get("event_scope") for row in rows}) == 1
        failed = proven and date.fromisoformat(departure[0]["value"]) > date.fromisoformat(arrival[0]["value"])
        results.append(RuleResult("date.transport_sequence", "failed" if failed else "passed" if proven else "undetermined",
                                  "已确认同运输事件范围、同当地日历和时区的离港日不得晚于到港日" if proven else "日期用途、运输事件对应、日历或时区未经受控确认，不推断先后",
                                  tuple(dict.fromkeys(sid for row in rows for sid in row.get("source_segment_ids", []))),
                                  tuple(row["id"] for row in rows if row.get("id")),
                                  calculation={"logical_document_id": lid, "rule_basis": "same-calendar-transport-sequence@3", "premises_confirmed": bool(proven)},
                                  severity="red" if failed else "yellow", blocks_export=bool(failed)))
    if not groups:
        results.append(RuleResult("date.transport_sequence", "not_run", "没有离港/到港日期候选，日期先后未运行"))
    return results
