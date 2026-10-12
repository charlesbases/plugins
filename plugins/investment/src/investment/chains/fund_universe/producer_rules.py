"""SC04 生产者三值准入、同类组聚合与原账户风险构造。"""
from __future__ import annotations

from investment.contracts import canonical, instant, is_known, known, na, require, unknown
from .source_discovery import identity_key


def available_at(value, origin):
    time, bound = value['available_time'], value['availability_bound_at']
    if not is_known(time) or not is_known(bound):
        return False
    interval = time['value']
    require(instant(interval['lower_bound']) <= instant(interval['upper_bound']) <= instant(bound['value']),
            'U14', '/availability_bound_at', 'Source availability interval contradicts its conservative bound')
    return instant(bound['value']) <= instant(origin)


def predicate_results(identity, rule):
    results = []
    identifiers = set()
    for predicate in rule['predicates']:
        key, values = predicate['predicate_id'], predicate['accepted_values']
        require(key not in identifiers and len(values) == len(set(values))
                and (predicate['operator'] != 'equals' or len(values) == 1),
                'U09', '/predicates', 'Frozen eligibility predicates are ambiguous')
        identifiers.add(key)
        observed = identity[predicate['field']] if identity is not None else None
        verdict = 'unknown'
        if is_known(observed):
            verdict = 'true' if observed['value'] in values else 'false'
        results.append({'predicate_id': key, 'verdict': verdict,
                        'citations': identity['identity_citations'] if identity is not None and verdict != 'unknown' else [],
                        'reason': 'Exact registered identity-field comparison' if verdict != 'unknown' else 'Available identity field is not established'})
    return results


def partition(member_ids, groups, identities, provenance, eligibility, origin):
    """先按每个真实关系三值处置，再按存在候选/仍有可能/全排除聚合。"""
    usable = {key: identity if identity is not None and available_at(identity, origin) else None
              for key, identity in identities.items()}
    predicates = {member: predicate_results(usable.get(member), eligibility) for member in member_ids}
    local = {key: {'candidate': [], 'excluded': [], 'unknown': []} for key in groups}
    relations = {}
    for row in provenance:
        relations.setdefault((row['member_id'], row['peer_group_id']), set()).add(row['relation'])
    global_status = {}
    for member in member_ids:
        verdicts = {row['verdict'] for row in predicates[member]}
        identity = usable.get(member)
        statuses = []
        for group in groups:
            relation = relations.get((member, group), {'possible'})
            status = 'unknown'
            if identity is not None and all(is_known(identity[key]) for key in ('entity_id', 'legal_fund_id', 'share_class_id')):
                if 'false' in verdicts or relation == {'proven_not_peer'}:
                    status = 'excluded'
                elif verdicts == {'true'} and relation == {'confirmed'}:
                    status = 'candidate'
            local[group][status].append(member)
            statuses.append(status)
        global_status[member] = 'candidate' if 'candidate' in statuses else 'unknown' if 'unknown' in statuses else 'excluded'
    return predicates, local, global_status


