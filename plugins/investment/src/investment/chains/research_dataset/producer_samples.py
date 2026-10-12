"""SC05 私有样本与成熟标签；仅消费准确上游上下文和真实源版本。"""
from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

from investment.contracts import ContractError, instant, is_known, known, require, unknown
from .producer_math import evidence_upper, exact_endpoint, observation_bound, quantized_decimal, reinvested_wealth, select_version
from .producer_sources import identifier

RESEARCH_USES = ['current_account_review', 'pit_training', 'selection_comparison', 'independent_evaluation']


def add_gap(gaps, rule, location, reason, *, uses=None):
    gap = {'rule_id': rule, 'field_or_object': location, 'reason': reason, 'owner': 'source',
           'required_evidence': ['source_bytes'], 'affected_uses': uses or RESEARCH_USES}
    if gap not in gaps:
        gaps.append(gap)
    return 'g-' + identifier('gap', gap).split('-', 1)[1]


def _predictors(protocol, entity):
    return [item for item in protocol['model_search_space']['predictors']
            if item['applicability']['kind'] == 'all_origin_forecast_entities'
            or entity in item['applicability']['entity_ids']]


def build_panels(protocol, contexts, observations, *, version_rule_hashes, source_rules, gaps):
    """context只由调用者沿真实SC04原行建立；不在本函数重做上游准入。"""
    panels = []
    for context in contexts:
        entity, origin = context['entity_id'], context['origin_at']
        sample_id = identifier('sample', [origin, entity, context['universe_version_id']])
        features, versions, bounds, gap_ids = [], [], [], list(context['gap_ids'])
        for predictor in _predictors(protocol, entity):
            source_rule = source_rules[predictor['source_rule_ref']['sha256']]
            fields = [field for field in source_rule['field_rules'] if field['field_name'] == predictor['field_name']]
            require(len(fields) == 1, 'D15', '/predictors', 'Predictor does not identify exactly one frozen source field')
            source_field = fields[0]
            binding = predictor['source_entity_binding']
            source_entity = entity if binding['kind'] == 'panel_entity' else binding['entity_id']
            row = select_version(
                [item for item in observations if predictor['source_rule_ref']['sha256'] in version_rule_hashes.get(item['version_id'], set())],
                source_entity, predictor['field_name'], origin)
            valid = (row is not None and row['unit']['status'] == 'known'
                     and (row['unit']['status'], row['unit']['value']) == (predictor['expected_unit']['status'], predictor['expected_unit']['value'])
                     and (row['currency']['status'], row['currency']['value']) == (predictor['currency']['status'], predictor['currency']['value']))
            if valid:
                valid = ((row['raw_unit']['status'], row['raw_unit']['value']) == (source_field['expected_unit']['status'], source_field['expected_unit']['value'])
                         and (row['raw_currency']['status'], row['raw_currency']['value']) == (source_field['currency']['status'], source_field['currency']['value']))
            if valid and predictor['transformation'] == 'documented_unit_conversion':
                valid = is_known(row['conversion'])
            if valid and predictor['transformation'] == 'identity':
                valid = (row['raw_unit']['status'], row['raw_unit']['value']) == (row['unit']['status'], row['unit']['value'])
                valid = valid and (row['raw_currency']['status'], row['raw_currency']['value']) == (row['currency']['status'], row['currency']['value'])
            feature_gaps = []
            if not valid:
                feature_gaps.append(add_gap(gaps, 'D16', '/asof_panel/' + sample_id + '/' + predictor['feature_id'],
                                           'No unambiguous origin-known source version with the frozen entity, metric, unit, currency and SourceRule binding'))
            bound = row['availability_bound_at'] if valid else unknown('No qualified origin-known feature version')
            feature = {'feature_id': predictor['feature_id'], 'value': row['value'] if valid else unknown('Required feature evidence is unavailable or conflicting'),
                       'unit': predictor['expected_unit'], 'currency': predictor['currency'],
                       'source_version_ids': [row['version_id']] if valid else [], 'availability_bound_at': bound,
                       'transformation': predictor['transformation'], 'transformation_spec_ref': predictor['source_rule_ref'], 'gap_ids': feature_gaps}
            features.append(feature)
            gap_ids.extend(feature_gaps)
            if valid:
                versions.append(row['version_id'])
                bounds.append(instant(bound['value']))
        inherited = context['availability_bound_at']
        complete = (features and all(is_known(item['value']) for item in features) and is_known(inherited)
                    and is_known(context['identity_version_ref']))
        if is_known(inherited):
            bounds.append(instant(inherited['value']))
        if not complete:
            gap_ids.append(add_gap(gaps, 'D09', '/asof_panel/' + sample_id,
                                   'Required feature, historical identity or inherited source availability is unknown',
                                   uses=['pit_training', 'selection_comparison', 'independent_evaluation']))
        bound = known(max(bounds).isoformat().replace('+00:00', 'Z')) if complete and bounds else unknown('Panel dependency availability is incomplete')
        panels.append({
            'sample_id': sample_id, 'origin_at': origin,
            'entity_id': entity, 'universe_version_id': context['universe_version_id'], 'feature_values': features,
            'source_version_ids': list(dict.fromkeys(versions)), 'availability_bound_at': bound,
            'identity_version_ref': context['identity_version_ref'], 'exposure_version_refs': context['exposure_version_refs'],
            'contract_version_ids': context['contract_version_ids'],
            'eligible_for_training': bool(complete and context['eligible'] and instant(bound['value']) <= instant(origin)),
            'gap_ids': list(dict.fromkeys(gap_ids)),
        })
    return panels


