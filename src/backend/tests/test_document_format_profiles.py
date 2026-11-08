from types import SimpleNamespace
from decimal import Decimal
import pytest

from app.documents.type_catalog import detect_document_type, catalog_view
from app.documents.preprocessing import split_document_segments
from app.documents.extraction import annotate_extraction_structure, build_line_values, field_schema, normalize_evidence_lines
from app.documents.extraction import structured_table_lines, merge_table_candidates
from app.documents.edi import read_iftmin_segments, EdiSyntaxError
from app.prompts.schemas import ExtractedCell


@pytest.mark.parametrize("title,code", [
    ("FACTURE COMMERCIALE", "commercial_invoice"), ("FACTURA COMERCIAL", "commercial_invoice"),
    ("КОММЕРЧЕСКИЙ СЧЁТ", "commercial_invoice"), ("LISTE DE COLISAGE", "packing_list"),
    ("Lista de embalaje", "packing_list"), ("Упаковочный лист", "packing_list"),
    ("Connaissement maritime", "bill_of_lading"), ("Declaración aduanera", "customs_declaration"),
    ("Таможенная декларация", "customs_declaration"), ("Certificat phytosanitaire pour la réexportation", "phytosanitary_reexport"),
])
def test_multilingual_titles_use_specific_type(title, code):
    assert detect_document_type(title, standalone=True) == code


def test_titles_do_not_guess_from_other_document_references():
    assert detect_document_type("This invoice refers to a packing list") == "unknown"
    assert detect_document_type("facture commerciale et liste de colisage") == "unknown"
    assert detect_document_type("предконосаментный документ") == "unknown"
    view = catalog_view()
    assert all(item["capability_state"] == "directory_supported" and not item["verified_sample_ids"] for item in view["items"])


def test_explicit_continuation_retains_sources_but_new_same_type_document_splits():
    items = [SimpleNamespace(kind="title", text="PACKING LIST"), SimpleNamespace(kind="text", text="Cartons 1-5"),
        SimpleNamespace(kind="title", text="PACKING LIST (continued)"), SimpleNamespace(kind="text", text="Cartons 6-10"),
        SimpleNamespace(kind="title", text="PACKING LIST"), SimpleNamespace(kind="text", text="Different batch")]
    groups = split_document_segments(items)
    assert [len(group["segments"]) for group in groups] == [4, 2]
    assert groups[0]["segments"][2] is items[2]


def test_repeated_headers_and_subtotals_never_become_cargo_rows():
    material = []
    for row, cells in enumerate([("SKU", "Quantity"), ("A-01", "10"), ("SKU", "Quantity"), ("B-02", "20"), ("Subtotal", "30")]):
        for col, text in enumerate(cells):
            material.append({"id": f"s{row}-{col}", "text": text, "structure": "table_cell", "section_id": "sec", "table_key": "table", "table_row": row, "table_col": col, "page_no": 1 if row < 2 else 2})
    result = annotate_extraction_structure(material)
    assert [s["extraction_row_kind"] for s in result[::2]] == ["header", "detail", "header", "detail", "summary"]
    assert result[6]["table_header_source_segment_ids"] == ["s2-0", "s2-1"]
    assert "extraction_row_kind" not in material[0]
    assert result[6]["page_no"] == 2
    source_map = {s["id"]: s["text"] for s in material}
    cells = [ExtractedCell(column_key="quantity", raw_value="30", source_segment_ids=["s4-1"])]
    assert build_line_values(cells, ["s4-0", "s4-1"], source_map, {s["id"]: s for s in result}) is None
    outside = annotate_extraction_structure([{"id": "summary", "text": "Subtotal: 60 cartons", "structure": "paragraph"}])
    assert outside[0]["extraction_row_kind"] == "summary"


def test_multiple_measurements_keep_units_and_exact_source_evidence():
    inputs = {"sku": "00003", "lot_no": "LOT-A", "quantity": "10", "quantity_unit": "cartons",
        "statutory_quantity_1": "24", "statutory_quantity_1_unit": "kg", "net_weight": "23.5", "net_weight_unit": "kg",
        "chargeable_weight": "26", "chargeable_weight_unit": "kg", "country_of_origin": "France"}
    cells = [ExtractedCell(column_key=key, raw_value=value, source_segment_ids=[key]) for key, value in inputs.items()]
    values = build_line_values(cells, list(inputs), inputs)
    assert values["sku"] == "00003" and values["quantity"] == Decimal("10")
    assert values["cell_sources"]["sku"] == ["sku"]
    measures = {m["quantity_kind"]: m for m in values["measurements"]}
    assert measures["statutory_quantity_1"]["value"] == "24"
    assert measures["statutory_quantity_1"]["unit_source_segment_ids"] == ["statutory_quantity_1_unit"]
    assert measures["quantity"]["unit"] == "cartons" and measures["net_weight"]["unit"] == "kg"
    assert "statutory_quantity_1" not in values
    invented = ExtractedCell(column_key="sku", raw_value="999", source_segment_ids=["sku"])
    assert build_line_values([invented], list(inputs), inputs) is None
    schema = field_schema("customs_declaration")
    assert {"sku", "specification", "statutory_quantity_2", "package_range"} <= set(schema["line_columns"])


