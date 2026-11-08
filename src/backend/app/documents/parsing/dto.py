"""
解析层对领域层的统一契约（单证工作流方案 5.1）。

无论原件走 pdfplumber、MinerU、python-docx 还是假解析器，领域层只看到这里的草稿对象；
切换解析器不改这些 schema。领域 ID（parse_artifact_id、logical_document_id、section_id、
source_segment_id）由落库时分配，这里只用本次解析内稳定的 `key` 互相引用。

坐标约定：`rect` 以页面左上角为原点、按该页显示宽高归一化到 [0, 1]。
只有坐标能校准回原图时才给 `rect`；不可靠就退到真实的页/段/单元格级，绝不画猜测框。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PREPROCESSING_VERSION = "source-display/2"

AnchorPrecision = Literal["rect", "page_only", "paragraph_only", "cell", "none"]
ParseRoute = Literal["native_pdf", "ocr", "docx", "fake"]
BlockKind = Literal["separator", "page_break",
    "title",
    "text",
    "list",
    "table",
    "image",
    "equation",
    "code",
    "header",
    "footer",
    "page_number",
    "footnote",
    "other",
]
SegmentKind = Literal[
    "paragraph",
    "title",
    "list_item",
    "table_cell",
    "caption",
    "header",
    "footer",
    "footnote",
    "equation",
    "other",
]

QualityCode = Literal[
    # 页级路线
    "page_needs_ocr",
    "ocr_unavailable",
    # 坐标
    "bbox_missing",
    "bbox_uncalibrated",
    "rotation_uncalibrated",
    "image_blur_suspected",
    "image_glare_suspected",
    "image_edge_content",
    "image_resolution_low",
    # 表格
    "table_structure_unparsed",
    "table_merged_cells_unresolved",
    # 图片/区域未转写
    "image_region_not_transcribed",
    "unknown_block_type",
    # DOCX
    "docx_textbox_not_extracted",
    "docx_drawing_not_extracted",
    "docx_embedded_image_not_ocr",
    "docx_external_reference_ignored",
    "docx_alt_chunk_not_extracted",
    "docx_nested_table_flattened",
    "docx_notes_not_extracted",
    # 假实现
    "fake_parser_output",
]

_Frozen = ConfigDict(frozen=True, extra="forbid")


class NormRect(BaseModel):
    model_config = _Frozen

    x0: float = Field(ge=0, le=1)
    y0: float = Field(ge=0, le=1)
    x1: float = Field(ge=0, le=1)
    y1: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _ordered(self) -> NormRect:
        if not (self.x0 < self.x1 and self.y0 < self.y1):
            raise ValueError("矩形必须有正面积")
        return self


class DocxLocation(BaseModel):
    model_config = _Frozen

    part: Literal["body", "header", "footer"]
    # 页眉页脚按节区分；正文为 None
    section_index: int | None = None
    header_kind: Literal["default", "first", "even"] | None = None
    paragraph_index: int | None = None
    table_index: int | None = None


class TableCellRef(BaseModel):
    model_config = _Frozen

    table_key: str
    row_index: int = Field(ge=0)
    col_index: int = Field(ge=0)


class SourceAnchorDraft(BaseModel):
    """
    原件位置。`precision` 与字段组合由校验器强制：
    rect 必有页码与矩形；page_only 只有页码；paragraph_only 必有 DOCX 段落位置；
    cell 必有表格单元格地址；none 不带任何位置。任何精度都不允许「顺带」一个矩形。
    """

    model_config = _Frozen

    precision: AnchorPrecision
    page_no: int | None = Field(default=None, ge=1)
    rect: NormRect | None = None
    coordinate_space: Literal["page_normalized_top_left"] | None = None
    page_rotation: int = 0
    docx: DocxLocation | None = None
    table_cell: TableCellRef | None = None
    # 产出坐标的解析器/渲染版本；换版本后旧坐标须重新核验，不能跨版本复用
    locator_version: str
    # Embedded-image coordinates stay separate from any DOCX page coordinate.
    embedded_image: dict | None = None
    # 派生识别坐标与原件显示层/原始像素的可证明变换；旧解析无此字段。
    preprocessing_transform: dict | None = None

    @model_validator(mode="after")
    def _consistent(self) -> SourceAnchorDraft:
        p = self.precision
        if p == "rect":
            if self.rect is None or self.page_no is None or self.coordinate_space is None:
                raise ValueError("rect 精度必须有页码、矩形与坐标系")
        elif self.rect is not None:
            raise ValueError(f"{p} 精度不得携带矩形")
        if p == "page_only" and self.page_no is None:
            raise ValueError("page_only 精度必须有页码")
        if p == "paragraph_only" and (
            self.docx is None or (self.docx.paragraph_index is None and self.docx.table_index is None)
        ):
            # DOCX 没有稳定页码，段落或整张表就是它最细的可靠结构位置
            raise ValueError("paragraph_only 精度必须有段落或表格位置")
        if p == "cell" and self.table_cell is None:
            raise ValueError("cell 精度必须有单元格地址")
        if p == "none" and (self.page_no is not None or self.docx is not None or self.table_cell is not None):
            raise ValueError("none 精度不得携带位置")
        if self.page_rotation not in (0, 90, 180, 270):
            raise ValueError("页面旋转必须是 90 的倍数")
        return self


class SourceSegmentDraft(BaseModel):
    model_config = _Frozen

    key: str
    kind: SegmentKind
    # 片段短文本；只在内存与受控存储中流转，不进日志
    text: str = Field(repr=False)
    reading_order: int = Field(ge=0)
    block_key: str | None = None
    table_cell: TableCellRef | None = None
    anchor: SourceAnchorDraft


class ParsedBlock(BaseModel):
    model_config = _Frozen

    key: str
    kind: BlockKind
    page_no: int | None = Field(default=None, ge=1)
    text: str = Field(repr=False)
    text_level: int | None = None
    reading_order: int = Field(ge=0)
    anchor: SourceAnchorDraft
    table_key: str | None = None
    segment_keys: tuple[str, ...] = ()


class ParsedTableCell(BaseModel):
    model_config = _Frozen

    row_index: int = Field(ge=0)
    col_index: int = Field(ge=0)
    row_span: int = Field(default=1, ge=1)
    col_span: int = Field(default=1, ge=1)
    text: str = Field(repr=False)
    segment_key: str | None = None


class ParsedTableRow(BaseModel):
    model_config = _Frozen

    row_index: int = Field(ge=0)
    cells: tuple[ParsedTableCell, ...]


class ParsedTable(BaseModel):
    model_config = _Frozen

    key: str
    page_nos: tuple[int, ...] = ()
    rows: tuple[ParsedTableRow, ...]
    caption: str | None = Field(default=None, repr=False)
    anchor: SourceAnchorDraft
    structure_source: Literal["pdfplumber", "mineru_html", "docx", "fake"]


class QualityFlag(BaseModel):
    model_config = _Frozen

    code: QualityCode
    page_no: int | None = None
    # 只描述问题类型与数量，不得包含原件文字
    detail: str = ""


class PageInfo(BaseModel):
    model_config = _Frozen

    page_no: int = Field(ge=1)
    width_pt: float | None = None
    height_pt: float | None = None
    rotation: int = 0
    route: ParseRoute
    # 页级判定依据，便于复核「为何这页走了 OCR」
    char_count: int | None = None
    garbled_ratio: float | None = None
    image_area_ratio: float | None = None
    needs_ocr: bool = False


class EmbeddedImageDraft(BaseModel):
    """DOCX 嵌图：本身不含可引用文字，交由后续 OCR 步骤按需处理。"""

    model_config = _Frozen

    key: str
    content_type: str
    sha256: str
    anchor: SourceAnchorDraft
    reading_order: int = Field(default=0,ge=0)
    data: bytes = Field(repr=False, exclude=True)


class ParseOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    parser: str
    parser_version: str
    # 解析选项的非秘密摘要；与 parser_version 一起决定 ParseArtifact 是否可复用
    config_fingerprint: str
    # 供应商异步任务 ID（如 MinerU batch_id），须在拿到时就持久化
    provider_task_id: str | None = None
    is_fake: bool = False
    page_count: int | None = None
    pages: tuple[PageInfo, ...] = ()
    blocks: tuple[ParsedBlock, ...] = ()
    tables: tuple[ParsedTable, ...] = ()
    segments: tuple[SourceSegmentDraft, ...] = ()
    embedded_images: tuple[EmbeddedImageDraft, ...] = ()
    quality_flags: tuple[QualityFlag, ...] = ()
    # 阅读副本，只作为视图，不是事实层
    markdown: str | None = Field(default=None, repr=False)
    # 供应商原始响应，由上层受控写入对象存储；不随 DTO 序列化
    raw_payload: bytes | None = Field(default=None, repr=False, exclude=True)
    preprocessing_manifest: dict | None = None

    @model_validator(mode="after")
    def _referential_integrity(self) -> ParseOutcome:
        seg_keys = [s.key for s in self.segments]
        if len(set(seg_keys)) != len(seg_keys):
            raise ValueError("片段 key 重复")
        known = set(seg_keys)
        table_keys = {t.key for t in self.tables}
        if len(table_keys) != len(self.tables):
            raise ValueError("表格 key 重复")
        for b in self.blocks:
            if any(k not in known for k in b.segment_keys):
                raise ValueError(f"块 {b.key} 引用了不存在的片段")
            if b.table_key is not None and b.table_key not in table_keys:
                raise ValueError(f"块 {b.key} 引用了不存在的表格")
        for t in self.tables:
            for row in t.rows:
                for cell in row.cells:
                    if cell.segment_key is not None and cell.segment_key not in known:
                        raise ValueError(f"表格 {t.key} 引用了不存在的片段")
        for s in self.segments:
            if s.table_cell is not None and s.table_cell.table_key not in table_keys:
                raise ValueError(f"片段 {s.key} 引用了不存在的表格")
        if self.page_count is not None:
            for s in self.segments:
                if s.anchor.page_no is not None and s.anchor.page_no > self.page_count:
                    raise ValueError(f"片段 {s.key} 的页码超出页数")
        return self


class ParserUnavailable(RuntimeError):
    """所需解析能力未配置或未验证。调用方应显示禁用原因，不能以空结果冒充成功。"""


class DocumentRejected(ValueError):
    """原件因安全原因拒绝解析（如含宏、结构异常的压缩包）。`code` 是稳定分流依据。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
