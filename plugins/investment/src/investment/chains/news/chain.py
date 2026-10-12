"""SC02 本轮处理：冻结输入、收齐来源任务、组装候选，不验收或封存。"""
from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import urlsplit

from investment.contracts import ContractError, SPEC, artifact, instant, is_known, now, require, unknown
from investment.chains.news.contracts import ROLES, REVIEW_USES, NewsResolver, news_dataset
from investment.chains.news.parsing import SourceCollector, identifier
from investment.chains.news.plugins import discover, load
from investment.chains.news.transport import CaptureSession
from investment.references import safe_path


class CollectionFailed(ContractError):
    """来源程序异常保留真实证据后阻断候选，不能伪造站点请求失败。"""

    def __init__(self, pointer: str, reason: str, evidence_refs: list[dict]):
        super().__init__('N04', pointer, reason[:2048], 'execution_failed')
        self.evidence_refs = evidence_refs


def load_process_input(input_ref: dict, resolver: NewsResolver, *, run_id: str, invocation_id: str, attempt_no: int):
    incoming = resolver.resolve(input_ref, 'investment.stage_input')
    require(incoming['stage_id'] == 'SC02' and incoming['run_id'] == run_id and incoming['invocation_id'] == invocation_id,
            'N03', '/run_id', 'Process identity differs from frozen StageInput', 'identity_mismatch')
    require([value['role'] for value in incoming['input_refs']] == [role for role, _ in ROLES],
            'N03', '/input_refs', 'Exact ordered SC02 roles required', 'identity_mismatch')
    refs = {value['role']: value['ref'] for value in incoming['input_refs']}
    prefix = input_ref['uri'].rsplit('/', 1)[0] + '/'
    for role, schema_id in ROLES:
        require(refs[role]['schema_id'] == schema_id, 'N03', '/input_refs/' + role, 'Input role schema mismatch', 'identity_mismatch')
        if role != 'account_risk_objects_ref':
            require(refs[role]['uri'].startswith(prefix) and len(resolver.run_parts(refs[role]['uri'])) == 3,
                    'N03', '/input_refs/' + role, 'Input must be frozen in this invocation', 'identity_mismatch')
    clock = resolver.resolve(refs['decision_clock_ref'], 'investment.decision_clock')
    window = resolver.resolve(refs['news_window_ref'], 'investment.news_window')
    scope = resolver.resolve(refs['source_scope_ref'], 'investment.source_scope')
    query = resolver.resolve(refs['query_protocol_ref'], 'investment.query_protocol')
    parameters = resolver.resolve(incoming['parameters_ref'], 'investment.sc02_parameters')
    require(clock['decision_at'] == incoming['decision_at'] and clock['information_cutoff_at'] == incoming['information_cutoff_at']
            and instant(window['start_at']) < instant(window['end_at']) <= instant(incoming['information_cutoff_at']) <= instant(incoming['decision_at']),
            'N03', '/decision_clock_ref', 'Window and decision clock disagree', 'identity_mismatch')
    require(incoming['scope_ref'] == refs['source_scope_ref'], 'N03', '/scope_ref', 'Scope reference mismatch', 'identity_mismatch')
    require(parameters['mode'] == resolver.mode and parameters['network_origin'] == resolver.network_origin
            and Path(parameters['plugins_dir']).resolve() == resolver.plugins_dir,
            'N01', '/parameters_ref', 'CLI and frozen namespace disagree', 'use_not_allowed')
    require(query['limits']['per_source_concurrency'] <= query['limits']['global_concurrency'],
            'N05', '/limits', 'Source concurrency exceeds global concurrency', 'resource_limit')
    source_ids = [entry['source_id'] for entry in scope['entries']]
    require(source_ids == sorted(set(source_ids)), 'N03', '/source_scope_ref', 'Source IDs must be unique and ordered')
    invocation = resolver.frozen_invocation(input_ref, run_id, invocation_id)
    method_ref = artifact((SPEC / 'method-sc02.json').read_bytes(), 'investment.sc02_method', 'investment-spec://v1/method-sc02.json')
    require(invocation['parameters_ref'] == incoming['parameters_ref'] and incoming['parameters_ref']['uri'] == prefix + 'parameters.json'
            and invocation['method_ref'] == method_ref and [b['source_id'] for b in invocation['profile_bindings']] == source_ids,
            'N03', '/profile_bindings', 'Frozen method/parameters/source bindings disagree', 'identity_mismatch')
    require(invocation['processing_state'] == 'running' and len(invocation['attempts']) == attempt_no
            and invocation['attempts'][-1]['processing_state'] == 'running',
            'N03', '/attempts', 'Process must own current running attempt', 'identity_mismatch')
    if attempt_no > 1:
        previous = invocation['attempts'][-2]
        require(previous['processing_state'] == 'failed' and is_known(previous['failure_ref']),
                'N03', '/attempts', 'Previous attempt must retain actual failure feedback')
        feedback = resolver.resolve(previous['failure_ref']['value'], 'investment.sc02_failure')
        require((feedback['run_id'], feedback['invocation_id'], feedback['attempt_no']) == (run_id, invocation_id, attempt_no - 1),
                'N03', '/failure_ref', 'Previous failure belongs to another attempt', 'identity_mismatch')
    account = _consume_account(refs['account_risk_objects_ref'], resolver, run_id)
    require((account['source_authority'] == 'synthetic_fixture') == (resolver.mode == 'test'),
            'N03', '/account_risk_objects_ref', 'SC01 authority crosses namespace', 'use_not_allowed')
    return {'input_ref': input_ref, 'incoming': incoming, 'window': window, 'scope': scope, 'query': query,
            'parameters': parameters, 'invocation': invocation, 'method_ref': method_ref}


