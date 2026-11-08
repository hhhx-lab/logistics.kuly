"""固定 DOCX 样式先排版，隔离 LibreOffice 输出 PDF，并逐产物回检。"""
from __future__ import annotations
import hashlib
import io
import shutil
import subprocess
import tempfile
from pathlib import Path
from docx import Document
from docx.shared import Cm,Pt,RGBColor
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.enum.section import WD_SECTION_START
import pdfplumber
from app.languages import language_name

TEMPLATE_VERSION="source-layout-translation-docx@4"
DRAFT_TEMPLATE_VERSION="source-layout-translation-draft@3"
DRAFT_MARKER="草稿 · 未经最终复核，不可正式使用"


def new_document(target_language="zh-CN"):
    document=Document()
    section=document.sections[0]
    section.top_margin=section.bottom_margin=Cm(2.2)
    section.left_margin=section.right_margin=Cm(2.3)
    styles={"Normal":("宋体" if target_language=="zh-CN" else "Arial",11),"Title":("黑体" if target_language=="zh-CN" else "Arial",18),"Heading 1":("黑体" if target_language=="zh-CN" else "Arial",15),"Heading 2":("黑体" if target_language=="zh-CN" else "Arial",12)}
    for name,(font,size) in styles.items():
        style=document.styles[name]
        style.font.name=font;style.font.size=Pt(size);style.font.color.rgb=RGBColor(0,0,0)
        props=style.element.get_or_add_rPr()
        fonts=props.find(qn("w:rFonts"))
        if fonts is None:fonts=OxmlElement("w:rFonts");props.append(fonts)
        fonts.set(qn("w:eastAsia"),"宋体")
        language=OxmlElement("w:lang");language.set(qn("w:val"),target_language);props.append(language)
        style.paragraph_format.space_after=Pt(6)
    document.styles["Normal"].paragraph_format.line_spacing=1.3
    footer=section.footer.paragraphs[0]
    footer.add_run("第 ")
    field=OxmlElement("w:fldSimple");field.set(qn("w:instr"),"PAGE");footer._p.append(field)
    footer.add_run(" 页 · 双语翻译件，供业务复核")
    return document


def build_bilingual(*,filename,target_language,scope,rows,fields,synthetic=False,draft=False):
    document=new_document(target_language)
    document.add_heading("单证双语翻译草稿" if draft else "单证双语翻译确认稿",0)
    if draft:
        for section in document.sections:
            header=section.header.paragraphs[0]
            header.alignment=1
            label=header.add_run(DRAFT_MARKER)
            label.bold=True;label.font.size=Pt(14);label.font.color.rgb=RGBColor(128,128,128)
            section.footer.paragraphs[0].add_run(" · 草稿")
        document.add_paragraph("草稿仅供核对：字段及疑点尚未最终确认，不代表成功导出或正式单证。")
    document.add_paragraph(f"原件：{filename}；目标语言：{language_name(target_language)}")
    document.add_paragraph("翻译件／供业务复核；本文件不替代签章原件。")
    if synthetic:document.add_paragraph("假模型输出，非真实翻译。")
    if scope!="all":document.add_paragraph("仅翻译指定范围；未选内容标明原文保留／未翻译。")
    if fields:
        document.add_heading("待复核的关键信息" if draft else "经复核的关键信息",1)
        table=document.add_table(rows=1,cols=3);table.style="Table Grid"
        for cell,text in zip(table.rows[0].cells,["字段／角色","原文","人工采用或规范值"]):cell.text=text
        for field in fields:
            cells=table.add_row().cells
            for cell,text in zip(cells,[f"{field['name']} / {field.get('role') or '未指定'}",field['source'],field['value']]):cell.text=str(text)
    section=None
    expected=[str(value) for item in fields for value in (item['name'],item.get('role') or '未指定',item['source'],item['value']) if value is not None and str(value).strip()]
    for row in rows:
        if row["section"]!=section:
            section=row["section"];document.add_heading(section or "单据内容",1)
        document.add_paragraph("原文："+row["source"])
        if row.get("translation") is not None:document.add_paragraph("译文："+row["translation"])
        else:document.add_paragraph("原文保留／未翻译（不属于本次确认范围）")
        expected.append(row["source"])
        if row.get("translation"):expected.append(row["translation"])
    stream=io.BytesIO();document.save(stream)
    return stream.getvalue(),expected


