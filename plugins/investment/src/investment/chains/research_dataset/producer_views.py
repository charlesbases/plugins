"""SC05 私有前向分区和物理视图；仅按冻结协议与成熟事实开放数据。"""
from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

from investment.contracts import instant, is_known, na, now
from .producer_sources import PREFIX, identifier, own_artifact, private_known


def table(ctx, name, rows, *, filename=None):
    schema_id = PREFIX + name
    definition = next(row for row in ctx.schemas.stage_registries['SC05']['tables'] if row['schema_id'] == schema_id)
    rows = sorted(rows, key=lambda row: tuple(str(row[key]).encode('utf-8') for key in definition['primary_key']))
    value = {'rows': rows}
    reference = ctx.put_json((filename or name) + '.json', value, schema_id)
    return ctx.dataset_ref(value, schema_id, reference['uri'])


def assignments(protocol, panels, labels):
    """每label/fold完整处置；训练区间、真实成熟和评估覆盖分别约束。"""
    panel_index = {row['sample_id']: row for row in panels}
    result = []
    split = protocol['split_rule']
    independent_start = instant(split['independent_origin_start'])
    independent_end = instant(split['independent_origin_end'])
    selection_freeze = instant(split['selection_information_cutoff_at'])
    embargo = timedelta(days=protocol['purge_embargo_rule']['dependency_window_days'])
    for fold in split['folds']:
        fit = instant(fold['fit_cutoff_at'])
        evaluation = [label for label in labels if instant(fold['evaluate_origin_start']) <= instant(label['origin_at'])
                      <= instant(fold['evaluate_origin_end'])]
        for label in labels:
            origin, finish = instant(label['origin_at']), instant(label['outcome_end_at'])
            panel = panel_index[label['sample_id']]
            partition, reason = 'excluded', 'Origin is outside this frozen fold'
            available = instant(label['label_available_at']['value']) if is_known(label['label_available_at']) else None
            mature = label['label_status'] == 'mature' and is_known(label['outcome']) and available is not None
            train = instant(fold['train_origin_start']) <= origin <= instant(fold['train_origin_end'])
            evaluate = instant(fold['evaluate_origin_start']) <= origin <= instant(fold['evaluate_origin_end'])
            if train:
                reason = 'Training requires eligible point-in-time features and a label mature by fit cutoff'
                if finish >= independent_start or mature and available > selection_freeze:
                    reason = 'Independent-period rolling updates are reserved for SC08; they cannot enter SC06/SC07 training views'
                elif panel['eligible_for_training'] and mature and available <= fit:
                    overlaps = any(origin <= instant(other['outcome_end_at']) and instant(other['origin_at']) <= finish
                                   for other in evaluation)
                    separated = finish + embargo < instant(fold['evaluate_origin_start'])
                    if not overlaps and separated:
                        partition, reason = 'training', None
                    else:
                        reason = 'Closed label interval overlaps evaluation or the registered dependency window'
            elif evaluate:
                reason = 'Evaluation requires mature labels and complete point-in-time features'
                if panel['eligible_for_training'] and mature:
                    if fold['purpose'] == 'independent_validation':
                        if independent_start <= origin <= independent_end:
                            partition, reason = 'independent_validation', None
                    elif finish < independent_start and available <= min(independent_start, selection_freeze):
                        partition, reason = 'selection', None
                    else:
                        reason = 'Selection label interval or publication crosses the frozen independent boundary'
            result.append({
                'sample_id': label['sample_id'], 'origin_at': label['origin_at'], 'outcome_end_at': label['outcome_end_at'],
                'label_id': label['label_id'], 'label_available_at': label['label_available_at'],
                'partition': partition, 'fold_id': fold['fold_id'], 'fit_cutoff_at': fold['fit_cutoff_at'],
                'exclusion_reason': private_known(reason) if reason is not None else na('Included in the exact frozen partition'),
            })
    return result


