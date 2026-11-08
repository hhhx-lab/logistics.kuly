"""内置疑点分类、处理入口与证据边界。模型建议不等于人工处置。"""
import re

SKILL_ID = "document-issue-review@3"
CATEGORIES = {
    "role_mistranslation": ("主体角色错译", "买卖、收发、承运或签字角色可能与原文不一致。"),
    "meaning_reversal": ("翻译含义改变", "译文可能改变运输、交付或业务含义。"),
    "terminology": ("专业术语待修订", "术语可能与原文语境或明确业务用语不一致。"),
    "omission": ("内容漏译", "原文内容可能未完整译出。"),
    "other": ("译文表达待核对", "当前表达需要结合原文核对。"),
    "translation_quality": ("译文完整性异常", "检查空译、漏段、保护值变化和目标语言。"),
    "protected_value_changed": ("编号、数字或单位变化", "对照源文核对受保护编号、数量、金额、日期或单位。"),
    "source_uncertain": ("扫描识别不清", "对照原图检查缺字、漏行或低清晰区域。"),
    "source_omission": ("扫描内容缺漏", "核对原件页、行、表格或签字区域是否完整。"),
    "field_missing": ("关键信息缺失", "区分原件未提供、识别未提取和信息未确认。"),
    "layout": ("结构或对应位置异常", "核对表格行列、分页及源译对应关系。"),
    "quality": ("识别候选待核验", "候选与现有识别内容需对照原图确认。"),
    "undetermined": ("资料不足，需判断", "现有资料不足以完成本项核对。"),
    "not_covered": ("专业规则未覆盖", "需要当前规则之外的专业依据。"),
    "not_run": ("检查条件不足", "必要信息不齐，本项检查尚未执行。"),
    "conflict": ("字段或单据不一致", "不同来源或口径的值需要核验。"),
    "adopted_value_translation": ("人工值与译文不一致", "核对人工采用值及其原文依据与译文关系。"),
}
TRANSLATION_CATEGORIES = frozenset({"role_mistranslation", "meaning_reversal", "terminology", "omission", "other", "translation_quality", "protected_value_changed"})
ACTION_NAMES = ("revise-translation", "ignore", "request-revision", "apply-recommendation", "supplement-upload")


def source_targets(issue):
    excluded = {"value", "label", "text", "target"}
    return [source.get("target") or {key: value for key, value in source.items() if key not in excluded} for source in issue.sources or []]


def translation_target(issue):
    targets = {str(t["translation_segment_id"]): t for t in source_targets(issue) if t.get("translation_segment_id")}
    return next(iter(targets.values())) if len(targets) == 1 else None


def field_problem(issue, check=None):
    """字段风险与译文、整页质量、规则范围提示分开。"""
    if issue.category in TRANSLATION_CATEGORIES or issue.category in {"layout", "quality", "not_covered", "not_run"}:
        return False
    if check and (check.rule_id == "document.required_fields" or check.rule_id.startswith("material.") or check.rule_id.startswith("jurisdiction.") or check.rule_id.startswith("line.")):
        return False
    return any(t.get("field_id") for t in source_targets(issue)) or bool(check and check.input_field_ids)


