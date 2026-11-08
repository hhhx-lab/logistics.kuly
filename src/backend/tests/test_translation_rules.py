from decimal import Decimal
import copy

from app.review.rules import RULE_PACK, decimal_value, evaluate_rules
from app.workflows.translation import protect_text, restore_translation, reading_html, sanitize_html, bounded_batches, translation_language_defects, adopted_value_translation_reason


def test_decimal_arithmetic_and_incomparable_data():
    data={"document_type":"commercial_invoice","quantity":Decimal("3"),"unit_price":Decimal("0.10"),"amount":Decimal("0.30"),"currency":"USD","source_segment_ids":["s1"]}
    results=evaluate_rules(documents=[],fields=[],lines=[data],rule_pack=RULE_PACK)
    assert next(r for r in results if r.rule_id=="invoice.line_arithmetic").status=="passed"
    results=evaluate_rules(documents=[],fields=[],lines=[{**data,"amount":Decimal("0.40")}],rule_pack=RULE_PACK)
    assert next(r for r in results if r.rule_id=="invoice.line_arithmetic").blocks_export
    results=evaluate_rules(documents=[],fields=[],lines=[{**data,"currency":None}],rule_pack=RULE_PACK)
    assert next(r for r in results if r.rule_id=="invoice.line_arithmetic").status=="undetermined"
    assert decimal_value(0.1) is None and decimal_value("NaN") is None


def test_protected_identifiers_and_omission():
    source="Invoice 000012 dated 2026-10-02: USD 120.00 email a@sample.test container MSKU0012345"
    protected,tokens=protect_text(source)
    restored,defects=restore_translation(protected,tokens)
    assert restored==source and not defects
    _,defects=restore_translation(protected.replace('⟦P1⟧',''),tokens)
    assert 'P1' in defects


def test_target_language_cannot_be_declared_while_copying_english():
    for source in ["COMMERCIAL INVOICE", "Seller: Example Trading Ltd", "Port of Loading: Shanghai"]:
        assert "target_language_missing" in translation_language_defects(source,source,"zh-CN")
        assert "source_copied" in translation_language_defects(source,source,"zh-CN")
    assert not translation_language_defects("Seller: Example Trading Ltd","卖方：Example Trading Ltd","zh-CN")
    assert not translation_language_defects("Port of Loading: Shanghai","装运港：上海","zh-CN")
    assert "source_copy_rate" in translation_language_defects("Port of Loading: Shanghai","中文 Port of Loading: Shanghai","zh-CN")
    assert not translation_language_defects("ACME LTD", "ACME LTD", "zh-CN")
    assert not translation_language_defects("MSKU0012345 USD 120.00", "MSKU0012345 USD 120.00", "zh-CN")
    assert "source_copied" in translation_language_defects("装运港：上海","中文 Port: 装运港：上海","en")
    assert not translation_language_defects("装运港：上海","Port of Loading: Shanghai","en")
    assert not translation_language_defects("商业发票","商业发票","zh-CN")
    for text in ['Atelier Horizon Démo SAS','620 kg／720 kg','14 Camino de la Huerta Demo, 46024 Valencia, Spain','SEAL-DEMO-B-1015','CONTAINER-DEMO-A-01']:
        assert not translation_language_defects(text,text,'zh-CN')
    assert not translation_language_defects('18 rue des Ateliers Démo, 69007 Lyon, France','18 rue des Ateliers Démo, 69007里昂，法国','zh-CN')
    assert translation_language_defects('DEMONSTRATION ONLY — NOT VALID FOR SHIPMENT | 1 / 2','DEMONSTRATION ONLY — NOT VALID FOR SHIPMENT | 1 / 2','zh-CN')
    protected,tokens=protect_text('SEAL-DEMO-B-1015')
    assert tokens=={'P1':'SEAL-DEMO-B-1015'} and protected=='⟦P1⟧'


