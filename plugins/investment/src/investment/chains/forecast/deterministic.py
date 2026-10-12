"""从本轮获准原输入独立核对 F01—F10/F15/F18；不调用生产流程。"""
from datetime import timedelta
from decimal import Decimal, localcontext
from fractions import Fraction

from investment.contracts import canonical, instant, is_known, require
from .contracts import host_work, index, known, number
from .numerics import close, model_check, prediction_reference, statistics


def inputs(s):
    ctx, p, b = s.ctx, s.payload, s.bundle
    roles = {row['role']: row['ref'] for row in ctx.input['input_refs']}
    require(p['dataset_bundle_ref'] == roles['dataset_bundle_ref'] and p['instrument_universe_ref'] == roles['instrument_universe_ref']
            and p['experiment_protocol_ref'] == b['experiment_protocol_ref'], 'F01', '/inputs', 'Payload changed its original inputs')
    require(p['information_cutoff_at'] == ctx.input['information_cutoff_at'] == s.protocol['information_cutoff_at']
            and p['target_definition'] == s.protocol['target_definition'] and p['horizons'] == s.protocol['horizons'],
            'F01', '/target', 'Forecast target, horizon or information clock differs from protocol')
    require(s.protocol['origin_rule']['current_origin_at'] == p['information_cutoff_at'],
            'F01', '/current_origin', 'Current prediction origin differs from the common information cutoff')
    require(ctx.candidate['method_ref'] == ctx.method_ref and ctx.candidate['scope_ref'] == ctx.input['scope_ref']
            and ctx.candidate['input_refs'] == ctx.input['input_refs'], 'F01', '/candidate', 'Candidate changed frozen identity')
    require(p['source_acquisition_audit_ref'] == b['source_acquisition_audit_ref']
            and p['discovery_audit_ref'] == s.universe['discovery_audit_ref'], 'F01', '/audit', 'Original acquisition/discovery audit was replaced')
    s.ctx.resolver.resolve(ctx.input['parameters_ref'], 'investment.sc06.parameters')


def references(s):
    require(s.payload['dataset_refs'] == list(s.view_refs.values()), 'F02', '/dataset_refs', 'Forecast must retain the three exact ordered authorized views')
    for key, view in s.views.items():
        labels = s.ctx.resolver.resolve(view['label_events_ref'])['rows']
        panels = s.ctx.resolver.resolve(view['asof_panel_ref'])['rows']
        require(set(view['label_ids']) == {row['label_id'] for row in labels}
                and set(view['sample_ids']) == {row['sample_id'] for row in panels}, 'F02', '/views', 'Physical partition identity differs from its explicit allowlist')
        require(key != 'current' or not labels, 'F02', '/current', 'Current view contains future labels')
    for key in ('source_acquisition_audit_ref', 'discovery_audit_ref'):
        s.ctx.resolver.resolve(s.payload[key])
    require(not any(row['schema_id'] == 'investment.sc05.source_rule' for row in s.ctx.definition['reference_grants']),
            'F02', '/grants', 'Source definitions must not recursively authorize raw evidence')
    allowed = list(s.evidence) + [row['ref'] for row in s.ctx.input['input_refs']]
    allowed += [s.ctx.input['parameters_ref'], s.ctx.input['scope_ref']]
    allowed += [row['source_rule_ref'] for row in s.protocol['model_search_space']['predictors']]
    allowed += [s.universe[key] for key in ('candidate_members_ref', 'excluded_members_ref', 'unknown_members_ref', 'coverage_ref')]
    allowed += [view[key] for view in s.views.values() for key in ('contract_vintages_ref', 'calendar_ref', 'equity_events_ref')]
    owned = {row['schema_id'] for row in s.ctx.schemas.stage_registries['SC06']['schemas']}
    prefix = s.ctx.candidate_ref['uri'].rsplit('/', 1)[0] + '/'
    for reference in s.ctx.candidate['evidence_refs']:
        own = reference['schema_id'] in owned and reference['uri'].startswith(prefix)
        require(reference in allowed or own, 'F02', '/evidence_refs', 'Candidate attempted to export an ungranted ancestor or independent-validation reference')


