"""同一源布局驱动识别阅读、译文阅读与正式Word/PDF，文字不作为指令。"""
from __future__ import annotations
from html import escape
from uuid import UUID
from sqlalchemy import select
from app.documents import evidence_models as em
from app.review import models as rm

VERSION = "source-layout@1"


def manifest_from_outcome(outcome, ids):
    tables = {table.key: table for table in outcome.tables}
    blocks = []
    for block in sorted(outcome.blocks, key=lambda item: item.reading_order):
        row = {"kind":block.kind, "page":block.page_no, "level":block.text_level, "ids":[ids[key] for key in block.segment_keys if key in ids]}
        if block.table_key and block.table_key in tables:
            table = tables[block.table_key]
            row["kind"] = "table"
            row["cells"] = [{"row":cell.row_index,"col":cell.col_index,"rowspan":cell.row_span,"colspan":cell.col_span,"ids":[ids[cell.segment_key]] if cell.segment_key in ids else []}
                for table_row in table.rows for cell in table_row.cells]
        if row["ids"] or row.get("cells") or row["kind"] in {"separator", "page_break"}: blocks.append(row)
    listed = {sid for block in blocks for sid in block["ids"]}
    for segment in outcome.segments:
        if segment.key in ids and ids[segment.key] not in listed:
            blocks.append({"kind":segment.kind,"page":segment.anchor.page_no,"ids":[ids[segment.key]]})
    return {"version":VERSION,"pages":[p.model_dump(mode="json") for p in outcome.pages],"blocks":blocks}


def document_layout(db, parse, raw=None, mime=None, ocr_raw=None):
    sources = list(db.scalars(select(em.SourceSegment).where(em.SourceSegment.parse_artifact_id==parse.id).order_by(em.SourceSegment.ordinal,em.SourceSegment.id)))
    anchors = list(db.scalars(select(em.SourceAnchor).where(em.SourceAnchor.parse_artifact_id==parse.id)))
    by_id = {a.source_segment_id:a for a in anchors}
    # 旧解析没有布局字段时，本地恢复PDF/DOCX结构，不重新调用OCR。
    if not parse.layout_manifest and raw and mime:
        outcome=None
        if ocr_raw:
            import io, json, zipfile
            from app.documents.parsing.mineru import normalize_content_list
            with zipfile.ZipFile(io.BytesIO(ocr_raw)) as archive:
                content_name = next((name for name in archive.namelist() if name.endswith("_content_list.json")), None)
                layout_name = next((name for name in archive.namelist() if name.rsplit("/", 1)[-1] == "layout.json"), None)
                if content_name:
                    outcome = normalize_content_list(json.loads(archive.read(content_name)), layout=json.loads(archive.read(layout_name)) if layout_name else None, markdown=None, raw_payload=None, provider_task_id=None, backend_version=None, config_fingerprint=parse.parser_config_hash)
        if outcome is None and mime == "application/pdf":
            from app.documents.parsing.native_pdf import NativePdfAdapter
            outcome=NativePdfAdapter().parse(raw)
        elif outcome is None and "word" in mime:
            from app.documents.parsing.docx_adapter import DocxAdapter
            outcome=DocxAdapter().parse(raw)
        if outcome and outcome.segments:
            pool = list(sources); ids={}
            for segment in outcome.segments:
                candidates=[s for s in pool if s.text.strip()==segment.text.strip()]
                if segment.anchor.page_no:
                    candidates.sort(key=lambda s: by_id.get(s.id) is None or by_id[s.id].page_no!=segment.anchor.page_no)
                if candidates: selected=candidates[0];ids[segment.key]=str(selected.id);pool.remove(selected)
            # 未匹配内容仍从授权来源补齐，防止旧OCR与文字层不同丢段。
            if ids:
                rebuilt=manifest_from_outcome(outcome,ids)
                for s in pool: rebuilt["blocks"].append({"kind":s.structure,"page":by_id[s.id].page_no if s.id in by_id else None,"ids":[str(s.id)]})
                rebuilt["blocks"].sort(key=lambda block: block.get("page") or 1)
                return rebuilt,sources
    if parse.layout_manifest: return parse.layout_manifest,sources
    blocks=[]; tables={}
    for s in sources:
        a=by_id.get(s.id); ref=(a.transform or {}).get("table_cell") if a else None
        if ref:
            key=(a.page_no,ref["table_key"])
            if key not in tables:
                block={"kind":"table","page":a.page_no,"ids":[],"cells":[]};tables[key]=block;blocks.append(block)
            tables[key]["ids"].append(str(s.id));tables[key]["cells"].append({"row":ref["row_index"],"col":ref["col_index"],"rowspan":1,"colspan":1,"ids":[str(s.id)]})
        else: blocks.append({"kind":s.structure,"page":a.page_no if a else None,"ids":[str(s.id)]})
    return {"version":VERSION,"pages":[],"blocks":blocks,"reconstructed":True},sources


