"""
原生 PDF 路线：pdfplumber 取文字与表格，pypdfium2 渲染页与局部裁图（单证工作流方案 5.1）。

逐页判定是否需要 OCR，而不是整份文件一刀切：混合 PDF 常见「首页电子版 + 后附扫描件」，
只看第一页有文字层就放弃后续扫描页，会让后面的提单或证书整页漏识别。
"""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass

import pdfplumber
import pypdfium2 as pdfium
from app.documents.parsing.dto import PREPROCESSING_VERSION

from app.documents.parsing.dto import (
    NormRect,
    PageInfo,
    ParsedBlock,
    ParsedTable,
    ParsedTableCell,
    ParsedTableRow,
    ParseOutcome,
    QualityFlag,
    SourceAnchorDraft,
    SourceSegmentDraft,
    TableCellRef,
)

NATIVE_PARSER_VERSION = f"pdfplumber-{pdfplumber.__version__}/3"

# 页级判定阈值。依据：物流单证电子版即便很稀疏（只有抬头与几行字段）也远超 20 个可见字符；
# 整页扫描件通常是一张覆盖大半页的图片，即便带隐藏 OCR 文字层也应重新走 OCR 取得坐标与表格。
# 这些值需在获授权样本上复核后签收，改动须提升 NATIVE_PARSER_VERSION。
MIN_NATIVE_CHARS = 20
MAX_GARBLED_RATIO = 0.10
SCANNED_IMAGE_AREA_RATIO = 0.5


@dataclass(frozen=True)
class PageInspection:
    page_no: int
    width_pt: float
    height_pt: float
    rotation: int
    char_count: int
    garbled_ratio: float
    image_area_ratio: float

    @property
    def needs_ocr(self) -> bool:
        if self.char_count < MIN_NATIVE_CHARS:
            return True
        if self.garbled_ratio > MAX_GARBLED_RATIO:
            return True
        return self.image_area_ratio >= SCANNED_IMAGE_AREA_RATIO

    def to_page_info(self, route: str) -> PageInfo:
        return PageInfo(
            page_no=self.page_no,
            width_pt=self.width_pt,
            height_pt=self.height_pt,
            rotation=self.rotation,
            route=route,  # type: ignore[arg-type]
            char_count=self.char_count,
            garbled_ratio=round(self.garbled_ratio, 4),
            image_area_ratio=round(self.image_area_ratio, 4),
            needs_ocr=self.needs_ocr,
        )


def _is_garbled(ch: str) -> bool:
    # 无 ToUnicode 映射的字体会被 pdfminer 输出为 "(cid:123)" 或替换字符；这类文字层不可信
    return ch == "�" or ch.startswith("(cid:")