def members(s):
    expected = {row['member_id']: row for row in s.members}
    actual = index(s.rows('member_forecast_dispositions'), ('member_id',), 'F03')
    require({key[0] for key in actual} == set(expected), 'F03', '/members', 'Member dispositions do not preserve the complete original U')
    for (identity,), row in actual.items():
        require(row['entity_id'] == expected[identity]['entity_id']
                and row['universe_ref'] == s.payload['instrument_universe_ref']
                and row['discovery_audit_ref'] == s.payload['discovery_audit_ref']
                and row['source_acquisition_audit_ref'] == s.payload['source_acquisition_audit_ref'],
                'F03', '/members/' + identity, 'Original member or audit identity was changed')
        risks = [risk['origin_ref'] for risk in s.risks if is_known(risk['member_id']) and risk['member_id']['value'] == identity]
        require(all(ref in row['retained_risk_refs'] for ref in risks), 'F03', '/retained_risk_refs', 'An original actual risk was removed')
    joint = s.artifacts['current_joint_scenarios']
    require(set(joint['mandatory_asset_ids']) == {row['risk_id'] for row in s.risks}
            and set(joint['required_member_ids']) == set(expected), 'F03', '/joint_scope', 'Joint scope omitted original members or mandatory risk')


def relations(s):
    for name, keys in (('historical_predictions', ('prediction_id',)), ('current_predictions', ('prediction_id',)),
                       ('residuals', ('origin_at', 'entity_id', 'horizon_id')), ('member_forecast_dispositions', ('member_id',))):
        index(s.rows(name), keys)
    index(s.rows('current_predictions'), ('entity_id', 'horizon_id'), 'F04')
    index(s.rows('historical_predictions'), ('sample_id', 'horizon_id', 'candidate_id'), 'F04')
    index(s.artifacts['tuning_audit']['selections'], ('entity_id', 'horizon_id', 'prediction_origin_at'), 'F04')
    models = s.artifacts['fitted_model']['models']
    index(models, ('fit_id',))
    index(s.artifacts['training_audit']['runs'], ('fit_id',))
    for name, obj in s.artifacts.items():
        if 'protocol_ref' in obj:
            require(obj['protocol_ref'] == s.protocol_ref, 'F04', '/' + name, 'Artifact points at a different protocol')
    require(s.artifacts['preprocessing']['model_catalog_ref'] == s.payload['fitted_model_ref'], 'F04', '/preprocessing', 'Preprocessing does not bind the actual model catalog')
    for row in list(s.panels.values()) + list(s.labels.values()) + [f for panel in s.panels.values() for f in panel['feature_values']]:
        require(all(key in s.gaps for key in row['gap_ids']), 'F04', '/gap_ids', 'Gap content key cannot be resolved to a complete original Gap')
    current = s.rows('current_predictions')
    require(set(s.payload['instrument_ids']) == {row['entity_id'] for row in current}, 'F04', '/instrument_ids', 'Actual current prediction identities differ')


def permissions(s):
    receipt = s.upstream['dataset_bundle_ref'][1]
    required = {'pit_training', 'selection_comparison'}
    if s.artifacts['fitted_model']['models'] or s.rows('historical_predictions') or s.rows('current_predictions'):
        require(required <= set(receipt['allowed_uses']), 'F05', '/upstream_receipt', 'Training or selection used an upstream reporting-only output')
    else:
        require(required <= set(receipt['allowed_uses']), 'F05', '/upstream_receipt', 'Upstream has not granted numerical prediction uses', 'missing_evidence')


