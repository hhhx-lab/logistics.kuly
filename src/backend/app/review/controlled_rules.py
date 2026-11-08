"""人工确认口径的窄规则；任何来源、范围、层级不充分保持无法判断。"""
from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal

from app.review.core_v2 import PACKAGING_UNITS, direct_sources
from app.review.rules import RuleResult, decimal_value


def party_mapping_rules(fields, sources, decisions):
    by_id = {row['id']: row for row in fields}
    source_by_id = {row['id']: row for row in sources}
    results = []
    for decision in decisions:
        if decision['kind'] != 'party_mapping':
            continue
        payload = decision['payload']
        selected = [by_id.get(value) for value in payload.get('field_assertion_ids', [])]
        members = payload.get('members', [])
        frozen = bool(selected) and all(selected) and len(members) >= 2 and bool(decision.get('decided_by')) and bool(decision.get('basis'))
        for key in payload.get('compare_fields', []):
            rows = []
            proven = frozen
            for member in members:
                local = [row for row in selected if row and row.get('logical_document_id') == member['logical_document_id'] and row.get('role') == member['role']
                         and row.get('field_key') in {key, member['role'] + '_' + key}]
                if len(local) != 1:
                    proven = False
                else:
                    rows.append(local[0])
            proven = proven and len(rows) == len(members) and all(isinstance(row.get('value'), str) and row['value'].strip()
                and direct_sources(row, source_by_id, row['value'].strip()) for row in rows)
            values = [row.get('value', '').strip() for row in rows]
            failed = proven and len(set(values)) > 1
            results.append(RuleResult('party.confirmed_mapping.' + key, 'failed' if failed else 'passed' if proven else 'undetermined',
                '人工确认同主体映射后逐角色核对' + key + '；不同词面仅提示冲突，不鉴定法律主体' if proven else '映射字段、当前来源或完整角色候选不足，不能判断同主体字段一致',
                tuple(dict.fromkeys(sid for row in rows for sid in row.get('source_segment_ids', []))), tuple(row['id'] for row in rows),
                calculation={'decision_id': decision['decision_id'], 'scope_key': decision['scope_key'], 'confirmed_by': decision['decided_by'], 'basis': decision['basis'], 'values': values, 'members': members, 'rule_basis': 'controlled-party-mapping@3'},
                severity='red' if failed else 'yellow', blocks_export=bool(failed)))
    if not results:
        results.append(RuleResult('party.role_mapping', 'undetermined', '未确认同主体映射，Seller/Buyer/Shipper/Consignee各自保留'))
    return results


def controlled_packing_rules(documents, lines, sources, decisions):
    results = []
    logicals = {logical.get('logical_document_id'): logical for doc in documents for logical in doc.get('logical_documents', [doc])}
    for decision in decisions:
        if decision['kind'] != 'packing_table':
            continue
        p = decision['payload']; lid = p['logical_document_id']
        local = [row for row in sources if row.get('logical_document_id') == lid]
        refs = tuple(row['id'] for row in local)
        context = {'decision_id': decision['decision_id'], 'scope_key': decision['scope_key'], 'basis': decision['basis'],
                   'confirmed_by': decision['decided_by'], 'rule_basis': 'controlled-multicolumn-packing@3', 'payload': p}
        def uncertain(reason):
            return RuleResult('packing.confirmed_table_total', 'undetermined', reason + '；不机械合计箱单', refs, calculation=context)
        if logicals.get(lid, {}).get('document_type') != 'packing_list':
            results.append(uncertain('所选逻辑单据用途不是确认箱单')); continue
        scope_sources = [row for row in local if row['id'] in p.get('scope_source_segment_ids', [])]
        explicit = bool(scope_sources) and all(re.search(r'complete consignment packing detail|完整本票包装明细', row.get('text', ''), re.I) for row in scope_sources)
        cells = [row for row in local if row.get('section_id') == p['section_id'] and row.get('structure') == 'table_cell']
        positions = [(row.get('table_row'), row.get('table_col')) for row in cells]
        if not explicit or p.get('complete') is not True or any(a is None or b is None for a, b in positions) or len(set(positions)) != len(positions):
            results.append(uncertain('缺少直接完整本票范围文字、结构或唯一单元格位置')); continue
        rows = defaultdict(dict)
        for cell in cells:
            rows[cell['table_row']][cell['table_col']] = cell
        first, last, dcol, ccol = (p[key] for key in ('header_row', 'total_row', 'description_column', 'count_column'))
        if sorted(rows) != list(range(first, last + 1)) or dcol == ccol or any(dcol not in row or ccol not in row for row in rows.values()):
            results.append(uncertain('行范围非连续完整或声明列缺少来源')); continue
        unit = rows[first][ccol]['text'].strip().casefold()
        unit = re.sub(r'^(?:packages?|package count|包装数|件数)\s*[:：(（]?\s*', '', unit).strip('()（）:： ')
        level = PACKAGING_UNITS.get(unit)
        if level != p['packaging_level'] or not re.fullmatch(r'TOTAL|合计|总计', rows[last][dcol]['text'].strip(), re.I):
            results.append(uncertain('包装计数列单位/层级或末尾总计未直接证实')); continue
        detail = [rows[index] for index in range(first + 1, last)]
        if any(re.search(r'partial|split|subtotal|mixed|分批|混装|小计', row.get('text', ''), re.I) for row in local):
            results.append(uncertain('存在分批、小计或混合层级范围')); continue
        # 同表同时出现其他包装层级的列，缺少装箱转换关系，不相加。
        other_levels = {PACKAGING_UNITS.get(row['text'].strip().casefold()) for row in rows[first].values()}
        if other_levels - {None, level}:
            results.append(uncertain('同表多包装层级缺少转换关系')); continue
        def count(cell):
            text = cell['text'].strip()
            return decimal_value(text) if re.fullmatch(r'[0-9]+(?:\.0+)?', text) else None
        counts = [count(row[ccol]) for row in detail]; total = count(rows[last][ccol])
        selected = [line for line in lines if line.get('logical_document_id') == lid and line.get('document_type') == 'packing_list']
        complete = total is not None and all(value is not None for value in counts) and len(selected) == len(detail)
        used = set()
        for line in selected:
            ids = set(line.get('source_segment_ids', []))
            hits = [index for index, row in enumerate(detail) if {row[dcol]['id'], row[ccol]['id']} <= ids and ids <= {cell['id'] for cell in row.values()} | {rows[first][ccol]['id']}]
            if len(hits) != 1 or hits[0] in used or PACKAGING_UNITS.get(str(line.get('package_unit', '')).casefold()) != level or decimal_value(line.get('package_count')) != counts[hits[0]]:
                complete = False
            else:
                used.add(hits[0])
        if not complete or len(used) != len(detail):
            results.append(uncertain('业务明细未逐行完整覆盖声明表列、来源数值或单位')); continue
        computed = sum(counts, Decimal(0)); failed = computed != total
        results.append(RuleResult('packing.confirmed_table_total', 'failed' if failed else 'passed', '人工确认完整本票范围、同包装层级多列表的逐行包装数汇总核对', refs,
            calculation={**context, 'counts': [str(value) for value in counts], 'computed': str(computed), 'total': str(total), 'difference': str(abs(computed - total)), 'unit': unit, 'packaging_level': level, 'tolerance': '0'},
            severity='red' if failed else 'yellow', blocks_export=failed))
    return results
