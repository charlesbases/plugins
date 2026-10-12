"""本链最低忠实报告的独立Gap核验；不变更研究用途或候选内容。"""
from __future__ import annotations

import re

from investment.contracts import ContractError, canonical, digest, require
from .contracts import TABLES, target_key


def local_gaps(snapshot):
    inherited = {canonical(gap) for gap in snapshot.inherited_gaps}
    grouped = {}
    for index, gap in enumerate(snapshot.payload['unresolved_questions']):
        raw = canonical(gap)
        if 'reporting' not in gap['affected_uses'] or raw in inherited:
            continue
        key = digest(raw)
        if key in grouped:
            require(canonical(grouped[key]['gap']) == raw, 'A12', '/reporting_gap_reviews', 'Gap digest collision')
        else:
            grouped[key] = {'gap': gap, 'indices': []}
        grouped[key]['indices'].append(index)
    return grouped


def gap_keys(snapshot):
    return {target_key('reporting_gap', key, '/unresolved_questions/' + str(index), 'reporting_scope'):
            ('reporting_gap', key, '/unresolved_questions/' + str(index), 'reporting_scope')
            for key, group in local_gaps(snapshot).items() for index in group['indices']}


def at(value, path):
    """只在当前已核字节对象内定位，不追引用，不容忍非法数组索引。"""
    require(re.fullmatch(r'(?:/(?:[^~/]|~[01])*)*', path) is not None,
            'A12', '/reviewed_objects/json_pointer', 'Invalid JSON Pointer')
    for raw in path.split('/')[1:]:
        token = raw.replace('~1', '/').replace('~0', '~')
        if isinstance(value, list):
            require(re.fullmatch(r'0|[1-9][0-9]*', token) is not None and int(token) < len(value),
                    'A12', '/reviewed_objects/json_pointer', 'Noncanonical or missing array index')
            value = value[int(token)]
        else:
            require(isinstance(value, dict) and token in value,
                    'A12', '/reviewed_objects/json_pointer', 'Pointer does not identify a current object')
            value = value[token]
    return value


def permitted_objects(snapshot):
    ctx = snapshot.ctx
    refs = [ctx.input_ref, ctx.input['parameters_ref'], ctx.candidate['payload_ref']]
    refs += [item['ref'] for item in ctx.input['input_refs']]
    refs += [snapshot.payload[field] for field, _, _ in TABLES.values()]
    refs += [snapshot.corpus[field] for field in ('documents_ref', 'coverage_ref', 'pending_items_ref')]
    refs += [row['body_ref'] for row in snapshot.documents.values()]
    refs += [ref for ref, _ in snapshot.upstream_receipts.values()]
    return {ref['uri']: ref for ref in refs}


def restricted_objects(snapshot, gap):
    """将本链业务位置映射到当前真实对象；无法定位时保留不足，不猜含义。"""
    path = gap['field_or_object']
    if not path or re.fullmatch(r'(?:/(?:[^~/]|~[01])*)*', path) is None:
        return []
    aliases = {name: name for name in TABLES}
    aliases['economic_observations'] = 'observations'
    parts = path.split('/')
    if len(parts) < 2:
        return []
    prefix = parts[1]
    suffix = '/' + '/'.join(parts[2:]) if len(parts) > 2 else ''
    try:
        if prefix in aliases:
            name = aliases[prefix]
            field, primary_key, schema_id = TABLES[name]
            reference, rows = snapshot.payload[field], snapshot.tables[name]
            if not suffix:
                return [(reference, '/rows')]
            identities = {row[primary_key]: index for index, row in enumerate(rows)}
            token = parts[2].replace('~1', '/').replace('~0', '~')
            if token in identities:
                index = identities[token]
                tail = '/' + '/'.join(parts[3:]) if len(parts) > 3 else ''
                at(rows[index], tail)
                return [(reference, '/rows/' + str(index) + tail)]
            # 空表列名仍来自冻结行结构，不能把任意单词当真实列。
            schema = snapshot.ctx.schemas.documents['urn:investment:spec:v1:sc03']
            table_name = snapshot.ctx.schemas.targets[schema_id]['target'].rsplit('/', 1)[-1]
            row_name = schema['$defs'][table_name]['properties']['rows']['items']['$ref'].rsplit('/', 1)[-1]
            if token not in schema['$defs'][row_name]['properties']:
                return []
            if not rows:
                return [(reference, '/rows')] if len(parts) == 3 else []
            for row in rows:
                at(row, suffix)
            return [(reference, '/rows/' + str(index) + suffix) for index in range(len(rows))]
        objects = {'analyst_identity': (snapshot.payload['analyst_identity_ref'], snapshot.identity),
                   'assessment_protocol': (snapshot.payload['assessment_protocol_ref'], snapshot.protocol)}
        if prefix in objects:
            reference, value = objects[prefix]
            at(value, suffix)
            return [(reference, suffix)]
        at(snapshot.payload, path)
        # Gap文本自身不是受限声明的独立证据。
        if prefix == 'unresolved_questions':
            return []
        return [(snapshot.ctx.candidate['payload_ref'], path)]
    except (ContractError, KeyError, IndexError, ValueError, TypeError):
        return []


def validate_reviews(snapshot, reviews, wanted):
    groups = local_gaps(snapshot)
    hashes = [row['gap_sha256'] for row in reviews]
    require(len(hashes) == len(set(hashes)) and set(hashes) == set(wanted),
            'A12', '/reporting_gap_reviews', 'Reporting gap reviews omit, duplicate or invent assigned gaps')
    permitted = permitted_objects(snapshot)
    for row in reviews:
        key = row['gap_sha256']
        require(key in groups, 'A12', '/reporting_gap_reviews', 'Review refers to a nonlocal or nonexistent gap')
        group = groups[key]
        for index in group['indices']:
            require(digest(canonical(snapshot.payload['unresolved_questions'][index])) == key,
                    'A12', '/reporting_gap_reviews', 'Gap index no longer matches its complete canonical value')
        reviewed = []
        for item in row['reviewed_objects']:
            reference, path = item['object_ref'], item['json_pointer']
            require(permitted.get(reference['uri']) == reference, 'A12', '/reviewed_objects/object_ref',
                    'Reporting evidence is outside the exact current business objects')
            value = snapshot.ctx.resolver.resolve(reference)
            at(value, path)
            reviewed.append((reference, path))
        require(reviewed and len({canonical(item) for item in row['reviewed_objects']}) == len(reviewed),
                'A12', '/reviewed_objects', 'Reporting object set is empty or duplicated')
        expected = restricted_objects(snapshot, group['gap'])
        if not expected:
            require(row['decision'] == 'insufficient_evidence', 'A12', '/reporting_gap_reviews/decision',
                    'An unmapped restricted object cannot establish a reporting decision')
            expected = [(snapshot.ctx.candidate['payload_ref'], '/unresolved_questions/' + str(index))
                        for index in group['indices']]
        for reference, path in expected:
            require(any(ref == reference and (point == path or point == '' or path.startswith(point + '/'))
                        for ref, point in reviewed),
                    'A12', '/reviewed_objects', 'Review does not cover every current restricted object')
        if row['decision'] == 'insufficient_evidence':
            require(row['missing_evidence'], 'A12', '/reporting_gap_reviews/missing_evidence',
                    'Insufficient reporting judgment hides missing evidence')

