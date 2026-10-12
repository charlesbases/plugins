"""Verified paging of the complete canonical original inventory.

Only the public material preparation operation can bind an index. A page reads
that immutable acceptance and the requested CAS parts; it never replays the
entire raw news domain or turns an engineering page into a coverage limit.
"""
import bisect
import copy
import hashlib
import json
import re
from zoneinfo import ZoneInfo

from artifacts import Artifacts
from contracts import fields, fingerprint, instant, require
from state_store import Store

PAGE_ENTRIES = 64
OUTPUT_BYTES = 16 * 1024
INDEX_SCHEMA = 'canonical_original_material_pages_v3'
MATERIAL_SCOPE = 'complete_canonical_original_inventory_not_fact_PIT_or_trade_qualification'
VERSION_FIELDS = ('version_id', 'document_id', 'source_id', 'title', 'published_at', 'body_ref')


def _encoded_size(value):
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')) + 1


def _collection(payload, store, artifacts, *, replay_originals=False):
    fields(payload, {'workflow_id', 'run_id', 'collection_id'}, label='original material preparation')
    workflow = payload['workflow_id']
    require(type(workflow) is str and re.fullmatch(r'[A-Za-z0-9_.:-]{1,120}', workflow),
            'Invalid original material workflow')
    require(payload['run_id'] == workflow + ':analysis', 'Original material analysis identity differs')
    opened, collected = store.operation(payload['run_id']), store.operation(workflow + ':collect')
    require(opened is not None and opened['status'] == 'completed'
            and opened['request']['operation'] == 'analysis_start', 'Original material analysis has not completed')
    require(collected is not None and collected['status'] == 'completed'
            and collected['request']['operation'] == 'news_collect', 'The current original collection has not completed')
    run = store.get('analysis_run', payload['run_id'])
    require(run is not None and opened['result']['run'] == run, 'Original material run differs from its accepted run')
    collection = artifacts.read_json(collected['result']['manifest_ref'])
    journal = store.get('news-operation', fingerprint({'operation_id': collection['collection_id']}))
    require(journal is not None and journal['manifest_ref'] == collected['result']['manifest_ref']
            and collection['collection_id'] == payload['collection_id']
            and collection['run_id'] == run['run_id']
            and collection['cutoff_at'] == run['publish_cutoff'], 'Original material collection binding differs')
    import news
    bound_run, registry = news.collection_policy(collection, store, artifacts)
    require(bound_run == run, 'Original material frozen policy differs')
    if replay_originals:
        at = instant(collection['information_cutoff_at'])
        news._verify_collection_bodies(collection, registry, at, at,
                                      run['news_policy']['max_age_seconds'], artifacts, store=store)
        news._verify_source_scopes(collection, registry)
    return run, collection, collected['result']['manifest_ref']


def _inventory(collection):
    """Derive every eligible version and every excluded version from captures."""
    versions = {row['version_id']: row for row in collection['versions']}
    require(len(versions) == len(collection['versions']), 'Repeated original material version')
    captures = {}
    for row in collection['retrievals']:
        identity = row.get('version_id')
        if row.get('state') == 'body' and identity:
            require(identity in versions, 'Original material capture has no canonical version')
            captures.setdefault(identity, []).append(row)
    require(set(captures) == set(versions), 'Original material inventory contains an unbound version')
    materials, excluded = [], []
    start = instant(collection['window_start'])
    for identity in sorted(versions):
        source = versions[identity]
        eligible = [row for row in captures[identity] if row.get('within_information_cutoff') is True]
        capture = (eligible or captures[identity])[0]
        row = {key: source[key] for key in VERSION_FIELDS}
        row.update(url=capture['final_url'], retrieved_at=capture['retrieved_at'],
                   publication_cutoff_relation=capture['publication_cutoff_relation'],
                   within_information_cutoff=bool(eligible), retrieval_id=capture['retrieval_id'])
        if not eligible:
            excluded.append({**row, 'reason': 'publication_or_revision_availability_not_proved_at_information_cutoff'})
            continue
        publication = source['published_at']
        if publication['precision'] == 'timestamp':
            role = 'historical_background' if instant(publication['value']) < start else 'recent_published_instant'
        else:
            start_day = start.astimezone(ZoneInfo(publication['timezone'])).date().isoformat()
            role = ('historical_background' if publication['value'] < start_day else
                    'overlaps_window_start_date_interval' if publication['value'] == start_day else
                    'recent_published_date_interval')
        row['publication_window_role'] = role
        materials.append(row)
    return materials, excluded