def label_decision(s, run, label):
    cutoff, origin = instant(run['fit_cutoff_at']), instant(run['origin_at'])
    if label['label_id'] not in s.views['training']['label_ids']:
        return 'not_authorized'
    if label['label_status'] == 'immature' or instant(label['outcome_end_at']) > cutoff:
        return 'immature'
    if label['label_status'] != 'mature' or not is_known(label['label_available_at']) or not is_known(label['outcome']):
        return 'unavailable'
    if instant(label['label_available_at']['value']) > cutoff:
        return 'immature'
    start = instant(label['origin_at'])
    fold = None
    if is_known(run['fold_id']):
        matches = [row for row in s.protocol['split_rule']['folds'] if row['fold_id'] == run['fold_id']['value'] and row['purpose'] == 'selection']
        require(len(matches) == 1, 'F06', '/fold_id', 'Unknown or independent-validation fold was used')
        fold = matches[0]
        require(instant(fold['fit_cutoff_at']) == cutoff and instant(fold['evaluate_origin_start']) <= origin <= instant(fold['evaluate_origin_end']),
                'F06', '/fold', 'Training cutoff or prediction origin does not belong to actual forward fold')
        if not instant(fold['train_origin_start']) <= start <= instant(fold['train_origin_end']):
            return 'outside_window'
    if start >= origin:
        return 'outside_window'
    boundary = instant(fold['evaluate_origin_start']) if fold else origin
    margin = s.protocol['purge_embargo_rule']['dependency_window_days']
    if instant(label['outcome_end_at']) + timedelta(days=margin) >= boundary:
        return 'overlaps_evaluation'
    if instant(label['outcome_end_at']) >= instant(s.protocol['split_rule']['independent_origin_start']):
        return 'overlaps_evaluation'
    panel = s.panels.get(label['sample_id'])
    if panel is None or not panel['eligible_for_training']:
        return 'missing_features'
    if not is_known(panel['availability_bound_at']) or instant(panel['availability_bound_at']['value']) > start:
        return 'unavailable'
    if any(not is_known(row['value']) or not is_known(row['availability_bound_at'])
           or instant(row['availability_bound_at']['value']) > start for row in panel['feature_values']):
        return 'missing_features'
    return 'included'


def expected_labels(s, run):
    labels = [row for row in s.labels.values() if row['entity_id'] == run['entity_id'] and row['horizon_id'] == run['horizon_id']]
    decisions = {row['label_id']: label_decision(s, run, row) for row in labels}
    if s.protocol['origin_rule']['update_rule'] == 'rolling_window':
        count = known(s.protocol['origin_rule']['lookback_observations'], 'F06', '/lookback')
        candidates = sorted((row for row in labels if decisions[row['label_id']] == 'included'), key=lambda row: (instant(row['origin_at']), row['label_id']))
        for row in candidates[:-count]:
            decisions[row['label_id']] = 'outside_window'
    return decisions