def test_missing_units_and_ambiguous_numeric_values_stay_unknown():
    cell = ExtractedCell(column_key="net_weight", raw_value="1,200", source_segment_ids=["s1"])
    value = build_line_values([cell], ["s1"], {"s1": "1,200"})
    assert "net_weight" not in value
    assert value["measurements"][0]["value"] is None and value["measurements"][0]["unit"] is None


def test_structured_rows_fill_missing_model_sku_without_counting_summary_or_allocation():
    material=[]
    for row, cells in enumerate([('SKU','Lot','Quantity','Unit','Net kg'),
            ('A-01','LOT-A','400','cajas','3200'),('B-01','LOT-B','200','cajas','1000'),
            ('Total','600','4200','','')]):
        for col,text in enumerate(cells):
            material.append({'id':f'{row}-{col}','text':text,'structure':'table_cell','section_id':'sec',
                'table_key':'goods','page_no':2,'table_row':row,'table_col':col})
    annotated=annotate_extraction_structure(material)
    rows=structured_table_lines(annotated)
    # 不接受没有真实SKU/数字的汇总作为货物；分配附表无SKU+数量表头也不生成货物。
    assert len(rows)==2
    model=type(rows[0])(row_source_segment_ids=['1-2'],cells=[ExtractedCell(column_key='quantity',raw_value='400',source_segment_ids=['1-2'])])
    assert merge_table_candidates([model],rows)==rows
    source_map={s['id']:s['text'] for s in annotated};structure={s['id']:s for s in annotated}
    value=build_line_values(rows[0].cells,rows[0].row_source_segment_ids,source_map,structure)
    assert value['sku']=='A-01' and value['quantity']==Decimal(400)
    assert value['net_weight']==Decimal(3200) and value['net_weight_unit']=='kg'
    assert value['cell_sources']['net_weight_unit']==['0-4']
    assert '0-4' not in rows[0].row_source_segment_ids


@pytest.mark.parametrize("unit", ["cajas", "caisses", "коробки", "箱", "boxes"])
def test_multilingual_summary_units_do_not_become_goods_and_total_sku_is_preserved(unit):
    material = []
    for row, cells in enumerate([("SKU", "Producto", "Cantidad", "Unidad"),
            ("FRA-8", "Fresas frescas", "400", unit),
            ("ARA-5", "Arándanos frescos", "200", unit),
            ("Total", "", "600", unit),
            ("Total", "50 ml", "10", unit)]):
        for col, text in enumerate(cells):
            material.append({"id":f"{row}-{col}", "text":text, "structure":"table_cell", "section_id":"s",
                "table_key":"goods", "page_no":1, "table_row":row, "table_col":col})
    annotated = annotate_extraction_structure(material)
    assert annotated[12]["extraction_row_kind"] == "summary"
    assert annotated[16]["extraction_row_kind"] == "detail"
    rows = structured_table_lines(annotated)
    assert len(rows) == 3
    assert [next(cell.raw_value for cell in row.cells if cell.column_key == "sku") for row in rows] == ["FRA-8", "ARA-5", "Total"]
    outside = annotate_extraction_structure([{"id":"summary", "text":f"Total: 600 {unit}", "structure":"paragraph"}])
    assert outside[0]["extraction_row_kind"] == "summary"


def edi(message="FTX+AAI+++Reference A?+B?:C?'D'", count=3):
    return "UNA:+.? 'UNB+UNOC:3+SENDER+RECEIVER+261014:1200+IC1'UNH+M1+IFTMIN:D:01B:UN'" + message + f"UNT+{count}+M1'UNZ+1+IC1'"


def test_edi_reader_release_source_offsets_and_envelope():
    text = edi()
    parsed = read_iftmin_segments(text)
    ftx = parsed.segments[2]
    assert ftx.elements[-1] == ("Reference A+B:C'D",)
    assert all(text[s.character_start:s.character_end] == s.raw for s in parsed.segments)
    assert parsed.capability_state == "pending_official_verification"