def build_translation(*, filename, target_language, scope, rows, fields, synthetic=False):
    document = new_document(target_language)
    english = target_language == "en"
    title = "Document Translation" if english else "单证翻译确认稿"
    document.add_heading(title, 0)
    document.add_paragraph(("Source file: " if english else "来源文件：") + filename)
    document.add_paragraph("Translation for business review; does not replace the signed original." if english else "翻译件／供业务复核，不替代签章原件。")
    if synthetic:
        document.add_paragraph("Synthetic test output, not a real translation." if english else "测试输出，非真实翻译。")
    if scope != "all":
        document.add_paragraph("Only the requested scope is included; untranslated sections are omitted." if english else "仅包含本次确认范围的译文，未译区段不包含在本译本中。")
    for section in document.sections:
        footer = section.footer.paragraphs[0]
        footer.clear()
        footer.add_run("Page " if english else "第 ")
        field = OxmlElement("w:fldSimple"); field.set(qn("w:instr"), "PAGE"); footer._p.append(field)
        footer.add_run(" · Translation" if english else " 页 · 翻译件")
    expected = []
    section_id, count = None, 0
    for row in rows:
        translated = row.get("translation")
        if translated is None or not translated.strip(): continue
        if row["section"] != section_id:
            section_id = row["section"]; count += 1
            document.add_heading(f"Section {count}" if english else f"单据内容 {count}", 1)
        document.add_paragraph(translated)
        expected.append(translated)
    if not expected: raise RuntimeError("确认范围没有可导出的译文")
    stream = io.BytesIO(); document.save(stream)
    return stream.getvalue(), expected


def build_layout_translation(layout, source, segments, *, target_language, draft=False, mode="translation"):
    from app.review.layout import layout_text
    document=new_document(target_language)
    for name,size in (("Normal",9),("Heading 1",12),("Heading 2",10)):
        style=document.styles[name]
        style.font.size=Pt(size)
        style.paragraph_format.space_before=Pt(0)
        style.paragraph_format.space_after=Pt(2)
        style.paragraph_format.line_spacing=1.1
    pages={item["page_no"]:item for item in layout.get("pages",[]) if item.get("page_no")}
    def page_size(section,number):
        info=pages.get(number,{})
        section.page_width=Pt(info.get("width_pt") or 595.28)
        section.page_height=Pt(info.get("height_pt") or 841.89)
        section.top_margin=section.bottom_margin=Cm(1.4)
        section.left_margin=section.right_margin=Cm(1.5)
        section.header_distance=section.footer_distance=Cm(.6)
    draft_labels={"zh-CN":("翻译件", "草稿"),"en":("Translation", "Draft"),"fr":("Traduction", "Brouillon"),"es":("Traducción", "Borrador"),"ru":("Перевод", "Черновик")}
    translated_label,draft_label=draft_labels[target_language]
    for section in document.sections:
        section.footer.paragraphs[0].clear()
        if mode != "source":
            section.footer.paragraphs[0].add_run(translated_label+" · "+draft_label if draft else translated_label)
    if draft:
        document.sections[0].header.paragraphs[0].text = draft_label
    expected=[];page=None
    for row in layout_text(layout,source,segments,mode=mode):
        current=row.get("page")
        if current is not None and current!=page:
            section=document.sections[0] if page is None else document.add_section(WD_SECTION_START.NEW_PAGE)
            page_size(section,current)
            page=current
        if row["kind"] == "page_break":
            document.add_page_break();continue
        if row["kind"] == "separator":
            paragraph=document.add_paragraph();ppr=paragraph._p.get_or_add_pPr();borders=OxmlElement("w:pBdr");border=OxmlElement("w:bottom");border.set(qn("w:val"),"single");border.set(qn("w:sz"),"6");borders.append(border);ppr.append(borders);continue
        if row.get("cells"):
            cells=row["cells"];height=max(c["row"]+c["rowspan"] for c in cells);width=max(c["col"]+c["colspan"] for c in cells)
            table=document.add_table(rows=height,cols=width);table.style="Table Grid"
            for cell in cells:
                tc=table.cell(cell["row"],cell["col"])
                if cell["rowspan"]>1 or cell["colspan"]>1:tc=tc.merge(table.cell(cell["row"]+cell["rowspan"]-1,cell["col"]+cell["colspan"]-1))
                tc.text=cell["text"]
                for paragraph in tc.paragraphs:
                    paragraph.paragraph_format.space_after=Pt(0)
                if cell["text"].strip():expected.append(cell["text"])
        elif row["text"].strip():
            if row["kind"] in {"title","heading"}:document.add_heading(row["text"],min(row.get("level") or 1,2))
            else:document.add_paragraph(row["text"])
            expected.append(row["text"])
    if mode != "source":
        rendered="".join("".join(value.split()) for value in expected)
        for segment in segments:
            if segment.state in {"not_translated","retained"}:continue
            if "".join(segment.translated_text.split()) not in rendered:
                raise RuntimeError("源布局未覆盖应交付译段，未生成不完整译本")
    if not expected:raise RuntimeError("没有可导出的有效识别内容" if mode == "source" else "没有可导出的有效译文")
    stream=io.BytesIO();document.save(stream);return stream.getvalue(),expected