def _consume_account(reference, resolver, run_id):
    """只消费SC01本轮封存身份与用途，不重新执行交易归并。"""
    context = resolver.resolve(reference, 'investment.transaction_context')
    uri = urlsplit(reference['uri'])
    parts = uri.path.strip('/').split('/')
    require(uri.scheme == 'investment-run' and uri.netloc == 'sc01' and len(parts) == 4 and parts[-1] == 'context.json',
            'N03', '/account_risk_objects_ref', 'SC01 frozen context locator required', 'identity_mismatch')
    manifest = resolver.schemas.document('investment.daily_manifest',
        safe_path(resolver.root, 'runs', 'sc01', parts[0], 'manifest.json').read_bytes())
    matches = [record for record in manifest['invocations'] if record['invocation_id'] == parts[1]]
    require(len(matches) == 1 and matches[0]['run_id'] == run_id and matches[0]['processing_state'] == 'completed'
            and matches[0]['next_chain_allowed'] and is_known(matches[0]['sealed_output_ref']),
            'N03', '/account_risk_objects_ref', 'SC01 handoff is unavailable for this round', 'missing_evidence')
    sealed = resolver.resolve(matches[0]['sealed_output_ref']['value'], 'investment.sealed_output')
    candidate = resolver.resolve(sealed['candidate_output_ref'], 'investment.candidate_output')
    receipt = resolver.resolve(sealed['validation_receipt_ref'], 'investment.validation_receipt')
    require(all(value['stage_id'] == 'SC01' and value['run_id'] == run_id for value in (sealed, candidate, receipt))
            and candidate['payload_ref'] == reference and receipt['candidate_output_ref'] == sealed['candidate_output_ref']
            and receipt['overall_verdict'] in ('passed', 'partial') and receipt['business_result'] in ('usable', 'observation_only')
            and 'reporting' in receipt['allowed_uses'],
            'N03', '/account_risk_objects_ref', 'SC01 seal/receipt/context identity mismatch', 'identity_mismatch')
    return context


def _unsupported(request, global_capture_count=0):
    stopped = global_capture_count >= request['limits']['request_budget']
    causes = [('unsupported_source', 'no_installed_profile')]
    if stopped:
        causes.append(('budget_stop', 'finite_resource_budget_stopped_scope'))
    causes.append(('coverage_unknown', 'whole_window_not_proven'))
    errors = [{'source_id': request['source_id'], 'code': code, 'capture_id': unknown('source_scope_gap'), 'message': message}
              for code, message in causes]
    coverage = {'source_id': request['source_id'], 'status': 'unsupported', 'listing_state': 'budget_stop' if stopped else 'unknown', 'window_coverage': 'unknown',
                'configured_entrypoints': request['entrypoints'], 'visited_entrypoints': [], 'capture_ids': [], 'opportunity_ids': [],
                'document_ids': [], 'pending_ids': [], 'errors': errors, 'reason': 'whole_window_not_proven'}
    return {'request_id': request['request_id'], 'source_id': request['source_id'], 'status': 'unsupported', 'documents': [],
            'opportunities': [], 'pending_items': [], 'coverage': coverage, 'errors': errors}