def test_edi_raw_source_ranges_preserve_trailing_segment_whitespace():
    text = edi("FTX+AAI+++Value \t'").replace("IC1'UNH", "IC1 \t'\r\nUNH")
    parsed = read_iftmin_segments(text)
    assert all(text[s.character_start:s.character_end] == s.raw for s in parsed.segments)
    assert parsed.segments[0].raw.endswith("IC1 \t'")
    assert parsed.segments[2].raw.endswith("Value \t'")


@pytest.mark.parametrize("mutate", [
    lambda t: t.replace("UNT+3", "UNT+5"), lambda t: t.replace("UNZ+1+IC1", "UNZ+1+OTHER"),
    lambda t: t.replace("IFTMIN:D:01B", "IFTMIN:D:96A"), lambda t: t[:-1],
])
def test_edi_invalid_counts_versions_and_termination_fail_closed(mutate):
    with pytest.raises(EdiSyntaxError):
        read_iftmin_segments(mutate(edi()))


def test_worker_extract_closure_persists_extended_cells_and_source_metadata(monkeypatch):
    from contextlib import contextmanager
    from uuid import uuid4
    from app.documents import evidence_models as em
    from app.workflows.worker import DocumentWorker
    from app.prompts.registry import PromptRegistry
    from app.prompts.schemas import ExtractionResult, ExtractedLineItem
    from app.infra.model_gateway import Succeeded, CallBudget

    logical_id, parse_id, section_id = uuid4(), uuid4(), uuid4()
    rows = [SimpleNamespace(id=uuid4(), text=text, structure="table_cell", section_id=section_id,
        table_row=1, table_col=index) for index, text in enumerate(("00003", "10", "cartons"))]
    goods_rows = list(rows)
    rows.extend([
        SimpleNamespace(id=uuid4(), text="Comprador / destinatario", structure="paragraph", section_id=section_id,
            table_row=None, table_col=None),
        SimpleNamespace(id=uuid4(), text="Shanghai Freshway Demo Foods Co., Ltd.", structure="table_cell", section_id=section_id,
            table_row=0, table_col=1),
    ])
    logical = SimpleNamespace(id=logical_id, parse_artifact_id=parse_id)
    anchors = [SimpleNamespace(source_segment_id=row.id, page_no=2,
        transform={"table_cell": {"table_key": "table1" if row in goods_rows else "party1"}} if row.structure == "table_cell" else {}) for row in rows]
    result = ExtractionResult(fields=[{"field_key":"name", "role":"seller", "raw_value":rows[-1].text,
        "source_segment_ids":[str(rows[-1].id)], "confidence_label":"high", "ambiguity":None}], line_items=[ExtractedLineItem(
        row_source_segment_ids=[str(row.id) for row in goods_rows], cells=[
            ExtractedCell(column_key=key, raw_value=row.text, source_segment_ids=[str(row.id)])
            for key, row in zip(("sku", "quantity", "quantity_unit"), goods_rows)])])
    result = result.model_copy(update={"line_items": [result.line_items[0], result.line_items[0]]})
    saved, captured = [], []
    class Db:
        def scalars(self, statement):
            expression = statement.column_descriptions[0]["expr"]
            if expression is em.SourceSegment: return rows
            if expression is em.SourceAnchor: return anchors
            if expression is em.LogicalDocument: return [logical]
            return [logical_id]
        def get(self, model, value): return logical
        def scalar(self, statement): return 0
        def add(self, obj): saved.append(obj)
        def flush(self): pass
    class Gateway:
        async def structured_generate(self, binding, prompts, *args, **kwargs):
            captured.append(prompts[0])
            return Succeeded(result, (), False)
        async def aclose(self): pass
    monkeypatch.setattr("app.workflows.worker.build_model_gateway", lambda *args, **kwargs: Gateway())
    worker = DocumentWorker.__new__(DocumentWorker)
    worker.tenant, worker.case_id, worker.run_id = uuid4(), uuid4(), uuid4()
    worker.parse_ids, worker.input_parse_ids, worker.input_business_id = [parse_id], [], None
    worker.purpose, worker.settings, worker.factory = "initial", SimpleNamespace(model_max_attempts=1, model_retry_total_timeout_seconds=30), lambda: None
    worker.lease, worker.fence = uuid4(), 1
    worker.single_type_input = lambda *args: False
    worker.confirmed_type = lambda *args: "commercial_invoice"
    worker.confirmed_cargo_profiles = lambda *args: []
    worker.registry = PromptRegistry.load()
    worker.prompt_versions = worker.registry.freeze(["document.extract"])
    worker.bindings = [{"role": "extract", "base_url": "https://fixture.invalid/v1", "model_name": "fixture",
        "protocol": "openai_compatible", "timeout_seconds": 30, "max_output_tokens": 3000,
        "max_concurrency": 1, "public_config_hash": "0" * 64, "adapter_version": "fixture"}]
    worker.ensure_time = worker.submit_intent = worker.complete_step = worker.record_model_response = lambda *args: None
    worker.locked = lambda *args: None
    worker.model_budget = lambda *args: CallBudget(max_calls=1)
    worker.request_key = lambda *args: "fixture"
    worker.step = lambda name, payload, operation: operation(uuid4())
    @contextmanager
    def transaction(): yield Db()
    worker.transaction = transaction
    def immediate(coro):
        try: coro.send(None)
        except StopIteration as stop: return stop.value
        raise AssertionError("Unexpected asynchronous external I/O")
    worker.runner = SimpleNamespace(run=immediate)
    worker.extract_fields({})
    line = next(obj for obj in saved if isinstance(obj, em.LineItem))
    assert len([obj for obj in saved if isinstance(obj, em.LineItem)]) == 1
    assert line.sku == "00003" and line.quantity == Decimal("10")
    assert line.measurements[0]["unit"] == "cartons" and line.page_start == line.page_end == 2
    assert line.cell_sources["sku"] == [str(rows[0].id)]
    assert captured[0].version == 2 and captured[0].variables["segments"][0]["table_key"] == "table1"
    party = next(obj for obj in saved if isinstance(obj, em.FieldAssertion))
    assert party.role == "buyer" and party.group == "parties"
    assert party.source_text == rows[-1].text and party.source_segment_ids == [str(rows[-1].id)]
    assert worker.registry.get("document.extract", 1).version == 1


