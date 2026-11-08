"""
原件解析适配层。领域层只依赖 `dto` 中的统一对象；具体解析器可替换而不改领域 schema。
"""

from app.documents.parsing.docx_adapter import DOCX_PARSER_VERSION, DocxAdapter
from app.documents.parsing.dto import (
    AnchorPrecision,
    DocumentRejected,
    DocxLocation,
    EmbeddedImageDraft,
    NormRect,
    PageInfo,
    ParsedBlock,
    ParsedTable,
    ParsedTableCell,
    ParsedTableRow,
    ParseOutcome,
    ParserUnavailable,
    QualityFlag,
    SourceAnchorDraft,
    SourceSegmentDraft,
    TableCellRef,
)
from app.documents.parsing.fake import FAKE_TEXT_MARKER, DeterministicFakeParser
from app.documents.parsing.mineru import (
    MinerUAdapter,
    MinerUError,
    MinerUOptions,
    MinerUResult,
    MinerUStatus,
    MinerUSubmission,
    normalize_content_list,
)
from app.documents.parsing.native_pdf import (
    NATIVE_PARSER_VERSION,
    NativePdfAdapter,
    crop_rect_png,
    extract_pages_pdf,
    render_page_png,
)
from app.documents.parsing.selector import (
    ParsePlan,
    merge_outcomes,
    ocr_input,
    parse_local_routes,
    require_ready,
    select_parser,
)

__all__ = [
    "DOCX_PARSER_VERSION",
    "FAKE_TEXT_MARKER",
    "NATIVE_PARSER_VERSION",
    "AnchorPrecision",
    "DeterministicFakeParser",
    "DocumentRejected",
    "DocxAdapter",
    "DocxLocation",
    "EmbeddedImageDraft",
    "MinerUAdapter",
    "MinerUError",
    "MinerUOptions",
    "MinerUResult",
    "MinerUStatus",
    "MinerUSubmission",
    "NativePdfAdapter",
    "NormRect",
    "PageInfo",
    "ParsePlan",
    "ParseOutcome",
    "ParsedBlock",
    "ParsedTable",
    "ParsedTableCell",
    "ParsedTableRow",
    "ParserUnavailable",
    "QualityFlag",
    "SourceAnchorDraft",
    "SourceSegmentDraft",
    "TableCellRef",
    "crop_rect_png",
    "extract_pages_pdf",
    "merge_outcomes",
    "normalize_content_list",
    "ocr_input",
    "parse_local_routes",
    "render_page_png",
    "require_ready",
    "select_parser",
]