def guidance_for(issue, overrides=None):
    stored = {**(getattr(issue, "guidance", None) or {}), **(overrides or {})}
    category, brief = CATEGORIES.get(issue.category, ("单证信息待核对", "按问题描述查看相关原件与业务信息。"))
    translation = translation_target(issue)
    field = next((t for t in source_targets(issue) if t.get("field_id")), None)
    target = translation or field or next(iter(source_targets(issue)), None)
    kind = "translation" if translation else "field" if field else "material"
    writable = bool(translation and issue.category in TRANSLATION_CATEGORIES)
    replacement = stored.get("replacement_text")
    default = ("核对原译对照；可手动修订该译段，或告诉 Agent 如何调整。" if writable else
        "核对现有原件；缺失或模糊信息可补充文件／图片。可让 Agent 分析证据和提出处理建议。")
    recommendation = stored.get("recommendation")
    recommendation = recommendation.strip() if isinstance(recommendation, str) else ""
    recommendation = recommendation or default
    can_apply = bool(recommendation or (writable and isinstance(replacement, str) and replacement.strip()))
    actions = [
        {"action": "revise-translation", "enabled": writable, "reason": None if writable else "本项未绑定唯一可修改译段；可先定位译文选择对应段，或提交 Agent 意见。"},
        {"action": "ignore", "enabled": True, "reason": None},
        {"action": "request-revision", "enabled": True, "reason": None},
        {"action": "apply-recommendation", "enabled": can_apply, "reason": None if can_apply else "暂无推荐处理，请先提交 Agent 意见。"},
        {"action": "supplement-upload", "enabled": True, "reason": None},
    ]
    busy = stored.get("agent_status") in {"queued", "needs_reconcile"}
    if busy:
        for action in actions:
            if action["action"] != "supplement-upload":
                action.update(enabled=False, reason="Agent 正在处理或外部请求待核查，请等待结果后继续。")
    return {"skill_version": SKILL_ID, "category_label": category, "explanation": issue.description or brief,
        "impact": stored.get("impact") or ("未处置的红项会阻止导出，请核对后修订、补件或由你明确忽略。" if issue.severity == "red" else "建议核对；本项不阻止导出。"),
        "recommendation": recommendation, "replacement_text": replacement,
        "target_kind": kind, "can_apply": can_apply, "target": target, "actions": actions,
        "agent_status": stored.get("agent_status"), "agent_response": stored.get("agent_response"),
        "agent_request_no": stored.get("agent_request_no", 0),
        "recommended_action": stored.get("recommended_action"),
        "agent_source_segment_ids": stored.get("agent_source_segment_ids") or []}


def recommendation_requires_assessment(guidance):
    """核验和补证建议先分析；没有证据时不能靠改译文结案。"""
    action = guidance.get("recommended_action")
    if action in {"supplement-upload", "human-review", "ignore"}:
        return True
    recommendation = (guidance.get("recommendation") or "").casefold()
    evidence_terms = ("补充材料", "补充文件", "补充原件", "补充图片", "补充证据", "补件", "补拍", "重拍", "补传", "重传", "上传", "重识别", "核对原件", "对照原件", "对照原图",
        "人工核验", "人工核对", "专业审核", "确认关联", "关联确认", "资料不足", "证据不足", "无法确认", "不能确认", "无法判断",
        "verify", "verification", "confirm", "upload", "rescan", "additional material", "additional document", "supplementary material",
        "missing source", "source is missing", "missing document", "missing original", "source text is missing", "illegible", "unclear source")
    return any(bool(re.search(r"\b" + re.escape(term) + r"\b", recommendation)) if term.isascii() else term in recommendation
        for term in evidence_terms)


def skill_instructions():
    from pathlib import Path
    root = Path(__file__).parent / "skills" / "document_issue_review"
    return (root / "SKILL.md").read_text() + "\n" + (root / "references" / "classification.md").read_text()


def classify_rule(result):
    """把确定性检查映射到识别/业务/证据类别，保留已证实错误的红项。"""
    rule = result.rule_id
    if rule in {"material.coverage", "catalog.extraction_coverage"}:
        category = "source_omission"
    elif rule.startswith("material."):
        category = "source_uncertain"
    elif rule == "document.required_fields":
        category = "field_missing"
    elif result.status == "not_covered":
        category = "not_covered"
    elif result.status == "not_run":
        category = "not_run"
    elif result.status == "failed" or rule == "field.duplicate_candidates":
        category = "conflict"
    else:
        category = "undetermined"
    proven = result.status == "failed"
    severity = "red" if proven and result.severity == "red" else "undetermined" if result.status in {"undetermined", "not_covered", "not_run"} else "yellow"
    return category, severity, severity == "red"


def guidance_actions_for_access(guidance, permissions, *, historical=False):
    for entry in guidance["actions"]:
        if historical or "review_revise" not in permissions:
            entry.update(enabled=False, reason="历史记录只读" if historical else "当前账号没有单据复核编辑权限")
    return guidance
