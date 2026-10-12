"""生产端获准视图、原任务全集和折内成熟标签；不调用验证算法。"""
from dataclasses import dataclass
from datetime import timedelta

from investment.contracts import canonical, digest, instant, is_known, known, na, require, unknown


def identifier(prefix, value):
    return prefix + '-' + digest(canonical(value))[:32]


def numeric_known(value):
    return {'status': 'known', 'value': value, 'reason': None}


def gap(rule, pointer, reason, uses=None):
    return {'rule_id': rule, 'field_or_object': pointer, 'reason': reason[:2048], 'owner': 'producer',
            'required_evidence': ['protocol_definition'],
            'affected_uses': uses or ['hypothesis_research', 'selection_comparison']}


def unique(values):
    return list({canonical(value): value for value in values}.values())


@dataclass
class ProductionInputs:
    ctx: object
    bundle: dict
    universe: dict
    protocol: dict
    roles: dict
    upstream: dict
    views: dict
    view_refs: dict
    panels: dict
    labels: dict
    calendars: dict
    members: list
    risks: list
    gaps: list

    @classmethod
    def load(cls, ctx):
        require(ctx.role == 'producer', 'F01', '/role', 'Forecast production requires producer context')
        bundle = ctx.read_role('dataset_bundle_ref', 'investment.sc05.dataset_bundle')
        universe = ctx.read_role('instrument_universe_ref', 'investment.sc04.instrument_universe')
        protocol = ctx.resolver.resolve(bundle['experiment_protocol_ref'], 'investment.sc05.experiment_protocol')
        require(protocol['information_cutoff_at'] == ctx.input['information_cutoff_at']
                == protocol['origin_rule']['current_origin_at'], 'F01', '/information_cutoff_at', 'Input and protocol clocks differ')
        upstream = {row['role']: ctx.upstream_receipt(row['role']) for row in ctx.definition['input_roles']}
        views, refs, panels, labels, calendars = {}, {}, {}, {}, {}
        for partition in ('training', 'selection', 'current'):
            reference = bundle[partition + '_view_ref']
            view = ctx.resolver.resolve(reference, 'investment.sc05.data_view')
            require(view['partition'] == partition and view['protocol_ref'] == bundle['experiment_protocol_ref']
                    and 'SC06' in view['permitted_stages'] and view['raw_access'] is False,
                    'F02', '/views/' + partition, 'Unauthorized physical view')
            views[partition], refs[partition] = view, reference
            for field, destination, identity in (('asof_panel_ref', panels, 'sample_id'), ('label_events_ref', labels, 'label_id')):
                rows = ctx.resolver.resolve(view[field])['rows']
                allowlist = view['sample_ids' if identity == 'sample_id' else 'label_ids']
                require(len({row[identity] for row in rows}) == len(rows) and set(allowlist) == {row[identity] for row in rows},
                        'F02', '/views/' + partition, 'Physical rows differ from the authorized allowlist')
                for row in rows:
                    require(row[identity] not in destination or destination[row[identity]] == row,
                            'F04', '/views', 'Conflicting authorized row identity')
                    destination[row[identity]] = row
            require(partition != 'current' or not view['label_ids'], 'F02', '/current', 'Current view cannot expose future labels')
            calendars[partition] = ctx.resolver.resolve(view['calendar_ref'], 'investment.sc05.calendar')['rows']
        members = ctx.resolver.resolve(universe['members_ref'])['rows']
        risks = ctx.resolver.resolve(universe['mandatory_assets_ref'])['rows']
        gaps = ctx.resolver.resolve(bundle['data_gaps_ref'])['gaps']
        gaps = unique(gaps + [item for _, receipt in upstream.values() for item in receipt['blockers']])
        gap_index = {'g-' + digest(canonical(row)) for row in gaps}
        for row in list(panels.values()) + list(labels.values()) + [f for panel in panels.values() for f in panel['feature_values']]:
            require(set(row['gap_ids']) <= gap_index, 'F04', '/gap_ids', 'Original Gap identity cannot be resolved')
        return cls(ctx, bundle, universe, protocol, {row['role']: row['ref'] for row in ctx.input['input_refs']},
                   upstream, views, refs, panels, labels, calendars, members, risks, gaps)

    @property
    def protocol_ref(self):
        return self.bundle['experiment_protocol_ref']

    def predictors(self, entity):
        return [row for row in self.protocol['model_search_space']['predictors']
                if row['applicability']['kind'] == 'all_origin_forecast_entities' or entity in row['applicability']['entity_ids']]

    def entities(self):
        entities = {row['entity_id'] for row in self.panels.values()}
        entities.update(row['entity_id']['value'] for row in self.members if is_known(row['entity_id']))
        entities.update(row['instrument_id']['value'] for row in self.risks if is_known(row['instrument_id']))
        return sorted(entities)

    def candidates(self):
        grid = [dict(row, family='elastic_net') for row in self.protocol['model_search_space']['candidate_order']]
        baselines = [{'candidate_id': row['baseline_id'], 'family': 'training_mean'}
                     for row in self.protocol['baseline_definitions'] if row['family'] == 'training_mean']
        keys = [row['candidate_id'] for row in grid + baselines]
        require(len(keys) == len(set(keys)), 'F09', '/candidate_order', 'Frozen candidate and baseline identities collide')
        return grid + baselines

    def tasks(self):
        p = self.protocol
        origins = p['origin_rule']['origins'] + [p['origin_rule']['current_origin_at']]
        require(all(instant(a) < instant(b) for a, b in zip(origins, origins[1:])),
                'F06', '/origins', 'Frozen origins must retain strict chronological order')
        tasks = []
        for entity in self.entities():
            for horizon in p['horizons']:
                for origin in origins:
                    folds = [row for row in p['split_rule']['folds'] if row['purpose'] == 'selection'
                             and instant(row['evaluate_origin_start']) <= instant(origin) <= instant(row['evaluate_origin_end'])]
                    require(len(folds) <= 1, 'F06', '/folds', 'A prediction origin has ambiguous forward folds')
                    fold = folds[0] if folds else None
                    cutoff = fold['fit_cutoff_at'] if fold else origin
                    selection_cutoff = min((origin, p['split_rule']['selection_information_cutoff_at']), key=instant)
                    for candidate in self.candidates():
                        identity = [self.ctx.run_id, self.ctx.invocation_id, self.ctx.attempt_no, self.ctx.input_ref,
                                    self.ctx.method_ref, self.protocol_ref, self.view_refs, entity, horizon['horizon_id'], origin, candidate]
                        tasks.append({'fit_id': identifier('fit', identity), 'candidate_id': candidate['candidate_id'],
                                      'entity_id': entity, 'horizon_id': horizon['horizon_id'], 'origin_at': origin,
                                      'fit_cutoff_at': cutoff, 'fold_id': known(fold['fold_id']) if fold else na('no_selection_fold'),
                                      'selection_information_cutoff_at': selection_cutoff})
        return tasks

    def decisions(self, task):
        origin, cutoff = instant(task['origin_at']), instant(task['fit_cutoff_at'])
        p = self.protocol
        fold = next((row for row in p['split_rule']['folds'] if is_known(task['fold_id']) and row['fold_id'] == task['fold_id']['value']), None)
        boundary = instant(fold['evaluate_origin_start']) if fold else origin
        decisions = []
        for label in self.labels.values():
            if (label['entity_id'], label['horizon_id']) != (task['entity_id'], task['horizon_id']):
                continue
            start = instant(label['origin_at'])
            panel = self.panels.get(label['sample_id'])
            if label['label_id'] not in self.views['training']['label_ids']:
                decision = 'not_authorized'
            elif label['label_status'] == 'immature' or instant(label['outcome_end_at']) > cutoff:
                decision = 'immature'
            elif label['label_status'] != 'mature' or not is_known(label['label_available_at']) or not is_known(label['outcome']):
                decision = 'unavailable'
            elif instant(label['label_available_at']['value']) > cutoff:
                decision = 'immature'
            elif fold and not instant(fold['train_origin_start']) <= start <= instant(fold['train_origin_end']) or start >= origin:
                decision = 'outside_window'
            elif instant(label['outcome_end_at']) + timedelta(days=p['purge_embargo_rule']['dependency_window_days']) >= boundary:
                decision = 'overlaps_evaluation'
            elif instant(label['outcome_end_at']) >= instant(p['split_rule']['independent_origin_start']):
                decision = 'overlaps_evaluation'
            elif panel is None or not panel['eligible_for_training']:
                decision = 'missing_features'
            elif not is_known(panel['availability_bound_at']) or instant(panel['availability_bound_at']['value']) > start:
                decision = 'unavailable'
            elif any(not is_known(f['value']) or not is_known(f['availability_bound_at']) or instant(f['availability_bound_at']['value']) > start
                     for f in panel['feature_values']):
                decision = 'missing_features'
            else:
                decision = 'included'
            decisions.append({key: label[key] for key in ('label_id', 'sample_id', 'entity_id', 'horizon_id', 'origin_at', 'outcome_end_at', 'label_available_at')}
                             | {'fit_cutoff_at': task['fit_cutoff_at'], 'decision': decision, 'reason': decision})
        decisions.sort(key=lambda row: (instant(row['origin_at']), row['label_id']))
        if p['origin_rule']['update_rule'] == 'rolling_window':
            lookback = p['origin_rule']['lookback_observations']
            require(is_known(lookback), 'F06', '/lookback_observations', 'Rolling window length is unknown', 'missing_evidence')
            for row in [row for row in decisions if row['decision'] == 'included'][:-lookback['value']]:
                row['decision'], row['reason'] = 'outside_window', 'outside_window'
        return decisions

    def panel_values(self, panel):
        definitions = self.predictors(panel['entity_id'])
        require(bool(definitions) and [row['feature_id'] for row in panel['feature_values']] == [row['feature_id'] for row in definitions],
                'F07', '/feature_values', 'Original ordered predictor slots are incomplete', 'missing_evidence')
        values = []
        for actual, definition in zip(panel['feature_values'], definitions):
            require(is_known(actual['value']) and is_known(actual['unit']) and is_known(definition['expected_unit'])
                    and actual['unit']['value'] == definition['expected_unit']['value'] and actual['currency'] == definition['currency']
                    and actual['transformation'] == definition['transformation'] and actual['transformation_spec_ref'] == definition['source_rule_ref'],
                    'F07', '/feature_values', 'Predictor value, unit or declared transformation is unavailable', 'missing_evidence')
            values.append(actual['value']['value'])
        return values
