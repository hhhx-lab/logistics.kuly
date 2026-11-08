"""工作流模型计价：冻结报价、供应商 usage 及预算；OCR 无报价保持未知。"""
from __future__ import annotations

from decimal import Decimal

from app.config import AdapterKind, ModelPrice, OCRPrice, Settings
from app.infra.model_gateway import CallBudget
from app.workflows.contracts import ModelBinding

NON_BILLABLE = {"not_sent", "rejected_retryable", "permanent_error", "budget_exhausted"}


def freeze_ocr_binding(settings: Settings) -> dict:
    from dataclasses import asdict
    from app.documents.parsing.mineru import MINERU_PROTOCOL_VERSION, MinerUOptions
    options = asdict(MinerUOptions.from_settings(settings))
    base = (settings.mineru_base_url or "").rstrip("/")
    matching = [price for price in settings.ocr_prices if price.verified and price.base_url.rstrip("/") == base
                and price.protocol == MINERU_PROTOCOL_VERSION and all(
                    getattr(price, key) == options[key] for key in ("model_version", "language", "enable_formula", "enable_table", "is_ocr"))]
    # 重叠报价不能凭列表顺序静默选价。
    price = matching[0] if len(matching) == 1 else None
    return {"base_url": base, "protocol": MINERU_PROTOCOL_VERSION, "options": options,
            "price": price.model_dump(mode="json") if price else None}


def estimate_ocr_request(binding: dict | None, *, pages: int | None, tasks: int | None = 1) -> dict:
    price = OCRPrice.model_validate(binding["price"]) if binding and binding.get("price") else None
    complete = (isinstance(pages, int) and not isinstance(pages, bool) and pages >= 0
                and isinstance(tasks, int) and not isinstance(tasks, bool) and tasks >= 0)
    amount = None
    if complete and tasks == 0:
        amount = Decimal(0)
    elif complete and price and price.verified:
        units = pages if price.billing_basis == "all_inclusive_per_page" else tasks
        amount = price.unit_cny * units
    return {"price_snapshot": price.model_dump(mode="json") if price else None, "estimated_pages": pages,
            "estimated_tasks": tasks, "estimate_upper_cny": str(amount) if amount is not None else None,
            "estimate_status": "bounded" if amount is not None else "unknown", "actual_cost_cny": None,
            "actual_cost_status": "unknown", "cost_scope": "ocr_only", "measurement_kind": "estimate"}


def estimate_source_ocr(settings: Settings, sources: list[dict], binding: dict) -> dict:
    pages, tasks, unknown = 0, 0, 0
    for source in sources:
        mime = source.get("mime")
        if mime in {"image/png", "image/jpeg"}:
            pages += 1
            tasks += 1
        elif mime == "application/pdf":
            count = source.get("page_count")
            if isinstance(count, int) and not isinstance(count, bool) and count > 0:
                pages += count
                tasks += 1
            else:
                unknown += 1
        elif mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            # 尚未解包时按允许的嵌图数量上限预留；每图为一个单页任务。
            pages += settings.docx_max_embedded_images
            tasks += settings.docx_max_embedded_images
        else:
            unknown += 1
    estimate = estimate_ocr_request(binding, pages=pages if not unknown else None, tasks=tasks if not unknown else None)
    return {"ocr_estimated_pages_upper": pages, "ocr_estimated_tasks_upper": tasks,
            "ocr_unknown_page_files": unknown, "ocr_estimate_upper_cny": estimate["estimate_upper_cny"],
            "ocr_estimate_status": estimate["estimate_status"],
            "ocr_price_versions": [binding["price"]["version"]] if binding.get("price") else [],
            "ocr_estimate_basis": "PDF全页上界、图片每张一页、DOCX嵌图配置上限；实际选路后按需OCR页数缩小"}


