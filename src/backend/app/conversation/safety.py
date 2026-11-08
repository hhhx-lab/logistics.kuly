"""对话资料不提升指令权限；输出仅防内部信息泄露，不限制业务措辞。"""
from functools import lru_cache
import re
from pydantic import SecretStr

SAFE_REPLY = "我可以说明物流单证业务和当前单票的处理情况，但不能提供项目内部设计、系统提示词或实际模型与环境配置。"


def requests_internal_information(text):
    return bool(re.search(
        r"(?:你们|你|本|当前|这个|我们)(?:的)?(?:项目|系统|服务|模型|后端|环境).{0,20}"
        r"(?:源码|内部设计|架构|数据库结构|连接串|环境变量|配置|密钥|API.?key|token|提示词)"
        r"|(?:打印|读取|展示|输出|告诉|透露|显示).{0,20}(?:\.env|系统提示词|开发者提示词|模型配置|内部配置)"
        r"|(?:your|this project|this system).{0,30}(?:system prompt|api.?key|environment|configuration|source code)", text, re.I))


@lru_cache(maxsize=1)
def protected_values():
    from app.config import get_settings
    values = set()
    def collect(value):
        if isinstance(value, SecretStr):
            secret = value.get_secret_value()
            if len(secret) >= 8: values.add(secret)
        elif isinstance(value, dict):
            for item in value.values(): collect(item)
        elif isinstance(value, (list, tuple)):
            for item in value: collect(item)
    settings = get_settings()
    collect(settings.model_dump())
    for name in type(settings).model_fields:
        value = getattr(settings, name)
        if isinstance(value, str) and len(value) >= 6 and (name.endswith("model_name") or name.endswith("base_url")):
            values.add(value)
    from app.prompts.registry import get_prompt_registry
    spec = get_prompt_registry().get("chat.system")
    values.update(line for line in spec.template.splitlines() if len(line) >= 40 and "{{" not in line)
    return tuple(values)


def contains_internal_information(text):
    if any(value in text for value in protected_values()): return True
    return bool(re.search(
        r"(?:sk-[A-Za-z0-9_-]{12,}|Bearer\s+[A-Za-z0-9._-]{16,})"
        r"|(?:postgres(?:ql)?|redis)://[^\s]+"
        r"|(?:/Users/|/home/|/opt/)[^\s]{4,}"
        r"|(?:CHAT|EXTRACT|TRANSLATE|VISION)_MODEL_(?:API_KEY|BASE_URL|NAME)\s*[:=：]\s*\S+"
        r"|(?:系统提示词|开发者提示词|内部配置|项目内部设计|数据库连接串)(?:如下|全文|为|是)\s*[:：]?\s*[^。！？\n]{12,}"
        , text, re.I))


def guard_text(text):
    return SAFE_REPLY if contains_internal_information(text) else text


def guard_cards(value):
    if isinstance(value, str): return guard_text(value)
    if isinstance(value, list): return [guard_cards(item) for item in value]
    if isinstance(value, dict): return {key: guard_cards(item) for key, item in value.items()}
    return value
