from decimal import Decimal
from app.documents.extraction import field_schema, extraction_batches, normalize_numeric, source_value_present


def test_exact_roles_and_no_invented_values():
    invoice=field_schema("commercial_invoice")
    roles={x["role"] for x in invoice["fields"]}
    assert {"seller","buyer","transport","goods","amounts"}<=roles
    assert "notify_party" not in roles
    assert "notify_party" in {x["role"] for x in field_schema("bill_of_lading")["fields"]}
    assert source_value_present("00012",["s1"],{"s1":"Invoice: 00012"})
    assert not source_value_present("12",["other"],{"s1":"Invoice: 00012"})


def test_numeric_no_currency_or_locale_guess():
    assert normalize_numeric("120.00")==Decimal("120.00")
    assert normalize_numeric("USD 120.00") is None
    assert normalize_numeric("1,000.00") is None
    assert normalize_numeric("NaN") is None


def test_extraction_batches_bounded_and_ordered():
    sources=[{"id":str(index),"text":"Line"} for index in range(41)]
    batches=list(extraction_batches(sources))
    assert [len(batch) for batch in batches]==[40,1]
    assert [x for batch in batches for x in batch]==sources


def test_serialized_subbatches_preserve_rows_and_completed_batch_keys():
    import json
    from app.documents.extraction import bounded_extraction_batches
    from app.prompts.registry import estimate_tokens
    sources = [{"id":str(index), "text":"SKU", "structure":"table_cell", "table_row":index // 2,
                "table_col":index % 2, "table_key":"x" * 300, "section_id":"s"} for index in range(8)]
    batches=list(bounded_extraction_batches(sources, max_input_tokens=1100))
    assert [row for _, batch in batches for row in batch] == sources
    assert all(estimate_tokens(json.dumps(batch, ensure_ascii=False)) <= 1100 for _, batch in batches)
    assert all(len(batch) % 2 == 0 for _, batch in batches)
    small=[{"id":"s","text":"short"}]
    assert list(bounded_extraction_batches(small, max_input_tokens=1100)) == [("0",small)]


def test_party_role_labels_preserve_multilingual_combined_roles():
    from app.documents.extraction import party_role_labels
    for seller, buyer in (("Seller / Shipper", "Buyer / Consignee"),
            ("卖方／发货人", "买方／收货人"), ("Vendeur / Expéditeur", "Acheteur / Destinataire"),
            ("Vendedor / expedidor", "Comprador / destinatario"),
            ("Продавец / Грузоотправитель", "Покупатель / Грузополучатель")):
        assert {value["role"] for value in party_role_labels(seller)} == {"seller", "shipper"}
        assert {value["role"] for value in party_role_labels(buyer)} == {"buyer", "consignee"}
    assert party_role_labels("The seller delivered the goods") == []
    assert party_role_labels("Seller: China Demo Trading Co.") == []
    assert party_role_labels("Vendedor / unclear") == []


def _party_segment(identifier, text, *, table=None, row=None, col=None, page=1, section="s"):
    return {"id": identifier, "text": text, "structure": "table_cell" if table else "paragraph",
            "table_key": table, "table_row": row, "table_col": col, "page_no": page, "section_id": section}


def test_spanish_party_tables_correct_model_role_from_explicit_source():
    from app.documents.extraction import annotate_party_roles, resolve_party_role
    material = annotate_party_roles([
        _party_segment("seller-heading", "Vendedor / expedidor"),
        _party_segment("seller-label", "Razón social", table="nt1", row=0, col=0),
        _party_segment("seller-name", "Huerta Clara Demo S.L.", table="nt1", row=0, col=1),
        _party_segment("buyer-heading", "Comprador / destinatario"),
        _party_segment("buyer-label", "Razón social", table="nt2", row=0, col=0),
        _party_segment("buyer-name", "Shanghai Freshway Demo Foods Co., Ltd.", table="nt2", row=0, col=1),
    ])
    by_id = {value["id"]: value for value in material}
    assert by_id["seller-name"]["party_role_context"] == ["seller", "shipper"]
    assert by_id["buyer-name"]["party_role_context"] == ["buyer", "consignee"]
    assert by_id["buyer-name"]["party_role_evidence"] == [
        {"role": "buyer", "label": "Comprador", "source_segment_id": "buyer-heading"},
        {"role": "consignee", "label": "destinatario", "source_segment_id": "buyer-heading"},
    ]
    allowed = {"seller", "buyer", "signatory"}
    assert resolve_party_role("seller", by_id["buyer-name"]["text"], ["buyer-name"], by_id, allowed) == "buyer"
    assert resolve_party_role("seller", by_id["seller-name"]["text"], ["seller-name"], by_id, allowed) == "seller"
    assert resolve_party_role("seller", by_id["buyer-name"]["text"], ["buyer-name"], by_id, {"seller"}) is None


def test_party_context_does_not_cross_table_page_section_or_unrelated_paragraph():
    from app.documents.extraction import annotate_party_roles
    material = annotate_party_roles([
        _party_segment("heading", "Seller"),
        _party_segment("one", "A Ltd", table="one", row=0, col=0),
        _party_segment("two", "B Ltd", table="two", row=0, col=0),
        _party_segment("page-heading", "Buyer"),
        _party_segment("next-page", "C Ltd", table="three", row=0, col=0, page=2),
        _party_segment("section-heading", "Buyer", page=2),
        _party_segment("next-section", "D Ltd", table="four", row=0, col=0, page=2, section="other"),
        _party_segment("last-heading", "Buyer", page=2, section="other"),
        _party_segment("interrupt", "Goods description", page=2, section="other"),
        _party_segment("disconnected", "E Ltd", table="five", row=0, col=0, page=2, section="other"),
    ])
    by_id = {value["id"]: value for value in material}
    assert by_id["one"]["party_role_context"] == ["seller"]
    for key in ("two", "next-page", "next-section", "disconnected"):
        assert "party_role_context" not in by_id[key]


def test_party_columns_and_conflicting_source_roles():
    from app.documents.extraction import annotate_party_roles, resolve_party_role
    material = annotate_party_roles([
        _party_segment("h0", "Seller", table="one", row=0, col=0),
        _party_segment("h1", "Buyer", table="one", row=0, col=1),
        _party_segment("s0", "Same Company", table="one", row=1, col=0),
        _party_segment("s1", "Same Company", table="one", row=1, col=1),
    ])
    by_id = {value["id"]: value for value in material}
    assert by_id["s0"]["party_role_context"] == ["seller"]
    assert by_id["s1"]["party_role_context"] == ["buyer"]
    assert resolve_party_role("seller", "Same Company", ["s1"], by_id, {"seller", "buyer"}) == "buyer"
    assert resolve_party_role("seller", "Same Company", ["s0", "s1"], by_id, {"seller", "buyer"}) is None
    assert resolve_party_role("seller", "Same Company", ["absent"], by_id, {"seller", "buyer"}) == "seller"