def test_adopted_business_value_does_not_silently_change_original_translation():
    assert adopted_value_translation_reason('500','5','金额500',numeric_value=Decimal('5'))=='adopted_numeric_value_absent'
    assert adopted_value_translation_reason('500','5.0','原文500；人工采用5',numeric_value=Decimal('5.0')) is None
    assert adopted_value_translation_reason('MSKU0012345','MSKU0012346','柜号MSKU0012345')=='adopted_protected_value_absent'
    assert adopted_value_translation_reason('ACME','BRAVO','BRAVO')=='changed_business_text_requires_review'
    assert adopted_value_translation_reason('12','12','数量12') is None


def test_static_preview_escapes_injection_and_preserves_dom_ids():
    content=reading_html([{"id":"s1","text":"<script>alert(1)</script>"}],[{"id":"t1","source_segment_ids":["s1"],"text":"<img src=x onerror=bad>"}],synthetic=False)
    assert '<script>' not in content and '<img' not in content
    assert 'translation-t1' in content
    safe=sanitize_html('<p id="translation-t1" onclick="bad">text</p><script>secret</script><img src="https://bad">')
    assert safe=='<p id="translation-t1">text</p>'


def test_unknown_coverage_and_jurisdiction_are_never_passed():
    results=evaluate_rules(documents=[{"id":"p1","page_count":None,"segment_count":1}],fields=[],lines=[],rule_pack=RULE_PACK)
    assert next(r for r in results if r.rule_id=="material.coverage").status=="undetermined"
    assert next(r for r in results if r.rule_id=="jurisdiction.hs_certificate").status=="not_covered"


def test_batches_preserve_group_and_bound_cost():
    rows=[{"group":"A","text":"Invoice line"} for _ in range(30)]+[{"group":"B","text":"Other document"}]
    batches=list(bounded_batches(rows,lambda x:x["text"],lambda x:x["group"]))
    assert [len(batch) for batch in batches]==[24,6,1]
    assert all(len({row["group"] for row in batch})==1 for batch in batches)


def test_general_invoice_total_and_cross_document_known_transport():
    fields=[{"id":"total","logical_document_id":"A","field_key":"total_amount","value":"0.30","numeric_value":Decimal(".30")},
        {"id":"currency","logical_document_id":"A","field_key":"currency","value":"USD"}]
    lines=[{"logical_document_id":"A","document_type":"commercial_invoice","quantity":Decimal(3),"unit_price":Decimal('.10'),"amount":Decimal('.30'),"currency":"USD"}]
    for doc,port in [('A','Ningbo'),('B','Shanghai')]:
        fields.extend([{"id":doc+'ref',"logical_document_id":doc,"field_key":"transport_reference","value":"BL001","role":"transport"},
            {"id":doc+'port',"logical_document_id":doc,"field_key":"port_of_loading","value":port,"role":"transport"}])
    results=evaluate_rules(documents=[],fields=fields,lines=lines,rule_pack=RULE_PACK)
    assert next(r for r in results if r.rule_id=='invoice.total').status=='passed'
    assert next(r for r in results if r.rule_id=='cross_document.port_of_loading').status=='failed'
    fields.append({"id":"fee","logical_document_id":"A","field_key":"freight","value":"0.10"})
    results=evaluate_rules(documents=[],fields=fields,lines=lines,rule_pack=RULE_PACK)
    assert next(r for r in results if r.rule_id=='invoice.total').status=='undetermined'


def test_confirmed_party_mapping_preserves_roles_without_guessing():
    rows=[{"id":"a","logical_document_id":"A","field_key":"name","role":"seller","entity_group":"confirmed-1","value":"ACME"},
        {"id":"b","logical_document_id":"B","field_key":"name","role":"shipper","entity_group":"confirmed-1","value":"OTHER"}]
    results=evaluate_rules(documents=[],fields=rows,lines=[],rule_pack=RULE_PACK)
    assert next(r for r in results if r.rule_id=='party.confirmed_mapping').status=='failed'
    for row in rows:row.pop('entity_group')
    results=evaluate_rules(documents=[],fields=rows,lines=[],rule_pack=RULE_PACK)
    assert next(r for r in results if r.rule_id=='party.role_mapping').status=='undetermined'