def training(s):
    families = _candidate_families(s, 'F06')
    models = {row['fit_id']: row for row in s.artifacts['fitted_model']['models']}
    for run in s.artifacts['training_audit']['runs']:
        require(run['candidate_id'] in families, 'F06', '/candidate_family', 'Training task has no registered forecast candidate or baseline')
        require(run['origin_at'] in s.protocol['origin_rule']['origins'] + [s.protocol['origin_rule']['current_origin_at']],
                'F06', '/origin', 'Actual fit task is outside frozen historical/current origins')
        require(instant(run['selection_information_cutoff_at']) <= instant(run['origin_at'])
                and instant(run['fit_cutoff_at']) <= instant(run['origin_at']), 'F06', '/fit_cutoff', 'Training or model selection saw future information')
        expected = expected_labels(s, run)
        actual = index(run['label_decisions'], ('label_id',), 'F06')
        require(set(expected) == {key[0] for key in actual}, 'F06', '/label_decisions', 'Audit does not partition every authorized candidate label')
        for identity, decision in expected.items():
            row, source = actual[(identity,)], s.labels[identity]
            require(row['decision'] == decision and row['sample_id'] == source['sample_id']
                    and row['origin_at'] == source['origin_at'] and row['outcome_end_at'] == source['outcome_end_at']
                    and row['label_available_at'] == source['label_available_at']
                    and row['entity_id'] == source['entity_id'] and row['horizon_id'] == source['horizon_id']
                    and row['fit_cutoff_at'] == run['fit_cutoff_at'], 'F06', '/label_decisions/' + identity, 'Maturity, window, availability or audit decision differs from original label')
        included = {key for key, value in expected.items() if value == 'included'}
        require(set(run['training_label_ids']) == included and set(run['rejected_label_ids']) == set(expected) - included,
                'F06', '/training_label_ids', 'Training/rejected member set is not the independent partition')
        require(run['actual_fit_started'] == is_known(run['started_at'])
                and (not run['actual_fit_started'] or is_known(run['finished_at'])
                     and instant(s.ctx.input['decision_at']) <= instant(run['started_at']['value'])
                     <= instant(run['finished_at']['value']) <= instant(s.ctx.candidate['created_at'])),
                'F06', '/actual_fit_started', 'Actual execution clocks contradict fit execution')
        if run['status'] == 'fitted':
            require(run['actual_fit_started'] and run['fit_id'] in models and included, 'F06', '/models', 'Fitted audit has no actual nonempty model')
            model = models[run['fit_id']]
            require(model['training_label_ids'] == run['training_label_ids'] and set(model['training_sample_ids']) == {s.labels[key]['sample_id'] for key in included}
                    and all(model[key] == run[key] for key in ('candidate_id', 'entity_id', 'horizon_id', 'origin_at', 'fit_cutoff_at'))
                    and model['family'] == families[run['candidate_id']],
                    'F06', '/model_training', 'Saved model training membership or time identity differs from audit')
        else:
            require(run['fit_id'] not in models, 'F06', '/failed_fit', 'Failed or unstarted fit was promoted to a numerical model')
    require(set(models) <= {row['fit_id'] for row in s.artifacts['training_audit']['runs']}, 'F06', '/models', 'Model lacks actual training audit')


def features(s):
    definitions = s.protocol['model_search_space']['predictors']
    require(s.artifacts['model_rule']['feature_ids'] == [row['feature_id'] for row in definitions]
            and s.artifacts['model_rule']['update_rule'] == s.protocol['origin_rule']['update_rule'],
            'F07', '/model_rule', 'Frozen predictor declaration or update rule was changed')
    require(instant(s.protocol['registered_at']) <= instant(s.artifacts['model_rule']['frozen_at'])
            <= instant(s.ctx.candidate['created_at']), 'F07', '/frozen_at', 'Model rule clock was falsely backdated or frozen after the completed candidate')
    for panel in s.panels.values():
        declared = s.predictors(panel['entity_id'])
        require([row['feature_id'] for row in panel['feature_values']] == [row['feature_id'] for row in declared],
                'F07', '/panel', 'Panel dropped unknown slots or reordered frozen predictors')
        for feature, definition in zip(panel['feature_values'], declared):
            source = s.ctx.resolver.resolve(definition['source_rule_ref'], 'investment.sc05.source_rule')
            if definition['source_rule_ref'] not in s.evidence:
                s.evidence.append(definition['source_rule_ref'])
            field = [row for row in source['field_rules'] if row['field_name'] == definition['field_name']]
            require(len(field) == 1 and field[0]['time_role'] == 'not_temporal' and definition['field_name'] not in source['identity_fields']
                    and definition['feature_id'] == definition['field_name'], 'F07', '/definition', 'Predictor is not the unique declared nonidentity numeric field')
            require(feature['transformation_spec_ref'] == definition['source_rule_ref'] and feature['transformation'] == definition['transformation']
                    and feature['unit'] == definition['expected_unit'] and feature['currency'] == definition['currency'],
                    'F07', '/feature', 'Feature changed source definition, target unit or transform')
            if not is_known(feature['value']):
                continue
            require(is_known(feature['unit']) and feature['source_version_ids'], 'F07', '/feature', 'Known numeric feature has no unit or actual source version')
            for version in feature['source_version_ids']:
                require(version in s.observations, 'F07', '/source_version', 'Feature source is outside authorized physical views')
                obs = s.observations[version]
                entity = panel['entity_id'] if definition['source_entity_binding']['kind'] == 'panel_entity' else definition['source_entity_binding']['entity_id']
                require(obs['metric_id'] == definition['feature_id'] and obs['entity_id'] == entity
                        and obs['unit'] == feature['unit'] and obs['currency'] == feature['currency']
                        and obs['raw_unit'] == field[0]['expected_unit'] and obs['raw_currency'] == field[0]['currency'],
                        'F07', '/source_binding', 'Metric/source entity/raw or target unit differs')
                require(is_known(obs['availability_bound_at']) and instant(obs['availability_bound_at']['value']) <= instant(panel['origin_at']),
                        'F07', '/availability', 'Feature source was not available at its historical origin')
                if feature['transformation'] == 'identity':
                    require(obs['raw_unit'] == obs['unit'] and obs['raw_currency'] == obs['currency'], 'F07', '/identity', 'Identity transformation changed units')
                require(obs['value'] == feature['value'], 'F07', '/value', 'Saved feature differs from its authorized observation')
    for run in s.artifacts['training_audit']['runs']:
        require(run['feature_ids'] == [row['feature_id'] for row in s.predictors(run['entity_id'])], 'F07', '/training_columns', 'Training dropped or reordered predictor slots')


