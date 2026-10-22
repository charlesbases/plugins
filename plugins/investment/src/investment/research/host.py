"""宿主新上下文工作协议；此模块不模拟模型调用或授予业务通过。"""
from __future__ import annotations

import json
import secrets
import uuid

from investment.contracts import ContractError, SPEC, artifact, canonical, digest, instant, is_known, known, na, now, require
from investment.runtime.runs import atomic

BINDING = ('run_id', 'stage_id', 'invocation_id', 'attempt_no', 'request_id', 'nonce', 'step_id', 'work_key', 'role')


def _strict_model_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate model output key')
            result[key] = value
        return result

    def no_float(value):
        raise ValueError('Model numeric estimates must use declared decimal strings')

    def integer(value):
        result = int(value)
        if value == '-0' or abs(result) > 9007199254740991:
            raise ValueError('Model integer out of interoperable range')
        return result

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_int=integer, parse_float=no_float, parse_constant=no_float)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ContractError('RS05', '/raw_response_ref', 'Model response is not strict JSON', 'schema_violation') from exc


def validate_host_response(request, response, resolver):
    """校验身份和实际字节，实际调用真实性仍由宿主工具记录证明。"""
    schemas = resolver.schemas
    schemas.validate('investment.host_request', request)
    schemas.validate('investment.host_response', response)
    require(all(response[key] == request[key] for key in BINDING)
            and response['request_sha256'] == digest(canonical(request)),
            'RS05', '/request_id', 'Host response does not bind current request', 'identity_mismatch')
    expected_dir = request['input_ref']['uri'].rsplit('/', 1)[0] + f"/attempt-{request['attempt_no']:02}/"
    references = [('event_ref', '-event.json', response['event_ref'])]
    references += [(key, suffix, response[key]['value']) for key, suffix in
                   (('raw_response_ref', '-raw.txt'), ('output_ref', '-output.json')) if is_known(response[key])]
    for key, suffix, reference in references:
        require(reference['uri'] == expected_dir + 'host-' + request['step_id'] + '-' + request['work_key'] + suffix,
                'RS05', '/' + key, 'Host artifact is outside current step', 'identity_mismatch')
        resolver.allow(reference)
    event = resolver.resolve(response['event_ref'], 'investment.host_event')
    require(all(event[key] == request[key] for key in BINDING)
            and event['request_sha256'] == response['request_sha256']
            and all(event[key] == response[key] for key in ('raw_response_ref', 'status', 'error')),
            'RS05', '/event_ref', 'Host event binding differs', 'identity_mismatch')
    require(instant(request['created_at']) <= instant(event['started_at']) <= instant(event['finished_at']) <= instant(now()),
            'RS05', '/event_ref/started_at', 'Host event chronology differs from current request', 'identity_mismatch')
    for key in ('input_ref', 'method_ref', 'prompt_ref', 'output_schema_ref'):
        resolver.resolve(request[key])
    validate_schema_bundle(request['output_schema_ref'], request['output_schema_id'], resolver)
    if response['status'] == 'failed':
        require(is_known(response['error']) and response['output_ref']['status'] == 'not_applicable'
                and response['raw_response_ref']['status'] in ('known', 'unknown'),
                'RS05', '/error', 'Failed host response must preserve actual error and no output', 'schema_violation')
        if is_known(response['raw_response_ref']):
            resolver.resolve(response['raw_response_ref']['value'], 'investment.research_text')
        raise ContractError('RS05', '/error', response['error']['value']['code'] + ': ' + response['error']['value']['reason'], 'execution_failed')
    require(response['error']['status'] == 'not_applicable' and is_known(response['raw_response_ref']) and is_known(response['output_ref']),
            'RS05', '/status', 'Completed host work requires actual raw and structured output', 'schema_violation')
    raw = resolver.resolve(response['raw_response_ref']['value'], 'investment.research_text')
    output = resolver.resolve(response['output_ref']['value'], request['output_schema_id'])
    require(canonical(_strict_model_json(raw)) == canonical(output), 'RS05', '/output_ref', 'Structured output differs from actual raw response', 'identity_mismatch')
    return output


