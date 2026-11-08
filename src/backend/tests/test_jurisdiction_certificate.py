from copy import deepcopy
from uuid import uuid4

import pytest

from app.review.jurisdiction import CERTIFICATE_PACK, evaluate_certificate, published_rule_packs
from app.review.rules import RULE_PACK, evaluate_rules
from app.workflows.commands import normalize_parameters


def fixture():
    document=str(uuid4());cert=str(uuid4())
    facts={"origin_country":"CN","dispatch_country":"CN","destination_country":"DE","eu_plant_health_territory":True,
           "movement":"ordinary_import","commodity":"fresh_apples","stated_code":"080810","import_date":"2026-10-02",
           "material_set_complete":True,"declaration_reason":"Synthetic shipment declaration and complete uploaded source set"}
    rows=[{"id":"goods","text":"Fresh apples HS 080810","document_id":document,"document_version":1},
          {"id":"cert","text":"PHYTOSANITARY CERTIFICATE","document_id":cert,"document_version":1}]
    docs=[{"id":"parse","segment_count":2,"page_count":1,"seen_pages":[1],"quality_flags":[],"is_synthetic":False}]
    return facts,rows,docs


def test_material_provided_missing_unknown_and_five_fruit_exemption():
    facts,rows,docs=fixture()
    missing=evaluate_certificate(facts,rows[:1],docs)
    assert missing.status=="failed" and missing.blocks_export and missing.source_segment_ids==("goods",)
    provided=evaluate_certificate({**facts,"certificate_source":{"document_id":rows[1]["document_id"],"document_version":1},"certificate_same_consignment":True},rows,docs)
    assert provided.status=="passed" and provided.calculation["evidence"]["material_status"]=="provided"
    assert provided.calculation["authenticity"]=="not_assessed" and provided.calculation["legal_review_status"]=="pending"
    for key in ["origin_country","movement","commodity","eu_plant_health_territory","import_date"]:
        assert evaluate_certificate({**facts,key:None},rows,docs).status=="undetermined"
    for commodity in ["bananas","pineapples","coconuts","durians","dates"]:
        assert evaluate_certificate({**facts,"commodity":commodity,"stated_code":None},[{**rows[0],"text":commodity}],docs).status=="not_applicable"
        assert evaluate_certificate({**facts,"commodity":commodity,"stated_code":None},rows,docs).status=="undetermined"
    assert evaluate_certificate({**facts,"import_date":"2026-10-15"},rows,docs).status=="not_covered"
    assert evaluate_certificate({**facts,"destination_country":"US"},rows,docs).status=="not_covered"
    assert evaluate_certificate({**facts,"movement":"transit_or_special"},rows,docs).status=="not_covered"
    assert evaluate_certificate({**facts,"commodity":"other"},rows,docs).status=="not_covered"


def test_missing_document_or_ambiguous_title_cannot_pass_or_fail():
    facts,rows,docs=fixture()
    provided={**facts,"certificate_source":{"document_id":rows[1]["document_id"],"document_version":1},"certificate_same_consignment":True}
    for text in ["No phytosanitary certificate provided", "Invoice: phytosanitary certificate needed"]:
        altered=deepcopy(rows);altered[1]["text"]=text
        assert evaluate_certificate(provided,altered,docs).status=="undetermined"
    assert evaluate_certificate({**provided,"certificate_same_consignment":False},rows,docs).status=="undetermined"
    assert evaluate_certificate({**facts,"material_set_complete":False},rows[:1],docs).status=="undetermined"
    assert evaluate_certificate(facts,rows,docs).status=="undetermined"
    assert evaluate_certificate(facts,rows[:1],[{**docs[0],"quality_flags":["ocr_unavailable"]}]).status=="undetermined"
    assert evaluate_certificate(facts,rows[:1],[{**docs[0],"page_count":2}]).status=="undetermined"
    assert evaluate_certificate(facts,[{**rows[0],"text":"Apple juice HS 080810"}],docs).status=="undetermined"


def test_pack_is_explicit_and_core_result_is_unchanged():
    facts,rows,docs=fixture()
    before=evaluate_rules(documents=docs,fields=[],lines=[],rule_pack=RULE_PACK)
    after=evaluate_rules(documents=docs,fields=[],lines=[],rule_pack=RULE_PACK,jurisdiction_inputs=facts,source_rows=rows)
    assert before==after and all(r.rule_id!="jurisdiction.phytosanitary_certificate" for r in after)
    scoped=evaluate_rules(documents=docs,fields=[],lines=[],rule_pack=CERTIFICATE_PACK,jurisdiction_inputs=facts,source_rows=rows[:1])
    assert next(r for r in scoped if r.rule_id=="jurisdiction.phytosanitary_certificate").status=="failed"
    params,_,_=normalize_parameters("translate_review",{"rule_pack_version":CERTIFICATE_PACK,"jurisdiction_inputs":facts,"target_language":"en"})
    assert params["jurisdiction_inputs"]["import_date"]=="2026-10-02"
    with pytest.raises(Exception):normalize_parameters("translate_review",{"jurisdiction_inputs":facts})
    published=next(pack for pack in published_rule_packs() if pack['version']==CERTIFICATE_PACK)
    assert published["parameter_schema"]["properties"]["certificate_source"]
