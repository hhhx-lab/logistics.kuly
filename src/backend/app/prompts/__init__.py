"""
项目统一 Prompt Registry（Agent 方案第 6 节）。

所有项目维护的 Prompt 只能经这里按 `(key, version)` 取出；业务节点不得散落长 Prompt。
"""

from app.prompts.registry import (
    CAPABILITY_PROMPTS,
    REQUIRED_PROMPT_KEYS,
    PromptBudgetExceeded,
    PromptRegistry,
    PromptRenderError,
    PromptSpec,
    PromptUnavailable,
    RegistryCheck,
    RegistryIssue,
    RenderedPrompt,
    TrustLevel,
    get_prompt_registry,
)

__all__ = [
    "CAPABILITY_PROMPTS",
    "REQUIRED_PROMPT_KEYS",
    "PromptBudgetExceeded",
    "PromptRegistry",
    "PromptRenderError",
    "PromptSpec",
    "PromptUnavailable",
    "RegistryCheck",
    "RegistryIssue",
    "RenderedPrompt",
    "TrustLevel",
    "get_prompt_registry",
]