def workflow_cost_summary(model_records: list[dict], ocr_steps: list[dict], *, model_steps: list[dict] = (), run_status: str | None = None) -> dict:
    result = model_cost_summary(model_records)
    pending = sum(step.get("status") in {"running", "submitting"} for step in model_steps)
    unknown_steps = sum(step.get("status") == "needs_reconcile" for step in model_steps)
    complete = run_status not in {"queued", "processing", "awaiting_input"}
    result.update({"pending_model_step_count": pending, "unknown_model_step_count": unknown_steps,
                   "cost_measurement_status": "pending" if pending or not complete else "unknown" if unknown_steps or result["unknown_model_call_count"] else "complete"})
    if pending or unknown_steps or not complete:
        result["actual_model_cost_cny"] = None
        result["model_cost_status"] = "unknown"
    estimate, estimated_unknown, actual_unknown, task_count = Decimal(0), 0, 0, 0
    seen = set()
    for step in ocr_steps:
        key = step.get("external_task_id") or step.get("step_id")
        if key in seen:
            continue
        seen.add(key)
        data = step.get("ocr_cost")
        if data:
            value = data.get("estimate_upper_cny")
            if value is None:
                estimated_unknown += 1
            else:
                estimate += Decimal(value)
        if step.get("external_task_id"):
            task_count += 1
            # 当前官方适配协议没有可核验费用回执，报价估算不能替代供应商实际fee。
            actual_unknown += 1
            if not data:
                estimated_unknown += 1
    actual_ocr = None if actual_unknown else "0"
    actual_model = result["actual_model_cost_cny"]
    result.update({"ocr_cost_cny": actual_ocr, "actual_ocr_cost_cny": actual_ocr,
                   "ocr_cost_status": "unknown" if actual_unknown else "known", "ocr_task_count": task_count,
                   "unknown_ocr_task_count": actual_unknown, "ocr_estimate_upper_cny": None if estimated_unknown else str(estimate),
                   "ocr_estimate_status": "unknown" if estimated_unknown else "bounded",
                   "ocr_estimate_scope": "prepared_requests_only",
                   "actual_total_cost_cny": str(Decimal(actual_model) + Decimal(actual_ocr)) if complete and actual_model is not None and actual_ocr is not None else None,
                   "total_cost_scope": "model_and_ocr", "total_cost_cap_enforced": False,
                   "notice": "模型按冻结报价和完整usage计价；OCR上界仅累计已准备请求，未准备页不含在内，报价不能替代实际fee。已有任务缺供应商费用回执时实际未知。总额仅含模型与OCR；人民币上限仅约束模型。"})
    return result


def usage_cost(price: ModelPrice | None, usage: dict | None, *, is_fake=False, outcome="succeeded") -> Decimal | None:
    if is_fake or outcome in NON_BILLABLE:
        return Decimal(0)
    if not price or not price.verified or not usage:
        return None
    prompt, completion, total = (usage.get(key) for key in ("prompt_tokens", "completion_tokens", "total_tokens"))
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in (prompt, completion, total)):
        return None
    if prompt + completion != total:
        return None
    # completion_tokens 已包含 reasoning_tokens，不重复计入推理 token。
    return (Decimal(prompt) * price.input_cny_per_million + Decimal(completion) * price.output_cny_per_million) / Decimal(1000000)


class PricedLedger:
    """在现有逐请求持久账本入口计价；不重新按当前配置计算历史调用。"""
    def __init__(self, ledger, binding: ModelBinding, budget):
        self.ledger, self.binding, self.budget = ledger, binding, budget
        self.tenant_id = getattr(ledger, "tenant_id", None)

    def before_send(self):
        callback = getattr(self.ledger, "before_send", None)
        if callback:
            callback()

    def begin_attempt(self, call_id, provider_request_key, timeout_seconds):
        callback = getattr(self.ledger, "begin_attempt", None)
        if callback:
            return callback(call_id, provider_request_key, timeout_seconds)
        else:
            self.before_send()

    def append(self, record):
        price = self.binding.price
        matched = (record.role == self.binding.role and record.model_id == self.binding.model_name
                   and record.protocol == self.binding.protocol and record.endpoint_config_hash == self.binding.public_config_hash
                   and record.provider_model_id in {None, self.binding.model_name})
        cost = usage_cost(price if matched else None, record.usage.model_dump() if record.usage else None,
                          is_fake=record.is_fake, outcome=record.outcome)
        params = {**record.params, "price_snapshot": price.model_dump(mode="json") if price else None,
                  "cost_status": "known" if cost is not None else "unknown", "cost_scope": "model_only"}
        priced = record.model_copy(update={"cost_cny": cost, "params": params})
        self.ledger.append(priced)
        if self.budget is not None:
            self.budget.record_cost(cost)