def test_large_numeric_and_units_preserve_raw_without_database_rounding():
    cells = [ExtractedCell(column_key="quantity", raw_value="100000000000000", source_segment_ids=["number"]),
        ExtractedCell(column_key="quantity_unit", raw_value="very long original unit", source_segment_ids=["unit"])]
    values = build_line_values(cells, ["number", "unit"], {"number": "100000000000000", "unit": "very long original unit"})
    assert "quantity" not in values and "quantity_unit" not in values
    assert values["measurements"][0]["value"] == "100000000000000"
    assert values["measurements"][0]["unit"] == "very long original unit"


def test_duplicate_model_candidates_use_exact_cell_evidence_not_quantity():
    from uuid import uuid4
    logical, parse = uuid4(), uuid4()
    def line(number, source="row1"):
        return SimpleNamespace(id=uuid4(), line_no=number, logical_document_id=logical, parse_artifact_id=parse,
            sku="00003", lot_no="LOT-A", specification="CT-40", quantity=Decimal("100.000000"), quantity_unit="cartons",
            cell_sources={key: [source] for key in ("sku", "lot_no", "specification", "quantity", "quantity_unit")},
            measurements=[{"quantity_kind": "quantity", "raw_value": "100", "value": "100", "unit": "cartons"}])
    first, duplicate, different_real_row = line(1), line(2), line(3, "row2")
    source_map = {"row1": "00003 CT-40 LOT-A 100 cartons", "row2": "00003 CT-40 LOT-A 100 cartons"}
    kept, aliases = normalize_evidence_lines([first, duplicate, different_real_row], source_map)
    assert kept == [first, different_real_row]
    assert aliases == {str(duplicate.id): str(first.id)}
    assert duplicate.quantity == Decimal("100") and duplicate.cell_sources["quantity"] == ["row1"]
    # 同一聚合源段真正包含两行时，没有行／字符位置就无法证明候选是重复。
    aggregated = {"row1": source_map["row1"] + "\n" + source_map["row1"]}
    kept, aliases = normalize_evidence_lines([first, duplicate], aggregated)
    assert kept == [first, duplicate] and aliases == {}


def test_same_physical_table_row_keeps_richer_candidate_without_recounting():
    from uuid import uuid4
    parse,logical=uuid4(),uuid4()
    def line(ids,sku,value):
        return SimpleNamespace(id=uuid4(),parse_artifact_id=parse,logical_document_id=logical,line_no=len(ids),
            source_segment_ids=ids,sku=sku,quantity=Decimal(value),quantity_unit='pcs',
            cell_sources={'quantity':['qty'],**({'sku':['sku']} if sku else {})},
            measurements=[{'quantity_kind':'quantity','raw_value':value,'value':value,'unit':'pcs'}])
    partial,full=line(['qty'],None,'20'),line(['sku','qty'],'SNS-T8','20')
    source={'sku':'SNS-T8','qty':'20'};positions={'sku':('sec',2,'goods',4),'qty':('sec',2,'goods',4)}
    kept,aliases=normalize_evidence_lines([partial,full],source,positions)
    assert kept==[full] and aliases=={str(partial.id):str(full.id)}
    conflicting=line(['qty'],None,'2')
    kept,aliases=normalize_evidence_lines([conflicting,full],source,positions)
    assert len(kept)==2 and not aliases
