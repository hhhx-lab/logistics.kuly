"""界面与任务共用语言目录；语种不改变单票事实或冻结任务。"""
import re
from typing import Literal

LanguageCode = Literal["zh-CN", "en", "fr", "es", "ru"]
LANGUAGES = {"zh-CN": "中文", "en": "English", "fr": "Français", "es": "Español", "ru": "Русский"}
LANGUAGE_NAMES = {"zh-CN": "中文", "en": "英文", "fr": "法语", "es": "西班牙语", "ru": "俄语"}
ALIASES = {
 "zh-CN": r"中文|汉语|中国话|简体|chinese|zh-cn",
 "en": r"英文|英语|english|(?<![a-z])en(?![a-z])",
 "fr": r"法语|法文|french|français|francais|(?<![a-z])fr(?![a-z])",
 "es": r"西班牙语|西班牙文|西语|spanish|español|espanol|(?<![a-z])es(?![a-z])",
 "ru": r"俄语|俄文|russian|русский|(?<![a-z])ru(?![a-z])",
}

def requested_language(text: str) -> str | None:
    # 同时提及源语和目标语时，只消费明确的目标部分；含多目标不擅自代选。
    target = re.search(r"(?:翻译(?:成|为|到)|译(?:成|为)|translate\s+(?:.+?\s+)?(?:into|to))\s*(.+)", text, re.I)
    value = target.group(1) if target else text
    matches = [code for code, pattern in ALIASES.items() if re.search(pattern, value, re.I)]
    return matches[0] if len(matches) == 1 else None

def language_reply(text: str) -> str | None:
    value = re.sub(r"^(?:请|要|用|翻译成|翻译为|目标语言[:：]?)\s*", "", text.strip()).strip(" 。.!！")
    matches = [code for code, pattern in ALIASES.items() if re.fullmatch(pattern, value, re.I)]
    return matches[0] if len(matches) == 1 else None

def language_name(code: str | None) -> str:
    return LANGUAGE_NAMES.get(code, code or "未识别")