def _body(row, store, artifacts):
    version = store.get('news-version', row['version_id'])
    require(version is not None and all(row[key] == version[key] for key in VERSION_FIELDS),
            'Material metadata differs from its canonical original version')
    semantic = {key: value for key, value in version.items() if key not in {'version_id', 'body_ref'}}
    require(fingerprint(semantic) == version['version_id']
            and version['body_ref']['sha256'] == version['body_sha256'], 'Original material version identity differs')
    body = artifacts.read(row['body_ref'])
    require(hashlib.sha256(body).hexdigest() == version['body_sha256'], 'Original material body identity differs')
    return body


def prepare(payload, store, artifacts, operation_id):
    """CAS-only producer for the public material_prepare controller stage."""
    require(operation_id == payload['workflow_id'] + ':materials', 'Original material operation identity differs')
    run, collection, collection_ref = _collection(payload, store, artifacts)
    materials, excluded = _inventory(collection)
    import news_membership
    query_membership_ref = artifacts.put_json(news_membership.build(payload, collection, collection_ref,
                                                                    store, artifacts))
    pages = []
    for offset in range(0, len(materials), PAGE_ENTRIES):
        rows = materials[offset:offset + PAGE_ENTRIES]
        pages.append({'offset': offset, 'count': len(rows), 'first_version_id': rows[0]['version_id'],
                      'last_version_id': rows[-1]['version_id'], 'part_ref': artifacts.put_json(rows)})
    index = {'schema_id': INDEX_SCHEMA, 'operation_id': operation_id, 'workflow_id': payload['workflow_id'],
             'run_id': run['run_id'], 'collection_id': collection['collection_id'],
             'collection_manifest_ref': collection_ref, 'total_materials': len(materials),
             'query_membership_ref': query_membership_ref,
             'total_original_versions': len(collection['versions']), 'page_entries': PAGE_ENTRIES,
             'pages': pages, 'excluded_ref': artifacts.put_json(excluded), 'excluded_count': len(excluded),
             'body_text_copied': False, 'scope': MATERIAL_SCOPE}
    result = {'status': 'original_materials_prepared', 'workflow_id': payload['workflow_id'],
            'run_id': run['run_id'], 'collection_id': collection['collection_id'],
            'collection_manifest_ref': collection_ref, 'material_index_ref': artifacts.put_json(index),
            'query_membership_ref': query_membership_ref,
            'total_materials': len(materials), 'total_original_versions': len(collection['versions']),
            'excluded_count': len(excluded), 'trade_ready': False, 'scope': MATERIAL_SCOPE}
    import source_reference_closure
    roots = source_reference_closure.material_roots(payload, result, store, artifacts)
    result['source_reference_closure_ref'] = artifacts.put_json(source_reference_closure.build(roots, artifacts))
    return result


