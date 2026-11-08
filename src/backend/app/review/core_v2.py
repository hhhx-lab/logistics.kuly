"""Small source-backed lexical checks and an explicitly complete packing table."""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from decimal import Decimal

from app.review.rules import RuleResult, decimal_value

PACKAGING_UNITS = {
    'ctn':'carton','ctns':'carton','carton':'carton','cartons':'carton','箱':'carton',
    'pc':'piece','pcs':'piece','piece':'piece','pieces':'piece','件':'piece',
    'plt':'pallet','plts':'pallet','pallet':'pallet','pallets':'pallet','托盘':'pallet',
}
UNITS = frozenset(PACKAGING_UNITS) | {'kg','kgs','g','gram','grams','lb','lbs','mt','tonne','tonnes','t','m3','m³','cbm','l','ml','ea','公斤','千克','克','吨','立方米','升'}
DATE_KEYS = {'date','document_date','invoice_date','packing_list_date','on_board_date','departure_date','arrival_date','issue_date','bill_of_lading_date'}


def direct_sources(row, source_by_id, value):
    ids=row.get('source_segment_ids') or []
    sources=[source_by_id[sid] for sid in ids if sid in source_by_id]
    if not ids or len(sources)!=len(ids):return False
    if row.get('logical_document_id') and any(source.get('logical_document_id')!=row['logical_document_id'] for source in sources):return False
    boundary=r'[A-Za-z0-9_]' if value.isascii() else r'\w'
    pattern=r'(?<!'+boundary+r')'+re.escape(value)+r'(?!'+boundary+r')'
    return any(re.search(pattern,source.get('text','')) for source in sources)


def lexical_shape(key,value):
    if key in {'phone','telephone','tel','fax'}:
        main=re.split(r'\s*(?:ext\.?|x|转)\s*',value,flags=re.I)
        valid=len(main)<=2 and bool(re.fullmatch(r'\+?[0-9][0-9 ()\-.]*[0-9]',main[0]))
        valid=valid and 7<=len(re.sub(r'\D','',main[0]))<=15 and main[0].count('(')==main[0].count(')')<=1
        valid=valid and ('(' not in main[0] or bool(re.search(r'\([0-9 ]+\)',main[0])))
        valid=valid and (len(main)==1 or bool(re.fullmatch(r'[0-9]{1,6}',main[1])))
        return valid,'电话基本词面，未核号码归属、国别区号或可拨通性'
    if key in DATE_KEYS or key.endswith('_date'):
        match=re.fullmatch(r'([0-9]{4})([-/.])([0-9]{1,2})\2([0-9]{1,2})',value)
        chinese=re.fullmatch(r'([0-9]{4})年([0-9]{1,2})月([0-9]{1,2})日',value)
        parts=(match[1],match[3],match[4]) if match else chinese.groups() if chinese else None
        try:valid=bool(parts) and bool(date(*(int(part) for part in parts)))
        except ValueError:valid=False
        return valid,'无歧义年在前的年月日及日历有效性；未核日期用途、先后或时区'
    if key=='currency':return bool(re.fullmatch(r'[A-Z]{3}',value)),'币种三位大写字母词面；未核ISO代码表有效性或兑换口径'
    if key=='hs_code':return bool(re.fullmatch(r'[0-9]{6}',value)),'HS六位数码词面；扩展码缺国别规则无法判断，未作货物归类或法律有效性评估'
    return value.casefold() in UNITS,'固定常用单位词面；未确认计量口径、包装层级对应或单位换算'


def lexical_rules(fields,lines,sources):
    source_by_id={row['id']:row for row in sources}
    rows=list(fields)
    for line in lines:
        for key in ('currency','quantity_unit','package_unit','weight_unit'):
            if line.get(key) is not None:rows.append({**line,'field_key':key,'value':line[key],'line_id':line.get('id'),'id':None})
    results=[]
    for row in rows:
        key=row.get('field_key','')
        if key not in {'phone','telephone','tel','fax','currency','hs_code','quantity_unit','package_unit','weight_unit'}|DATE_KEYS and not key.endswith('_date'):continue
        value=row.get('value');text=value.strip() if isinstance(value,str) else ''
        valid,scope=lexical_shape(key,text)
        proven=bool(text) and direct_sources(row,source_by_id,text)
        status='passed' if valid and proven else 'undetermined'
        results.append(RuleResult('field.lexical.'+key,status,scope+('；直接来源词面符合，仍是格式候选' if status=='passed' else '；来源不足、歧义或异常，请核对原件'),
            tuple(row.get('source_segment_ids',[])),(row['id'],) if row.get('id') else (),
            calculation={'raw_value':value,'shape_valid':valid,'direct_source':proven,'candidate_only':True,'assessment':'lexical_only','line_id':row.get('line_id'),'rule_basis':'core-lexical@2'}))
    if not results:results.append(RuleResult('field.lexical','not_run','未形成电话、日期、币种、HS或单位候选，词面检查未运行'))
    return results


