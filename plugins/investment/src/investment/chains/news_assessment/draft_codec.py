"""SC03私有生成格式转换；保留原始宿主输出，明确组装公开业务草稿。"""
from __future__ import annotations

from copy import deepcopy

from investment.contracts import require


def compile_draft(generated, schemas, layouts):
    """组装声明、源单位和机制引用总集；语义与资格仍由独立验证判断。"""
    draft = deepcopy(generated)
    facts = {row['fact_id']: row for row in draft['facts']['rows']}
    for fact in facts.values():
        for gap in fact.pop('source_gaps'):
            if gap not in draft['unresolved_questions']:
                draft['unresolved_questions'].append(gap)
    for observation in draft['economic_observations']['rows']:
        identities = observation.pop('measurement_fact_ids')
        require(all(key in facts and key in observation['related_fact_ids']
                    and facts[key]['assertion']['kind'] == 'statement' for key in identities),
                'A02', '/observations/' + observation['observation_id'],
                'Measurement facts must have exact current value-fact identities')
        basis = observation['measurement_basis']
        if basis['status'] == 'known':
            for key in identities:
                assertion = facts[key]['assertion']
                if basis['value'] not in assertion['subject']:
                    assertion['subject'] += '（计量口径：' + basis['value'] + '）'
    for event in draft['events']['rows']:
        identities = event.pop('identity_fact_ids')
        require(all(key in facts and facts[key]['assertion']['kind'] == 'source_name' for key in identities),
                'A02', '/events/' + event['event_id'], 'Event labels require source-name facts')
        event['fact_ids'] = sorted(set(event['fact_ids']) | set(identities))
    note_rows = []
    for observation in draft['economic_observations']['rows']:
        if observation['raw_value']['status'] != 'not_applicable':
            continue
        note = observation.pop('non_numeric_cell')
        path = '/observations/' + observation['observation_id']
        references = note['explanation_fact_ids']
        require(note['fact_id'] in observation['related_fact_ids'] and note['fact_id'] in facts
                and note['fact_id'] not in references and all(key in facts for key in references),
                'A02', path, 'Cell marker and explanation facts must have exact current identities')
        cells = [cell['text'] for layout in layouts
                 if layout['document_version_id'] == observation['document_version_id']
                 for block in layout['blocks'] if block['kind'] == 'table'
                 for cells in block['rows'] for cell in cells]
        compact = ''.join(note['literal'].split())
        require(compact in {''.join(cell.split()) for cell in cells},
                'A08', path, 'Non-numeric marker must equal a saved original table cell')
        claim = facts[note['fact_id']]['assertion']
        claim['predicate'] = observation['metric'] + '的原表标记为'
        claim['object'] = note['literal']
        observation['related_fact_ids'] = sorted(set(observation['related_fact_ids']) | set(references))
        note_rows.append((observation, note))
    for row in draft['facts']['rows']:
        claim = row['assertion']
        if claim['kind'] == 'statement':
            row['assertion'] = ' '.join(claim[key] for key in ('subject', 'predicate', 'object'))
        elif claim['kind'] == 'goal':
            row['assertion'] = claim['subject'] + ' 的目标为 ' + claim['goal']
        else:
            name = ''.join(claim['name'].split())
            require(any(name in ''.join(q['quote'].split()) for q in row['quote_locators']),
                    'A03', '/facts/' + row['fact_id'], 'Source name must occur in an exact source quote')
            row['assertion'] = claim['subject'] + ' 的' + claim['label'] + '为 ' + claim['name']
    for observation, note in note_rows:
        reasons = ['原表标记：' + note['literal']]
        reasons += [facts[key]['assertion'] for key in note['explanation_fact_ids']]
        observation['raw_value']['reason'] = '；'.join(reasons)
    for row in draft['economic_observations']['rows']:
        unit = row['unit']
        if unit['status'] != 'known':
            continue
        expression = unit['value']
        quote = ''.join(row['quote_locator']['quote'].casefold().split())
        tokens = expression['symbol'].replace('/', ' ').split()
        path = '/observations/' + row['observation_id'] + '/unit'
        require(tokens and all(token.casefold() in quote for token in tokens),
                'A08', path, 'Generated unit symbol must use the exact source terms')
        qualifier = expression['qualifier']
        require(not qualifier or ''.join(qualifier.casefold().split()) in quote,
                'A08', path, 'Generated unit qualifier must be stated in the source quote')
        unit['value'] = expression['symbol'] + (f'（{qualifier}）' if qualifier else '')
    for row in draft['hypotheses']['rows']:
        # 总支持集由声明依据及各步骤依据共同组成，不新增事实或替代语义核验。
        supports = set(row['supporting_fact_ids'])
        for step in row['mechanism']:
            supports.update(step['supporting_fact_ids'])
        row['supporting_fact_ids'] = sorted(supports)
    schemas.validate('investment.sc03.assessment_draft', draft)
    return draft