def validate(value, payload, store, artifacts, *, replay_originals=True):
    """Independently bind complete page membership to the current collection."""
    fields(value, {'status', 'workflow_id', 'run_id', 'collection_id', 'collection_manifest_ref',
                   'material_index_ref', 'query_membership_ref', 'source_reference_closure_ref',
                   'total_materials', 'total_original_versions', 'excluded_count',
                   'trade_ready', 'scope'}, label='original material result')
    run, collection, reference = _collection(payload, store, artifacts, replay_originals=replay_originals)
    materials, excluded = _inventory(collection)
    expected = {key: payload[key] for key in ('workflow_id', 'run_id', 'collection_id')}
    expected.update(status='original_materials_prepared', collection_manifest_ref=reference,
                    total_materials=len(materials), total_original_versions=len(collection['versions']),
                    excluded_count=len(excluded), trade_ready=False, scope=MATERIAL_SCOPE)
    require(all(value[key] == item for key, item in expected.items()), 'Original material result binding or total differs')
    index = artifacts.read_json(value['material_index_ref'])
    fields(index, {'schema_id', 'operation_id', 'workflow_id', 'run_id', 'collection_id',
                   'collection_manifest_ref', 'query_membership_ref', 'total_materials', 'total_original_versions', 'page_entries',
                   'pages', 'excluded_ref', 'excluded_count', 'body_text_copied', 'scope'}, label='original material index')
    header = {**{key: expected[key] for key in ('workflow_id', 'run_id', 'collection_id',
        'collection_manifest_ref', 'total_materials', 'total_original_versions', 'excluded_count', 'scope')},
        'schema_id': INDEX_SCHEMA, 'operation_id': payload['workflow_id'] + ':materials',
        'query_membership_ref': value['query_membership_ref'],
        'page_entries': PAGE_ENTRIES, 'body_text_copied': False}
    require(all(index[key] == item for key, item in header.items()), 'Original material index header differs')
    require(type(index['pages']) is list and len(index['pages']) == (len(materials) + PAGE_ENTRIES - 1) // PAGE_ENTRIES,
            'Original material part count differs from the complete inventory')
    for position, descriptor in enumerate(index['pages']):
        fields(descriptor, {'offset', 'count', 'first_version_id', 'last_version_id', 'part_ref'}, label='original material part')
        rows = materials[position * PAGE_ENTRIES:(position + 1) * PAGE_ENTRIES]
        require(descriptor['offset'] == position * PAGE_ENTRIES and descriptor['count'] == len(rows)
                and descriptor['first_version_id'] == rows[0]['version_id']
                and descriptor['last_version_id'] == rows[-1]['version_id'], 'Original material part boundaries differ')
        require(artifacts.read_json(descriptor['part_ref']) == rows,
                'Original material page membership differs from the complete canonical collection')
        for row in rows:
            _body(row, store, artifacts)
    require(artifacts.read_json(index['excluded_ref']) == excluded,
            'Excluded original material membership differs from the canonical collection')
    for row in excluded:
        _body(row, store, artifacts)
    import news_membership
    membership_proof = news_membership.validate(value['query_membership_ref'], payload, collection,
                                              reference, store, artifacts)
    import source_reference_closure
    closure = artifacts.read_json(value['source_reference_closure_ref'])
    roots = source_reference_closure.material_roots(payload, value, store, artifacts, recorded_roots=closure['roots'])
    closure_proof = source_reference_closure.validate(value['source_reference_closure_ref'], roots, artifacts)
    return {'status': 'passed', 'scope': MATERIAL_SCOPE,
            'checks': ['canonical_completed_collection', 'complete_version_partition', 'actual_part_and_body_SHA256',
                       'exact_page_membership', 'exact_excluded_membership', *membership_proof['checks'], *closure_proof['checks']],
            'total_materials': len(materials), 'excluded_count': len(excluded), 'trade_ready': False}