async def collect_all(bundle: dict, resolver: NewsResolver, base: Path, prefix: str, *, run_id: str,
                      invocation_id: str, attempt_no: int, transport=None):
    """调度全部配置来源，收齐后冻结全请求与候选；异常不取消同级来源。"""
    limits, bindings = bundle['query']['limits'], bundle['invocation']['profile_bindings']
    installed = discover(resolver.plugins_dir)
    async with CaptureSession(resolver, base, prefix, run_id=run_id, invocation_id=invocation_id,
            attempt_no=attempt_no, limits=limits, user_agent=bundle['parameters']['user_agent'], transport=transport) as session:
        async def collect_source(entry, binding):
            source_id = entry['source_id']
            request = {'request_id': identifier('req', run_id, invocation_id, attempt_no, source_id),
                       'source_id': source_id, 'entrypoints': entry['entrypoints'], 'window': bundle['window'],
                       'information_cutoff_at': bundle['incoming']['information_cutoff_at'], 'limits': limits}
            resolver.schemas.validate('investment.collector_request', request)
            require(binding['manifest_ref']['status'] == binding['profile_ref']['status']
                    and binding['manifest_ref']['status'] in ('known', 'unknown'),
                    'N03', '/profile_bindings/' + source_id, 'Partial source binding', 'identity_mismatch')
            if not is_known(binding['manifest_ref']):
                require(source_id not in installed, 'N03', '/profile_bindings/' + source_id,
                        'Installed source cannot be silently omitted', 'identity_mismatch')
                return request, None, _unsupported(request)
            frozen = resolver.source_config(bundle['input_ref'], run_id, invocation_id, source_id)
            context = session.context(request, frozen)
            require(source_id in installed, 'N03', '/profile_bindings/' + source_id, 'Frozen installation is missing', 'missing_evidence')
            collector = load(source_id, installed[source_id], frozen, resolver.schemas)
            result = await collector(request, context)
            resolver.schemas.validate('investment.collector_result', result)
            require(result['source_id'] == source_id and result['request_id'] == request['request_id'],
                    'N03', '/sources/' + source_id, 'Collector returned a different identity', 'identity_mismatch')
            state = SourceCollector(request, context)
            for response in sorted(context.responses.values(), key=lambda value: value.envelope['sequence_no']):
                await state.observe(response)
            # 内部结果必须对应实际捕获；全局预算与配置类别在全部任务结束后统一归并。
            expected = state.finish()
            for key in ('documents', 'pending_items'):
                require(result[key] == expected[key], 'N08', '/sources/' + source_id + '/' + key,
                        'Collector rows differ from captured source evidence', 'unsupported_output')
            def inventory(rows):
                return [{key: value for key, value in row.items() if key != 'reason'} for row in rows]
            require(inventory(result['opportunities']) == inventory(expected['opportunities']),
                    'N08', '/sources/' + source_id + '/opportunities', 'Collector opportunity inventory differs from actual discovery', 'unsupported_output')
            return request, state, result
        tasks = [asyncio.create_task(collect_source(entry, binding), name='sc02:' + entry['source_id'])
                 for entry, binding in zip(bundle['scope']['entries'], bindings)]
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            session.freeze_log()
            raise
        log_ref = session.freeze_log()
        evidence = [log_ref] + [response.capture_ref for _, response in sorted(session.responses.items())]
        failures = [(entry['source_id'], result) for entry, result in zip(bundle['scope']['entries'], results) if isinstance(result, BaseException)]
        if failures:
            reason = '; '.join(source_id + ': ' + type(error).__name__ + ': ' + str(error) for source_id, error in failures)
            raise CollectionFailed('/sources/' + failures[0][0], reason, evidence)
        final = []
        for entry, (request, state, result) in zip(bundle['scope']['entries'], results):
            if state is not None:
                state.material_kinds = entry['material_kinds']
                result = state.finish(global_capture_count=len(session.responses))
            else:
                result = _unsupported(request, global_capture_count=len(session.responses))
            resolver.schemas.validate('investment.collector_result', result)
            final.append(result)
        if not any(result['documents'] for result in final) and not all(result['coverage']['window_coverage'] == 'complete' for result in final):
            raise CollectionFailed('/documents', 'No supported material and no proven empty window', evidence)
        try:
            return _write_candidate(bundle, final, session, evidence), log_ref
        except (ContractError, OSError, ValueError) as error:
            if isinstance(error, ContractError):
                error.evidence_refs = evidence
                raise
            raise CollectionFailed('/candidate', str(error) or type(error).__name__, evidence) from error