def validate_schema_bundle(reference, schema_id, resolver):
    schemas = resolver.schemas
    bundle = resolver.resolve(reference, 'investment.research_schema_bundle')
    require(bundle['root_schema_id'] == schema_id and bundle['root_target'] == schemas.targets[schema_id]['target'],
            'RS05', '/root_target', 'Frozen schema root differs', 'identity_mismatch')
    resources = bundle['resources']
    require([row['resource_id'] for row in resources] == sorted(schemas.documents),
            'RS05', '/resources', 'Frozen transitive schema resources differ', 'identity_mismatch')
    for row in resources:
        rid = row['resource_id']
        resolver.allow(row['document_ref'])
        require(row['installed_filename'] == schemas.installed_files[rid]
                and row['installed_sha256'] == digest((SPEC / row['installed_filename']).read_bytes())
                and resolver.resolve(row['document_ref'], 'investment.research_schema_document') == schemas.documents[rid],
                'RS05', '/resources', 'Installed schema bytes changed after freezing', 'identity_mismatch')
    return bundle


def _step(ctx, step_id):
    matches = [step for step in ctx.frozen_host_steps if step['step_id'] == step_id and step['role'] == ctx.role]
    require(len(matches) == 1, 'RS05', '/step_id', 'Unknown role or host step')
    return matches[0]


def _path_ref(ctx, filename, schema_id):
    path = ctx.attempt_dir / filename
    raw = path.read_bytes()
    uri = 'investment-run://' + ctx.stage_id.lower() + '/' + path.relative_to(ctx.root / 'runs' / ctx.stage_id.lower()).as_posix()
    reference = artifact(raw, schema_id, uri)
    reference['media_type'] = ctx.schemas.targets[schema_id]['media_type']
    ctx.resolver.allow(reference)
    return reference


def create_host_request(ctx, step_id, allowed_evidence_refs, *, work_key='main'):
    step = _step(ctx, step_id)
    ctx.schemas.validate_type('PathID', work_key)
    for reference in allowed_evidence_refs:
        ctx.resolver.resolve(reference)
        require(not reference['uri'].rsplit('/', 1)[-1].startswith('host-') or reference in ctx.resolver.host_output_refs,
                'RS06', '/allowed_evidence_refs', 'Host work may only reuse bound same-role structured outputs', 'use_not_allowed')
    require(len({item['uri'] for item in allowed_evidence_refs}) == len(allowed_evidence_refs),
            'RS05', '/allowed_evidence_refs', 'Duplicate host evidence reference')
    fixed = {key: getattr(ctx, key) for key in ('run_id', 'stage_id', 'invocation_id', 'attempt_no', 'role')}
    fixed.update(step_id=step_id, work_key=work_key, input_ref=ctx.input_ref,
                 candidate_ref=known(ctx.candidate_ref) if ctx.candidate_ref else na('producer_has_no_candidate'),
                 method_ref=ctx.method_ref, prompt_ref=step['prompt_ref'], output_schema_ref=step['output_schema_ref'],
                 output_schema_id=step['output_schema_id'], allowed_evidence_refs=allowed_evidence_refs, context_policy='fresh_context')
    path = ctx.attempt_dir / ('host-' + step_id + '-' + work_key + '-request.json')
    if path.exists():
        reference = _path_ref(ctx, path.name, 'investment.host_request')
        request = ctx.resolver.resolve(reference, 'investment.host_request')
        require(all(request[key] == value for key, value in fixed.items()),
                'RS05', '/request_id', 'Existing host request cannot change on continuation', 'identity_mismatch')
        return request
    request = dict(fixed, request_id='host-' + uuid.uuid4().hex, nonce=secrets.token_hex(32), created_at=now())
    ctx.schemas.validate('investment.host_request', request)
    atomic(path, canonical(request), immutable=True)
    ctx.resolver.allow(_path_ref(ctx, path.name, 'investment.host_request'))
    return request


def read_host_response(ctx, step_id, *, work_key='main'):
    _step(ctx, step_id)
    ctx.schemas.validate_type('PathID', work_key)
    prefix = 'host-' + step_id + '-' + work_key
    path = ctx.attempt_dir / (prefix + '-response.json')
    if not path.exists():
        return None
    request_ref = _path_ref(ctx, prefix + '-request.json', 'investment.host_request')
    request = ctx.resolver.resolve(request_ref, 'investment.host_request')
    require(all(request[key] == getattr(ctx, key) for key in ('run_id', 'stage_id', 'invocation_id', 'attempt_no', 'role'))
            and request['step_id'] == step_id and request['work_key'] == work_key,
            'RS05', '/request_id', 'Host request differs from current context', 'identity_mismatch')
    response_ref = _path_ref(ctx, path.name, 'investment.host_response')
    response = ctx.resolver.resolve(response_ref, 'investment.host_response')
    output = validate_host_response(request, response, ctx.resolver)
    reference = response['output_ref']['value']
    if reference not in ctx.resolver.host_output_refs:
        ctx.resolver.host_output_refs.append(reference)
    return output