def matrix(s, model):
    labels = [s.labels[key] for key in model['training_label_ids']]
    rows = [[number(known(feature['value'], 'F07', '/feature')) for feature in s.panels[label['sample_id']]['feature_values']] for label in labels]
    return rows, [number(known(row['outcome'], 'F08', '/outcome')) for row in labels]


def _candidate_families(s, rule):
    # 家族由冻结登记决定，不能从待核模型或Score自报反推。
    grid = index(s.protocol['model_search_space']['candidate_order'], ('candidate_id',), rule)
    baselines = index(s.protocol['baseline_definitions'], ('baseline_id',), rule)
    require(not set(grid) & set(baselines), rule, '/candidate_family', 'Frozen grid and baseline identities collide')
    families = {key[0]: 'elastic_net' for key in grid}
    families.update({key[0]: row['family'] for key, row in baselines.items() if row['family'] == 'training_mean'})
    return families


def numerical_models(s):
    require(bool(s.artifacts['fitted_model']['models']), 'F08', '/models', 'No model has actually been fitted', 'missing_evidence')
    families = _candidate_families(s, 'F08')
    grid = {row['candidate_id']: row for row in s.protocol['model_search_space']['candidate_order']}
    search = {row['name']: row['values'] for row in s.protocol['model_search_space']['numeric_search']}
    for model in s.artifacts['fitted_model']['models']:
        require(model['candidate_id'] in families and model['family'] == families[model['candidate_id']],
                'F08', '/model_family', 'Actual fitted family differs from the registered candidate or baseline')
        require([row['feature_id'] for row in model['feature_statistics']] == [row['feature_id'] for row in s.predictors(model['entity_id'])],
                'F08', '/model_columns', 'Model coefficient order differs from frozen predictor matrix')
        for statistic, definition in zip(model['feature_statistics'], s.predictors(model['entity_id'])):
            require(statistic['unit'] == known(definition['expected_unit'], 'F08', '/feature_unit')
                    and statistic['currency'] == definition['currency'], 'F08', '/feature_unit', 'Model statistics changed source feature dimensions')
        require(all(ref == s.view_refs['training'] for ref in model['input_view_refs']), 'F08', '/training_views', 'Readable selection/current labels were promoted to training permission')
        if model['family'] == 'elastic_net':
            require(model['candidate_id'] in grid and model['alpha']['value'] == grid[model['candidate_id']]['alpha']
                    and model['l1_ratio']['value'] == grid[model['candidate_id']]['l1_ratio']
                    and model['fit_intercept'] == s.protocol['model_search_space']['intercept']
                    and model['solver']['max_iterations'] == int(number(search['max_iterations'][0]))
                    and model['solver']['convergence_tolerance']['value'] == search['convergence_tolerance'][0],
                    'F08', '/model_parameters', 'Actual optimizer or budget differs from registered candidate')
        x, y = matrix(s, model)
        predicted = {}
        for row in s.rows('historical_predictions') + s.rows('current_predictions'):
            if row['fit_id'] == model['fit_id']:
                require(row['sample_id'] in s.panels, 'F08', '/prediction_sample', 'Numerical audit prediction has no authorized sample')
                values = [number(known(feature['value'], 'F08', '/feature')) for feature in s.panels[row['sample_id']]['feature_values']]
                predicted[row['prediction_id']] = (values, row['prediction'])
        model_check(model, x, y, predicted)


