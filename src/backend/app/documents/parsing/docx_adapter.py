"""
DOCX 路线：先受控解包检查容器，再用 python-docx 取正文、表格、页眉页脚与嵌图（单证工作流方案 5.1）。

安全边界：
- 含宏（vbaProject、macroEnabled 主类型）或 ActiveX 的文件直接拒绝：上传白名单只收 DOCX，
  带宏说明它实际是 DOCM 或被篡改的容器，不做「剥掉宏继续解析」的猜测性修复。
- 外部关系（链接模板、外链图片、OLE 链接、子文档）一律不取回、不执行，只记 quality_flags；
  超链接仅作为段落文字保留。
- XML 解析沿用 python-docx 的 `resolve_entities=False` 解析器，不展开外部实体。
- 文本框、绘图区无法稳定还原阅读顺序，不猜测位置拼进正文，而是记为未覆盖交人工复核。
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass, field

import docx
from docx.oxml.ns import nsmap as _DOCX_NSMAP, qn
from docx.text.paragraph import Paragraph
from lxml import etree

from app.documents.parsing.dto import (
    DocumentRejected,
    DocxLocation,
    EmbeddedImageDraft,
    ParsedBlock,
    ParsedTable,
    ParsedTableCell,
    ParsedTableRow,
    ParseOutcome,
    QualityFlag,
    SegmentKind,
    SourceAnchorDraft,
    SourceSegmentDraft,
    TableCellRef,
)

DOCX_PARSER_VERSION = f"python-docx-{docx.__version__}/2"

MAX_DOCX_ENTRIES = 2000
MAX_DOCX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
# 正常 Office XML 压缩比多在 10 倍以内；远超此值的成员按压缩炸弹处理
MAX_COMPRESSION_RATIO = 200

_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_HYPERLINK_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"
_ALT_CHUNK_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/aFChunk"
_SAFE_XML = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
# python-docx 的命名空间表不含 mc；文本框常包在 mc:AlternateContent 里，需要显式补上
_XPATH_NS = {**_DOCX_NSMAP, "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006"}


def _xpath(el: etree._Element, expr: str) -> list:
    return etree._Element.xpath(el, expr, namespaces=_XPATH_NS)


@dataclass
class _PackageReport:
    external_refs: int = 0
    alt_chunks: int = 0
    notes: int = 0


def _inspect_package(data: bytes) -> _PackageReport:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise DocumentRejected("docx_invalid_container", "文件不是有效的 DOCX 容器") from None
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_DOCX_ENTRIES:
            raise DocumentRejected("docx_invalid_container", "DOCX 容器条目数异常")
        total = 0
        for info in infos:
            if info.flag_bits & 0x1:
                raise DocumentRejected("docx_encrypted", "DOCX 含加密成员，无法安全解析")
            total += info.file_size
            if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
                raise DocumentRejected("docx_invalid_container", "DOCX 成员压缩比异常")
        if total > MAX_DOCX_UNCOMPRESSED_BYTES:
            raise DocumentRejected("docx_invalid_container", "DOCX 解包后体积超限")

        names = {i.filename for i in infos}
        lowered = {n.lower() for n in names}
        if any(n.endswith("vbaproject.bin") or n.endswith("vbadata.xml") for n in lowered):
            raise DocumentRejected("docx_macro", "文件含宏，已拒绝解析")
        if any(n.startswith("word/activex/") for n in lowered):
            raise DocumentRejected("docx_active_content", "文件含 ActiveX 控件，已拒绝解析")
        if "[Content_Types].xml" not in names:
            raise DocumentRejected("docx_invalid_container", "DOCX 缺少内容类型清单")
        try:
            ct_root = etree.fromstring(archive.read("[Content_Types].xml"), _SAFE_XML)
        except etree.XMLSyntaxError:
            raise DocumentRejected("docx_invalid_container", "DOCX 内容类型清单无法解析") from None
        for override in ct_root.iter(f"{{{_CT_NS}}}Override", f"{{{_CT_NS}}}Default"):
            ctype = (override.get("ContentType") or "").lower()
            if "macroenabled" in ctype or "vbaproject" in ctype:
                raise DocumentRejected("docx_macro", "文件为启用宏的文档类型，已拒绝解析")

        report = _PackageReport()
        for name in names:
            if not name.endswith(".rels"):
                continue
            try:
                rels = etree.fromstring(archive.read(name), _SAFE_XML)
            except etree.XMLSyntaxError:
                raise DocumentRejected("docx_invalid_container", "DOCX 关系清单无法解析") from None
            for rel in rels.iter(f"{{{_REL_NS}}}Relationship"):
                rtype = rel.get("Type") or ""
                if rtype == _ALT_CHUNK_TYPE:
                    report.alt_chunks += 1
                if (rel.get("TargetMode") or "").lower() == "external" and rtype != _HYPERLINK_TYPE:
                    report.external_refs += 1

        for part, tag in (("word/footnotes.xml", "footnote"), ("word/endnotes.xml", "endnote"), ("word/comments.xml", "comment")):
            if part not in names:
                continue
            try:
                root = etree.fromstring(archive.read(part), _SAFE_XML)
            except etree.XMLSyntaxError:
                continue
            for el in root.iter(qn(f"w:{tag}")):
                # 分隔符脚注（带 w:type）是版式占位，不是内容
                if el.get(qn("w:type")) is None:
                    report.notes += 1
        return report


@dataclass
class _CellBuild:
    row_index: int
    col_index: int
    col_span: int
    text: str
    row_span: int = 1


@dataclass
class _Ctx:
    key_prefix: str
    blocks: list[ParsedBlock] = field(default_factory=list)
    tables: list[ParsedTable] = field(default_factory=list)
    segments: list[SourceSegmentDraft] = field(default_factory=list)
    images: list[EmbeddedImageDraft] = field(default_factory=list)
    flags: dict[str, int] = field(default_factory=dict)
    order: int = 0

    def flag(self, code: str, n: int = 1) -> None:
        self.flags[code] = self.flags.get(code, 0) + n


class DocxAdapter:
    parser_name = "docx"

    def config_fingerprint(self) -> str:
        payload = json.dumps({"version": DOCX_PARSER_VERSION, "ratio": MAX_COMPRESSION_RATIO}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    def parse(self, data: bytes, *, key_prefix: str = "d") -> ParseOutcome:
        report = _inspect_package(data)
        try:
            document = docx.Document(io.BytesIO(data))
        except Exception:
            # python-docx 对非 Word 主类型抛 ValueError，对结构损坏抛各类异常；统一视为拒绝
            raise DocumentRejected("docx_invalid_container", "DOCX 结构无法解析") from None

        ctx = _Ctx(key_prefix=key_prefix)
        self._walk(document.element.body, document.part, ctx, DocxLocation(part="body"))

        seen_parts: set[int] = set()
        for s_index, section in enumerate(document.sections):
            for kind, hf, part_name in (
                ("default", section.header, "header"),
                ("first", section.first_page_header, "header"),
                ("even", section.even_page_header, "header"),
                ("default", section.footer, "footer"),
                ("first", section.first_page_footer, "footer"),
                ("even", section.even_page_footer, "footer"),
            ):
                # 链接到上一节的页眉页脚内容已在前一节取过；python-docx 对未定义的也返回链接态
                if hf.is_linked_to_previous:
                    continue
                part = hf.part
                if id(part) in seen_parts:
                    continue
                seen_parts.add(id(part))
                loc = DocxLocation(part=part_name, section_index=s_index, header_kind=kind)  # type: ignore[arg-type]
                self._walk(hf._element, part, ctx, loc)

        flags: list[QualityFlag] = []
        detail = {
            "docx_textbox_not_extracted": "文本框内容未提取，需人工核对",
            "docx_drawing_not_extracted": "绘图/图形区域未提取",
            "docx_embedded_image_not_ocr": "嵌入图片中的文字尚未识别",
            "docx_nested_table_flattened": "嵌套表格已展平为外层单元格文字",
        }
        for code, count in ctx.flags.items():
            flags.append(QualityFlag(code=code, detail=f"{detail.get(code, '')}（{count} 处）"))  # type: ignore[arg-type]
        if report.external_refs:
            flags.append(
                QualityFlag(code="docx_external_reference_ignored", detail=f"外部引用未取回（{report.external_refs} 处）")
            )
        if report.alt_chunks:
            flags.append(QualityFlag(code="docx_alt_chunk_not_extracted", detail=f"嵌入的外部格式内容未提取（{report.alt_chunks} 处）"))
        if report.notes:
            flags.append(QualityFlag(code="docx_notes_not_extracted", detail=f"脚注/尾注/批注未提取（{report.notes} 处）"))

        return ParseOutcome(
            parser=self.parser_name,
            parser_version=DOCX_PARSER_VERSION,
            config_fingerprint=self.config_fingerprint(),
            page_count=None,
            blocks=tuple(ctx.blocks),
            tables=tuple(ctx.tables),
            segments=tuple(ctx.segments),
            embedded_images=tuple(ctx.images),
            quality_flags=tuple(flags),
        )

    # ---- 遍历 ----

    def _walk(self, container: etree._Element, part, ctx: _Ctx, base: DocxLocation) -> None:
        counters = {"p": 0, "tbl": 0}
        self._walk_children(container, part, ctx, base, counters)

    def _walk_children(self, container, part, ctx: _Ctx, base: DocxLocation, counters: dict[str, int]) -> None:
        for child in container.iterchildren():
            tag = child.tag
            if tag == qn("w:p"):
                self._paragraph(child, part, ctx, base.model_copy(update={"paragraph_index": counters["p"]}))
                counters["p"] += 1
            elif tag == qn("w:tbl"):
                self._table(child, part, ctx, base.model_copy(update={"table_index": counters["tbl"]}))
                counters["tbl"] += 1
            elif tag == qn("w:sdt"):
                # 内容控件内的段落/表格按原顺序计入同一编号序列
                content = child.find(qn("w:sdtContent"))
                if content is not None:
                    self._walk_children(content, part, ctx, base, counters)
            elif tag == qn("w:altChunk"):
                # 计数已在包检查中按关系统计，这里不重复登记
                continue

    def _scan_inline_objects(self, el, part, ctx: _Ctx, anchor: SourceAnchorDraft) -> None:
        textboxes = _xpath(el, ".//w:txbxContent")
        if textboxes:
            # mc:AlternateContent 的 Choice 与 Fallback 各有一份同样的文本框，只按 Choice 计数避免翻倍
            in_choice = _xpath(el, ".//mc:Choice//w:txbxContent")
            ctx.flag("docx_textbox_not_extracted", len(in_choice) or len(textboxes))
        blips = _xpath(el, ".//a:blip")
        seen: set[str] = set()
        for blip in blips:
            rid = blip.get(qn("r:embed"))
            if not rid or rid in seen:
                continue
            seen.add(rid)
            try:
                image_part = part.related_parts[rid]
            except KeyError:
                continue
            blob = image_part.blob
            ctx.images.append(
                EmbeddedImageDraft(
                    key=f"{ctx.key_prefix}i{len(ctx.images)}",
                    content_type=image_part.content_type,
                    sha256=hashlib.sha256(blob).hexdigest(),
                    anchor=anchor,
                    reading_order=ctx.order,
                    data=blob,
                )
            )
            ctx.flag("docx_embedded_image_not_ocr")
        drawings = _xpath(el, ".//w:drawing | .//w:pict | .//w:object")
        if drawings and not blips and not textboxes:
            ctx.flag("docx_drawing_not_extracted", len(drawings))

    def _paragraph(self, p_el, part, ctx: _Ctx, loc: DocxLocation) -> None:
        paragraph = Paragraph(p_el, None)
        text = paragraph.text.strip()
        anchor = SourceAnchorDraft(precision="paragraph_only", docx=loc, locator_version=DOCX_PARSER_VERSION)
        self._scan_inline_objects(p_el, part, ctx, anchor)
        style_id = ""
        ppr = p_el.pPr
        if ppr is not None and ppr.pStyle is not None:
            style_id = (ppr.pStyle.val or "").lower()
        is_title = style_id.startswith("heading") or style_id == "title"
        is_list = ppr is not None and ppr.numPr is not None
        if loc.part == "header":
            seg_kind: SegmentKind = "header"
            block_kind = "header"
        elif loc.part == "footer":
            seg_kind, block_kind = "footer", "footer"
        elif is_title:
            seg_kind, block_kind = "title", "title"
        elif is_list:
            seg_kind, block_kind = "list_item", "list"
        else:
            seg_kind, block_kind = "paragraph", "text"
        page_before = bool(_xpath(p_el, "./w:pPr/w:pageBreakBefore"))
        page_after = bool(_xpath(p_el, ".//w:br[@w:type='page'] | .//w:lastRenderedPageBreak"))
        separator = bool(_xpath(p_el, "./w:pPr/w:pBdr/w:bottom"))
        def structure(kind):
            ctx.blocks.append(ParsedBlock(key=f"{ctx.key_prefix}b{len(ctx.blocks)}", kind=kind, text="", reading_order=len(ctx.blocks), anchor=anchor))
        if page_before: structure("page_break")
        if not text:
            if separator: structure("separator")
            if page_after: structure("page_break")
            ctx.order += 1
            return
        block_key = f"{ctx.key_prefix}b{len(ctx.blocks)}"
        seg_key = f"{ctx.key_prefix}s{len(ctx.segments)}"
        ctx.segments.append(
            SourceSegmentDraft(
                key=seg_key, kind=seg_kind, text=text, reading_order=ctx.order, block_key=block_key, anchor=anchor
            )
        )
        ctx.order += 1
        ctx.blocks.append(
            ParsedBlock(
                key=block_key,
                kind=block_kind,  # type: ignore[arg-type]
                text=text,
                reading_order=len(ctx.blocks),
                anchor=anchor,
                segment_keys=(seg_key,),
            )
        )

        if separator: structure("separator")
        if page_after: structure("page_break")

    def _cell_text(self, tc, part, ctx: _Ctx, anchor: SourceAnchorDraft) -> str:
        parts: list[str] = []
        for child in tc.iterchildren():
            if child.tag == qn("w:p"):
                self._scan_inline_objects(child, part, ctx, anchor)
                t = Paragraph(child, None).text.strip()
                if t:
                    parts.append(t)
            elif child.tag == qn("w:tbl"):
                ctx.flag("docx_nested_table_flattened")
                for p in child.iter(qn("w:p")):
                    t = Paragraph(p, None).text.strip()
                    if t:
                        parts.append(t)
        return "\n".join(parts)

    def _table(self, tbl, part, ctx: _Ctx, loc: DocxLocation) -> None:
        table_key = f"{ctx.key_prefix}t{len(ctx.tables)}"
        block_key = f"{ctx.key_prefix}b{len(ctx.blocks)}"
        table_anchor = SourceAnchorDraft(precision="paragraph_only", docx=loc, locator_version=DOCX_PARSER_VERSION)
        built: list[_CellBuild] = []
        # 列号 → 仍在纵向合并中的主单元格
        open_vmerge: dict[int, _CellBuild] = {}
        for r_index, tr in enumerate(tbl.tr_lst):
            col = 0
            trpr = tr.trPr
            if trpr is not None:
                before = trpr.find(qn("w:gridBefore"))
                if before is not None and (val := before.get(qn("w:val"))) and val.isdigit():
                    col = int(val)
            for tc in tr.tc_lst:
                span = tc.grid_span
                vmerge = tc.vMerge
                if vmerge == "continue" and col in open_vmerge:
                    open_vmerge[col].row_span += 1
                    col += span
                    continue
                ref = TableCellRef(table_key=table_key, row_index=r_index, col_index=col)
                cell_anchor = SourceAnchorDraft(
                    precision="cell", docx=loc, table_cell=ref, locator_version=DOCX_PARSER_VERSION
                )
                cell = _CellBuild(row_index=r_index, col_index=col, col_span=span, text=self._cell_text(tc, part, ctx, cell_anchor))
                built.append(cell)
                if vmerge == "restart":
                    open_vmerge[col] = cell
                else:
                    open_vmerge.pop(col, None)
                col += span

        rows: dict[int, list[ParsedTableCell]] = {}
        seg_keys: list[str] = []
        for cell in built:
            seg_key = None
            ref = TableCellRef(table_key=table_key, row_index=cell.row_index, col_index=cell.col_index)
            for index,image in enumerate(ctx.images):
                if image.anchor.table_cell == ref:
                    ctx.images[index]=image.model_copy(update={"reading_order":ctx.order})
            if cell.text:
                seg_key = f"{ctx.key_prefix}s{len(ctx.segments)}"
                ctx.segments.append(
                    SourceSegmentDraft(
                        key=seg_key,
                        kind="table_cell",
                        text=cell.text,
                        reading_order=ctx.order,
                        block_key=block_key,
                        table_cell=ref,
                        anchor=SourceAnchorDraft(
                            precision="cell", docx=loc, table_cell=ref, locator_version=DOCX_PARSER_VERSION
                        ),
                    )
                )
                seg_keys.append(seg_key)
            ctx.order += 1
            rows.setdefault(cell.row_index, []).append(
                ParsedTableCell(
                    row_index=cell.row_index,
                    col_index=cell.col_index,
                    row_span=cell.row_span,
                    col_span=cell.col_span,
                    text=cell.text,
                    segment_key=seg_key,
                )
            )
        ctx.tables.append(
            ParsedTable(
                key=table_key,
                rows=tuple(ParsedTableRow(row_index=r, cells=tuple(cs)) for r, cs in sorted(rows.items())),
                anchor=table_anchor,
                structure_source="docx",
            )
        )
        ctx.blocks.append(
            ParsedBlock(
                key=block_key,
                kind="table",
                text="",
                reading_order=len(ctx.blocks),
                anchor=table_anchor,
                table_key=table_key,
                segment_keys=tuple(seg_keys),
            )
        )
