"""SC03宿主工作读写边界；模型执行由根宿主实际调用，不在Python中模拟。"""
from __future__ import annotations

from pathlib import Path

from investment.contracts import ContractError, canonical, digest, known, na, require, unknown, instant
from investment.research.contracts import ResearchSchemas, guard_namespace
from investment.research.host import BINDING, _strict_model_json, validate_host_response, validate_schema_bundle
from investment.research.references import ResearchResolver
from investment.runtime.paths import validate_input_path
from investment.runtime.runs import atomic


def open_host_work(root, request_path, *, mode):
    """仅向模型开放该请求冻结的提示、Schema及授权材料。"""
    selected = guard_namespace(root, mode)
    path = validate_input_path(selected, request_path)
    schemas = ResearchSchemas(('SC03',))
    controller = ResearchResolver(selected, schemas, mode=mode)
    request_ref = controller.path_ref(path, 'investment.host_request')
    request = controller.resolve(request_ref, 'investment.host_request')
    require(request['stage_id'] == 'SC03', 'RS05', '/stage_id', 'SC03 host work required')
    expected = (selected / 'runs/sc03' / Path(request['input_ref']['uri'].split('://sc03/', 1)[1]).parent
                / f"attempt-{request['attempt_no']:02}"
                / ('host-' + request['step_id'] + '-' + request['work_key'] + '-request.json'))
    require(path == expected.resolve(), 'RS05', '/request', 'Host request locator differs from its invocation')
    method = controller.resolve(request['method_ref'], 'investment.sc03_method')
    allowed = [request_ref] + [request[key] for key in ('input_ref', 'method_ref', 'prompt_ref', 'output_schema_ref')]
    allowed += request['allowed_evidence_refs']
    if request['candidate_ref']['status'] == 'known':
        allowed.append(request['candidate_ref']['value'])
    resolver = ResearchResolver(selected, schemas, mode=mode, allowed_refs=allowed,
                                reference_grants=method['runtime']['reference_grants'])
    # 上游凭证只可读其自身，不能通过内含引用扩大权限。
    for reference in allowed:
        if reference['schema_id'] == 'investment.validation_receipt':
            resolver.opaque_refs.add(reference['uri'])
    validate_schema_bundle(request['output_schema_ref'], request['output_schema_id'], resolver)
    return request, resolver


def record_host_result(root, request_path, *, mode, started_at, finished_at,
                       context_id, tool_call_id, model_id=None, error=None):
    """绑定真实工具执行后产生的raw文件；无法解析的实际响应如实记失败。"""
    request, resolver = open_host_work(root, request_path, mode=mode)
    require(instant(request['created_at']) <= instant(started_at) <= instant(finished_at),
            'RS05', '/started_at', 'Actual host timestamps are inconsistent')
    directory = Path(request_path).resolve().parent
    prefix = 'host-' + request['step_id'] + '-' + request['work_key']
    writer = ResearchResolver(resolver.root, resolver.schemas, mode=mode)

    def save(suffix, value, schema_id):
        resolver.schemas.validate(schema_id, value)
        target = directory / (prefix + suffix)
        atomic(target, canonical(value), immutable=True)
        reference = writer.path_ref(target, schema_id)
        resolver.allow(reference)
        return reference

    raw_path = directory / (prefix + '-raw.txt')
    raw_ref = unknown('actual_tool_returned_no_raw_output')
    output_ref = na('host_execution_failed')
    if raw_path.exists():
        raw = raw_path.read_bytes().decode('utf-8')
        raw_reference = writer.path_ref(raw_path, 'investment.research_text')
        resolver.allow(raw_reference)
        raw_ref = known(raw_reference)
        if error is None:
            try:
                output = _strict_model_json(raw)
                resolver.schemas.validate(request['output_schema_id'], output)
            except (ContractError, ValueError, TypeError) as exc:
                error = {'code': 'invalid_model_output', 'reason': str(exc)[:2048]}
            else:
                output_ref = known(save('-output.json', output, request['output_schema_id']))
    else:
        require(error is not None, 'RS05', '/raw_response_ref', 'Actual model output is missing')
    binding = {key: request[key] for key in BINDING}
    binding['request_sha256'] = digest(canonical(request))
    status = 'failed' if error is not None else 'completed'
    host_error = known(error) if error is not None else na('host_execution_completed')
    event = dict(binding, status=status, error=host_error, executor='codex_host', context_policy='fresh_context',
                 context_id=known(context_id) if context_id else unknown('actual_context_identifier_not_exposed_by_host'),
                 tool_call_id=known(tool_call_id) if tool_call_id else unknown('actual_tool_call_identifier_not_exposed_by_host'),
                 model_id=known(model_id) if model_id else unknown('actual_model_identifier_not_exposed_by_host'),
                 started_at=started_at, finished_at=finished_at, raw_response_ref=raw_ref)
    event_ref = save('-event.json', event, 'investment.host_event')
    response = dict(binding, status=status, error=host_error, event_ref=event_ref,
                    raw_response_ref=raw_ref, output_ref=output_ref)
    reference = save('-response.json', response, 'investment.host_response')
    if status == 'completed':
        validate_host_response(request, response, resolver)
    return reference, response
