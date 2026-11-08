from types import SimpleNamespace
from app.documents.preprocessing import assess_language,split_document_segments
from app.documents.preprocessing import crop_source_region
from app.documents.parsing.dto import NormRect
from PIL import Image
import io
import pytest


def test_mixed_language_does_not_silently_choose_chinese():
    result=assess_language("商业发票 原件内容 Seller ACME Export Buyer BRAVO Import")
    assert result.language=="mixed" and result.uncertain
    assert assess_language("000123 USD 12.00").uncertain
    assert assess_language("商业发票货物名称纯棉毛巾").language=="zh-CN"


def test_real_titles_partition_without_changing_sources():
    segments=[SimpleNamespace(kind='title',text='COMMERCIAL INVOICE'),SimpleNamespace(kind='text',text='Seller: ACME Export'),
        SimpleNamespace(kind='title',text='PACKING LIST'),SimpleNamespace(kind='text',text='12 cartons towels')]
    groups=split_document_segments(segments)
    assert [g['type_candidate'] for g in groups]==['commercial_invoice','packing_list']
    assert groups[0]['segments'][0] is segments[0]
    assert len(groups[1]['sections'])==1


@pytest.mark.parametrize('mime,format',[('image/png','PNG'),('image/jpeg','JPEG')])
def test_image_vision_region_is_actual_bounded_crop(mime,format):
    output=io.BytesIO()
    with Image.new('RGB',(100,80),'white') as original:original.save(output,format=format)
    data=crop_source_region(output.getvalue(),mime=mime,page_no=1,rect=NormRect(x0=.1,y0=.25,x1=.6,y1=.75))
    with Image.open(io.BytesIO(data)) as region:
        assert region.size==(50,40) and region.format=='PNG'
    with pytest.raises(ValueError):crop_source_region(output.getvalue(),mime=mime,page_no=2,rect=NormRect(x0=0,y0=0,x1=1,y1=1))


def test_native_pdf_standalone_titles_partition_paragraphs():
    from reportlab.pdfgen import canvas
    from app.documents.parsing.native_pdf import NativePdfAdapter
    output=io.BytesIO();pdf=canvas.Canvas(output)
    for y,text in [(760,'COMMERCIAL INVOICE'),(730,'Seller: ACME Export Buyer: BRAVO Import'),(650,'PACKING LIST'),(620,'Goods: twelve cartons of cotton towels')]:pdf.drawString(50,y,text)
    pdf.save();outcome=NativePdfAdapter().parse(output.getvalue())
    assert all(segment.kind=='paragraph' for segment in outcome.segments)
    groups=split_document_segments(outcome.segments)
    assert [group['type_candidate'] for group in groups]==['commercial_invoice','packing_list']
    sentence=[SimpleNamespace(kind='paragraph',text='This commercial invoice refers to packing list PL001')]
    assert len(split_document_segments(sentence))==1


@pytest.mark.parametrize("orientation",range(1,9))
def test_exif_derivative_preserves_original_and_pixel_inverse(orientation):
    from app.documents.preprocessing import prepare_image
    output=io.BytesIO()
    with Image.new("RGB",(80,40),"white") as image:
        exif=Image.Exif();exif[274]=orientation
        image.save(output,format="JPEG",exif=exif)
    original=output.getvalue();prepared=prepare_image(original,mime="image/jpeg")
    with Image.open(io.BytesIO(prepared.data)) as corrected:
        assert corrected.size==((40,80) if orientation>=5 else (80,40))
        assert corrected.getexif().get(274,1)==1
    with Image.open(io.BytesIO(original)) as untouched:assert untouched.getexif()[274]==orientation
    transform=prepared.manifest["pages"]["1"]
    a,b,c,d,e,f=transform["recognition_to_source_raw"]
    # Orthogonal transforms have determinant ±1 and preserve the page bounds.
    assert abs(a*e-b*d)==1
    assert {(a*x+b*y+c,d*x+e*y+f) for x,y in ((0,0),(0,1),(1,0),(1,1))}=={(0,0),(0,1),(1,0),(1,1)}
    region=crop_source_region(original,mime="image/jpeg",page_no=1,rect=NormRect(x0=.1,y0=.25,x1=.6,y1=.75))
    with Image.open(io.BytesIO(region)) as crop:assert crop.size==((20,40) if orientation>=5 else (40,20))


def test_rotated_pdf_derivative_matches_original_display_and_anchor_crop():
    import pypdfium2 as pdfium
    from reportlab.pdfgen import canvas
    from app.documents.preprocessing import prepare_pdf_ocr
    from app.documents.parsing.native_pdf import NativePdfAdapter, render_page_png, crop_rect_png
    from app.documents.parsing.selector import attach_preprocessing
    from app.documents.parsing.mineru import normalize_content_list
    from PIL import ImageChops
    output=io.BytesIO();pdf=canvas.Canvas(output,pagesize=(200,100));pdf.drawString(20,40,"INVOICE NUMBER ABC123456");pdf.save()
    source=pdfium.PdfDocument(output.getvalue());page=source[0];page.set_rotation(90)
    rotated=io.BytesIO();source.save(rotated);page.close();source.close()
    original=rotated.getvalue();prepared=prepare_pdf_ocr(original,(1,))
    derivative=pdfium.PdfDocument(prepared.data);page=derivative[0]
    assert page.get_rotation()==0 and page.get_size()==(100,200)
    page.close();derivative.close()
    with Image.open(io.BytesIO(render_page_png(original,1))) as before,Image.open(io.BytesIO(render_page_png(prepared.data,1))) as after:
        assert before.size==after.size
        diff=ImageChops.difference(before.convert("RGB"),after.convert("RGB"))
        from PIL import ImageStat
        assert max(ImageStat.Stat(diff).mean)<1
    parsed=NativePdfAdapter().parse(original)
    assert parsed.segments and parsed.segments[0].anchor.precision=="rect"
    crop=crop_rect_png(original,1,parsed.segments[0].anchor.rect)
    with Image.open(io.BytesIO(crop)) as image:assert min(image.size)>0
    normalized=normalize_content_list([{"type":"text","text":"ABC123","bbox":[100,100,400,400],"page_idx":0}],layout=None,markdown=None,
        raw_payload=None,provider_task_id="already-known",backend_version="3.4.4",config_fingerprint="cfg",page_map=(1,),page_rotations={1:0})
    mapped=attach_preprocessing(normalized,prepared.manifest,prepared.quality_flags)
    assert mapped.segments[0].anchor.precision=="rect" and mapped.segments[0].anchor.page_rotation==90
    assert mapped.segments[0].anchor.preprocessing_transform["recognition_to_source_display"]==[1,0,0,0,1,0]
    assert mapped.config_fingerprint!=normalized.config_fingerprint