def packing_total_rules(documents,fields,lines,sources):
    logicals=[logical for doc in documents for logical in doc.get('logical_documents',[doc]) if logical.get('document_type')=='packing_list']
    if not logicals:return [RuleResult('packing.package_total','not_applicable','当前没有已识别箱单；包装汇总不适用')]
    results=[]
    for logical in logicals:
        lid=logical.get('logical_document_id');local=sorted([source for source in sources if lid and source.get('logical_document_id')==lid],key=lambda source:source['id'])
        refs=tuple(source['id'] for source in local)
        context={'logical_document_id':lid,'rule_basis':'packing-complete-two-column-table@2','assessment_scope':'explicit_complete_table_only'}
        def uncertain(reason):return RuleResult('packing.package_total','undetermined',reason+'；不能机械相加包装数',refs,calculation=context)
        sections=defaultdict(list)
        for source in local:
            if source.get('structure')=='table_cell':sections[source.get('section_id')].append(source)
        tables=[]
        for section,cells in sections.items():
            positions=[(cell.get('table_row'),cell.get('table_col')) for cell in cells]
            if section is None or any(a is None or b is None for a,b in positions) or len(set(positions))!=len(positions):continue
            rows=defaultdict(dict)
            for cell in cells:rows[cell['table_row']][cell['table_col']]=cell
            if sorted(rows)!=list(range(len(rows))) or len(rows)<3 or any(set(row)!={0,1} for row in rows.values()):continue
            header=rows[0][0]['text'].strip();unit_text=rows[0][1]['text'].strip().casefold()
            explicit=bool(re.fullmatch(r'(?:complete consignment packing detail|完整本票包装明细)',header,re.I))
            if explicit and unit_text in PACKAGING_UNITS:tables.append((section,rows,unit_text))
        if len(tables)!=1:
            results.append(uncertain('缺少唯一、明确完整本票范围与共同包装单位的连续两列表格'));continue
        section,rows,unit=tables[0];last=len(rows)-1;total_label=rows[last][0]['text'].strip()
        if not re.fullmatch(r'TOTAL|合计|总计',total_label,re.I) or any(re.fullmatch(r'TOTAL|合计|总计|SUBTOTAL|小计',rows[index][0]['text'].strip(),re.I) for index in range(1,last)):
            results.append(uncertain('没有唯一末尾总计，或存在小计／分批口径'));continue
        data_rows=[rows[index] for index in range(1,last)]
        selected=[line for line in lines if line.get('logical_document_id')==lid and line.get('document_type')=='packing_list']
        def count(text):return decimal_value(text.strip()) if re.fullmatch(r'[0-9]+(?:\.0+)?',text.strip()) else None
        raw_counts=[count(row[1]['text']) for row in data_rows];total=count(rows[last][1]['text'])
        complete=len(selected)==len(data_rows) and total is not None and total>=0 and total==total.to_integral_value() and all(number is not None and number>=0 and number==number.to_integral_value() for number in raw_counts)
        matched=set()
        for line in selected:
            ids=set(line.get('source_segment_ids') or []);hits=[index for index,row in enumerate(data_rows) if {row[0]['id'],row[1]['id']}<=ids and ids<={row[0]['id'],row[1]['id'],rows[0][1]['id']}]
            if len(hits)!=1 or hits[0] in matched or PACKAGING_UNITS.get(str(line.get('package_unit') or '').casefold())!=PACKAGING_UNITS[unit] or decimal_value(line.get('package_count'))!=raw_counts[hits[0]]:complete=False;continue
            matched.add(hits[0])
        # Layer and range are explicit in the header. Labels suggesting other layers or partial shipments invalidate it.
        contradictory=any(re.search(r'\b(?:partial|split|subtotal|mixed)\b|分批|混装|小计',source['text'],re.I) for source in local) or any(any(re.search(r'(?<!\w)'+re.escape(word)+r'(?!\w)',row[0]['text'],re.I) for word,layer in PACKAGING_UNITS.items() if layer!=PACKAGING_UNITS[unit]) for row in data_rows)
        if not complete or len(matched)!=len(data_rows) or contradictory:
            results.append(uncertain('明细行未完整覆盖原表、数值／单位来源不符，或包装层级／范围有歧义'));continue
        computed=sum(raw_counts,Decimal(0));failed=computed!=total
        results.append(RuleResult('packing.package_total','failed' if failed else 'passed','明确完整同票、同层级与同单位包装明细合计核对',refs,
            calculation={**context,'section_id':section,'unit':unit,'packaging_level':PACKAGING_UNITS[unit],'complete_source_rows':True,'line_ids':[line.get('id') for line in selected],'counts':[str(number) for number in raw_counts],'computed':str(computed),'total':str(total),'difference':str(abs(computed-total)),'tolerance':'0'},severity='red' if failed else 'yellow',blocks_export=failed))
    return results