def test_required_candidates_and_format_never_pass_without_source_inputs():
    document={"id":"p","document_type":"commercial_invoice","logical_documents":[{"logical_document_id":"A","document_type":"commercial_invoice","source_segment_ids":["s"]}],"page_count":1,"seen_pages":[1],"segment_count":1}
    rows=[{"id":str(i),"logical_document_id":"A","field_key":key,"role":role,"value":value,"source_segment_ids":["s"]} for i,(key,role,value) in enumerate([('invoice_no','document','001'),('name','seller','ACME'),('name','buyer','BRAVO'),('goods_description','goods','widgets'),('email','seller','bad@@mail'),('container_no','transport','MSKU0012345')])]
    result=evaluate_rules(documents=[document],fields=rows,lines=[],rule_pack=RULE_PACK)
    assert next(r for r in result if r.rule_id=='document.required_fields').status=='passed'
    assert next(r for r in result if r.rule_id=='field.format.email').status=='undetermined'
    assert next(r for r in result if r.rule_id=='field.format.container_no').status=='passed'
    result=evaluate_rules(documents=[document],fields=[],lines=[],rule_pack=RULE_PACK)
    required=next(r for r in result if r.rule_id=='document.required_fields')
    assert required.status=='undetermined' and required.blocks_export
    assert required.calculation['missing_candidates']==['单据编号','seller名称','buyer名称','货描']
    result=evaluate_rules(documents=[],fields=[],lines=[],rule_pack=RULE_PACK)
    assert next(r for r in result if r.rule_id=='document.required_fields').status=='not_run'


def test_cross_logical_measurements_require_confirmed_same_basis_and_exact_units():
    fields=[{'logical_document_id':doc,'field_key':'transport_reference','value':'BL-001'} for doc in ['A','B']]
    lines=[{'id':doc,'logical_document_id':doc,'quantity':Decimal(q),'quantity_unit':'pieces','net_weight':Decimal(n),'gross_weight':Decimal(g),'weight_unit':'KG','source_segment_ids':[doc+'source']} for doc,q,n,g in [('A','12','90','100'),('B','13','90','110')]]
    edge={'id':'match','left_line_id':'A','right_line_id':'B','state':'confirmed','relation':'one_to_one','decided_by':'human','basis':{'same_goods':True,'complete':True,'scope_key':'SKU-1 shipment','role':'goods','packaging_level':'item'}}
    def check(rows=lines,edges=[edge],refs=fields):return evaluate_rules(documents=[],fields=refs,lines=rows,line_matches=edges,rule_pack=RULE_PACK)
    results=check()
    for key in ['quantity','gross_weight']:
        row=next(r for r in results if r.rule_id=='cross_document.'+key)
        assert row.status=='failed' and row.severity=='red' and row.blocks_export and row.source_segment_ids==('Asource','Bsource')
    assert next(r for r in results if r.rule_id=='cross_document.net_weight').status=='passed'
    for change in [{'state':'candidate'},{'decided_by':None},{'relation':'many_to_one'},{'basis':{}},{'basis':{**edge['basis'],'role':'seller'}}]:
        assert not any(r.rule_id.startswith('cross_document.') and r.status=='failed' for r in check(edges=[{**edge,**change}]))
    altered=copy.deepcopy(lines);altered[1]['quantity_unit']='cartons';altered[1]['weight_unit']='LB'
    assert all(r.status=='undetermined' for r in check(rows=altered) if r.rule_id in {'cross_document.quantity','cross_document.net_weight','cross_document.gross_weight'})
    assert not any(r.status=='failed' and r.rule_id.startswith('cross_document.') for r in check(refs=[]))
    duplicate={**edge,'id':'other','right_line_id':'A'}
    assert not any(r.status=='failed' and r.rule_id.startswith('cross_document.') for r in check(edges=[edge,duplicate]))