def test_quality_flags_are_suspicions_and_blank_page_not_glare():
    from app.documents.preprocessing import image_quality
    from PIL import ImageDraw,ImageFilter
    with Image.new("RGB",(1200,1200),"white") as blank:
        assert "image_glare_suspected" not in {f.code for f in image_quality(blank,page_no=1)}
        ImageDraw.Draw(blank).rectangle((0,0,300,1200),fill="black")
        flags={f.code for f in image_quality(blank,page_no=1)}
        assert {"image_glare_suspected","image_edge_content"}<=flags
        blurred=blank.filter(ImageFilter.GaussianBlur(30))
        try:assert "image_blur_suspected" in {f.code for f in image_quality(blurred,page_no=1)}
        finally:blurred.close()


def test_native_invoice_table_stays_after_title_in_reading_order():
    from reportlab.pdfgen import canvas
    from app.documents.parsing.native_pdf import NativePdfAdapter
    output = io.BytesIO()
    pdf = canvas.Canvas(output)
    pdf.drawString(50, 780, 'COMMERCIAL INVOICE')
    pdf.rect(50, 630, 420, 110)
    pdf.line(50, 690, 470, 690)
    pdf.line(260, 630, 260, 740)
    for x, y, label in [(60, 715, 'Invoice No.'), (270, 715, 'TEST-001'), (60, 665, 'Seller'), (270, 665, 'ACME Export')]:
        pdf.drawString(x, y, label)
    pdf.drawString(50, 580, 'PACKING LIST')
    pdf.drawString(50, 550, '12 cartons cotton towels')
    pdf.save()
    outcome = NativePdfAdapter().parse(output.getvalue())
    assert outcome.tables, '该回归必须实际解析出表格'
    assert outcome.segments[0].text == 'COMMERCIAL INVOICE'
    assert [s.reading_order for s in outcome.segments] == list(range(len(outcome.segments)))
    groups = split_document_segments(outcome.segments)
    assert [g['type_candidate'] for g in groups] == ['commercial_invoice', 'packing_list']
    assert all(s.kind == 'table_cell' for s in groups[0]['segments'][1:])
    assert [s.text for s in groups[0]['segments'][1:]] == ['Invoice No.', 'TEST-001', 'Seller', 'ACME Export']
    # 源段keys、原表格单元格引用及坐标仍可回到原图。
    assert all(s.table_cell and s.anchor.rect for s in groups[0]['segments'][1:])
    assert len({s.key for s in outcome.segments}) == len(outcome.segments)


def test_pdf_header_and_table_labels_do_not_create_extra_documents():
    from reportlab.pdfgen import canvas
    from app.documents.parsing.native_pdf import NativePdfAdapter
    output = io.BytesIO()
    pdf = canvas.Canvas(output)
    for page in range(2):
        pdf.drawString(50, 825, 'PL-TEST-001 | DOCUMENT SET')
        pdf.drawString(50, 775, 'PACKING LIST' + (' (continued)' if page else ''))
        pdf.rect(50, 600, 420, 120)
        pdf.line(50, 660, 470, 660)
        pdf.line(260, 600, 260, 720)
        for x, y, text in [(60, 690, 'Purchase order'), (270, 690, 'PO-001'),
                           (60, 630, 'Bill of lading'), (270, 630, 'BL-001')]:
            pdf.drawString(x, y, text)
        pdf.showPage()
    pdf.save()
    outcome = NativePdfAdapter().parse(output.getvalue())
    assert outcome.tables and any(s.kind == 'table_cell' for s in outcome.segments)
    groups = split_document_segments(outcome.segments)
    assert [g['document_type'] for g in groups] == ['packing_list']
    assert groups[0]['title'] == 'PACKING LIST'
    assert groups[0]['segments'] == list(outcome.segments)
    assert {s.anchor.page_no for s in groups[0]['segments']} == {1, 2}


def test_unknown_cover_body_is_not_silently_swallowed_by_next_title():
    segments = [SimpleNamespace(kind='paragraph', text='Additional terms and unclassified instructions',
        anchor=SimpleNamespace(page_no=1, rect=NormRect(x0=.1,y0=.01,x1=.8,y1=.05))),
        SimpleNamespace(kind='title', text='PACKING LIST',
        anchor=SimpleNamespace(page_no=1, rect=NormRect(x0=.1,y0=.1,x1=.8,y1=.2)))]
    assert [g['document_type'] for g in split_document_segments(segments)] == ['unknown', 'packing_list']