def _overlap_area(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return max(0.0, w) * max(0.0, h)


@dataclass(frozen=True)
class _PageFrame:
    """单页坐标系：以 CropBox 为基准归一化，与 pypdfium2 渲染出的原图一致。"""

    page_no: int
    rotation: int
    box: tuple[float, ...]

    def anchor(self, box: tuple[float, float, float, float] | None, cell: TableCellRef | None = None) -> SourceAnchorDraft:
        rect = None
        if box is not None:
            x_off, y_off = self.box[0], self.box[1]
            width, height = self.box[2] - self.box[0], self.box[3] - self.box[1]
            x0 = max(0.0, (box[0] - x_off) / width)
            y0 = max(0.0, (box[1] - y_off) / height)
            x1 = min(1.0, (box[2] - x_off) / width)
            y1 = min(1.0, (box[3] - y_off) / height)
            if x0 < x1 and y0 < y1:
                rect = NormRect(x0=x0, y0=y0, x1=x1, y1=y1)
        if rect is not None:
            return SourceAnchorDraft(
                precision="rect",
                page_no=self.page_no,
                rect=rect,
                coordinate_space="page_normalized_top_left",
                table_cell=cell,
                page_rotation=self.rotation,
                preprocessing_transform={"version":PREPROCESSING_VERSION,"recognition_to_source_display":[1,0,0,0,1,0],"source_rotation":self.rotation},
                locator_version=NATIVE_PARSER_VERSION,
            )
        if cell is not None:
            return SourceAnchorDraft(
                precision="cell",
                page_no=self.page_no,
                page_rotation=self.rotation,
                table_cell=cell,
                locator_version=NATIVE_PARSER_VERSION,
            )
        return SourceAnchorDraft(
            precision="page_only", page_no=self.page_no, page_rotation=self.rotation, locator_version=NATIVE_PARSER_VERSION
        )


class NativePdfAdapter:
    parser_name = "native_pdf"

    def inspect_pages(self, data: bytes) -> list[PageInspection]:
        result: list[PageInspection] = []
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages:
                # 渲染与预览显示的是 CropBox，坐标与面积都以它为准才能回到原图
                bbox = tuple(float(v) for v in page.cropbox)
                area = max(1e-6, (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]))
                chars = [c for c in page.chars if not c.get("text", "").isspace()]
                garbled = sum(1 for c in chars if _is_garbled(c.get("text", "")))
                image_area = 0.0
                for img in page.images:
                    box = (float(img["x0"]), float(img["top"]), float(img["x1"]), float(img["bottom"]))
                    image_area += _overlap_area(box, bbox)
                result.append(
                    PageInspection(
                        page_no=page.page_number,
                        width_pt=bbox[2] - bbox[0],
                        height_pt=bbox[3] - bbox[1],
                        rotation=int(page.rotation or 0) % 360,
                        char_count=len(chars) - garbled,
                        garbled_ratio=(garbled / len(chars)) if chars else 0.0,
                        image_area_ratio=min(1.0, image_area / area),
                    )
                )
        return result

    def config_fingerprint(self) -> str:
        payload = json.dumps(
            {
                "version": NATIVE_PARSER_VERSION,
                "min_chars": MIN_NATIVE_CHARS,
                "garbled": MAX_GARBLED_RATIO,
                "scan_area": SCANNED_IMAGE_AREA_RATIO,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def parse(self, data: bytes, *, pages: list[int] | None = None, key_prefix: str = "n") -> ParseOutcome:
        """
        解析指定页（1 起）；缺省为全部页。仍需 OCR 的页只登记 `page_needs_ocr`，
        不输出其残缺文字层，避免同一页出现两套互相矛盾的片段。
        """
        inspections = {p.page_no: p for p in self.inspect_pages(data)}
        wanted = sorted(pages) if pages is not None else sorted(inspections)
        blocks: list[ParsedBlock] = []
        tables: list[ParsedTable] = []
        segments: list[SourceSegmentDraft] = []
        flags: list[QualityFlag] = []
        page_infos: list[PageInfo] = []
        order = 0

        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page_no in wanted:
                insp = inspections[page_no]
                if insp.needs_ocr:
                    page_infos.append(insp.to_page_info("ocr"))
                    flags.append(QualityFlag(code="page_needs_ocr", page_no=page_no, detail="文字层不足或为扫描页"))
                    continue
                page_infos.append(insp.to_page_info("native_pdf"))
                page = pdf.pages[page_no - 1]
                # pdfplumber已按Rotate转为显示坐标；超出CropBox的内容仍降级。
                frame = _PageFrame(page_no=page_no, rotation=insp.rotation, box=tuple(float(v) for v in page.cropbox))
                anchor = frame.anchor

                table_boxes: list[tuple[float, float, float, float]] = []
                for found in page.find_tables():
                    table_key = f"{key_prefix}t{len(tables)}"
                    block_key = f"{key_prefix}b{len(blocks)}"
                    box = tuple(float(v) for v in found.bbox)
                    table_boxes.append(box)  # type: ignore[arg-type]
                    matrix = found.extract()
                    rows: list[ParsedTableRow] = []
                    seg_keys: list[str] = []
                    unresolved = False
                    x_edges = sorted({float(c[i]) for c in found.cells for i in (0, 2)})
                    y_edges = sorted({float(c[i]) for c in found.cells for i in (1, 3)})
                    for r_index, row in enumerate(found.rows):
                        cells: list[ParsedTableCell] = []
                        for c_index, cell_box in enumerate(row.cells):
                            if cell_box is None:
                                # pdfplumber 对合并区域给 None，无法确定属于哪个主单元格
                                continue
                            text = (matrix[r_index][c_index] or "").strip() if r_index < len(matrix) else ""
                            ref = TableCellRef(table_key=table_key, row_index=r_index, col_index=c_index)
                            seg_key = None
                            if text:
                                seg_key = f"{key_prefix}s{len(segments)}"
                                segments.append(
                                    SourceSegmentDraft(
                                        key=seg_key,
                                        kind="table_cell",
                                        text=text,
                                        reading_order=order,
                                        block_key=block_key,
                                        table_cell=ref,
                                        anchor=anchor(tuple(float(v) for v in cell_box), ref),  # type: ignore[arg-type]
                                    )
                                )
                                order += 1
                                seg_keys.append(seg_key)
                            cells.append(
                                ParsedTableCell(row_index=r_index, col_index=c_index, row_span=max(1, y_edges.index(float(cell_box[3]))-y_edges.index(float(cell_box[1]))), col_span=max(1, x_edges.index(float(cell_box[2]))-x_edges.index(float(cell_box[0]))), text=text, segment_key=seg_key)
                            )
                        rows.append(ParsedTableRow(row_index=r_index, cells=tuple(cells)))
                    if unresolved:
                        flags.append(
                            QualityFlag(code="table_merged_cells_unresolved", page_no=page_no, detail="表格含合并单元格")
                        )
                    table_anchor = anchor(box)  # type: ignore[arg-type]
                    tables.append(
                        ParsedTable(
                            key=table_key,
                            page_nos=(page_no,),
                            rows=tuple(rows),
                            anchor=table_anchor,
                            structure_source="pdfplumber",
                        )
                    )
                    blocks.append(
                        ParsedBlock(
                            key=block_key,
                            kind="table",
                            page_no=page_no,
                            text="",
                            reading_order=len(blocks),
                            anchor=table_anchor,
                            table_key=table_key,
                            segment_keys=tuple(seg_keys),
                        )
                    )

                for line in page.extract_text_lines(return_chars=False):
                    text = (line.get("text") or "").strip()
                    if not text:
                        continue
                    box = (float(line["x0"]), float(line["top"]), float(line["x1"]), float(line["bottom"]))
                    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
                    # 表格内的文字已作为单元格片段输出，不重复出一份行片段
                    if any(tb[0] <= cx <= tb[2] and tb[1] <= cy <= tb[3] for tb in table_boxes):
                        continue
                    block_key = f"{key_prefix}b{len(blocks)}"
                    seg_key = f"{key_prefix}s{len(segments)}"
                    a = anchor(box)
                    segments.append(
                        SourceSegmentDraft(
                            key=seg_key, kind="paragraph", text=text, reading_order=order, block_key=block_key, anchor=a
                        )
                    )
                    order += 1
                    blocks.append(
                        ParsedBlock(
                            key=block_key,
                            kind="text",
                            page_no=page_no,
                            text=text,
                            reading_order=len(blocks),
                            anchor=a,
                            segment_keys=(seg_key,),
                        )
                    )

                for line in page.lines:
                    if abs(float(line.get("y0",0))-float(line.get("y1",0))) > 1: continue
                    x0,x1=float(line["x0"]),float(line["x1"]);top=float(line["top"])
                    if any(tb[0]-1<=x0<=x1<=tb[2]+1 and tb[1]-1<=top<=tb[3]+1 for tb in table_boxes):continue
                    if x1-x0<20:continue
                    blocks.append(ParsedBlock(key=f"{key_prefix}line{len(blocks)}",kind="separator",page_no=page_no,text="",reading_order=len(blocks),anchor=anchor((x0,top-.5,x1,top+.5)),segment_keys=()))
                if any(s.anchor.page_no==page_no and s.anchor.precision=="page_only" for s in segments):
                    flags.append(QualityFlag(code="bbox_uncalibrated",page_no=page_no,detail="内容坐标未落在原件显示区域，降为页级定位"))

        # 按实际页面块位置排序；表格作为完整块，其内部保持原行列序。
        # 标题应先于下面的表格，不能因提取表格在先而制造“未知单据”。
        def block_position(block):
            rect = block.anchor.rect
            return (block.page_no, rect.y0 if rect else 1.0, rect.x0 if rect else 1.0, block.reading_order)

        blocks = [block.model_copy(update={"reading_order": index}) for index, block in enumerate(sorted(blocks, key=block_position))]
        positions = {key: index for index, block in enumerate(blocks) for key in block.segment_keys}
        segments = [segment.model_copy(update={"reading_order": index}) for index, segment in enumerate(
            sorted(segments, key=lambda segment: (positions.get(segment.key, len(blocks)), segment.reading_order)))]

        return ParseOutcome(
            parser=self.parser_name,
            parser_version=NATIVE_PARSER_VERSION,
            config_fingerprint=self.config_fingerprint(),
            page_count=len(inspections),
            pages=tuple(page_infos),
            blocks=tuple(blocks),
            tables=tuple(tables),
            segments=tuple(segments),
            quality_flags=tuple(flags),
        )


# ---- 渲染 ---------------------------------------------------------------------


def page_rotations(data: bytes) -> dict[int, int]:
    """原件各页的 /Rotate，供 OCR 归一化判断坐标是否可校准。"""
    doc = pdfium.PdfDocument(data)
    try:
        return {i + 1: int(doc[i].get_rotation()) % 360 for i in range(len(doc))}
    finally:
        doc.close()


def render_page_png(data: bytes, page_no: int, *, scale: float = 2.0) -> bytes:
    doc = pdfium.PdfDocument(data)
    page = bitmap = image = None
    try:
        if not 1 <= page_no <= len(doc):
            raise ValueError("页码超出原件范围")
        page = doc[page_no - 1]
        bitmap = page.render(scale=scale)
        image = bitmap.to_pil()
        out = io.BytesIO()
        image.save(out, format="PNG")
        return out.getvalue()
    finally:
        if image is not None:
            image.close()
        if bitmap is not None:
            bitmap.close()
        if page is not None:
            page.close()
        doc.close()


def crop_rect_png(data: bytes, page_no: int, rect: NormRect, *, scale: float = 3.0) -> bytes:
    """
    按已校准矩形从原件渲染局部裁图，供 VISION 第二转写。只接受 rect 精度的锚点坐标：
    裁图必须是原图上可证明的区域，不能凭页级定位去猜一块区域送模型。
    """
    doc = pdfium.PdfDocument(data)
    page = bitmap = image = cropped = None
    try:
        if not 1 <= page_no <= len(doc):
            raise ValueError("页码超出原件范围")
        page = doc[page_no - 1]
        bitmap = page.render(scale=scale)
        image = bitmap.to_pil()
        w, h = image.size
        box = (int(rect.x0 * w), int(rect.y0 * h), max(int(rect.x1 * w), int(rect.x0 * w) + 1), max(int(rect.y1 * h), int(rect.y0 * h) + 1))
        out = io.BytesIO()
        cropped = image.crop(box)
        cropped.save(out, format="PNG")
        return out.getvalue()
    finally:
        if cropped is not None:
            cropped.close()
        if image is not None:
            image.close()
        if bitmap is not None:
            bitmap.close()
        if page is not None:
            page.close()
        doc.close()


def extract_pages_pdf(data: bytes, page_nos: list[int]) -> bytes:
    """
    把需要 OCR 的页抽成子 PDF 送 MinerU：只外发这些页（数据最小化），也不为已有可靠文字层的页付费。
    返回文件第 i 页对应原件 `page_nos[i]`，归一化时以此作为 page_map。
    """
    src = pdfium.PdfDocument(data)
    dst = pdfium.PdfDocument.new()
    try:
        dst.import_pages(src, [p - 1 for p in page_nos])
        out = io.BytesIO()
        dst.save(out)
        return out.getvalue()
    finally:
        dst.close()
        src.close()