def _horizon_end(panel, horizon, observations, calendar, calendar_at, price_basis):
    origin = instant(panel['origin_at'])
    if horizon['unit'] == 'calendar_day' and horizon['end_rule'] == 'exact_calendar_boundary':
        return (origin + timedelta(days=horizon['length'])).isoformat().replace('+00:00', 'Z')
    rows = calendar_at(calendar, panel['entity_id'], horizon['calendar_id'], panel['origin_at'])
    source_zones = {row['scheduled_valuation_period']['value']['timezone'] for row in rows if is_known(row['scheduled_valuation_period'])}
    measurement = 'unit_nav' if price_basis == 'unit_nav_with_documented_equity_events' else 'documented_reinvested_return_index'
    source_zones.update(row['observation_period']['value']['timezone'] for row in observations
                        if row['entity_id'] == panel['entity_id'] and row['measurement_basis'] == measurement and is_known(row['observation_period']))
    if len(source_zones) != 1:
        return None
    timezone = ZoneInfo(next(iter(source_zones)))
    origin_date = origin.astimezone(timezone).date()
    dates = {row['date']: row for row in rows}
    if not dates:
        return None
    cursor, trading = origin_date + timedelta(days=1), 0
    last = max(dates)
    while cursor.isoformat() <= last:
        row = dates.get(cursor.isoformat())
        if row is None or not is_known(row['is_trading_day']):
            return None
        if row['is_trading_day']['value']:
            trading += 1
            reached = trading == horizon['length'] if horizon['unit'] == 'trading_day' else cursor >= origin_date + timedelta(days=horizon['length'])
            if reached:
                period = row['scheduled_valuation_period']
                if is_known(period):
                    return period['value']['upper_bound']
                actual = [item['observation_period'] for item in observations if item['entity_id'] == panel['entity_id']
                          and item['measurement_basis'] == measurement and is_known(item['observation_period'])
                          and instant(item['observation_period']['value']['upper_bound']).astimezone(timezone).date() == cursor]
                endpoints = {item['value']['upper_bound'] for item in actual}
                return next(iter(endpoints)) if len(endpoints) == 1 else None
        cursor += timedelta(days=1)
    return None


def build_labels(protocol, panels, observations, events, contracts, calendar, *, calendar_at, equity_complete, gaps):
    """每实际样本/horizon保留成熟、未知或未来状态；不把结束日当可得日。"""
    result = []
    observation_index = {row['version_id']: row for row in observations}
    contract_index = {row['contract_version_id']: row for row in contracts}
    target = protocol['target_definition']
    cutoff = instant(protocol['information_cutoff_at'])
    for panel in panels:
        for horizon in protocol['horizons']:
            label_id = identifier('label', [panel['sample_id'], target['target_id'], horizon['horizon_id']])
            location = '/label_events/' + label_id
            endpoint = _horizon_end(panel, horizon, observations, calendar, calendar_at, target['price_basis'])
            if endpoint is None:
                add_gap(gaps, 'D11', location,
                        'The requested sample/target/horizon has no source-supported economic endpoint; the original protocol request remains retained',
                        uses=['pit_training', 'selection_comparison', 'independent_evaluation'])
                continue
            start = exact_endpoint(observations, panel['entity_id'], panel['origin_at'], target['price_basis'], protocol['information_cutoff_at'], availability_metadata=True)
            end = exact_endpoint(observations, panel['entity_id'], endpoint, target['price_basis'], protocol['information_cutoff_at'], availability_metadata=True)
            source_ids = {row['version_id'] for row in (start, end) if row is not None}
            contract_ids, event_ids, gap_ids = set(), set(), []
            available, outcome = unknown('Complete label dependency availability is unknown'), unknown('Label outcome is not yet established')
            status = 'immature' if instant(endpoint) > cutoff else 'unknown'
            try:
                if start is None or end is None:
                    raise ValueError('The exact complete start/end economic periods have no origin-safe available source versions')
                value, source_ids, contract_ids, event_ids, bounds = reinvested_wealth(
                    start, end, events, observation_index, contract_index,
                    equity_complete=equity_complete(panel['entity_id'], panel['origin_at'], endpoint))
                if any(bound is None for bound in bounds):
                    raise ValueError('A necessary NAV, equity event, reinvestment or contract has unknown publication availability')
                upper = max(bounds)
                if upper < instant(endpoint):
                    raise ValueError('Label evidence predates the complete economic endpoint and cannot establish an observed outcome')
                available = known(upper.isoformat().replace('+00:00', 'Z'))
                if upper <= cutoff:
                    outcome = known(quantized_decimal(value, target['numerical_precision']['decimal_places']))
                    status = 'mature'
                else:
                    status = 'immature'
                    raise ValueError('Necessary label evidence was unavailable at the frozen information cutoff')
            except (ContractError, ValueError) as error:
                gap_ids.append(add_gap(gaps, 'D12', location, str(error),
                                       uses=['pit_training', 'selection_comparison', 'independent_evaluation']))
            result.append({
                'label_id': label_id, 'sample_id': panel['sample_id'], 'entity_id': panel['entity_id'],
                'target_id': target['target_id'], 'horizon_id': horizon['horizon_id'], 'origin_at': panel['origin_at'],
                'outcome_end_at': endpoint, 'label_available_at': available, 'label_status': status, 'outcome': outcome,
                'unit': 'ratio', 'currency': start['currency'] if start else unknown('Observed endpoint currency is unknown'),
                'start_version_id': known(start['version_id']) if start else unknown('Exact start version unavailable'),
                'end_version_id': known(end['version_id']) if end else unknown('Exact end version unavailable'),
                'equity_event_ids': sorted(event_ids), 'contract_version_ids': sorted(contract_ids),
                'source_version_ids': sorted(source_ids), 'calculation_basis': target['price_basis'] if is_known(outcome) else 'unavailable',
                'gap_ids': gap_ids,
            })
    return result