def tuning(s):
    host_work(s, 'F09')
    families = _candidate_families(s, 'F09')
    audit = s.artifacts['tuning_audit']
    runs = s.artifacts['training_audit']['runs']
    require(all(row['candidate_id'] in families for row in runs), 'F09', '/candidate_family', 'Fit inventory contains an unregistered forecast candidate or baseline')
    models = {row['fit_id']: row for row in s.artifacts['fitted_model']['models']}
    require(audit['actual_model_fit_count'] == sum(row['actual_fit_started'] for row in runs)
            and audit['actual_model_fit_count'] <= s.protocol['search_budget']['max_model_fits'], 'F09', '/fit_count', 'Actual fit count omits failures/baselines or exceeds budget')
    grid = s.protocol['model_search_space']['candidate_order']
    grid_ids = [row['candidate_id'] for row in grid]
    baseline_ids = {row['baseline_id'] for row in s.protocol['baseline_definitions'] if row['family'] == 'training_mean'}
    require(baseline_ids, 'F09', '/baseline', 'Protocol has no actual training-mean comparator', 'missing_evidence')
    require(audit['actual_candidate_count'] == len({row['candidate_id'] for row in runs if row['actual_fit_started'] and row['candidate_id'] in grid_ids}),
            'F09', '/candidate_count', 'Candidate count differs from actual attempted distinct grid members')
    predictions = {row['prediction_id']: row for row in s.rows('historical_predictions')}
    for selection in audit['selections']:
        require(instant(selection['information_cutoff_at']) <= instant(selection['prediction_origin_at'])
                and instant(selection['information_cutoff_at']) <= instant(s.protocol['split_rule']['selection_information_cutoff_at']),
                'F09', '/selection_clock', 'Historical selection saw later labels or final winner')
        scores = selection['candidate_scores']
        index(scores, ('candidate_id',), 'F09')
        require(all(row['candidate_id'] in families and row['family'] == families[row['candidate_id']] for row in scores),
                'F09', '/score_family', 'Score family differs from the frozen candidate or baseline identity')
        require({row['candidate_id'] for row in scores if row['family'] == 'elastic_net'} == set(grid_ids)
                and {row['candidate_id'] for row in scores if row['family'] == 'training_mean'} == baseline_ids,
                'F09', '/candidate_scores', 'Full grid or registered comparison baseline is missing', 'missing_evidence')
        expected = {key for key in s.views['selection']['label_ids'] if s.labels[key]['entity_id'] == selection['entity_id']
                    and s.labels[key]['horizon_id'] == selection['horizon_id'] and is_known(s.labels[key]['label_available_at'])
                    and instant(s.labels[key]['label_available_at']['value']) <= instant(selection['information_cutoff_at'])
                    and instant(s.labels[key]['origin_at']) < instant(selection['prediction_origin_at'])}
        independent_scores = {}
        for score in scores:
            require(score['entity_id'] == selection['entity_id'] and score['horizon_id'] == selection['horizon_id']
                    and instant(score['selection_at']) == instant(selection['information_cutoff_at']),
                    'F09', '/score_identity', 'Score coordinate or information boundary differs from selection')
            require(score['status'] == 'scored' and expected and set(score['selection_label_ids']) == expected,
                    'F09', '/scoring_members', 'Candidate scoring is incomplete or changed the common label denominator', 'missing_evidence')
            errors, seen = [], set()
            for key in score['prediction_ids']:
                require(key in predictions, 'F09', '/prediction_ids', 'Scoring prediction does not exist')
                row = predictions[key]
                label_id = known(row['label_id'], 'F09', '/label_id')
                require(label_id in expected and label_id not in seen and row['candidate_id'] == score['candidate_id'],
                        'F09', '/score_pair', 'Score pairs duplicate, foreign or retrospectively selected predictions')
                require(row['fit_id'] in models and models[row['fit_id']]['candidate_id'] == score['candidate_id']
                        and models[row['fit_id']]['family'] == families[score['candidate_id']],
                        'F09', '/score_fit_family', 'Scored prediction is backed by a different actual fitted candidate family')
                seen.add(label_id)
                errors.append((Fraction(row['prediction']) - Fraction(s.labels[label_id]['outcome']['value'])) ** 2)
            require(seen == expected and score['observation_count'] == len(expected), 'F09', '/score_count', 'Candidate omits required scoring pairs')
            sse = sum(errors, Fraction(0))
            with localcontext() as ctx:
                ctx.prec = 80
                exact = Decimal(sse.numerator) / sse.denominator
                close(known(score['squared_error_sum'], 'F09', '/sse'), exact, [exact], len(errors) * 3 + 1, 'F09')
                close(known(score['mse'], 'F09', '/mse'), exact / len(expected), [exact / len(expected)], len(errors) * 3 + 2, 'F09')
            if score['family'] == 'elastic_net':
                independent_scores[score['candidate_id']] = sse / len(expected)
        winner = min(grid_ids, key=lambda key: (independent_scores[key], grid_ids.index(key)))
        require(selection['status'] == 'selected' and known(selection['selected_candidate_id'], 'F09', '/selected') == winner,
                'F09', '/selected', 'Selected model differs from exact aggregate forward MSE and declared tie order')
    require(bool(audit['selections']), 'F09', '/selections', 'No actual complete forward selection is available', 'missing_evidence')
    entities = {row['entity_id'] for row in s.panels.values()}
    entities |= {row['entity_id']['value'] for row in s.members if is_known(row['entity_id'])}
    entities |= {row['instrument_id']['value'] for row in s.risks if is_known(row['instrument_id'])}
    origins = set(s.protocol['origin_rule']['origins']) | {s.protocol['origin_rule']['current_origin_at']}
    tasks = {(row['entity_id'], row['horizon_id'], row['origin_at'], row['candidate_id']) for row in runs}
    require(all((entity, horizon['horizon_id'], origin, candidate) in tasks
                for entity in entities for horizon in s.protocol['horizons'] for origin in origins
                for candidate in set(grid_ids) | baseline_ids),
            'F09', '/unprocessed_tasks', 'Full original forecasting task inventory has unrecorded fits or retained failures', 'missing_evidence')


