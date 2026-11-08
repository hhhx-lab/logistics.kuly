from typing import TypedDict


class RunGraphState(TypedDict, total=False):
    run_id: str
    workflow_version: int
    execution_epoch: int
    task_spec_hash: str
    artifact_ids: list[str]
    comparison_batch_id: str
    branch_decision: str | None
    error_class: str | None