def _accepted_result(operation, store, artifacts):
    """Authenticate immutable acceptance without recursively rereading originals."""
    result = operation['result']
    core = {key: value for key, value in result.items() if key != 'automatic_validation'}
    automatic = result.get('automatic_validation')
    require(type(automatic) is dict and automatic['core_result_hash'] == fingerprint(core),
            'Original material operation lacks its accepted core binding')
    manifest = artifacts.read_json(automatic['manifest_ref'])
    final = artifacts.read_json(automatic['final_review_ref'])
    bindings = artifacts.read_json(automatic['operation_bindings_ref'])
    import verify
    require(manifest['operation_id'] == operation['id'] and manifest['round_no'] == automatic['round_no']
            and manifest['code_identity'] == verify.code_identity()
            and final['status'] in {'passed', 'partial'} and final['status'] == automatic['status']
            and final['scope'] == automatic['scope'], 'Original material final acceptance differs')
    name, round_no = 'operation_material_prepare', automatic['round_no']
    evidence = manifest['stages'][name]
    pointer, binding = evidence['accepted'], evidence['binding']
    key = fingerprint({'operation_id': operation['id'], 'stage': name}) + ':r' + str(round_no)
    require(binding == store.get('validation_binding', key)
            and binding['inputs_hash'] == fingerprint({'request': operation['request']})
            and binding['operation_id'] == operation['id'] and binding['stage'] == name
            and binding['round_no'] == round_no and binding['code_identity_hash'] == fingerprint(manifest['code_identity'])
            and binding['validator'] == {'name': 'investment:' + name, 'version': 'automatic-validation-v1'}
            and pointer['status'] == 'accepted' and pointer['binding_key'] == key,
            'Original material immutable validation input binding differs')
    attempt = pointer['attempt']
    require(type(attempt) is int and 1 <= attempt <= 3, 'Invalid original material accepted attempt')
    attempt_key = key + ':a' + str(attempt)
    end = store.get('validation_attempt_end', attempt_key)
    require(end is not None and evidence['attempts'][attempt - 1]['end'] == end
            and evidence['attempts'][attempt - 1]['start'] == store.get('validation_attempt_start', attempt_key),
            'Original material immutable accepted attempt differs')
    for field in ('operation_id', 'stage', 'round_no', 'inputs_hash', 'code_identity_hash', 'validator'):
        require(pointer[field] == binding[field], 'Original material accepted pointer binding differs')
    require(all(pointer[field] == end[field] for field in ('value_ref', 'validation_ref')),
            'Original material accepted result references differ')
    proof = artifacts.read_json(pointer['validation_ref'])
    require(proof['status'] in {'passed', 'partial'} and end['status'] == proof['status']
            and pointer['validation_status'] == proof['status']
            and artifacts.read_json(pointer['value_ref']) == core,
            'Original material accepted core contents differ')
    completion = {'status': 'passed', 'operation_id': operation['id'], 'round_no': round_no,
                  **{field: automatic[field] for field in ('final_review_ref', 'manifest_ref',
                      'operation_bindings_ref', 'core_result_hash')}}
    require(store.get('workflow_validation_end', operation['id'] + ':r' + str(round_no)) == completion,
            'Original material final publication proof differs')
    require(type(bindings) is dict, 'Original material operation bindings must be an object')
    return core


def _index(root, plan, workflow_id):
    store = Store(root, plan)
    artifacts = Artifacts(store.base)
    if workflow_id is None:
        runs = [run for key, run in store.scan('analysis_run') if key.endswith(':analysis')]
        require(runs, 'Prepare the current plan before reading original materials')
        workflow_id = max(runs, key=lambda value: (instant(value['started_at']), value['run_id']))['run_id'][:-9]
    operation = store.operation(workflow_id + ':materials')
    require(operation is not None and operation['status'] == 'completed'
            and operation['request']['operation'] == 'material_prepare',
            'The current original material preparation has not completed; do not reuse an older report')
    core = _accepted_result(operation, store, artifacts)
    payload = operation['request']['payload']
    require(payload['workflow_id'] == workflow_id and payload['run_id'] == workflow_id + ':analysis'
            and all(core[key] == payload[key] for key in ('workflow_id', 'run_id', 'collection_id')),
            'Original material result belongs to another workflow')
    collected = store.operation(workflow_id + ':collect')
    require(collected is not None and collected['status'] == 'completed'
            and collected['request']['operation'] == 'news_collect'
            and collected['result']['manifest_ref'] == core['collection_manifest_ref']
            and collected['result']['collection_id'] == core['collection_id'],
            'Original material index belongs to another accepted source collection')
    index = artifacts.read_json(core['material_index_ref'])
    require(index['schema_id'] == INDEX_SCHEMA and index['operation_id'] == operation['id']
            and all(index[key] == core[key] for key in ('workflow_id', 'run_id', 'collection_id',
                'collection_manifest_ref', 'query_membership_ref', 'total_materials', 'total_original_versions', 'excluded_count', 'scope'))
            and index['page_entries'] == PAGE_ENTRIES and index['body_text_copied'] is False,
            'Original material accepted index header differs')
    return store, artifacts, core, index