def predictions(s):
    families = _candidate_families(s, 'F10')
    models = {row['fit_id']: row for row in s.artifacts['fitted_model']['models']}
    for purpose, name in (('forward_oos', 'historical_predictions'), ('current', 'current_predictions')):
        for row in s.rows(name):
            require(row['fit_id'] in models and row['purpose'] == purpose and row['sample_id'] in s.panels, 'F10', '/prediction', 'Prediction lacks its actual model or permitted original sample')
            model = models[row['fit_id']]
            require(model['candidate_id'] in families and model['family'] == families[model['candidate_id']],
                    'F10', '/prediction_family', 'Prediction uses a different actual family from its frozen candidate identity')
            require(all(model[key] == row[key] for key in ('entity_id', 'horizon_id', 'candidate_id', 'origin_at'))
                    and row['sample_id'] not in model['training_sample_ids'] and model['purpose'] == purpose,
                    'F10', '/prediction_identity', 'Prediction is in-sample or mismatches actual model coordinates')
            panel = s.panels[row['sample_id']]
            require(panel['entity_id'] == row['entity_id'] and panel['origin_at'] == row['origin_at'],
                    'F10', '/sample_identity', 'Prediction is bound to another original panel entity/origin')
            x, y = matrix(s, model)
            weights = [number(value['coefficient']) for value in model['feature_statistics']]
            values = [number(known(value['value'], 'F10', '/features')) for value in panel['feature_values']]
            value, terms, operations, conversion = prediction_reference(values, x, weights, number(model['intercept']))
            close(row['prediction'], value, terms, operations, 'F10', '/prediction_value', conversion)
            require(row['input_version_ids'] == panel['source_version_ids']
                    and instant(row['selection_information_cutoff_at']) <= instant(row['origin_at']), 'F10', '/input_versions', 'Prediction input or historical selection clock differs')
            if is_known(row['label_id']):
                label = s.labels[row['label_id']['value']]
                require(label['sample_id'] == row['sample_id'] and row['outcome_end_at'] == label['outcome_end_at']
                        and label['entity_id'] == row['entity_id'] and label['horizon_id'] == row['horizon_id'],
                        'F10', '/label', 'Prediction target endpoint or identity differs from its label')
            if purpose == 'current':
                selections = [value for value in s.artifacts['tuning_audit']['selections']
                              if value['entity_id'] == row['entity_id'] and value['horizon_id'] == row['horizon_id']
                              and value['prediction_origin_at'] == row['origin_at']]
                require(len(selections) == 1 and selections[0]['status'] == 'selected'
                        and known(selections[0]['selected_candidate_id'], 'F10', '/current_selection') == row['candidate_id']
                        and row['selection_information_cutoff_at'] == selections[0]['information_cutoff_at'],
                        'F10', '/current_selection', 'Current prediction did not use the exact complete prior forward selection')
    require(bool(s.rows('current_predictions')), 'F10', '/current_predictions', 'Current model predictions are absent', 'missing_evidence')


