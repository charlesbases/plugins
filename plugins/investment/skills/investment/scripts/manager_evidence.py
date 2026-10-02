"""Subject-bound, whole-original manager inventory and coverage evidence.

Locators identify evidence after extraction; they never define the manager
universe. A dated list without an explicit coverage claim is source fact only.
"""
import copy
import datetime as dt
import re

from contracts import fingerprint

HEADERS = {'基金经理', '任职日期', '离任日期'}


def _date(value):
    match = re.fullmatch(r'(\d{4})年(\d{1,2})月(\d{1,2})日', value)
    return (dt.date(*map(int, match.groups())) if match else dt.date.fromisoformat(value)).isoformat()


def subject_blocks(document, identity):
    """Partition labelled primary products before examining any manager rows."""
    blocks = [row for row in document['blocks'] if row['kind'] not in ('table_cell', 'pdf_line')]
    anchors = []
    for index, block in enumerate(blocks):
        text = block['text']
        codes = set(re.findall(r'(?<!主)(?<!下属)基金(?:交易)?代码\s*[:：|]?\s*(\d{6})(?!\d)', text))
        if block.get('kind') == 'table_row':
            cells, headers = block.get('cells', []), block.get('headers', [])
            if len(cells) == len(headers) and '基金代码' in headers:
                value = cells[headers.index('基金代码')]
                if re.fullmatch(r'\d{6}', value):
                    codes.add(value)
            for offset in range(0, len(cells)-1, 2):
                if cells[offset].rstrip(':：') == '基金代码' and re.fullmatch(r'\d{6}', cells[offset+1]):
                    codes.add(cells[offset+1])
        if codes:
            anchors.append((index, codes))
    selected, ranges = [], []
    for offset, (start, codes) in enumerate(anchors):
        end = anchors[offset+1][0] if offset+1 < len(anchors) else len(blocks)
        if codes == {identity['code']}:
            selected.extend(blocks[start:end])
            ranges.append({'first_locator': blocks[start]['locator'], 'last_locator': blocks[end-1]['locator']})
    # Several labels for the same product partition its sections, not its rows.
    status = 'unique_primary_product' if selected else 'primary_product_scope_unresolved'
    legal = identity.get('legal_name')
    if legal and selected:
        names = set()
        for block in selected:
            names.update(re.findall(r'基金(?:全称|名称)\s*[:：]\s*([^;；|]+)', block['text']))
        if names and any(re.sub(r'\s+', '', name) != re.sub(r'\s+', '', legal) for name in names):
            status = 'legal_subject_conflict'
    return selected, {'status': status, 'code': identity['code'], 'legal_name': legal, 'ranges': ranges,
                      'primary_code_inventory': sorted({code for _, codes in anchors for code in codes})}


def _labels(blocks):
    values = {}
    for block in blocks:
        texts = block.get('lines', [block['text']])
        for text in texts:
            for part in re.split(r'[;；]', text):
                match = re.fullmatch(r'\s*([^:：]+)[:：]\s*(.+?)\s*', part)
                if match:
                    values.setdefault(match[1], set()).add(match[2])
        cells = block.get('cells', [])
        for index in range(0, len(cells)-1, 2):
            values.setdefault(cells[index].rstrip(':：'), set()).add(cells[index+1])
    return values


def _one(labels, *names):
    values = {value for name in names for value in labels.get(name, [])}
    return next(iter(values)) if len(values) == 1 else None