def translations_by_source(segments):
    grouped={}
    for segment in segments:
        for source_id in segment.source_segment_ids:
            grouped.setdefault(source_id,[]).append(segment.translated_text)
    return {source_id:'\n'.join(texts) for source_id,texts in grouped.items()}


def report_expected(case_name,manifest,checks,issues,fields):
    values=['单票复核报告',case_name,manifest['target_language'],manifest['translation_scope'],
            manifest.get('business_revision_no',manifest['business_revision_id']),
            manifest.get('translation_revision_no',manifest['translation_revision_id'])]
    for source in manifest['source_versions']:
        values.extend((source['filename'],f"原件v{source['document_version']}",source.get('parse_artifact_id')))
    for check in checks:
        values.extend((check['rule_id'],check['version'],check['status'],check['explanation'],check['calculation']))
    for issue in issues:
        values.extend((issue['title'],issue['description']))
        for source in issue['sources']:
            target=source.get('target',source)
            values.append(source.get('value',''))
            values.append(target.get('page_no',target.get('source_segment_id')))
        if issue.get('disposition'):values.append(issue['disposition'])
    for item in fields:
        values.extend((item['name'],item.get('role') or '未指定',item['source'],item['value'],item.get('review_status') or '机器候选，未逐项人工确认',item.get('reason') or '未另填理由',item.get('basis') or '原件来源'))
    return [str(value) for value in values if value is not None and str(value).strip()]


