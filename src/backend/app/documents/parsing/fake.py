"""
确定性假解析器：没有 MinerU Key 时替代 OCR 路线，让上传→解析→抽取→翻译的工程链路可联调。

它只替代「外部 OCR」这一环：原生 PDF 文字层与 DOCX 仍走真实的本地解析。产出结构完整，
但所有文字带显著标记、`is_fake=True`，锚点一律 `none`——假内容在原件上没有位置，
给它画框或标页码都等于伪造证据。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from app.documents.parsing.dto import (
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

FAKE_PARSER_VERSION = "deterministic_fake_parser/1"
FAKE_TEXT_MARKER = "【假解析结果·非真实识别】"


class DeterministicFakeParser:
    parser_name = "deterministic_fake"

    def parse(
        self,
        data: bytes,
        *,
        page_nos: Sequence[int] = (1,),
        page_count: int | None = None,
        key_prefix: str = "f",
    ) -> ParseOutcome:
        digest = hashlib.sha256(data).hexdigest()[:16]
        none_anchor = SourceAnchorDraft(precision="none", locator_version=FAKE_PARSER_VERSION)
        blocks: list[ParsedBlock] = []
        tables: list[ParsedTable] = []
        segments: list[SourceSegmentDraft] = []
        pages: list[PageInfo] = []

        def add_text(kind_block: str, kind_seg: str, text: str) -> None:
            block_key = f"{key_prefix}b{len(blocks)}"
            seg_key = f"{key_prefix}s{len(segments)}"
            segments.append(
                SourceSegmentDraft(
                    key=seg_key,
                    kind=kind_seg,  # type: ignore[arg-type]
                    text=text,
                    reading_order=len(segments),
                    block_key=block_key,
                    anchor=none_anchor,
                )
            )
            blocks.append(
                ParsedBlock(
                    key=block_key,
                    kind=kind_block,  # type: ignore[arg-type]
                    text=text,
                    reading_order=len(blocks),
                    anchor=none_anchor,
                    segment_keys=(seg_key,),
                )
            )

        for page_no in page_nos:
            pages.append(PageInfo(page_no=page_no, route="fake", needs_ocr=True))
            add_text("title", "title", f"{FAKE_TEXT_MARKER} 第 {page_no} 页")
            add_text("text", "paragraph", f"{FAKE_TEXT_MARKER} 原件摘要 {digest}")

            table_key = f"{key_prefix}t{len(tables)}"
            block_key = f"{key_prefix}b{len(blocks)}"
            rows: list[ParsedTableRow] = []
            seg_keys: list[str] = []
            for r in range(2):
                cells: list[ParsedTableCell] = []
                for c in range(2):
                    ref = TableCellRef(table_key=table_key, row_index=r, col_index=c)
                    seg_key = f"{key_prefix}s{len(segments)}"
                    text = f"{FAKE_TEXT_MARKER} R{r}C{c}"
                    segments.append(
                        SourceSegmentDraft(
                            key=seg_key,
                            kind="table_cell",
                            text=text,
                            reading_order=len(segments),
                            block_key=block_key,
                            table_cell=ref,
                            anchor=none_anchor,
                        )
                    )
                    seg_keys.append(seg_key)
                    cells.append(ParsedTableCell(row_index=r, col_index=c, text=text, segment_key=seg_key))
                rows.append(ParsedTableRow(row_index=r, cells=tuple(cells)))
            tables.append(ParsedTable(key=table_key, rows=tuple(rows), anchor=none_anchor, structure_source="fake"))
            blocks.append(
                ParsedBlock(
                    key=block_key,
                    kind="table",
                    text="",
                    reading_order=len(blocks),
                    anchor=none_anchor,
                    table_key=table_key,
                    segment_keys=tuple(seg_keys),
                )
            )

        return ParseOutcome(
            parser=self.parser_name,
            parser_version=FAKE_PARSER_VERSION,
            config_fingerprint=hashlib.sha256(FAKE_PARSER_VERSION.encode()).hexdigest(),
            provider_task_id=f"fake-{digest}",
            is_fake=True,
            page_count=page_count if page_count is not None else (max(page_nos) if page_nos else None),
            pages=tuple(pages),
            blocks=tuple(blocks),
            tables=tuple(tables),
            segments=tuple(segments),
            quality_flags=(QualityFlag(code="fake_parser_output", detail="假解析器产出，不是真实识别结果"),),
            markdown=f"{FAKE_TEXT_MARKER}\n",
        )
