from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from app.consignment.engine import compare_measurements, match_candidates, identifier
from app.consignment.dto import MatchScope, ScopePayload


def line(qty,unit="箱",*,sku="A1",lot="L1",spec="grade A",logical=None,measurements=None):
    return SimpleNamespace(id=uuid4(),logical_document_id=logical or uuid4(),sku=sku,lot_no=lot,specification=spec,
        quantity=Decimal(str(qty)) if qty is not None else None,quantity_unit=unit,measurements=measurements or [])


def scope(**changes):
    return {"cargo_id":"cargo-1","leg_id":"sea-1","quantity_kind":"quantity","completeness":"complete",**changes}


def test_complete_split_shipment_aggregates_decimal_without_edges():
    result=compare_measurements([line(100)],[line(40),line(58)],scope(),"confirmed")
    assert result["status"]=="failed" and result["severity"]=="red"
    assert result["left_total"]=="100" and result["right_total"]=="98" and result["delta"]=="2"


@pytest.mark.parametrize("condition",[{"completeness":"partial"},{"leg_id":None},{"cargo_id":None}])
def test_missing_scope_cannot_produce_red_conflict(condition):
    result=compare_measurements([line(100)],[line(98)],scope(**condition),"confirmed")
    assert result["status"]=="undetermined" and result["severity"]=="unknown"


def test_candidate_same_sku_does_not_establish_business_relationship():
    result=compare_measurements([line(100)],[line(98)],scope(),"candidate")
    assert result["status"]=="undetermined"


def test_different_units_and_quantity_kinds_are_never_added():
    result=compare_measurements([line(100,"箱")],[line(100,"件")],scope(),"confirmed")
    assert result["status"]=="undetermined"
    declared=line(None,measurements=[{"quantity_kind":"declared_quantity","value":"100","unit":"pcs"}])
    result=compare_measurements([declared],[line(100,"pcs")],scope(),"confirmed")
    assert result["status"]=="undetermined"


def test_weight_requires_explicit_precision_and_verified_tolerance():
    left=line(None,measurements=[{"quantity_kind":"net_weight","value":"12.00","unit":"kg"}])
    right=line(None,measurements=[{"quantity_kind":"net_weight","value":"12.01","unit":"kg"}])
    result=compare_measurements([left],[right],scope(quantity_kind="net_weight"),"confirmed")
    assert result["status"]=="undetermined"
    result=compare_measurements([left],[right],scope(quantity_kind="net_weight",absolute_tolerance="0.01"),"confirmed")
    assert result["status"]=="passed"


def test_exact_mass_conversion_preserves_original_units():
    left=line(None,measurements=[{"quantity_kind":"net_weight","value":"1000","unit":"kg"}])
    right=line(None,measurements=[{"quantity_kind":"net_weight","value":"1","unit":"tonne"}])
    result=compare_measurements([left],[right],scope(quantity_kind="net_weight",absolute_tolerance="0"),"confirmed")
    assert result["status"]=="passed" and result["unit"]=="kg"
    assert result["members"]["right"][0]["unit"]=="t"
    assert result["members"]["right"][0]["normalized_value"]=="1000"


def test_invalid_engine_tolerance_does_not_crash_worker():
    result=compare_measurements([line(100)],[line(98)],scope(absolute_tolerance="NaN"),"confirmed")
    assert result["status"]=="undetermined"


def test_many_to_many_matching_is_not_automatically_created():
    a,b=uuid4(),uuid4()
    assert match_candidates([line(10,logical=a),line(10,logical=a),line(10,logical=b),line(10,logical=b)])==[]


def test_missing_specification_does_not_hide_matching_sku_and_lot():
    assert len(match_candidates([line(10,spec='400 W'),line(10,spec=None)]))==1
    assert match_candidates([line(10,spec='400 W'),line(10,spec='500 W')])==[]
    assert match_candidates([line(10,lot='LOT-A'),line(10,lot='LOT-B')])==[]
    result=compare_measurements([line(400,'cajas')],[line(390,'cartons')],scope(),'confirmed')
    assert result['status']=='failed' and result['delta']=='10'
    assert compare_measurements([line(120,'pièces')],[line(120,'pcs')],scope(),'confirmed')['status']=='passed'


def test_number_normalization_preserves_leading_zero_and_punctuation():
    assert identifier(" 001-A ")=="001-A"
    assert identifier("001-A")!=identifier("1A")


def test_scope_rejects_arbitrary_memory_and_tolerance_cannot_be_nan():
    with pytest.raises(ValidationError):ScopePayload(materials=[],parties=[{"id":"x","label":"x","role":"seller","internal_key":"secret"}])
    with pytest.raises(ValidationError):MatchScope(cargo_id="x",leg_id="y",absolute_tolerance="NaN")
    with pytest.raises(ValidationError):MatchScope(cargo_id="x",leg_id="y",quantity_kind="unknown_measure")


def test_binding_compares_extraction_prompts_without_translation_or_qa_keys():
    from app.consignment.service import comparison_prompts
    extract={"document.extract":{"version":"1","sha256":"a"}}
    translation={**extract,"document.translate":{"version":"2"},"document.semantic_qa":{"version":"3"}}
    assert comparison_prompts(extract)==comparison_prompts(translation)
    changed={**translation,"document.extract":{"version":"2","sha256":"b"}}
    assert comparison_prompts(extract)!=comparison_prompts(changed)
