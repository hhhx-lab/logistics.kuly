"""有限业务算术：仅Decimal和白名单单位，不执行表达式代码。"""
import re
from decimal import Decimal, InvalidOperation, localcontext
from app.api.idempotency import request_digest
from app.conversation.evidence import EvidenceClarification

UNITS = {"kg": ("weight", Decimal("1")), "千克": ("weight", Decimal("1")), "公斤": ("weight", Decimal("1")),
    "g": ("weight", Decimal("0.001")), "克": ("weight", Decimal("0.001")), "t": ("weight", Decimal("1000")), "吨": ("weight", Decimal("1000")),
    "m": ("length", Decimal("1")), "米": ("length", Decimal("1")), "cm": ("length", Decimal("0.01")), "厘米": ("length", Decimal("0.01")),
    "usd": ("usd", Decimal("1")), "美元": ("usd", Decimal("1")), "cny": ("cny", Decimal("1")), "rmb": ("cny", Decimal("1")), "元": ("cny", Decimal("1")),
    "eur": ("eur", Decimal("1")), "欧元": ("eur", Decimal("1")), "件": ("count", Decimal("1")), "箱": ("boxes", Decimal("1"))}
UNIT_PATTERN = "|".join(sorted(UNITS, key=len, reverse=True))
NUMBER = r"[-+]?\d+(?:\.\d+)?"


def calculation_requested(question):
    explicit = r"计算|算一下|算一算|算出|求和|相加|加起来|加总|差多少|差值|相差|换算|转换为|折合|(?:毛重|净重|数量|金额).{0,40}(?:一致|相同|相等)|运费.{0,8}怎么算|\b(?:sum|difference|convert|calculate)\b"
    if re.search(explicit, question, re.I):
        return True
    operand = NUMBER + r"\s*(?:" + UNIT_PATTERN + r")(?![a-z])"
    return bool(re.search(r"(?:合计|总和|一共|共计)\s*(?:是|为)?\s*" + operand
        + r"\s*(?:和|加|与|及|\+|and)\s*" + operand, question, re.I))


def format_decimal(value):
    return format(value.normalize(), "f")


def _operand(value, unit, evidence_id=None):
    try:
        number = Decimal(str(value).replace(",", ""))
    except InvalidOperation as exc:
        raise EvidenceClarification("操作数不是明确数字，请确认字段值和单位。") from exc
    if not number.is_finite() or abs(number) > Decimal("1e18") or number.as_tuple().exponent < -12:
        raise EvidenceClarification("数值超出支持范围，请确认计算口径。")
    normalized = (unit or "").lower()
    if normalized not in UNITS:
        raise EvidenceClarification("请明确每个操作数的重量单位、数量口径或币种；不猜单位或汇率。")
    return {"value": number, "unit": normalized, "evidence_id": evidence_id}


def calculate(question, facts, case_id):
    if not calculation_requested(question):
        return None
    matches = re.findall(r"(" + NUMBER + r")\s*(" + UNIT_PATTERN + r")(?![a-z])", question, re.I)
    operands = [_operand(value, unit) for value, unit in matches]
    if not operands:
        # 译文用于阅读，同源翻译不能再次计入数量/金额操作数。
        unique = {f["evidence_id"]: f for f in facts if f.get("value") is not None
            and f["claim_kind"] in {"source_value", "adopted_value"}}
        if len({f.get("field_key") for f in unique.values()}) > 1:
            raise EvidenceClarification("请明确同一字段的对应范围，不能把不同业务字段相加或比较。")
        # 同一逻辑字段有人工作用值时，以冻结修订采用值为操作数，原值仍保留证据展示。
        adopted = {(f["target"].get("logical_document_id"), f.get("field_key")) for f in unique.values() if f["claim_kind"] == "adopted_value"}
        operands = [_operand(f["value"], f.get("unit"), f["evidence_id"]) for f in unique.values()
            if f["claim_kind"] == "adopted_value" or (f["target"].get("logical_document_id"), f.get("field_key")) not in adopted]
    if not operands or len(operands) > 50:
        raise EvidenceClarification("请明确要计算的字段、操作数、单位和适用范围。")
    with localcontext() as context:
        context.prec = 40
        difference = False
        rate = re.search(r"(" + NUMBER + r")\s*(USD|CNY|RMB|EUR|美元|元|欧元)\s*(?:/|每)\s*(kg|千克|公斤|吨|t|件|箱)", question, re.I)
        if "运费" in question and rate:
            price = _operand(rate[1], rate[2])
            basis = rate[3].lower()
            quantities = [o for o in operands if UNITS[o["unit"]][0] == UNITS[basis][0]]
            if len(quantities) != 1 or len(operands) != 2 or re.search(r"附加|税|最低|阶梯|燃油|体积|另", question):
                raise EvidenceClarification("请确认单价、计费重量或数量及附加费口径；复杂运价不由一般算式推断。")
            quantity = quantities[0]
            amount = quantity["value"] * UNITS[quantity["unit"]][1] / UNITS[basis][1]
            value, unit = amount * price["value"], price["unit"]
            formula = f"{format_decimal(amount)} {basis} × {format_decimal(price['value'])} {unit}/{basis} = {format_decimal(value)} {unit}"
        else:
            dimensions = {UNITS[o["unit"]][0] for o in operands}
            if len(dimensions) != 1:
                raise EvidenceClarification("操作数单位或币种不同，请明确换算依据；未提供汇率不能合并币种。")
            target = re.search(r"(?:换算(?:成|为)?|转换为|折合|成|为|convert.{0,8}to)\s*(" + UNIT_PATTERN + r")(?![a-z])", question, re.I)
            unit = target[1].lower() if target else operands[0]["unit"]
            if UNITS[unit][0] not in dimensions:
                raise EvidenceClarification("目标单位与操作数维度不同，请确认单位或汇率。")
            values = [o["value"] * UNITS[o["unit"]][1] / UNITS[unit][1] for o in operands]
            equality = bool(re.search(r"一致|相同|相等", question))
            difference = equality or bool(re.search(r"差多少|差值|相差|difference", question, re.I))
            if difference and len(values) != 2:
                raise EvidenceClarification("差值计算需要恰好两个明确操作数，请关联两份对应单据或明确两项值。")
            if target and len(values) != 1:
                raise EvidenceClarification("单位换算请明确单个操作数；多项合计请另行说明。")
            if len(values) == 1 and not target:
                raise EvidenceClarification("请提供第二个操作数或明确目标换算单位。")
            value = abs(values[0] - values[1]) if difference else sum(values, Decimal("0"))
            expression = " − ".join(format_decimal(v) for v in values) if difference else " + ".join(format_decimal(v) for v in values)
            formula = f"{'绝对差值 ' if difference else ''}{expression} {unit} = {format_decimal(value)} {unit}"
    ids = [o["evidence_id"] for o in operands if o["evidence_id"]]
    identifier = "calculation:" + request_digest({"formula": formula, "evidence_ids": ids, "question": question})
    return {"evidence_id": identifier, "claim_kind": "computed", "canonical_text": "程序计算：" + formula + "；操作数依据：" + ("本轮冻结证据" if ids else "用户本轮明确输入，未核验为单据事实") + "。",
        "value": format_decimal(value), "unit": unit, "formula": formula, "operand_evidence_ids": ids,
        "comparison": "数值相等" if difference and value == 0 else "数值不相等" if difference else None,
        "calculation_question": question,
        "target": {"case_id": str(case_id) if case_id else None}, "card": None}
