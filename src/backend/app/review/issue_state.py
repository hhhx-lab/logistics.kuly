"""当前疑点提示与交付门禁共用的处置判定。"""


def issue_resolved(issue, disposition):
    if disposition is None:
        return False
    if disposition.action == "ignore":
        return True
    if disposition.action == "revise":
        return bool(disposition.reason and disposition.reason.strip())
    if disposition.action in {"adjudicate", "not-applicable"}:
        return bool(disposition.basis and disposition.basis.strip())
    return disposition.action == "accept" and issue.severity == "yellow"


def issue_blocks_delivery(issue, disposition):
    return issue.severity == "red" and not issue_resolved(issue, disposition)