def build_report(case_name,manifest,checks,issues,fields):
    document=new_document(manifest.get("target_language", "zh-CN"));document.add_heading("单票复核报告",0)
    document.add_paragraph(f"单票：{case_name}；业务版：{manifest.get('business_revision_no',manifest['business_revision_id'])}；译文版：{manifest.get('translation_revision_no',manifest['translation_revision_id'])}")
    document.add_paragraph("本报告保留原始检查结论、来源、采用值和人工处置。未覆盖不表示通过。")
    document.add_heading("参与原件与翻译范围",1)
    for source in manifest["source_versions"]:document.add_paragraph(f"{source['filename']} · 原件v{source['document_version']} · 解析{source.get('parse_artifact_id')}")
    document.add_paragraph(f"目标语言：{manifest['target_language']}；范围：{manifest['translation_scope']}；模板：{TEMPLATE_VERSION}")
    if manifest["translation_scope"]!="all":document.add_paragraph("仅翻译指定范围，未译区段不包含在译本中；原文可在工作台查看。")
    document.add_heading("原始规则检查",1)
    for check in checks:document.add_paragraph(f"{check['rule_id']}@{check['version']}：{check['status']}；{check['explanation']}；{check['calculation']}")
    document.add_heading("疑点及人工处置",1)
    for issue in issues:
        document.add_paragraph(f"{issue['title']}：{issue['description']}")
        for source in issue["sources"]:
            target=source.get("target",source)
            origin=next((s["filename"] for s in manifest["source_versions"] if s["document_id"]==target.get("document_id")),target.get("document_id",target.get("source_segment_id","未提供")))
            document.add_paragraph(f"原值：{source.get('value','')}；来源：{origin}；原件v{target.get('document_version','—')}；页/段 {target.get('page_no',target.get('source_segment_id','—'))}")
        if issue.get("disposition"):document.add_paragraph("人工处置："+str(issue["disposition"]))
    document.add_heading("字段审核与采用值",1)
    for field in fields:document.add_paragraph(f"{field['name']} / {field.get('role') or '未指定'}：原文 {field['source']}；采用/规范 {field['value']}；状态 {field.get('review_status') or '机器候选，未逐项人工确认'}；理由 {field.get('reason') or '未另填理由'}；依据 {field.get('basis') or '原件来源'}")
    stream=io.BytesIO();document.save(stream)
    return stream.getvalue()


def to_pdf(docx_bytes):
    executable=shutil.which("soffice") or "/Applications/LibreOffice.app/Contents/MacOS/soffice"
    if not Path(executable).exists():raise RuntimeError("确认稿 PDF 渲染器不可用")
    with tempfile.TemporaryDirectory(prefix="logistics-export-") as temp:
        root=Path(temp);source=root/"document.docx";source.write_bytes(docx_bytes)
        profile=(root/"profile").as_uri()
        subprocess.run([executable,f"-env:UserInstallation={profile}","--headless","--convert-to","pdf","--outdir",str(root),str(source)],
            check=True,timeout=120,capture_output=True)
        result=root/"document.pdf"
        if not result.exists():raise RuntimeError("确认稿 PDF 未生成")
        return result.read_bytes()


def verify(data,kind,expected,*,allow_draft=False):
    if not data:raise RuntimeError("导出文件为空")
    if kind=="docx":
        document=Document(io.BytesIO(data))
        text="\n".join([p.text for p in document.paragraphs]+[cell.text for table in document.tables for row in table.rows for cell in row.cells])
        text+="\n"+"\n".join(p.text for section in document.sections for region in (section.header,section.footer) for p in region.paragraphs)
        pages=None
    else:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages=len(pdf.pages)
            texts=[page.extract_text() or "" for page in pdf.pages]
            if not pages or any(not text.strip() for text in texts):raise RuntimeError("PDF 存在空页或无法抽取文本")
            for page in pdf.pages:
                x0,y0,x1,y1=page.bbox
                if any(c['x0']<x0-1 or c['x1']>x1+1 or c['top']<y0-1 or c['bottom']>y1+1 for c in page.chars):
                    raise RuntimeError("PDF 文本超出页面边界")
            table_texts=[cell for page in pdf.pages for table in page.extract_tables() for row in table for cell in row if cell]
            text="\n".join([*texts,*table_texts])
    if not text.strip() or any(char in text for char in ("\ufffd","\u25a1")):raise RuntimeError("导出文本为空或有缺字符")
    collapsed="".join(text.split())
    if not allow_draft and any("".join(marker.split()) in collapsed for marker in ("待确认双语草稿",DRAFT_MARKER,"单证双语翻译草稿")):
        raise RuntimeError("确认稿残留草稿水印")
    for value in expected:
        if "".join(value.split()) not in collapsed:raise RuntimeError("导出回检缺少应交付内容")
    return {"sha256":hashlib.sha256(data).hexdigest(),"size_bytes":len(data),"text_characters":len(text),"page_count":pages,
        "expected_segment_count":len(expected),"template_version":DRAFT_TEMPLATE_VERSION if allow_draft else TEMPLATE_VERSION,"text_checked":True}
