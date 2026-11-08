"""同票核对内部规则指导；复用疑点动作政策，不增加模型调用或自动处置。"""
from pathlib import Path

from app.review.issue_skill import ACTION_NAMES

SKILL_ID = "consignment-review@1"


def classify_comparison(result: dict) -> tuple[str, str, bool]:
    status = result.get("status")
    scope = result.get("scope") or {}
    # 否定、未知或尚无依据的比较不能由指导层升级为确定红项。
    proven = status == "failed" and result.get("difference_kind") == "conflict"
    severity = "red" if proven and result.get("severity") == "red" else "yellow" if proven else "undetermined"
    if severity == "red" and (scope.get("same_semantics") is not True and scope.get("completeness") != "complete"):
        severity = "undetermined"
    return ("conflict" if proven else "not_covered" if status == "not_covered" else "not_run" if status == "not_run" else "undetermined", severity, severity == "red")


def comparison_guidance(result: dict, rule_id: str) -> dict:
    category, severity, blocks = classify_comparison(result)
    basis = result.get("basis", "source")
    basis_label = "原件" if basis == "source" else "人工采用值"
    scope = result.get("scope") or {}
    members = result.get("members") or {}
    missing = result.get("difference_kind") == "scope_insufficient" or category != "conflict"
    if missing:
        explanation = "当前材料不足以确认此项比较，不能据此认定单据错误。" + str(result.get("explanation") or "")
        recommendation = "核对关联货物、运输阶段、材料覆盖范围、数量含义和单位；缺少原件时补充上传。主体或货描对应不清时提交 Agent 分析意见，再由你确认关联。"
    elif "left_total" in result and "right_total" in result:
        explanation = (f"{basis_label}的{result.get('kind', '数量')}口径出现差异：左侧合计{result['left_total']}，"
            f"右侧合计{result['right_total']}，单位{result.get('unit') or '未明确'}，"
            f"差额{result.get('delta', '未计算')}，允许误差{result.get('tolerance', '未审定')}。")
        recommendation = "先核对两侧原件对应货物行及完整分批范围；原件有误时补充正确材料，识别值有误时核对原图并修订对应业务采用值。译文修订不会消除原件数量冲突。"
    else:
        values = {side: [str(m.get("value")) for m in members.get(side, []) if m.get("value") is not None] for side in ("left", "right")}
        explanation = f"{basis_label}在明确比较口径下出现差异：左侧{'；'.join(values['left']) or '待核验'}；右侧{'；'.join(values['right']) or '待核验'}。"
        recommendation = "对照两侧原件及主体角色和运输阶段，区分名称写法、正常阶段变化、识别错误和真正原件冲突。不要只为使两侧相同而修改正确译文；可提交 Agent 分析或补充正确原件。"
    return {"skill_version": SKILL_ID, "rule_id": rule_id, "comparison_basis": basis,
        "classification": category, "severity": severity, "blocks_export": blocks,
        "explanation": explanation, "impact": "未解决且适用于本次交付的已证实红项会阻断导出。" if blocks else "本项不硬阻断导出，建议根据现有来源核对。",
        "recommendation": recommendation, "replacement_text": None,
        "actions": list(ACTION_NAMES), "scope": scope}


def skill_instructions() -> str:
    return (Path(__file__).parent / "skills" / "consignment_review" / "SKILL.md").read_text()