def _page(index, number, artifacts):
    descriptor = index['pages'][number]
    rows = artifacts.read_json(descriptor['part_ref'])
    require(type(rows) is list and len(rows) == descriptor['count'] and rows
            and descriptor['offset'] == number * PAGE_ENTRIES
            and rows[0]['version_id'] == descriptor['first_version_id']
            and rows[-1]['version_id'] == descriptor['last_version_id'], 'Original material part differs')
    return rows


def _small_row(row, artifacts):
    return {key: row[key] for key in ('version_id', 'document_id', 'source_id', 'body_ref')} | {
        'material_metadata_ref': artifacts.put_json(row), 'body_requires_version_read': True}


def read(root, plan, *, workflow_id=None, cursor=None, version_id=None, body_cursor=None, output_bytes=OUTPUT_BYTES):
    """Read only requested immutable pages/bodies; complete counts stay visible."""
    require(type(output_bytes) is int and 4096 <= output_bytes <= OUTPUT_BYTES, 'Invalid original output byte budget')
    store, artifacts, core, index = _index(root, plan, workflow_id)
    workflow_id = core['workflow_id']
    require(not (cursor is not None and version_id is not None), 'Choose a page cursor or one original version')
    require(body_cursor is None or version_id is not None, 'An original body cursor requires its version-id')
    if version_id is not None:
        require(type(version_id) is str, 'Original version identity must be text')
        number = bisect.bisect_left([page['last_version_id'] for page in index['pages']], version_id)
        require(number < len(index['pages']), 'Original news version is outside this prepared workflow')
        rows = _page(index, number, artifacts)
        found = next((row for row in rows if row['version_id'] == version_id), None)
        require(found is not None, 'Original news version is outside this prepared workflow')
        text = _body(found, store, artifacts).decode('utf-8')
        position = 0 if body_cursor is None else body_cursor
        require(type(position) is int and 0 <= position <= len(text), 'Invalid original body cursor')
        output = {'status': 'original_material_ready', 'workflow_id': workflow_id, 'material': copy.deepcopy(found),
                  'body_cursor': position, 'next_body_cursor': None, 'total_body_characters': len(text),
                  'body_is_complete': False, 'total_materials': index['total_materials'],
                  'material_index_ref': core['material_index_ref'], 'trade_ready': False, 'scope': MATERIAL_SCOPE}
        if _encoded_size(output) > output_bytes // 2:
            output['material'] = _small_row(found, artifacts)
        low, high = 0, min(len(text) - position, output_bytes)
        while low < high:
            length = (low + high + 1) // 2
            end = position + length
            candidate = {**output, 'material': {**output['material'], 'body': text[position:end]},
                         'next_body_cursor': end if end < len(text) else None,
                         'body_is_complete': position == 0 and end == len(text)}
            if _encoded_size(candidate) <= output_bytes:
                low = length
            else:
                high = length - 1
        end = position + low
        output.update(material={**output['material'], 'body': text[position:end]},
                      next_body_cursor=end if end < len(text) else None,
                      body_is_complete=position == 0 and end == len(text))
        require(_encoded_size(output) <= output_bytes and (low > 0 or position == len(text)),
                'Original body metadata exceeds the output budget')
        return output
    offset = 0 if cursor is None else cursor
    require(type(offset) is int and 0 <= offset <= index['total_materials'], 'Invalid original material cursor')
    output = {'status': 'original_material_page', 'workflow_id': workflow_id, 'assessment_material': [],
              'total_materials': index['total_materials'], 'total_original_versions': index['total_original_versions'],
              'excluded_count': index['excluded_count'], 'excluded_material_ref': index['excluded_ref'],
              'cursor': offset, 'next_cursor': offset if offset < index['total_materials'] else None,
              'remaining_materials': index['total_materials'] - offset,
              'material_index_ref': core['material_index_ref'], 'trade_ready': False, 'scope': MATERIAL_SCOPE,
              'output_paging_is_coverage_limit': False}
    number, page = None, None
    for position in range(offset, min(index['total_materials'], offset + PAGE_ENTRIES)):
        requested_page = position // PAGE_ENTRIES
        if number != requested_page:
            number, page = requested_page, _page(index, requested_page, artifacts)
        row = page[position % PAGE_ENTRIES]
        body = _body(row, store, artifacts)
        item = copy.deepcopy(row)
        if len(body) <= output_bytes // 3:
            item['body'] = body.decode('utf-8')
        else:
            item['body_requires_version_read'] = True
        candidate = {**output, 'assessment_material': [*output['assessment_material'], item],
                     'next_cursor': position + 1 if position + 1 < index['total_materials'] else None,
                     'remaining_materials': index['total_materials'] - position - 1}
        if _encoded_size(candidate) > output_bytes and 'body' in item:
            item.pop('body')
            item['body_requires_version_read'] = True
            candidate['assessment_material'][-1] = item
        if _encoded_size(candidate) > output_bytes:
            if output['assessment_material']:
                break
            candidate['assessment_material'][-1] = _small_row(row, artifacts)
        require(_encoded_size(candidate) <= output_bytes, 'Original material identity exceeds output budget')
        output = candidate
    require(_encoded_size(output) <= output_bytes, 'Original material page exceeds output budget')
    return output


