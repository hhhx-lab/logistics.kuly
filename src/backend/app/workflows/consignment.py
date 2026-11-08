"""同票核对子流程；原件核对与翻译范围分开，复用已发布的解析与翻译节点。"""
from uuid import UUID

from app.workflows.graphs import build_consignment_graph
from app.workflows.translation import TranslationNodes


class ConsignmentNodes:
    def __init__(self, worker, *, translate=False):
        self.worker = worker
        self.translate = translate
        self.translation = TranslationNodes(worker) if translate else None

    def __getattr__(self, name):
        if name in {"validate_documents", "translate_segments", "quality_and_preview", "publish_review"} and self.translation:
            return getattr(self.translation, name)
        return getattr(self.worker, name)

    def load_spec(self, state):
        result = self.worker.load_spec(state)
        self.translation_documents = list(self.worker.documents)
        if self.translate:
            self.worker.documents = [(UUID(v["document_id"]), v["document_version"])
                                     for v in self.worker.spec["comparison_source_versions"]]
        return result

    def associate_documents(self, state):
        from app.consignment.execution import prepare_comparison
        with self.worker.transaction() as db:
            run = self.worker.locked(db)
            from app.workflows.worker import append_event
            run.current_stage = "同票核对"
            append_event(db, run, "stage", "正在关联并核对同票原始材料")
            append_event(db, run, "comparison.associating", "正在关联同票单证")
            self.batch_id = prepare_comparison(db, run, self.worker.settings, self.worker.parse_ids)
        return {"comparison_batch_id": str(self.batch_id)}

    def publish_comparison(self, state):
        from app.consignment.execution import publish_comparison
        with self.worker.transaction() as db:
            run = self.worker.locked(db)
            publish_comparison(db, run, self.worker.settings, UUID(state["comparison_batch_id"]))
        if self.translate:
            self.worker.documents = self.translation_documents
            wanted = set(self.translation_documents)
            from app.documents.evidence_models import ParseArtifact
            with self.worker.transaction() as db:
                self.worker.locked(db)
                self.worker.parse_ids = [p for p in self.worker.parse_ids
                                         if (db.get(ParseArtifact, p).document_id, db.get(ParseArtifact, p).document_version) in wanted]
        return {}


def build_registered_consignment_graph(worker):
    return build_consignment_graph(ConsignmentNodes(worker))


def build_registered_translation_v2_graph(worker):
    return build_consignment_graph(ConsignmentNodes(worker, translate=True), translate=True)