class ModelCostBudget(CallBudget):
    def __init__(self, *, settings: Settings, records: list[dict], binding: ModelBinding):
        charged = [r for r in records if r.get("outcome") not in NON_BILLABLE]
        tokens = sum((r.get("usage") or {}).get("total_tokens") if (r.get("usage") or {}).get("total_tokens") is not None
                     else r.get("params", {}).get("estimated_total_tokens", 15000) for r in charged)
        super().__init__(max_calls=settings.run_max_model_calls, max_total_tokens=settings.run_max_model_tokens,
                         calls_used=len(records), tokens_used=tokens)
        summary = model_cost_summary(records)
        self.cost_used = Decimal(summary["known_model_cost_cny"])
        self.unknown_cost = bool(summary["unknown_model_call_count"])
        self.max_cost = settings.run_max_model_cost_cny
        self.price = binding.price
        self.is_fake = binding.protocol == "deterministic_fake"
        self.context_window = binding.context_window_tokens

    def can_start(self, reserved_tokens=0):
        if not super().can_start(reserved_tokens):
            return False
        if self.max_cost is None:
            return True
        if self.unknown_cost:
            return False
        if self.is_fake:
            return self.cost_used <= self.max_cost
        if self.price is None:
            return False
        # 输入 tokenizer/视觉 token 未可靠实测时按上下文总窗口预留，
        # 可比字符估算更保守；供应商 usage 超过预留后停止下一次调用。
        reserve = Decimal(max(reserved_tokens, self.context_window)) * max(self.price.input_cny_per_million, self.price.output_cny_per_million) / Decimal(1000000)
        return self.cost_used + reserve <= self.max_cost

    def record_cost(self, value):
        if value is None:
            self.unknown_cost = True
        else:
            self.cost_used += value


def model_cost_summary(records):
    known = Decimal(0)
    unknown = 0
    seen = set()
    for record in records:
        call_id = record.get("call_id")
        if call_id and call_id in seen:
            continue
        if call_id:
            seen.add(call_id)
        if record.get("is_fake") or record.get("outcome") in NON_BILLABLE:
            continue
        cost = record.get("cost_cny")
        if cost is None:
            unknown += 1
        else:
            known += Decimal(str(cost))
    return {"known_model_cost_cny": str(known), "actual_model_cost_cny": None if unknown else str(known),
            "unknown_model_call_count": unknown, "model_cost_status": "unknown" if unknown else "known",
            "ocr_cost_cny": None, "actual_total_cost_cny": None, "budget_scope": "model_only",
            "notice": "模型费用仅按冻结人民币全包报价与完整usage计价；OCR价格未核实，总费用未知。"}


def estimate_workflow_cost(settings: Settings, workflow_type: str, source_versions: list[dict], parameters: dict):
    from app.config import ModelRole
    from app.workflows.registry import get_workflow
    definition = get_workflow(workflow_type, 1)
    fake = settings.model_adapter is AdapterKind.DETERMINISTIC_FAKE
    prices = []
    roles = list(definition.model_roles)
    from app.infra.model_gateway import capabilities_for
    try:
        vision_profile = settings.model_profile(ModelRole.VISION)
    except ValueError:
        vision_profile = None
    if fake or (vision_profile and capabilities_for(ModelBinding.from_profile(ModelRole.VISION, vision_profile, "price-estimate")).image_input):
        roles.append(ModelRole.VISION)
    for role in roles:
        try:
            profile = settings.model_profile(role)
        except ValueError:
            profile = None
        prices.append(profile.price if profile else None)
    pages = sum(row.get("page_count") or 0 for row in source_versions)
    unknown_pages = sum(row.get("page_count") is None for row in source_versions)
    languages = 1 if parameters.get("target_language") else 0
    quoted = all(prices) and not unknown_pages
    # 单页OCR文字量与供应商tokenizer尚无基线，只给规模及配置预算上界，不编造页→token单价。
    upper = None
    if fake:
        upper = Decimal(0)
    elif quoted:
        highest = max(max(price.input_cny_per_million, price.output_cny_per_million) for price in prices)
        upper = Decimal(settings.run_max_model_tokens) * highest / Decimal(1000000)
    if upper is not None and settings.run_max_model_cost_cny is not None:
        upper = min(upper, settings.run_max_model_cost_cny)
    ocr = estimate_source_ocr(settings, source_versions, freeze_ocr_binding(settings))
    ocr_upper = ocr["ocr_estimate_upper_cny"]
    total_upper = str(upper + Decimal(ocr_upper)) if upper is not None and ocr_upper is not None else None
    return {"status": "synthetic" if fake else "bounded_model_budget" if quoted else "unknown",
            "cost_level": "synthetic" if fake else "unknown", "known_pages": pages, "unknown_page_files": unknown_pages,
            "target_language_count": languages, "target_language": parameters.get("target_language"),
            "scope": parameters.get("translation_scope", "selected_documents"), "model_budget_upper_cny": str(upper) if upper is not None else None,
            "predicted_model_cost_cny": None, "predicted_total_cost_cny": None, "ocr_cost_cny": None,
            "model_price_versions": [price.version for price in prices if price], "budget_scope": "model_only",
            **ocr, "estimated_external_cost_upper_cny": total_upper, "total_cost_scope": "model_and_ocr",
            "total_cost_cap_enforced": False,
            "notice": "按页数、目标语言与范围显示规模；OCR按已核实模式报价给保守上界，模型仅给配置预算上界，没有用量基线不作点预测。总额仅含模型与OCR，人民币上限只约束模型，实际OCR缺回执仍未知。"}