def compact(prepared, root, plan):
    """Keep stdout bounded while preserving complete metadata and original refs."""
    if _encoded_size(prepared) <= OUTPUT_BYTES:
        return prepared
    store = Store(root, plan)
    artifacts = Artifacts(store.base)
    full_ref = artifacts.put_json(prepared)
    collection, run = prepared.get('collection'), prepared['analysis_run']
    output = {'status': prepared['status'], 'workflow_id': prepared['workflow_id'],
              'currency': prepared['currency'], 'platform': prepared['platform'],
              'account_status_ref': artifacts.put_json(prepared['account_status']),
              'analysis_run': {'run_id': run['run_id'], 'publish_cutoff': run['publish_cutoff'],
                  'news_policy_hash': run['news_policy_hash'], 'registered_sources': len(run['news_policy']['sources']),
                  'required_source_group_ids': sorted(run['news_policy']['required_source_groups'])},
              'publisher_discovery_ref': artifacts.put_json(prepared['publisher_discovery']),
              'news_retrieval_ref': artifacts.put_json(prepared['news_retrieval']) if prepared.get('news_retrieval') else None,
              'news_controller_ref': artifacts.put_json(prepared['news_controller']),
              'complete_preparation_ref': full_ref, 'trade_ready': False,
              'scope': prepared['scope'], 'output_paging_is_coverage_limit': False,
              'required_actions': prepared['required_actions']}
    if collection is not None:
        output['collection'] = {key: collection[key] for key in
                               ('collection_id', 'status', 'information_cutoff_at', 'manifest_ref')}
        output['assessment_material_index_ref'] = prepared['assessment_material_index_ref']
        output['total_materials'] = prepared['total_materials']
        output['total_original_versions'] = prepared['total_original_versions']
        output['excluded_body_versions_count'] = prepared['excluded_body_versions_count']
        output['material_page'] = read(root, plan, workflow_id=prepared['workflow_id'], output_bytes=OUTPUT_BYTES // 2)
        output['request_templates_ref'] = artifacts.put_json(prepared['request_templates'])
        output['next_operation'] = 'read_all_relevant_original_materials_then_assess'
    require(_encoded_size(output) <= OUTPUT_BYTES, 'Prepared output metadata exceeds its bounded stdout budget')
    return output