def build_proof(document, identity, document_ref=None):
    blocks, subject = subject_blocks(document, identity)
    labels = _labels(blocks)
    gaps, inventory, appointments = [], [], []
    if subject['status'] != 'unique_primary_product':
        gaps.append(subject['status'])
    if document.get('unreadable_pages'):
        gaps.append('unreadable_original_pages')
    manager_tables = {row['table'] for row in blocks if HEADERS <= set(row.get('headers', []))}
    table_headers = {row['table']: row['headers'] for row in blocks if HEADERS <= set(row.get('headers', []))}
    prior_headers, continuation = None, False
    for block in blocks:
        if block['kind'] != 'table_row':
            if '续表' in block['text']:
                continuation = True
            # PDF extraction has no reliable table cells. Preserve all relevant
            # page/line evidence as unparsed, including cross-page continuations.
            if block['kind'] == 'pdf_page' and any(term in block['text'] for term in ('基金经理', '任职日期', '离任日期', '续表')):
                inventory.append({'locator': block['locator'], 'status': 'unparsed_pdf_manager_range', 'text': block['text']})
                gaps.append('manager_pdf_table_structure_unresolved')
            continue
        headers, cells = block.get('headers', []), block.get('cells', [])
        if HEADERS <= set(headers):
            prior_headers = headers
        elif continuation and prior_headers and block['table'] not in manager_tables:
            headers = prior_headers
            manager_tables.add(block['table'])
            table_headers[block['table']] = headers
        elif not headers and block['table'] in table_headers:
            headers = table_headers[block['table']]
        if block['table'] not in manager_tables:
            possible_manager_row = '基金经理' in block['text'] or (prior_headers and any(
                re.fullmatch(r'\d{4}-\d{2}-\d{2}|至今|现任', value) for value in cells))
            if possible_manager_row:
                inventory.append({'locator': block['locator'], 'status': 'unparsed', 'text': block['text']})
                gaps.append('unclassified_manager_table_row')
            continue
        continuation = False
        if cells == headers:
            inventory.append({'locator': block['locator'], 'status': 'header', 'text': block['text']})
            continue
        item = {'locator': block['locator'], 'status': 'unparsed', 'text': block['text']}
        inventory.append(item)
        spans = block.get('cell_spans', [])
        if len(cells) != len(headers) or not HEADERS <= set(headers) or any(
                span.get('rowspan', 1) != 1 or span.get('colspan', 1) != 1 for span in spans):
            gaps.append('unparsed_manager_row')
            continue
        row = dict(zip(headers, cells))
        try:
            begin = _date(row['任职日期'])
            end = None if row['离任日期'] in ('至今', '现任') else _date(row['离任日期'])
        except ValueError:
            gaps.append('unparsed_manager_date')
            continue
        if not row['基金经理'] or end is not None and end < begin:
            gaps.append('invalid_manager_appointment')
            continue
        item['status'] = 'appointment'
        appointments.append({'name': row['基金经理'], 'start_date': begin, 'end_date': end,
                             'locator': block['locator']})
    # Product overviews disclose present appointments, not a complete history.
    # Preserve these dated facts without importing the old selected-locator
    # parser or interpreting the distributor's current roster as coverage.
    overview_rows = []
    for block in blocks:
        lines = block.get('lines', [])
        for index, line in enumerate(lines):
            if not re.fullmatch(r'[一-鿿·]{2,12}', line) or index+2 >= len(lines):
                continue
            if lines[index+1] != '开始担任本基金':
                continue
            match = re.fullmatch(r'基金经理的日期\s*(\d{4}-\d{2}-\d{2})', lines[index+2])
            if match:
                try:
                    overview_rows.append({'name': line, 'start_date': _date(match[1]), 'end_date': None,
                                          'locator': block['locator']})
                except ValueError:
                    gaps.append('unparsed_manager_date')
    if overview_rows:
        inventory.extend({'locator': row['locator'], 'status': 'snapshot_appointment', 'text': row['name']+' '+row['start_date']}
                         for row in overview_rows)
        appointments.extend(overview_rows)
        gaps.append('product_overview_current_appointments_do_not_prove_complete_history')
    scope = _one(labels, '经理覆盖范围', 'Manager Coverage Scope')
    scope = {'完整期间': 'complete_period', '完整历史': 'complete_history', '当前快照': 'current_snapshot'}.get(scope, scope)
    through = _one(labels, '任期披露截至', '报告期末', '编制日期', 'Manager Disclosure As Of')
    begin = _one(labels, '经理覆盖起始', 'Manager Coverage From')
    semantics = _one(labels, '经理日期语义', 'Manager Date Semantics')
    endpoint = {'任职日生效、离任日含当日': 'start_inclusive_end_inclusive',
                '任职日生效、离任日不含当日': 'start_inclusive_end_exclusive'}.get(semantics)
    try:
        through = _date(through) if through else None
        begin = _date(begin) if begin else None
    except (ValueError, TypeError):
        gaps.append('invalid_coverage_date')
        through, begin = None, None
    if not through:
        gaps.append('missing_任期披露截至')
    if scope not in ('complete_period', 'complete_history', 'current_snapshot'):
        gaps.append('explicit_manager_coverage_claim_missing')
    if scope in ('complete_period', 'complete_history') and (not begin or not through or begin > through):
        gaps.append('complete_coverage_period_missing')
    if not endpoint:
        gaps.append('manager_endpoint_semantics_missing')
    if not appointments:
        gaps.append('supported_manager_appointments_missing')
    coverage = {'scope': scope, 'from_date': through if scope == 'current_snapshot' else begin,
                'through_date': through, 'endpoint_semantics': endpoint,
                'status': 'complete' if not gaps else 'unknown', 'gaps': sorted(set(gaps))}
    refs = [{'document_id': document['document_id'], 'document_ref': document_ref,
             'locator': row['locator'], 'raw_sha256': document['raw_sha256']} for row in blocks]
    proof = {'schema_version': 1, 'document_id': document['document_id'], 'document_ref': document_ref,
             'raw_sha256': document['raw_sha256'], 'subject': subject, 'row_inventory': inventory,
             'coverage': coverage, 'appointments': appointments, 'evidence_refs': refs,
             'source_known_at': document['retrieved_at']}
    proof['proof_hash'] = fingerprint(proof)
    return proof


def merge(identity, proof):
    result = copy.deepcopy(identity)
    proofs = {row['document_id']: row for row in result.get('manager_evidence_proofs', [])}
    proofs[proof['document_id']] = proof
    result['manager_evidence_proofs'] = [proofs[key] for key in sorted(proofs)]
    rows = {}
    for item in result['manager_evidence_proofs']:
        for row in item['appointments']:
            appointment = {key: row[key] for key in ('name', 'start_date', 'end_date')}
            key = fingerprint(appointment)
            entry = rows.setdefault(key, {**appointment, 'evidence_refs': []})
            entry['evidence_refs'].extend([ref for ref in item['evidence_refs'] if ref['locator'] == row['locator']])
    result['manager_tenures'] = sorted(rows.values(), key=lambda row: (row['start_date'], row['name'], row['end_date'] or '')) or None
    return result