def _write_candidate(bundle, results, session, evidence):
    datasets, tables = [], {}
    tables['documents'] = [row for result in results for row in result['documents']]
    tables['retrieval_attempts'] = []
    fields = ('capture_id', 'request_id', 'source_id', 'sequence_no', 'purpose', 'requested_url', 'final_url',
              'request_started_at', 'response_finished_at', 'capture_state', 'origin')
    for response in session.responses.values():
        tables['retrieval_attempts'].append({**{key: response.envelope[key] for key in fields}, 'capture_ref': response.capture_ref})
    tables['opportunity_inventory'] = [row for result in results for row in result['opportunities']]
    tables['pending_items'] = [row for result in results for row in result['pending_items']]
    tables['extraction_spec'] = []
    for binding in bundle['invocation']['profile_bindings']:
        context = session.contexts.get(binding['source_id'])
        tables['extraction_spec'].append({**binding, 'calibration_state': context.profile['calibration_state'] if context else 'missing'})
    tables['coverage'] = [result['coverage'] for result in results]
    for name in ('documents', 'retrieval_attempts', 'opportunity_inventory', 'pending_items', 'extraction_spec', 'coverage'):
        schema_id = 'investment.news_' + name
        table = next(table for table in session.schemas.news_registry['tables'] if table['schema_id'] == schema_id)
        rows = sorted(tables[name], key=lambda row: tuple(row[key] for key in table['order_by']))
        value = {'rows': rows}
        session.write(name + '.json', value, schema_id)
        datasets.append(news_dataset(value, schema_id, session.prefix + name + '.json', session.schemas))
        tables[name] = rows
    refs = {name + '_ref': reference for name, reference in zip(tables, datasets)}
    incoming = bundle['incoming']
    roles = {value['role']: value['ref'] for value in incoming['input_refs']}
    corpus = {'corpus_id': 'corpus-' + bundle['input_ref']['sha256'][:32], 'window_ref': roles['news_window_ref'],
              'source_scope_ref': roles['source_scope_ref'], **refs}
    corpus_ref = session.write('corpus.json', corpus, 'investment.evidence_corpus')
    gaps = []
    def gap(rule, pointer, reason):
        gaps.append({'rule_id': rule, 'field_or_object': pointer, 'reason': reason, 'owner': 'source',
                     'required_evidence': ['source_bytes'], 'affected_uses': list(REVIEW_USES)})
    for coverage in tables['coverage']:
        if coverage['window_coverage'] != 'complete':
            gap('N10', '/coverage/' + coverage['source_id'], 'whole_window_not_proven')
    for document in tables['documents']:
        if document['window_membership'] == 'uncertain':
            reason = 'conflicting_publication_evidence' if document['window_time']['reason'] == 'conflicting_publication_evidence' else 'window_membership_uncertain'
            gap('N07', '/documents/' + document['document_id'] + '/window_time', reason)
        if not is_known(document['available_time']):
            gap('N07', '/documents/' + document['document_id'] + '/available_time', 'no_availability_by_cutoff')
    candidate = {'contract_id': 'investment.candidate_output', 'contract_version': 1, 'run_id': session.run_id,
                 'stage_id': 'SC02', 'output_id': identifier('output', session.run_id, session.invocation_id, session.attempt_no),
                 'created_at': now(), 'processing_state': 'completed', 'input_refs': incoming['input_refs'],
                 'scope_ref': incoming['scope_ref'], 'method_ref': bundle['method_ref'], 'payload_ref': corpus_ref,
                 'datasets': datasets, 'evidence_refs': evidence,
                 'gaps': sorted(gaps, key=lambda value: (value['rule_id'], value['field_or_object']))}
    return session.write('candidate.json', candidate, 'investment.candidate_output')