def test_export_contains_target_translation_without_source_or_duplicate_fields():
    from app.exports.render import build_translation, verify
    from docx import Document
    import io
    rows = [{"section":"s1", "source":"Original invoice text", "translation":"发票内容：货物共20箱"},
            {"section":"s2", "source":"Unselected source paragraph", "translation":None}]
    data, expected = build_translation(filename="invoice.pdf", target_language="zh-CN", scope="selected_sections", rows=rows,
        fields=[{"name":"internal_field", "source":"SourceOnlyField", "value":"20"}])
    text = "\n".join(p.text for p in Document(io.BytesIO(data)).paragraphs)
    assert "发票内容：货物共20箱" in text
    assert all(value not in text for value in ("Original invoice text", "Unselected source paragraph", "SourceOnlyField", "原文：", "译文："))
    assert verify(data, "docx", expected)["template_version"] == "source-layout-translation-docx@4"
    english, _ = build_translation(filename="invoice.pdf", target_language="en", scope="all", rows=[{"section":"s1", "translation":"Commercial Invoice: 20 cartons"}], fields=[])
    translated = "\n".join(p.text for p in Document(io.BytesIO(english)).paragraphs)
    assert "Commercial Invoice: 20 cartons" in translated and not any("\u4e00" <= char <= "\u9fff" for char in translated)


def test_layout_translation_preserves_merged_tables_lines_and_page_boundaries():
    import io
    from types import SimpleNamespace
    from docx import Document
    from docx.oxml.ns import qn
    from app.review.layout import render_html
    from app.exports.render import build_layout_translation, verify
    layout={"blocks":[{"kind":"table","page":1,"ids":["s1","s2"],"cells":[{"row":0,"col":0,"rowspan":1,"colspan":2,"ids":["s1"]},{"row":1,"col":0,"rowspan":1,"colspan":1,"ids":["s2"]},{"row":1,"col":1,"rowspan":1,"colspan":1,"ids":[]}]},{"kind":"separator","page":1,"ids":[]},{"kind":"text","page":2,"ids":["s3"]}]}
    source={"s1":"SOURCE TABLE", "s2":"SOURCE CELL", "s3":"SOURCE SECOND PAGE"}
    layout["pages"]=[{"page_no":1,"width_pt":595,"height_pt":842},{"page_no":2,"width_pt":842,"height_pt":595}]
    segments=[SimpleNamespace(id=f"t{i}",source_segment_ids=[f"s{i}"],translated_text=text,state="translated") for i,text in enumerate(["表格标题","单元格内容","第二页译文"],1)]
    html=render_html(layout,source,segments)
    assert 'colspan="2"' in html and '<hr>' in html and html.count('class="document-page"')==2
    assert all(value not in html for value in source.values()) and 'translation-t2' in html
    assert "SOURCE TABLE" in render_html(layout,source,segments,"bilingual")
    assert 'data-source-segment-ids="s2"' in html and 'data-translation-segment-ids="t2"' in html
    data,expected=build_layout_translation(layout,source,segments,target_language="zh-CN")
    doc=Document(io.BytesIO(data));assert len(doc.tables)==1
    assert doc.tables[0].cell(0,0).text=="表格标题"
    assert doc.element.xpath('.//w:gridSpan') and doc.element.xpath('.//w:pBdr')
    assert len(doc.sections)==2
    assert round(doc.sections[0].page_width.pt)==595 and round(doc.sections[1].page_width.pt)==842
    assert round(doc.sections[1].page_height.pt)==595
    verify(data,"docx",expected)
    original,original_expected=build_layout_translation(layout,source,segments,target_language="zh-CN",mode="source")
    original_doc=Document(io.BytesIO(original))
    assert original_doc.tables[0].cell(0,0).text=="SOURCE TABLE"
    assert original_expected==list(source.values())
    assert original_doc.element.xpath('.//w:gridSpan') and original_doc.element.xpath('.//w:pBdr')
    assert len(original_doc.sections)==2
    assert not original_doc.sections[0].footer.paragraphs[0].text
    verify(original,"docx",original_expected)