def reconcile_entries(entries, identities, provenance, exposures, origin, uses):
    """只合并有源法律、份额及收费事实一致的对象；原条目和分类证据全部保留。"""
    buckets, basics, aliases, merge_citations, gaps = {}, {}, {}, {}, []
    fields = ('entity_id', 'legal_fund_id', 'share_class_id', 'fund_code', 'issuer_id', 'currency', 'asset_class',
              'benchmark_id', 'benchmark_terms', 'strategy_id', 'legal_structure', 'share_class_terms', 'channel_eligibility')
    for member, _, _ in entries:
        identity = identities.get(member)
        aliases[member] = member
        if identity is None or not all(is_known(identity[key]) for key in ('entity_id', 'legal_fund_id', 'share_class_id')):
            continue
        basic = canonical([identity[key]['value'] for key in ('entity_id', 'legal_fund_id', 'share_class_id')])
        basics.setdefault(basic, []).append(member)
        if not all(is_known(identity[key]) for key in ('fund_code', 'share_class_terms')) or not identity['identity_citations'] or not available_at(identity, origin):
            continue
        key = canonical([(key, identity[key]['status'], identity[key]['value']) for key in fields])
        buckets.setdefault(key, []).append(member)
    normalized = dict(identities)
    for members in buckets.values():
        if len(members) < 2:
            continue
        representative = min(members)
        usable = [member for member in members if available_at(identities[member], origin)]
        selected = min(usable, key=lambda member: instant(identities[member]['availability_bound_at']['value'])) if usable else representative
        citations = {canonical(citation): citation for member in members for citation in identities[member]['identity_citations']}
        normalized[representative] = dict(identities[selected], identity_id=representative, identity_citations=list(citations.values()))
        merge_citations[representative] = list(citations.values())
        for member in members:
            aliases[member] = representative
            if member != representative:
                normalized.pop(member, None)
    for members in basics.values():
        if len({aliases[member] for member in members}) > 1:
            gaps.append({'rule_id': 'U06', 'field_or_object': '/identity_merges/' + identity_key('relation', members),
                         'reason': 'Original entries with matching legal/share identifiers still have unresolved or conflicting fee/code/identity dimensions; all original entries are retained',
                         'owner': 'source', 'required_evidence': ['source_bytes'], 'affected_uses': list(uses)})
    sources = {}
    for entry in entries:
        sources.setdefault(aliases[entry[0]], []).append(entry)
    relations, normalized_provenance = {}, []
    for row in provenance:
        member = aliases[row['member_id']]
        citations = {canonical(item): item for item in [*row['identity_merge_citations'], *merge_citations.get(member, ())]}
        value = dict(row, member_id=member, identity_merge_citations=list(citations.values()))
        normalized_provenance.append(value)
        relations.setdefault((member, row['peer_group_id']), []).append(value)
    for (member, group), rows in relations.items():
        states = {row['relation'] for row in rows}
        if 'confirmed' in states and 'proven_not_peer' in states:
            for row in rows:
                row['relation'] = 'possible'
            gaps.append({'rule_id': 'U08', 'field_or_object': '/members/' + member + '/peer_groups/' + group,
                         'reason': 'Original source classifications conflict after a supported identity merge; no source is silently selected',
                         'owner': 'source', 'required_evidence': ['source_bytes'], 'affected_uses': list(uses)})
        elif states - {'possible'}:
            relation = next(iter(states - {'possible'}))
            citations = {canonical(item): item for row in rows if row['relation'] == relation for item in row['classification_citations']}
            for row in rows:
                row['relation'] = relation
                row['classification_citations'] = list(citations.values())
    normalized_exposures = {}
    for row in exposures:
        value = dict(row, member_id=aliases[row['member_id']])
        key = canonical({key: item for key, item in value.items() if key not in ('exposure_id', 'citations')})
        previous = normalized_exposures.get(key)
        if previous is None:
            normalized_exposures[key] = value
        else:
            previous['citations'] = list({canonical(item): item for item in [*previous['citations'], *value['citations']]}.values())
    return sources, normalized, normalized_provenance, list(normalized_exposures.values()), gaps


def mandatory_assets(account, positions, pending, *, funding, funding_ref, parameters_ref, requested_uses, identities, channel):
    rows = []
    for kind, originals, reference in (
        ('registered_position', positions, account['positions_ref']),
        ('pending', pending, account['pending_transactions_ref']),
    ):
        for index, original in enumerate(originals):
            is_position = kind == 'registered_position'
            risk_kind = kind if is_position else 'pending_' + original['side']
            trade = na('position_has_trade_history_not_one_pending_trade') if is_position else known(original['trade_id'])
            matches = [member for member, identity in identities.items() if identity is not None
                       and all(is_known(identity[key]) for key in ('entity_id', 'legal_fund_id', 'share_class_id'))
                       and (original['instrument_id'] in (identity['entity_id']['value'], identity['share_class_id']['value'])
                            or account['platform'] == channel and is_known(identity['fund_code']) and is_known(identity['share_class_terms'])
                            and identity['identity_citations'] and original['instrument_id'] == identity['fund_code']['value'])]
            matched = len(matches) == 1
            rows.append({
                'risk_id': identity_key('risk', risk_kind, original['instrument_id'], trade),
                'risk_kind': risk_kind, 'instrument_id': known(original['instrument_id']),
                'member_id': known(matches[0]) if matched else unknown('No unique supported entity/share identifier bridge to the original catalog member is established'),
                'origin_ref': reference, 'origin_locator': '/rows/' + str(index), 'trade_id': trade,
                'confirmed_shares': original['confirmed_shares'] if is_position else unknown('Pending row does not establish confirmed shares'),
                'amount': unknown('Upstream position/pending does not establish a valuation amount'),
                'currency': known(account['currency']),
                'pending_phase': na('registered_position') if is_position else known(original['pending_phase']),
                'coverage': 'recorded_transactions_since_start', 'retention_status': 'retained', 'identity_state': 'known' if matched else 'unknown',
            })
    if funding is not None or 'selection_comparison' in requested_uses:
        rows.append({
            'risk_id': 'cash-comparator', 'risk_kind': 'cash_comparator', 'instrument_id': na('cash_comparator'),
            'member_id': na('cash_is_not_catalog_fund'), 'origin_ref': funding_ref if funding is not None else parameters_ref,
            'origin_locator': '/available_amount' if funding is not None else '/requested_uses', 'trade_id': na('cash_comparator'),
            'confirmed_shares': na('cash_has_no_fund_shares'),
            'amount': funding['available_amount'] if funding is not None else unknown('No independent FundingPlan was supplied'),
            'currency': known(funding['currency'] if funding is not None else account['currency']),
            'pending_phase': na('cash_comparator'), 'coverage': 'funding_plan_only' if funding is not None else 'unknown',
            'retention_status': 'retained', 'identity_state': 'known' if funding is not None else 'unknown',
        })
    return rows
