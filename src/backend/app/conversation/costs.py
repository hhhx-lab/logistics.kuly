"""对话Turn内共享冻结费用上限；不使用Run预算或重算历史报价。"""
from decimal import Decimal

from app.infra.model_gateway import CallBudget
from app.workflows.costs import model_cost_summary


class ConversationCostBudget(CallBudget):
    def __init__(self, *, settings, binding, records, frozen_limit):
        # 传输重试/结构修复继续共用原有单生成调用和token预算。
        super().__init__(max_calls=3, max_total_tokens=settings.chat_input_token_budget + settings.response_token_reserve)
        summary = model_cost_summary(records)
        self.cost_used = Decimal(summary["known_model_cost_cny"])
        self.unknown_cost = bool(summary["unknown_model_call_count"])
        self.max_cost = Decimal(str(frozen_limit)) if frozen_limit is not None else None
        self.binding = binding

    def can_start(self, reserved_tokens=0):
        if not super().can_start(reserved_tokens):
            return False
        if self.max_cost is None:
            return True
        if self.unknown_cost:
            return False
        if self.binding.protocol == "deterministic_fake":
            return self.cost_used <= self.max_cost
        price = self.binding.price
        if price is None or not price.verified:
            return False
        reserve = Decimal(max(reserved_tokens, self.binding.context_window_tokens)) * max(
            price.input_cny_per_million, price.output_cny_per_million) / Decimal(1000000)
        return self.cost_used + reserve <= self.max_cost

    def record_cost(self, value):
        if value is None:
            self.unknown_cost = True
        else:
            self.cost_used += value
