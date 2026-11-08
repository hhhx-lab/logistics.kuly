import copy
from decimal import Decimal

import pytest

from app.review.rules import RULE_PACK, RULE_PACK_V2, evaluate_rules
from app.review.jurisdiction import published_rule_packs


def evaluate(fields=None,lines=None,sources=None,documents=None,pack=RULE_PACK_V2):
    return evaluate_rules(documents=documents or [],fields=fields or [],lines=lines or [],source_rows=sources or [],rule_pack=pack)


@pytest.mark.parametrize('key,value,expected',[
    ('phone','+86 (21) 1234-5678 ext 23','passed'),('phone','1234567)(','undetermined'),
    ('phone','call me','undetermined'),('phone','1234','undetermined'),
    ('invoice_date','2024-02-29','passed'),('invoice_date','2025-02-29','undetermined'),
    ('document_date','02/10/2026','undetermined'),('on_board_date','2026年10月2日','passed'),
    ('currency','USD','passed'),('currency','ZZZ','passed'),('currency','usd','undetermined'),
    ('currency','$','undetermined'),('hs_code','080810','passed'),('hs_code','08081010','undetermined'),
    ('hs_code','0808.10','undetermined'),('package_unit','CTNS','passed'),('weight_unit','KG','passed'),
    ('quantity_unit','cartons + pallets','undetermined'),('weight_unit','unknown','undetermined'),
])
def test_source_backed_lexical_candidates_and_limits(key,value,expected):
    row={'id':'field','logical_document_id':'logical','field_key':key,'value':value,'source_text':value,'source_segment_ids':['source']}
    source={'id':'source','logical_document_id':'logical','text':'原件字段：'+value}
    result=next(rule for rule in evaluate([row],sources=[source]) if rule.rule_id=='field.lexical.'+key)
    assert result.status==expected and not result.blocks_export
    assert result.calculation['candidate_only'] is True and result.calculation['assessment']=='lexical_only'


def test_lexical_values_require_complete_correct_source_and_string_hs():
    row={'id':'f','logical_document_id':'L','field_key':'hs_code','value':'080810','source_segment_ids':['s']}
    for source,value in [(None,'080810'),({'id':'s','logical_document_id':'OTHER','text':'080810'},'080810'),({'id':'s','logical_document_id':'L','text':'10808100'},'080810'),({'id':'s','logical_document_id':'L','text':'080810'},80810)]:
        result=next(rule for rule in evaluate([{**row,'value':value}],sources=[source] if source else []) if rule.rule_id=='field.lexical.hs_code')
        assert result.status=='undetermined'
    legacy=evaluate([row],sources=[{'id':'s','logical_document_id':'L','text':'080810'}],pack=RULE_PACK)
    assert not any(rule.rule_id.startswith('field.lexical') for rule in legacy)
    assert not any(rule.rule_id=='packing.package_total' for rule in legacy)


def packing_table(total='5'):
    cells=[('Complete consignment packing detail','CTNS'),('Cotton shirts','2'),('Cotton trousers','3'),('TOTAL',total)]
    sources=[{'id':f's{row}{column}','logical_document_id':'L','section_id':'SECTION','structure':'table_cell','table_row':row,'table_col':column,'text':value} for row,pair in enumerate(cells) for column,value in enumerate(pair)]
    lines=[{'id':f'line{row}','logical_document_id':'L','document_type':'packing_list','package_count':Decimal(value),'package_unit':'CTNS','source_segment_ids':[f's{row}0',f's{row}1']} for row,value in [(1,'2'),(2,'3')]]
    documents=[{'id':'parse','page_count':1,'seen_pages':[1],'segment_count':8,'logical_documents':[{'logical_document_id':'L','document_type':'packing_list'}]}]
    return documents,lines,sources


def total_result(documents,lines,sources):
    return next(rule for rule in evaluate(lines=lines,sources=sources,documents=documents) if rule.rule_id=='packing.package_total')


def test_packing_complete_one_layer_sum_and_red_conflict():
    documents,lines,sources=packing_table()
    result=total_result(documents,lines,sources)
    assert result.status=='passed' and result.calculation['computed']=='5' and result.calculation['packaging_level']=='carton'
    sources[-1]['text']='6';result=total_result(documents,lines,sources)
    assert result.status=='failed' and result.blocks_export and result.severity=='red'
    assert result.calculation['difference']=='1'


@pytest.mark.parametrize('defect',[
    'unconfirmed_scope','missing_line','duplicate_line','wrong_unit','wrong_value','fractional_count',
    'missing_source','cross_row_source','duplicate_position','other_table','no_total','subtotal','partial','mixed_layer','row_gap','no_structure','extra_column','scientific_count',
])
def test_packing_never_sums_incomplete_or_mixed_basis(defect):
    documents,lines,sources=packing_table()
    if defect=='unconfirmed_scope':sources[0]['text']='Packing detail'
    elif defect=='missing_line':lines.pop()
    elif defect=='duplicate_line':lines.append(copy.deepcopy(lines[0]))
    elif defect=='wrong_unit':lines[1]['package_unit']='PLTS'
    elif defect=='wrong_value':lines[1]['package_count']=Decimal(4)
    elif defect=='fractional_count':sources[3]['text']='2.5';lines[0]['package_count']=Decimal('2.5')
    elif defect=='missing_source':lines[0]['source_segment_ids'].pop()
    elif defect=='cross_row_source':lines[0]['source_segment_ids'].append('s21')
    elif defect=='duplicate_position':sources.append(copy.deepcopy(sources[0]))
    elif defect=='other_table':sources.extend([{**row,'id':row['id']+'other','section_id':'OTHER'} for row in sources])
    elif defect=='no_total':sources[-2]['text']='Freight'
    elif defect=='subtotal':sources[2]['text']='SUBTOTAL'
    elif defect=='partial':sources.append({'id':'partial','logical_document_id':'L','text':'Partial shipment','structure':'paragraph'})
    elif defect=='mixed_layer':sources[2]['text']='2 pallets'
    elif defect=='row_gap':sources[2]['table_row']=8
    elif defect=='no_structure':sources[0]['structure']='paragraph'
    elif defect=='extra_column':sources.append({**sources[0],'id':'extra','table_col':2})
    elif defect=='scientific_count':sources[3]['text']='2e0'
    result=total_result(documents,lines,sources)
    assert result.status=='undetermined' and not result.blocks_export


def test_uncontrolled_party_mapping_is_not_a_published_v2_feature():
    fields=[{'id':'seller','logical_document_id':'A','field_key':'name','role':'seller','value':'ACME','entity_group':'fake'}, {'id':'shipper','logical_document_id':'B','field_key':'name','role':'shipper','value':'OTHER','entity_group':'fake'}]
    result=evaluate(fields)
    assert next(rule for rule in result if rule.rule_id=='party.role_mapping').status=='not_covered'
    assert not any(rule.rule_id=='party.confirmed_mapping' for rule in result)
    assert next(rule for rule in evaluate(fields,pack=RULE_PACK) if rule.rule_id=='party.confirmed_mapping').status=='failed'
    assert {pack['version'] for pack in published_rule_packs()} >= {RULE_PACK,RULE_PACK_V2}
