"""维护者核对的公开知识入口；不构成法律审定或本票证据。"""
import re

SOURCES = (
    {"source_id": "icc-incoterms-2020", "title": "ICC：Incoterms® 2020", "url": "https://iccwbo.org/business-solutions/incoterms-rules/incoterms-2020/",
     "version": "Incoterms 2020", "scope": "FOB/CIF等贸易术语的用途及版本范围；具体义务须查合同引用的正式规则全文。", "terms": "FOB CIF Incoterms 贸易术语",
     "facts": ["Incoterms规则说明买卖双方在货物交付中承担的任务、费用和风险。", "Incoterms说明费用、风险及义务；费用安排与风险节点需分别核对。", "适用规则须由合同约定；这里不提供未核对的条款全文。"]},
    {"source_id": "wco-hs-2022", "title": "WCO：Harmonized System", "url": "https://www.wcoomd.org/en/topics/nomenclature/overview/what-is-the-harmonized-system.aspx",
     "version": "公开概述核对版 2026-10-04", "scope": "协调制度的商品分类用途；不据此确定具体货物税号、税率或通关结论。", "terms": "HS 编码 归类 税号",
     "facts": ["协调制度是由WCO维护的国际商品名称和编码分类体系。", "该通用介绍不确定具体货物的归类、税率或任何通关结论。"]},
    {"source_id": "ita-incoterms-overview", "title": "Trade.gov：Know Your Incoterms", "url": "https://www.trade.gov/know-your-incoterms",
     "version": "公开概述核对版 2026-10-04", "scope": "贸易术语版本、适用运输方式及费用／风险关系的公开概述；不替代合同与正式规则全文。", "terms": "FOB CIF Incoterms 贸易术语",
     "facts": ["FOB、CIF属于Incoterms 2020用于海运及内河运输的规则。", "贸易术语须明确采用版本；费用承担与货物交付及风险转移应分别核对。", "贸易术语不决定货物所有权何时转移，也不覆盖合同全部条款。"]},
)


def knowledge_sources(question):
    selected = [s for s in SOURCES if any(re.search(re.escape(term), question, re.I) for term in s["terms"].split())]
    return [{k: v for k, v in s.items() if k != "terms"} for s in selected]


def knowledge_cards(source_ids, supplied):
    allowed = {s["source_id"]: s for s in supplied}
    if len(source_ids) != len(set(source_ids)) or any(i not in allowed for i in source_ids):
        raise ValueError("知识来源不属于本轮注册资料")
    return [{"type": "knowledge_source", **{k: v for k, v in allowed[i].items() if k != "facts"}} for i in source_ids]


def knowledge_quote(key, source_ids, supplied):
    """未知通用知识引用不发布；不将模型标记包装成真实资料。"""
    source_id, separator, ordinal = key.rpartition(":")
    allowed = {source["source_id"]: source for source in supplied}
    if not separator or source_id not in source_ids or source_id not in allowed or not ordinal.isdigit():
        return ""
    facts = allowed[source_id]["facts"]
    return facts[int(ordinal)] if int(ordinal) < len(facts) else ""


def verify_knowledge_paragraphs(paragraphs, source_ids, supplied, *, require_source=False):
    """自由解释不做关键词拦截；引用占位符与来源归属仍校验。"""
    from app.conversation.safety import guard_text
    texts = []
    for paragraph in paragraphs:
        slots = re.findall(r"\[\[([^\[\]]+)\]\]", paragraph)
        remainder = re.sub(r"\[\[[^\[\]]+\]\]", "", paragraph)
        if "[[" in remainder or "]]" in remainder:
            raise ValueError("知识段落包含未绑定来源的断言")
        text = paragraph
        for key in slots:
            text = text.replace("[[" + key + "]]", knowledge_quote(key, source_ids, supplied))
        texts.append(guard_text(text))
    return texts