def test_layout_keeps_target_language_cells_and_rejects_missing_delivery_segments():
    import io
    import pytest
    from types import SimpleNamespace
    from docx import Document
    from app.review.layout import render_html
    from app.exports.render import build_layout_translation
    layout={"blocks":[{"kind":"table","page":1,"ids":["s1"],"cells":[{"row":0,"col":0,"rowspan":1,"colspan":1,"ids":["s1"]}]}]}
    segment=SimpleNamespace(id="t1",source_segment_ids=["s1"],translated_text="收货地址：上海",state="retained_original")
    assert "收货地址：上海" in render_html(layout,{"s1":"收货地址：上海"},[segment])
    data,expected=build_layout_translation(layout,{"s1":"收货地址：上海"},[segment],target_language="zh-CN")
    assert Document(io.BytesIO(data)).tables[0].cell(0,0).text=="收货地址：上海"
    assert expected==["收货地址：上海"]
    missing=SimpleNamespace(id="t2",source_segment_ids=["s2"],translated_text="货物共400箱",state="translated")
    with pytest.raises(RuntimeError,match="未覆盖应交付译段"):
        build_layout_translation(layout,{"s1":"收货地址：上海","s2":"400 cartons"},[segment,missing],target_language="zh-CN")


def test_docx_explicit_page_break_and_horizontal_rule_are_retained():
    import io
    from docx import Document
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from app.documents.parsing.docx_adapter import DocxAdapter
    from app.review.layout import manifest_from_outcome
    doc=Document();first=doc.add_paragraph('Invoice');borders=OxmlElement('w:pBdr');line=OxmlElement('w:bottom');line.set(qn('w:val'),'single');borders.append(line);first._p.get_or_add_pPr().append(borders)
    doc.add_page_break();doc.add_paragraph('Second page');stream=io.BytesIO();doc.save(stream)
    parsed=DocxAdapter().parse(stream.getvalue());layout=manifest_from_outcome(parsed,{s.key:s.key for s in parsed.segments})
    assert [b['kind'] for b in layout['blocks']]==['text','separator','page_break','text']


def test_issue_skill_risk_classification_keeps_uncertainty_nonblocking():
    from app.review.issue_skill import classify_rule, guidance_for
    from app.review.rules import RuleResult
    from types import SimpleNamespace
    missing = RuleResult('document.required_fields', 'undetermined', '尚未提取单号', severity='red', blocks_export=True)
    assert classify_rule(missing) == ('field_missing', 'undetermined', False)
    actual = RuleResult('material.coverage', 'failed', '确认缺页', severity='red', blocks_export=True)
    assert classify_rule(actual) == ('source_omission', 'red', True)
    ambiguous = RuleResult('material.quality', 'undetermined', '图像模糊')
    assert classify_rule(ambiguous) == ('source_uncertain', 'undetermined', False)
    duplicate = RuleResult('field.duplicate_candidates', 'undetermined', '不同候选口径待核对',
        input_field_ids=('cif-total','goods-total'), severity='red', blocks_export=True)
    assert classify_rule(duplicate) == ('conflict', 'undetermined', False)
    issue = SimpleNamespace(category='source_uncertain', severity='undetermined', description='扫描模糊',
        sources=[{'target':{'translation_segment_id':'some-segment'}}],guidance={'replacement_text':'不能用译文覆盖源文缺失'})
    guide = guidance_for(issue)
    assert not guide['can_apply'] and guide['actions'][2]['enabled']