def physical_views(ctx, protocol, protocol_ref, panels, labels, observations, contracts, calendar, events, splits, *, calendar_at):
    """每用途真实不同文件；投影完整实际依赖，不通过raw开放其它用途的数据。"""
    horizons = {row['horizon_id']: row for row in protocol['horizons']}
    views = {}
    for partition, permitted in (('training', ['SC06', 'SC07', 'SC08']), ('selection', ['SC06', 'SC07', 'SC08']),
                                 ('independent_validation', ['SC08']), ('current', ['SC06', 'SC07', 'SC08'])):
        label_ids = {row['label_id'] for row in splits if row['partition'] == partition}
        chosen_labels = [row for row in labels if row['label_id'] in label_ids] if partition != 'current' else []
        samples = {row['sample_id'] for row in chosen_labels}
        if partition == 'current':
            samples = {row['sample_id'] for row in panels if row['origin_at'] == protocol['information_cutoff_at']}
        chosen_panels = [row for row in panels if row['sample_id'] in samples]
        version_ids = {version for row in chosen_panels for version in row['source_version_ids']}
        version_ids.update(version for row in chosen_labels for version in row['source_version_ids'])
        event_ids = {event_id for row in chosen_labels for event_id in row['equity_event_ids']}
        chosen_events = [row for row in events if row['event_id'] in event_ids]
        for row in chosen_events:
            if is_known(row['reinvestment_nav_version_id']):
                version_ids.add(row['reinvestment_nav_version_id']['value'])
        contract_ids = {version for row in [*chosen_panels, *chosen_labels] for version in row['contract_version_ids']}
        contract_ids.update(row['contract_version_id'] for row in chosen_events)
        chosen_contracts = [row for row in contracts if row['contract_version_id'] in contract_ids]
        chosen_observations = [row for row in observations if row['version_id'] in version_ids]
        observation_index = {row['version_id']: row for row in observations}
        calendar_keys = set()
        chosen_calendar = []
        # selector接收完整源版本Rows和准确origin；具体谱系规则属于根冻结的日历契约。
        for label in chosen_labels:
            horizon = horizons[label['horizon_id']]
            for row in calendar_at(calendar, label['entity_id'], horizon['calendar_id'], label['origin_at']):
                endpoint = observation_index[label['end_version_id']['value']]
                source_timezone = ZoneInfo(endpoint['observation_period']['value']['timezone'])
                if (instant(label['origin_at']).astimezone(source_timezone).date().isoformat() <= row['date']
                        <= instant(label['outcome_end_at']).astimezone(source_timezone).date().isoformat()):
                    key = (row['calendar_id'], row['date'], row['entity_id'], row['calendar_version_id'])
                    if key not in calendar_keys:
                        calendar_keys.add(key)
                        chosen_calendar.append(row)
        if partition == 'current':
            for panel in chosen_panels:
                for horizon in protocol['horizons']:
                    for row in calendar_at(calendar, panel['entity_id'], horizon['calendar_id'], panel['origin_at']):
                        key = (row['calendar_id'], row['date'], row['entity_id'], row['calendar_version_id'])
                        if key not in calendar_keys:
                            calendar_keys.add(key)
                            chosen_calendar.append(row)
        prefix = 'view-' + partition + '-'
        tables = {
            'asof_panel_ref': table(ctx, 'asof_panel', chosen_panels, filename=prefix + 'panels'),
            'label_events_ref': table(ctx, 'label_events', chosen_labels, filename=prefix + 'labels'),
            'observation_vintages_ref': table(ctx, 'observation_vintages', chosen_observations, filename=prefix + 'observations'),
            'contract_vintages_ref': table(ctx, 'contract_vintages', chosen_contracts, filename=prefix + 'contracts'),
            'calendar_ref': table(ctx, 'calendar', chosen_calendar, filename=prefix + 'calendar'),
            'equity_events_ref': table(ctx, 'equity_events', chosen_events, filename=prefix + 'events'),
        }
        existing = own_artifact(ctx, 'view-' + partition + '.json', PREFIX + 'data_view')
        created_at = existing[1]['created_at'] if existing else now()
        value = {'view_id': identifier('view', [ctx.input_ref, protocol_ref, partition, sorted(samples), sorted(label_ids)]),
                 'partition': partition, 'protocol_ref': protocol_ref, 'sample_ids': sorted(samples),
                 'label_ids': sorted(label_ids), **tables, 'permitted_stages': permitted, 'raw_access': False, 'created_at': created_at}
        views[partition + '_view_ref'] = ctx.put_json('view-' + partition + '.json', value, PREFIX + 'data_view')
    return views