def layout_text(layout, source, segments, mode="translation"):
    targets={sid:[] for sid in source}
    for segment in segments:
        if segment.state in {"not_translated", "retained"}:continue
        for sid in segment.source_segment_ids:targets.setdefault(sid,[]).append(segment)
    used=set()
    def values(ids):
        original="\n".join(source.get(sid,"") for sid in ids)
        translated=[];target_ids=[]
        for sid in ids:
            for segment in targets.get(sid,[]):
                if str(segment.id) not in used:
                    used.add(str(segment.id));translated.append(segment.translated_text);target_ids.append(str(segment.id))
        return {"source":"\n".join(source.get(sid,"") for sid in ids),"text":original if mode=="source" else "\n".join(translated),"source_ids":ids,"target_ids":target_ids}
    rows=[]
    for block in layout.get("blocks",[]):
        row={**block, **values([] if block.get("cells") else block.get("ids",[]))}
        if block.get("cells"):
            row["cells"]=[{**cell,**values(cell["ids"])} for cell in block["cells"]]
        rows.append(row)
    return rows


def render_html(layout, source, segments, mode="translation"):
    if mode not in {"source", "translation", "bilingual"}:
        raise ValueError("不支持的阅读模式")
    rows = layout_text(layout, source, segments, mode)
    parts = []
    page = None
    serial = 0
    def content(row):
        identifiers = row["source_ids"] if mode == "source" else row["target_ids"]
        prefix = "source-" if mode == "source" else "translation-"
        anchor = f' id="{escape(prefix + identifiers[0], quote=True)}"' if identifiers else ""
        aliases = ''.join(f' data-{kind}-segment-ids="{escape(" ".join(row[key]), quote=True)}"'
            for kind, key in (("source", "source_ids"), ("translation", "target_ids")) if row[key])
        text = escape(row["text"]).replace("\n", "<br>")
        if mode == "bilingual":
            text = escape(row["source"]).replace("\n", "<br>") + "<br>" + text
        return "<p" + anchor + aliases + ">" + text + "</p>"
    def new_page(number):
        nonlocal page, serial
        if page is not None:
            parts.append("</section>")
        page = number
        serial += 1
        parts.append(f'<section class="document-page" id="page-{serial}">')
    for row in rows:
        number = row.get("page") or page or 1
        if page is None or number != page:
            new_page(number)
        if row["kind"] == "page_break":
            new_page(page + 1)
            continue
        if row["kind"] == "separator":
            parts.append("<hr>")
            continue
        if row.get("cells"):
            parts.append("<table>")
            groups = {}
            for cell in row["cells"]:
                groups.setdefault(cell["row"], []).append(cell)
            for _, cells in sorted(groups.items()):
                parts.append("<tr>")
                for cell in sorted(cells, key=lambda c: c["col"]):
                    parts.append(f'<td rowspan="{cell["rowspan"]}" colspan="{cell["colspan"]}">' + content(cell) + "</td>")
                parts.append("</tr>")
            parts.append("</table>")
        elif row["text"].strip() or mode == "bilingual":
            parts.append(content(row))
    if page is not None:
        parts.append("</section>")
    return "".join(parts)