def dispositions(s):
    predictions = {row['prediction_id']: row for row in s.rows('current_predictions')}
    horizons = {row['horizon_id'] for row in s.protocol['horizons']}
    for row in s.rows('member_forecast_dispositions'):
        for reference in row['evidence_refs']:
            s.ctx.resolver.resolve(reference)
        if row['forecast_status'] == 'ready':
            require(is_known(row['entity_id']) and set(row['horizon_ids']) == horizons and row['prediction_ids'], 'F15', '/ready', 'Ready disposition has no complete prediction evidence')
            require(all(key in predictions and predictions[key]['entity_id'] == row['entity_id']['value'] for key in row['prediction_ids']),
                    'F15', '/predictions', 'Member claimed another entity prediction')
            require({predictions[key]['horizon_id'] for key in row['prediction_ids']} == horizons,
                    'F15', '/prediction_horizons', 'Ready member did not actually forecast every requested horizon')
        if row['forecast_status'] == 'proven_not_applicable':
            require(not row['prediction_ids'] and row['evidence_refs'] and row['rule_ref'] == s.payload['model_rule_ref'],
                    'F15', '/not_applicable', 'Not-applicable status lacks its frozen rule and actual evidence')
        if row['forecast_status'] == 'unknown':
            require(bool(row['affected_uses']), 'F15', '/unknown', 'Unknown member lost its use impact')


def invocation(s):
    require(s.artifacts['training_audit']['upstream_receipt_refs'] == [ref for ref, _ in s.upstream.values()],
            'F18', '/upstream_receipts', 'Training audit did not preserve exact actual upstream receipts')
    required = [s.payload[key] for key in ('member_forecast_dispositions_ref', 'historical_predictions_ref', 'current_predictions_ref', 'residuals_ref')]
    require(all(ref in s.ctx.candidate['datasets'] for ref in required), 'F18', '/datasets', 'Candidate omitted required exported forecast tables')
    for reference in s.evidence:
        s.ctx.resolver.resolve(reference)
    inherited = {canonical(gap) for gap in s.gaps.values()}
    retained = {canonical(gap) for gap in s.payload['missing_evidence'] + s.ctx.candidate['gaps']}
    require(inherited <= retained, 'F18', '/gaps', 'An actual inherited gap or its original use impact was discarded')
