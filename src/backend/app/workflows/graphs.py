from typing import Protocol

from langgraph.graph import END, START, StateGraph

from app.workflows.state import RunGraphState


class IngestNodes(Protocol):
    def load_spec(self, state: RunGraphState) -> dict[str, object]: ...

    def inspect_sources(self, state: RunGraphState) -> dict[str, object]: ...

    def parse_documents(self, state: RunGraphState) -> dict[str, object]: ...

    def classify_sections(self, state: RunGraphState) -> dict[str, object]: ...

    def extract_fields(self, state: RunGraphState) -> dict[str, object]: ...

    def verify_sources(self, state: RunGraphState) -> dict[str, object]: ...


class ReadIndexNodes(IngestNodes, Protocol):
    def publish_index(self, state: RunGraphState) -> dict[str, object]: ...


class TranslateReviewNodes(IngestNodes, Protocol):
    def validate_documents(self, state: RunGraphState) -> dict[str, object]: ...

    def translate_segments(self, state: RunGraphState) -> dict[str, object]: ...

    def quality_and_preview(self, state: RunGraphState) -> dict[str, object]: ...

    def publish_review(self, state: RunGraphState) -> dict[str, object]: ...


class ExportNodes(Protocol):
    def preflight(self, state: RunGraphState) -> dict[str, object]: ...

    def render(self, state: RunGraphState) -> dict[str, object]: ...

    def verify(self, state: RunGraphState) -> dict[str, object]: ...

    def commit_snapshot(self, state: RunGraphState) -> dict[str, object]: ...


def build_document_ingest(nodes: IngestNodes) -> StateGraph:
    graph = StateGraph(RunGraphState)
    graph.add_node("load_spec", nodes.load_spec)
    graph.add_node("inspect_sources", nodes.inspect_sources)
    graph.add_node("parse_documents", nodes.parse_documents)
    graph.add_node("classify_sections", nodes.classify_sections)
    graph.add_node("extract_fields", nodes.extract_fields)
    graph.add_node("verify_sources", nodes.verify_sources)
    graph.add_edge(START, "load_spec")
    graph.add_edge("load_spec", "inspect_sources")
    graph.add_edge("inspect_sources", "parse_documents")
    graph.add_edge("parse_documents", "classify_sections")
    graph.add_conditional_edges("classify_sections",lambda state: "wait" if nodes.run_awaiting_input(state) else "continue",{"wait":END,"continue":"extract_fields"})
    graph.add_edge("extract_fields", "verify_sources")
    graph.add_edge("verify_sources", END)
    return graph


def build_read_index_graph(nodes: ReadIndexNodes) -> StateGraph:
    graph = StateGraph(RunGraphState)
    graph.add_node("document_ingest", build_document_ingest(nodes).compile())
    graph.add_node("publish_index", nodes.publish_index)
    graph.add_edge(START, "document_ingest")
    graph.add_conditional_edges("document_ingest",lambda state: "wait" if nodes.run_awaiting_input(state) else "continue",{"wait":END,"continue":"publish_index"})
    graph.add_edge("publish_index", END)
    return graph


def build_translate_review_graph(nodes: TranslateReviewNodes) -> StateGraph:
    graph = StateGraph(RunGraphState)
    graph.add_node("document_ingest", build_document_ingest(nodes).compile())
    graph.add_node("validate_documents", nodes.validate_documents)
    graph.add_node("translate_segments", nodes.translate_segments)
    graph.add_node("quality_and_preview", nodes.quality_and_preview)
    graph.add_node("publish_review", nodes.publish_review)
    graph.add_edge(START, "document_ingest")
    graph.add_conditional_edges("document_ingest",lambda state: "wait" if nodes.run_awaiting_input(state) else "continue",{"wait":END,"continue":"validate_documents"})
    graph.add_conditional_edges("validate_documents",lambda state: "wait" if nodes.run_awaiting_input(state) else "continue",{"wait":END,"continue":"translate_segments"})
    graph.add_conditional_edges("translate_segments",lambda state: "wait" if nodes.run_awaiting_input(state) else "continue",{"wait":END,"continue":"quality_and_preview"})
    graph.add_edge("quality_and_preview", "publish_review")
    graph.add_edge("publish_review", END)
    return graph


def build_consignment_graph(nodes, *, translate=False) -> StateGraph:
    graph = StateGraph(RunGraphState)
    graph.add_node("document_ingest", build_document_ingest(nodes).compile())
    graph.add_node("associate_documents", nodes.associate_documents)
    graph.add_node("publish_comparison", nodes.publish_comparison)
    graph.add_edge(START, "document_ingest")
    graph.add_conditional_edges("document_ingest", lambda state: "wait" if nodes.run_awaiting_input(state) else "continue",
                                {"wait": END, "continue": "associate_documents"})
    graph.add_edge("associate_documents", "publish_comparison")
    if translate:
        for name in ("validate_documents", "translate_segments", "quality_and_preview", "publish_review"):
            graph.add_node(name, getattr(nodes, name))
        graph.add_edge("publish_comparison", "validate_documents")
        for name, next_name in (("validate_documents", "translate_segments"), ("translate_segments", "quality_and_preview")):
            graph.add_conditional_edges(name, lambda state: "wait" if nodes.run_awaiting_input(state) else "continue",
                                        {"wait": END, "continue": next_name})
        graph.add_edge("quality_and_preview", "publish_review")
        graph.add_edge("publish_review", END)
    else:
        graph.add_edge("publish_comparison", END)
    return graph


def build_export_graph(nodes: ExportNodes) -> StateGraph:
    graph = StateGraph(RunGraphState)
    graph.add_node("preflight", nodes.preflight)
    graph.add_node("render", nodes.render)
    graph.add_node("verify", nodes.verify)
    graph.add_node("commit_snapshot", nodes.commit_snapshot)
    graph.add_edge(START, "preflight")
    graph.add_edge("preflight", "render")
    graph.add_edge("render", "verify")
    graph.add_edge("verify", "commit_snapshot")
    graph.add_edge("commit_snapshot", END)
    return graph
