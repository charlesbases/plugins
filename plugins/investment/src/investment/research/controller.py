"""冻结研究调用，实际运行独立流程/验证进程，并保持三次预算与宿主续行。"""
from __future__ import annotations

import argparse
import html
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from investment.contracts import ContractError, canonical, instant, is_known, known, na, now, require, unknown
from investment.research.contracts import PLUGIN, STAGES, ResearchSchemas, StageContext, guard_namespace
from investment.research.host import validate_host_response, validate_schema_bundle
from investment.research.references import ResearchResolver
from investment.chains.news.contracts import parse_news
from investment.runtime.paths import validate_input_path
from investment.runtime.runs import atomic


def _schemas(stage_id):
    require(stage_id in STAGES, 'RS03', '/stage_id', 'Unsupported research stage')
    return ResearchSchemas(STAGES[:STAGES.index(stage_id) + 1])


def _put(resolver, path, value, schema_id):
    require(path.resolve().is_relative_to(resolver.root / 'runs'), 'RS01', '/path', 'Output path escapes selected run root', 'use_not_allowed')
    resolver.schemas.validate(schema_id, value)
    atomic(path, canonical(value), immutable=True)
    return resolver.path_ref(path, schema_id)


def _text(resolver, path, raw, schema_id):
    require(path.resolve().is_relative_to(resolver.root / 'runs'), 'RS01', '/path', 'Output path escapes selected run root', 'use_not_allowed')
    resolver.schemas.validate(schema_id, raw.decode('utf-8'))
    atomic(path, raw, immutable=True)
    return resolver.path_ref(path, schema_id)


def _manifest(resolver, stage_id, day):
    path = resolver.root / 'runs' / stage_id.lower() / day / 'manifest.json'
    if not path.exists():
        return {'schema_version': 1, 'stage_id': stage_id, 'execution_date': day, 'timezone': 'Asia/Shanghai', 'invocations': []}
    manifest = resolver.schemas.document('investment.research_daily_manifest', path.read_bytes())
    ids = [item['invocation_id'] for item in manifest['invocations']]
    require(manifest['stage_id'] == stage_id and manifest['execution_date'] == day and len(ids) == len(set(ids)),
            'RS03', '/manifest', 'Manifest identity or invocation uniqueness differs')
    for invocation in manifest['invocations']:
        numbers = [item['attempt_no'] for item in invocation['attempts']]
        require(numbers == list(range(1, len(numbers) + 1)) and len(numbers) <= 3,
                'RS04', '/attempts', 'Persisted attempt budget is inconsistent')
        require(all(item['state'] == 'failed' for item in invocation['attempts'][:-1]),
                'RS04', '/attempts', 'Only failed attempts may precede the current attempt')
    return manifest


def _save(resolver, manifest):
    resolver.schemas.validate('investment.research_daily_manifest', manifest)
    directory = resolver.root / 'runs' / manifest['stage_id'].lower() / manifest['execution_date']
    require(directory.resolve().is_relative_to(resolver.root / 'runs'), 'RS01', '/manifest', 'Manifest directory escapes selected run root', 'use_not_allowed')
    atomic(directory / 'manifest.json', canonical(manifest))
    lines = ['# ' + manifest['stage_id'] + ' ' + manifest['execution_date'], '',
             'Invocation files retain frozen input, methods, schema resources, process logs, host work, candidates and receipts.', '',
             '| Invocation | Run | State | Attempts | Failures | Files | Retention |', '|---|---|---|---|---|---|---|']
    for invocation in manifest['invocations']:
        base = directory / invocation['invocation_id']
        files = sorted(path.relative_to(directory).as_posix() for path in base.rglob('*') if path.is_file()) if base.exists() else []
        links = ', '.join('[' + html.escape(name) + '](' + name + ')' for name in files)
        failures = []
        for attempt in invocation['attempts']:
            if not is_known(attempt['failure_ref']):
                continue
            reference = attempt['failure_ref']['value']
            failure = resolver.resolve(reference, 'investment.research_failure')
            stage, parts = resolver.run_parts(reference['uri'])
            require(stage == manifest['stage_id'].lower() and parts[:3] == [manifest['execution_date'], invocation['invocation_id'], f"attempt-{attempt['attempt_no']:02}"]
                    and failure['run_id'] == invocation['run_id'] and failure['invocation_id'] == invocation['invocation_id']
                    and failure['stage_id'] == manifest['stage_id'] and failure['attempt_no'] == attempt['attempt_no'],
                    'RS08', '/failure_ref', 'README failure does not belong to its recorded attempt', 'identity_mismatch')
            reason = ' '.join(failure['reason'].split())
            summary = html.escape(reason[:160] + ('…' if len(reason) > 160 else '')).replace('|', '&#124;')
            relative = '/'.join(parts[1:])
            failures.append(f"attempt-{attempt['attempt_no']:02}: {summary} ([details]({relative}))")
        lines.append('| ' + ' | '.join((invocation['invocation_id'], invocation['run_id'], invocation['state'],
                                       str(len(invocation['attempts'])), '<br>'.join(failures) or '—', links, html.escape(invocation['retention']['reason']))) + ' |')
    atomic(directory / 'README.md', ('\n'.join(lines) + '\n').encode('utf-8'))


@contextmanager
def _stage_lock(root, stage_id):
    directory = root / 'runs' / stage_id.lower()
    require(directory.resolve().is_relative_to(root / 'runs'), 'RS01', '/lock', 'Lock directory escapes selected run root', 'use_not_allowed')
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / '.controller.lock').open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _input(resolver, reference, stage_id, run_id, invocation_id, *, upstream_receipts=None):
    incoming = resolver.resolve(reference, 'investment.stage_input')
    require((incoming['stage_id'], incoming['run_id'], incoming['invocation_id']) == (stage_id, run_id, invocation_id),
            'RS03', '/input', 'Stage input differs from CLI identity', 'identity_mismatch')
    definition = resolver.schemas.definition(stage_id)
    actual = [item['role'] for item in incoming['input_refs']]
    registered = {item['role']: item for item in definition['input_roles']}
    expected = [item['role'] for item in definition['input_roles'] if item['role'] in actual]
    require(actual == expected and len(actual) == len(set(actual))
            and all(not item['required'] or item['role'] in actual for item in definition['input_roles']),
            'RS03', '/input_refs', 'Required, ordered, unique input roles differ')
    require(instant(incoming['information_cutoff_at']) <= instant(incoming['decision_at']),
            'RS03', '/decision_at', 'Information cutoff is after frozen decision time')
    for item in incoming['input_refs']:
        resolver.resolve(item['ref'], registered[item['role']]['schema_id'])
        source = registered[item['role']]['source']
        if source['kind'] == 'upstream_output':
            binding = _upstream_binding(resolver, item['ref'], source, incoming)
            if upstream_receipts is not None:
                upstream_receipts[item['role']] = binding
    resolver.resolve(incoming['parameters_ref'], definition['parameters_schema_id'])
    resolver.resolve(incoming['scope_ref'])
    return incoming


def _upstream_binding(resolver, reference, source, incoming):
    """核对本轮真实封存交接，不把类型登记或存在字节当成上游业务验收。"""
    stage_id = source['stage_id']
    parsed = urlsplit(reference['uri'])
    parts = parsed.path.strip('/').split('/')
    require(parsed.scheme == 'investment-run' and parsed.netloc == stage_id.lower() and len(parts) == 4,
            'RS03', '/input_refs', 'Upstream output requires its exact stage attempt locator', 'identity_mismatch')
    day, invocation_id, attempt_directory, _ = parts
    path = resolver.root / 'runs' / stage_id.lower() / day / 'manifest.json'
    require(path.resolve().is_relative_to(resolver.root / 'runs'), 'RS01', '/manifest', 'Upstream manifest escapes selected root')
    schema_id = {'SC01': 'investment.daily_manifest', 'SC02': 'investment.sc02_daily_manifest'}.get(stage_id, 'investment.research_daily_manifest')
    manifest = resolver.schemas.document(schema_id, path.read_bytes())
    matches = [item for item in manifest['invocations'] if item['invocation_id'] == invocation_id]
    require(manifest['stage_id'] == stage_id and manifest['execution_date'] == day and len(matches) == 1,
            'RS03', '/upstream_manifest', 'Upstream manifest identity is ambiguous', 'identity_mismatch')
    invocation = matches[0]
    state = invocation['processing_state'] if stage_id in ('SC01', 'SC02') else invocation['state']
    require(state == 'completed' and invocation['run_id'] == incoming['run_id'] and is_known(invocation['sealed_output_ref']),
            'RS03', '/upstream_invocation', 'Upstream is not completed and sealed in this run', 'use_not_allowed')
    if stage_id == 'SC01':
        require(is_known(invocation['selected_attempt']) and invocation['next_chain_allowed'], 'RS03', '/selected_attempt', 'SC01 has no committed selected attempt')
        attempts = [item for item in invocation['attempts'] if item['attempt_no'] == invocation['selected_attempt']['value']]
        require(len(attempts) == 1, 'RS03', '/selected_attempt', 'SC01 selected attempt is ambiguous')
        selected = attempts[0]
        require(selected['state'] == 'passed', 'RS03', '/selected_attempt', 'SC01 selected attempt has not passed')
        receipt_ref = selected['output_validation_ref']
        require(selected['sealed_ref'] == invocation['sealed_output_ref'], 'RS03', '/sealed_ref', 'SC01 committed seal binding differs')
    else:
        require(bool(invocation['attempts']), 'RS03', '/attempts', 'Completed upstream has no actual attempt')
        selected = invocation['attempts'][-1]
        selected_state = selected['processing_state'] if stage_id == 'SC02' else selected['state']
        require(selected_state == 'completed', 'RS03', '/attempts', 'Selected upstream attempt is not completed')
        receipt_ref = selected['receipt_ref']
    require(attempt_directory == f"attempt-{selected['attempt_no']:02}" and is_known(selected['candidate_ref']) and is_known(receipt_ref),
            'RS03', '/attempt_no', 'Input belongs to a different or unvalidated upstream attempt', 'identity_mismatch')
    seal = resolver.resolve(invocation['sealed_output_ref']['value'], 'investment.sealed_output')
    require(seal['candidate_output_ref'] == selected['candidate_ref']['value'] and seal['validation_receipt_ref'] == receipt_ref['value'],
            'RS03', '/sealed_output', 'Seal differs from upstream manifest bindings', 'identity_mismatch')
    candidate = resolver.resolve(seal['candidate_output_ref'], 'investment.candidate_output')
    receipt = resolver.resolve(seal['validation_receipt_ref'], 'investment.validation_receipt')
    original = resolver.resolve(invocation['frozen_input_ref'], 'investment.stage_input')
    require(all(item['run_id'] == incoming['run_id'] and item['stage_id'] == stage_id for item in (seal, candidate, receipt, original))
            and original['invocation_id'] == invocation_id and seal['output_id'] == candidate['output_id']
            and candidate['scope_ref'] == original['scope_ref'] and candidate['input_refs'] == original['input_refs']
            and candidate['method_ref'] == invocation['method_ref'] and receipt['input_refs'] == original['input_refs']
            and receipt['candidate_output_ref'] == seal['candidate_output_ref']
            and receipt['validator_id'] == stage_id.lower() + '-validator-v1' and receipt['validator_version'] == 'v1',
            'RS03', '/upstream_binding', 'Upstream frozen input, scope, candidate or receipt identity differs', 'identity_mismatch')
    require(reference in [candidate['payload_ref']] + candidate['datasets'] + candidate['evidence_refs'],
            'RS03', '/input_refs', 'Input is not an exported artifact owned by the completed candidate', 'identity_mismatch')
    require(instant(original['information_cutoff_at']) <= instant(incoming['information_cutoff_at']),
            'RS03', '/information_cutoff_at', 'Upstream cutoff is later than current permitted information')
    require(receipt['overall_verdict'] in ('passed', 'partial') and receipt['business_result'] in ('usable', 'observation_only')
            and set(source['required_uses']) <= set(receipt['allowed_uses']),
            'RS07', '/required_uses', 'Upstream receipt does not permit this declared consumption', 'use_not_allowed')
    return seal['validation_receipt_ref'], receipt


def _candidate(resolver, candidate, incoming, method_ref, definition, *, attempt_no):
    resolver.schemas.validate('investment.candidate_output', candidate)
    require(candidate['run_id'] == incoming['run_id'] and candidate['stage_id'] == incoming['stage_id']
            and candidate['input_refs'] == incoming['input_refs'] and candidate['scope_ref'] == incoming['scope_ref']
            and candidate['method_ref'] == method_ref, 'RS03', '/candidate', 'Candidate identities or frozen inputs differ', 'identity_mismatch')
    prefix = method_ref['uri'].rsplit('/', 1)[0] + f'/attempt-{attempt_no:02}/'
    require(candidate['payload_ref']['uri'].startswith(prefix), 'RS03', '/payload_ref', 'Candidate payload must be produced in current attempt', 'identity_mismatch')
    require(instant(incoming['decision_at']) <= instant(candidate['created_at']) <= instant(now()),
            'RS03', '/created_at', 'Candidate creation time differs from current invocation')
    resolver.resolve(candidate['payload_ref'], definition['payload_schema_id'])
    for reference in candidate['datasets'] + candidate['evidence_refs']:
        require(not reference['uri'].rsplit('/', 1)[-1].startswith('host-'),
                'RS06', '/evidence_refs', 'Candidate cannot expose producer private host records', 'use_not_allowed')
        resolver.resolve(reference)


def _receipt(resolver, receipt, incoming, candidate_ref, method):
    resolver.schemas.validate('investment.validation_receipt', receipt)
    definition = method['runtime']
    require(receipt['validator_id'] == incoming['stage_id'].lower() + '-validator-v1' and receipt['validator_version'] == 'v1'
            and receipt['run_id'] == incoming['run_id'] and receipt['stage_id'] == incoming['stage_id']
            and receipt['input_refs'] == incoming['input_refs'] and receipt['candidate_output_ref'] == candidate_ref,
            'RS07', '/receipt', 'Independent receipt identity differs', 'identity_mismatch')
    candidate = resolver.resolve(candidate_ref, 'investment.candidate_output')
    require(instant(candidate['created_at']) <= instant(receipt['validated_at']) <= instant(now()),
            'RS07', '/validated_at', 'Receipt precedes completed candidate or is in the future')
    require([check['rule_id'] for check in receipt['checks']] == definition['required_rules'],
            'RS07', '/checks', 'Independent receipt lacks exact ordered rules', 'identity_mismatch')
    verdicts = {check['rule_id']: check['verdict'] for check in receipt['checks']}
    require('not_evaluated' not in verdicts.values(), 'RS07', '/checks', 'Required rule was not evaluated')
    critical = all(verdicts[rule] == 'passed' for rule in definition['critical_rules'])
    allowed = [item['use'] for item in definition['use_rules'] if critical
               and all(verdicts[rule] == 'passed' for rule in item['required_pass_rules'])]
    require(receipt['allowed_uses'] == allowed, 'RS07', '/allowed_uses', 'Receipt does not match frozen use-to-rule gates', 'use_not_allowed')
    for check in receipt['checks']:
        for reference in check['evidence_refs']:
            resolver.resolve(reference)
    return bool(allowed) and critical and receipt['overall_verdict'] in ('passed', 'partial') and receipt['business_result'] in ('usable', 'observation_only')


def load_context(root, *, mode, stage_id, input_path, run_id, invocation_id, attempt_no, role, candidate_path=None):
    """仅从控制器已登记的当前attempt构造阶段能力，不替直接脚本创建预算。"""
    selected = guard_namespace(root, mode)
    path = validate_input_path(selected, input_path)
    schemas = _schemas(stage_id)
    resolver = ResearchResolver(selected, schemas, mode=mode)
    input_ref = resolver.path_ref(path, 'investment.stage_input')
    stage, parts = resolver.run_parts(input_ref['uri'])
    require(stage == stage_id.lower() and parts[1:] == [invocation_id, 'input.json'],
            'RS03', '/input', 'Stage subprocess requires exact frozen input')
    manifest = _manifest(resolver, stage_id, parts[0])
    matches = [item for item in manifest['invocations'] if item['invocation_id'] == invocation_id]
    require(len(matches) == 1, 'RS03', '/invocation_id', 'Invocation was not registered before process execution')
    invocation = matches[0]
    require(invocation['run_id'] == run_id and invocation['frozen_input_ref'] == input_ref
            and 1 <= attempt_no <= len(invocation['attempts']) and attempt_no == len(invocation['attempts']),
            'RS03', '/attempt_no', 'Subprocess is not the current persisted attempt', 'identity_mismatch')
    attempt = invocation['attempts'][-1]
    require(role in ('producer', 'validator') and attempt['state'] == ('processing' if role == 'producer' else 'validating'),
            'RS06', '/role', 'Subprocess role does not match persisted state')
    upstream_receipts = {}
    incoming = _input(resolver, input_ref, stage_id, run_id, invocation_id, upstream_receipts=upstream_receipts)
    method = resolver.resolve(invocation['method_ref'], 'investment.' + stage_id.lower() + '_method')
    require(method == schemas.stage_methods[stage_id], 'RS03', '/method_ref', 'Installed method changed after freezing', 'identity_mismatch')
    definition = method['runtime']
    validate_schema_bundle(invocation['schema_bundle_ref'], definition['payload_schema_id'], resolver)
    base = path.parent
    attempt_dir = base / f'attempt-{attempt_no:02}'
    candidate, candidate_ref = None, None
    allowed = [input_ref, incoming['scope_ref'], incoming['parameters_ref'], invocation['method_ref']]
    allowed += [item['ref'] for item in incoming['input_refs']]
    if role == 'validator':
        require(candidate_path is not None and Path(candidate_path).resolve() == (attempt_dir / 'candidate.json').resolve()
                and is_known(attempt['candidate_ref']), 'RS06', '/candidate', 'Validator requires the bound completed candidate')
        candidate_ref = resolver.path_ref(Path(candidate_path), 'investment.candidate_output')
        require(candidate_ref == attempt['candidate_ref']['value'], 'RS06', '/candidate', 'Candidate binding changed')
        candidate = resolver.resolve(candidate_ref, 'investment.candidate_output')
        _candidate(resolver, candidate, incoming, invocation['method_ref'], definition, attempt_no=attempt_no)
        allowed += [candidate_ref, candidate['payload_ref']] + candidate['datasets'] + candidate['evidence_refs']
    steps = [step for step in invocation['host_steps'] if step['role'] == role]
    allowed += [step[key] for step in steps for key in ('prompt_ref', 'output_schema_ref')]
    host_outputs = []
    for request_ref in attempt['host_request_refs']:
        request = resolver.resolve(request_ref, 'investment.host_request')
        if request['role'] != role:
            continue
        require(request['run_id'] == run_id and request['stage_id'] == stage_id and request['invocation_id'] == invocation_id
                and request['attempt_no'] == attempt_no, 'RS05', '/request_id', 'Persisted host work differs from current role attempt')
        response_uri = request_ref['uri'].removesuffix('-request.json') + '-response.json'
        matches = [ref for ref in attempt['host_response_refs'] if ref['uri'] == response_uri]
        if not matches:
            continue
        require(len(matches) == 1, 'RS05', '/host_response_refs', 'Duplicate bound host response')
        response = resolver.resolve(matches[0], 'investment.host_response')
        validate_host_response(request, response, resolver)
        host_outputs.append(response['output_ref']['value'])
    allowed += host_outputs
    scoped = ResearchResolver(selected, schemas, mode=mode, allowed_refs=allowed, reference_grants=definition['reference_grants'])
    scoped.local_context = (stage_id, role, attempt_dir)
    scoped.host_output_refs = host_outputs
    feedback = [resolver.resolve(item['failure_ref']['value'], 'investment.research_failure')
                for item in invocation['attempts'][:-1] if is_known(item['failure_ref'])] if role == 'producer' else []
    return StageContext(root=selected, mode=mode, stage_id=stage_id, run_id=run_id, invocation_id=invocation_id,
                        attempt_no=attempt_no, role=role, schemas=schemas, resolver=scoped, input=incoming, input_ref=input_ref,
                        method=method, method_ref=invocation['method_ref'], definition=definition, attempt_dir=attempt_dir,
                        candidate=candidate, candidate_ref=candidate_ref, feedback=feedback,
                        frozen_host_steps=steps, upstream_receipts=upstream_receipts)


def _failure(incoming, input_ref, attempt, phase, error):
    code = getattr(error, 'code', 'execution_failed')
    permitted = {'schema_violation', 'invalid_reference', 'identity_mismatch', 'unsupported_output',
                 'missing_evidence', 'resource_limit', 'use_not_allowed', 'execution_failed'}
    return {'run_id': incoming['run_id'], 'stage_id': incoming['stage_id'], 'invocation_id': incoming['invocation_id'],
            'attempt_no': attempt['attempt_no'], 'failed_at': now(), 'phase': phase, 'rule_id': getattr(error, 'rule', 'RS04'),
            'code': code if code in permitted else 'execution_failed', 'pointer': getattr(error, 'pointer', '/'),
            'reason': (str(error) or type(error).__name__)[:2048], 'input_ref': input_ref,
            'candidate_ref': attempt['candidate_ref'], 'evidence_refs': [input_ref] + ([attempt['candidate_ref']['value']] if is_known(attempt['candidate_ref']) else []) + attempt.get('host_response_refs', []),
            'retryable': True}


def _parser(*, subprocess_role=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--mode', required=True, choices=('formal', 'test'))
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--invocation-id', required=True)
    if subprocess_role:
        parser.add_argument('--attempt-no', required=True, type=int, choices=(1, 2, 3))
        parser.add_argument('--candidate', type=Path, required=subprocess_role == 'validator')
    return parser


def stage_main(stage_id, role, handler, argv=None):
    args = _parser(subprocess_role=role).parse_args(argv)
    try:
        ctx = load_context(args.root, mode=args.mode, stage_id=stage_id, input_path=args.input,
                           run_id=args.run_id, invocation_id=args.invocation_id, attempt_no=args.attempt_no,
                           role=role, candidate_path=args.candidate)
    except (ContractError, OSError, ValueError, KeyError) as error:
        print(str(error), file=sys.stderr)
        return 2
    try:
        result = handler(ctx)
        if isinstance(result, dict) and 'request_id' in result:
            ctx.schemas.validate('investment.host_request', result)
            code = 3
        elif role == 'producer':
            _candidate(ctx.resolver, result, ctx.input, ctx.method_ref, ctx.definition, attempt_no=ctx.attempt_no)
            code = 0
        else:
            code = 0 if _receipt(ctx.resolver, result, ctx.input, ctx.candidate_ref, ctx.method) else 1
    except (ContractError, OSError, ValueError, KeyError, TypeError) as error:
        attempt = {'attempt_no': ctx.attempt_no, 'candidate_ref': known(ctx.candidate_ref) if ctx.candidate_ref else na('candidate_not_produced')}
        result = _failure(ctx.input, ctx.input_ref, attempt, 'processing' if role == 'producer' else 'validation', error)
        code = 1
    sys.stdout.buffer.write(canonical(result))
    return code


def run_stage_cli(stage_id, argv=None):
    args = _parser().parse_args(argv)
    try:
        result = ResearchController(args.root, mode=args.mode, stage_id=stage_id).run(
            args.input, run_id=args.run_id, invocation_id=args.invocation_id)
    except (ContractError, OSError, ValueError, KeyError) as error:
        print(str(error), file=sys.stderr)
        return 2
    sys.stdout.buffer.write(canonical(result))
    return {'completed': 0, 'failed': 1, 'host_work_required': 3}[result['status']]


class ResearchController:
    """预算先落盘，真实流程后独立验证；宿主工作在同attempt内等待和续行。"""

    def __init__(self, root, *, mode, stage_id):
        self.root = guard_namespace(root, mode)
        require(stage_id in STAGES, 'RS03', '/stage_id', 'Unsupported research stage')
        self.mode, self.stage_id = mode, stage_id

    def _bundle(self, base, filename, schema_id):
        resources = []
        for rid, document in sorted(self.schemas.documents.items()):
            reference = _put(self.resolver, base / ('schema-' + rid.rsplit(':', 1)[-1] + '.json'),
                             document, 'investment.research_schema_document')
            resources.append({'resource_id': rid, 'installed_filename': self.schemas.installed_files[rid],
                              'installed_sha256': self.schemas.installed_hashes[rid], 'document_ref': reference})
        bundle = {'schema_version': 1, 'root_schema_id': schema_id, 'root_target': self.schemas.targets[schema_id]['target'],
                  'resources': resources}
        return _put(self.resolver, base / filename, bundle, 'investment.research_schema_bundle')

    def _freeze(self, incoming, base):
        input_ref = _put(self.resolver, base / 'input.json', incoming, 'investment.stage_input')
        method = self.schemas.stage_methods[self.stage_id]
        method_ref = _put(self.resolver, base / 'method.json', method, 'investment.' + self.stage_id.lower() + '_method')
        bundle_ref = self._bundle(base, 'schema-bundle.json', method['runtime']['payload_schema_id'])
        steps = []
        for step in method['runtime']['host_steps']:
            prompt = (PLUGIN / step['prompt_path']).resolve()
            require(prompt.is_relative_to((PLUGIN / 'prompts' / self.stage_id.lower()).resolve()),
                    'RS01', '/prompt_path', 'Installed prompt escapes stage directory')
            prompt_ref = _text(self.resolver, base / ('prompt-' + step['step_id'] + '.md'),
                               prompt.read_bytes(), 'investment.research_markdown')
            schema_ref = self._bundle(base, 'schema-bundle-' + step['step_id'] + '.json', step['output_schema_id'])
            steps.append({'step_id': step['step_id'], 'role': step['role'], 'prompt_ref': prompt_ref,
                          'output_schema_ref': schema_ref, 'output_schema_id': step['output_schema_id'], 'required': step['required']})
        return {'run_id': incoming['run_id'], 'invocation_id': incoming['invocation_id'], 'frozen_input_ref': input_ref,
                'method_ref': method_ref, 'schema_bundle_ref': bundle_ref, 'host_steps': steps, 'state': 'pending',
                'started_at': now(), 'finished_at': unknown('not_finished'), 'attempts': [], 'sealed_output_ref': na('not_sealed'),
                'retention': {'state': 'in_use', 'reason': 'Frozen source evidence and receipts are required by this research run',
                              'required_by': [incoming['run_id']]}}

    def _verify_frozen(self, invocation):
        incoming = _input(self.resolver, invocation['frozen_input_ref'], self.stage_id,
                          invocation['run_id'], invocation['invocation_id'])
        method = self.resolver.resolve(invocation['method_ref'], 'investment.' + self.stage_id.lower() + '_method')
        require(method == self.schemas.stage_methods[self.stage_id], 'RS03', '/method_ref', 'Installed method differs from frozen method', 'identity_mismatch')
        validate_schema_bundle(invocation['schema_bundle_ref'], method['runtime']['payload_schema_id'], self.resolver)
        expected = method['runtime']['host_steps']
        require(len(expected) == len(invocation['host_steps']), 'RS03', '/host_steps', 'Frozen host step count differs')
        for step, frozen in zip(expected, invocation['host_steps']):
            require(all(step[key] == frozen[key] for key in ('step_id', 'role', 'output_schema_id', 'required')),
                    'RS03', '/host_steps', 'Frozen host template identity differs')
            self.resolver.resolve(frozen['prompt_ref'], 'investment.research_markdown')
            validate_schema_bundle(frozen['output_schema_ref'], frozen['output_schema_id'], self.resolver)
        return incoming, method

    def _new_attempt(self, invocation, manifest):
        attempt = {'attempt_no': len(invocation['attempts']) + 1, 'state': 'processing', 'started_at': now(),
                   'finished_at': unknown('running'), 'candidate_ref': na('candidate_not_produced'),
                   'receipt_ref': na('validation_not_run'), 'failure_ref': na('attempt_not_failed'),
                   'host_request_refs': [], 'host_response_refs': []}
        invocation['attempts'].append(attempt)
        invocation['state'] = 'running'
        # 先保存预算，再创建进程及本attempt工作材料。
        _save(self.resolver, manifest)
        return attempt

    def _invoke(self, base, invocation, attempt, role, method):
        directory = base / f"attempt-{attempt['attempt_no']:02}"
        entry = method['runtime']['producer_entry' if role == 'producer' else 'validator_entry']
        command = [sys.executable, '-B', str(PLUGIN / 'scripts' / entry), '--root', str(self.root), '--mode', self.mode,
                   '--input', str(base / 'input.json'), '--run-id', invocation['run_id'],
                   '--invocation-id', invocation['invocation_id'], '--attempt-no', str(attempt['attempt_no'])]
        if role == 'validator':
            command += ['--candidate', str(directory / 'candidate.json')]
        process = subprocess.run(command, cwd=PLUGIN,
                                 env=dict(os.environ, PYTHONPATH=str(PLUGIN / 'src'), PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8'),
                                 capture_output=True)
        index = len(list(directory.glob(role + '-*.stdout.txt'))) + 1
        prefix = role + f'-{index:03}'
        atomic(directory / (prefix + '.stdout.txt'), process.stdout, immutable=True)
        atomic(directory / (prefix + '.stderr.txt'), process.stderr, immutable=True)
        atomic(directory / (prefix + '.exit.txt'), str(process.returncode).encode('ascii'), immutable=True)
        return process

    def _request(self, request, invocation, attempt, role, base):
        self.schemas.validate('investment.host_request', request)
        require(all(request[key] == value for key, value in
                    {'run_id': invocation['run_id'], 'stage_id': self.stage_id, 'invocation_id': invocation['invocation_id'],
                     'attempt_no': attempt['attempt_no'], 'role': role, 'input_ref': invocation['frozen_input_ref'],
                     'method_ref': invocation['method_ref']}.items()),
                'RS05', '/request', 'Host request identity or frozen input differs', 'identity_mismatch')
        matches = [step for step in invocation['host_steps'] if step['step_id'] == request['step_id'] and step['role'] == role]
        require(len(matches) == 1 and all(request[key] == matches[0][key] for key in ('prompt_ref', 'output_schema_ref', 'output_schema_id')),
                'RS05', '/step_id', 'Host request differs from installed frozen template', 'identity_mismatch')
        expected_candidate = attempt['candidate_ref'] if role == 'validator' else na('producer_has_no_candidate')
        require(request['candidate_ref'] == expected_candidate, 'RS05', '/candidate_ref', 'Host request candidate differs')
        ctx = load_context(self.root, mode=self.mode, stage_id=self.stage_id, input_path=base / 'input.json',
                           run_id=invocation['run_id'], invocation_id=invocation['invocation_id'], attempt_no=attempt['attempt_no'],
                           role=role, candidate_path=base / f"attempt-{attempt['attempt_no']:02}" / 'candidate.json' if role == 'validator' else None)
        # 依据必须沿本链显式grant可达，不能用进程回传列表扩大模型读取范围。
        for item in ctx.input['input_refs']:
            ctx.resolver.resolve(item['ref'])
        if role == 'validator':
            ctx.resolver.resolve(ctx.candidate_ref)
        for reference in request['allowed_evidence_refs']:
            if ctx.resolver.allowed.get(reference['uri']) != reference:
                ctx.resolver._admit_local(reference)
            visited = set()
            while ctx.resolver.allowed.get(reference['uri']) != reference:
                frontier = [ref for uri, ref in ctx.resolver.allowed.items() if uri not in visited]
                require(frontier, 'RS06', '/allowed_evidence_refs', 'Requested evidence is not reachable through frozen grants', 'use_not_allowed')
                for parent in frontier:
                    visited.add(parent['uri'])
                    ctx.resolver.resolve(parent)
            ctx.resolver.resolve(reference)
            require(not reference['uri'].rsplit('/', 1)[-1].startswith('host-') or reference in ctx.resolver.host_output_refs,
                    'RS06', '/allowed_evidence_refs', 'Host work may only reuse bound same-role structured outputs')
        directory = base / f"attempt-{attempt['attempt_no']:02}"
        path = directory / ('host-' + request['step_id'] + '-' + request['work_key'] + '-request.json')
        return _put(self.resolver, path, request, 'investment.host_request')

    def _response(self, request_ref, directory):
        request = self.resolver.resolve(request_ref, 'investment.host_request')
        path = directory / ('host-' + request['step_id'] + '-' + request['work_key'] + '-response.json')
        if not path.exists():
            return None
        response_ref = self.resolver.path_ref(path, 'investment.host_response')
        response = self.resolver.resolve(response_ref, 'investment.host_response')
        try:
            validate_host_response(request, response, self.resolver)
        except ContractError as error:
            error.host_response_ref = response_ref
            raise
        return response_ref

    def _required_work(self, invocation, attempt, role, directory):
        completed = set()
        for reference in attempt['host_request_refs']:
            request = self.resolver.resolve(reference, 'investment.host_request')
            if request['role'] != role:
                continue
            response_ref = self._response(reference, directory)
            require(response_ref is not None and response_ref in attempt['host_response_refs'],
                    'RS05', '/host_response_refs', 'Current host work has no bound successful response')
            completed.add(request['step_id'])
        required = {step['step_id'] for step in invocation['host_steps'] if step['role'] == role and step['required']}
        require(required <= completed, 'RS06', '/host_steps', 'Required current host template was not executed', 'missing_evidence')

    def _finish_failure(self, manifest, invocation, attempt, base, incoming, phase, error):
        response_ref = getattr(error, 'host_response_ref', None)
        if response_ref is not None and response_ref not in attempt['host_response_refs']:
            attempt['host_response_refs'].append(response_ref)
        failure = _failure(incoming, invocation['frozen_input_ref'], attempt, phase, error)
        directory = base / f"attempt-{attempt['attempt_no']:02}"
        path = directory / 'failure.json'
        if path.exists():
            reference = self.resolver.path_ref(path, 'investment.research_failure')
            previous = self.resolver.resolve(reference, 'investment.research_failure')
            require(all(previous[key] == failure[key] for key in
                        ('run_id', 'stage_id', 'invocation_id', 'attempt_no', 'input_ref', 'candidate_ref')),
                    'RS04', '/failure_ref', 'Interrupted failure binding differs', 'identity_mismatch')
            failure = previous
        else:
            reference = _put(self.resolver, path, failure, 'investment.research_failure')
        attempt.update(state='failed', finished_at=known(failure['failed_at']), failure_ref=known(reference))
        invocation['state'] = 'failed' if len(invocation['attempts']) == 3 else 'running'
        if invocation['state'] == 'failed':
            invocation['finished_at'] = known(now())
        _save(self.resolver, manifest)

    def _seal(self, manifest, invocation, attempt, base, incoming, method):
        candidate_ref, receipt_ref = attempt['candidate_ref']['value'], attempt['receipt_ref']['value']
        candidate = self.resolver.resolve(candidate_ref, 'investment.candidate_output')
        receipt = self.resolver.resolve(receipt_ref, 'investment.validation_receipt')
        _candidate(self.resolver, candidate, incoming, invocation['method_ref'], method['runtime'], attempt_no=attempt['attempt_no'])
        require(_receipt(self.resolver, receipt, incoming, candidate_ref, method), 'RS07', '/receipt', 'Receipt does not permit any handoff', 'use_not_allowed')
        directory = base / f"attempt-{attempt['attempt_no']:02}"
        self._required_work(invocation, attempt, 'producer', directory)
        self._required_work(invocation, attempt, 'validator', directory)
        path = directory / 'sealed.json'
        if path.exists():
            reference = self.resolver.path_ref(path, 'investment.sealed_output')
            seal = self.resolver.resolve(reference, 'investment.sealed_output')
            require(seal['run_id'] == invocation['run_id'] and seal['stage_id'] == self.stage_id
                    and seal['output_id'] == candidate['output_id'] and seal['candidate_output_ref'] == candidate_ref
                    and seal['validation_receipt_ref'] == receipt_ref, 'RS08', '/sealed_output', 'Existing seal binding differs')
        else:
            seal = {'run_id': invocation['run_id'], 'stage_id': self.stage_id, 'output_id': candidate['output_id'],
                    'candidate_output_ref': candidate_ref, 'validation_receipt_ref': receipt_ref, 'sealed_at': now()}
            reference = _put(self.resolver, path, seal, 'investment.sealed_output')
        attempt.update(state='completed', finished_at=known(seal['sealed_at']))
        invocation.update(state='completed', finished_at=known(seal['sealed_at']), sealed_output_ref=known(reference))
        _save(self.resolver, manifest)

    def _result(self, invocation):
        pending = na('no_pending_host_work')
        if invocation['state'] == 'host_work_required':
            pending = known(invocation['attempts'][-1]['host_request_refs'][-1])
        result = {'run_id': invocation['run_id'], 'stage_id': self.stage_id, 'invocation_id': invocation['invocation_id'],
                  'status': invocation['state'], 'attempt_count': len(invocation['attempts']), 'input_ref': invocation['frozen_input_ref'],
                  'sealed_output_ref': invocation['sealed_output_ref'], 'host_request_ref': pending,
                  'failure_refs': [item['failure_ref']['value'] for item in invocation['attempts'] if is_known(item['failure_ref'])],
                  'downstream_allowed': invocation['state'] == 'completed'}
        self.schemas.validate('investment.research_run_result', result)
        return result

    def run(self, input_path, *, run_id, invocation_id):
        submitted = validate_input_path(self.root, input_path)
        self.schemas = _schemas(self.stage_id)
        self.resolver = ResearchResolver(self.root, self.schemas, mode=self.mode)
        submitted_ref = self.resolver.path_ref(submitted, 'investment.stage_input')
        stage, parts = self.resolver.run_parts(submitted_ref['uri'])
        require(stage == self.stage_id.lower() and len(parts) == 3 and parts[1] == invocation_id,
                'RS03', '/input', 'Input is outside current stage invocation', 'identity_mismatch')
        incoming = _input(self.resolver, submitted_ref, self.stage_id, run_id, invocation_id)
        day = parts[0]
        base = self.root / 'runs' / stage / day / invocation_id
        with _stage_lock(self.root, self.stage_id):
            for path in sorted((self.root / 'runs' / stage).glob('*/manifest.json')):
                previous = self.schemas.document('investment.research_daily_manifest', path.read_bytes())
                require(path.parent.name == day or not any(item['invocation_id'] == invocation_id for item in previous['invocations']),
                        'RS04', '/invocation_id', 'Existing invocation cannot move dates to reset its budget', 'identity_mismatch')
            manifest = _manifest(self.resolver, self.stage_id, day)
            matches = [item for item in manifest['invocations'] if item['invocation_id'] == invocation_id]
            if matches:
                invocation = matches[0]
                require(invocation['run_id'] == run_id and self.resolver.resolve(invocation['frozen_input_ref'], 'investment.stage_input') == incoming,
                        'RS03', '/input', 'Existing invocation facts cannot change', 'identity_mismatch')
            else:
                invocation = self._freeze(incoming, base)
                manifest['invocations'].append(invocation)
                _save(self.resolver, manifest)
            incoming, method = self._verify_frozen(invocation)
            if invocation['state'] == 'completed':
                require(invocation['attempts'] and invocation['attempts'][-1]['state'] == 'completed'
                        and is_known(invocation['sealed_output_ref']), 'RS08', '/state', 'Completed invocation has no completed seal')
                self._seal(manifest, invocation, invocation['attempts'][-1], base, incoming, method)
                return self._result(invocation)
            if invocation['state'] == 'failed':
                require(len(invocation['attempts']) == 3 and all(item['state'] == 'failed' and is_known(item['failure_ref'])
                        for item in invocation['attempts']), 'RS04', '/attempts', 'Terminal failure requires three real failed attempts')
                for attempt in invocation['attempts']:
                    self.resolver.resolve(attempt['failure_ref']['value'], 'investment.research_failure')
                return self._result(invocation)
            if invocation['attempts'] and invocation['attempts'][-1]['state'] not in ('failed', 'completed'):
                attempt = invocation['attempts'][-1]
                directory = base / f"attempt-{attempt['attempt_no']:02}"
                if attempt['state'] == 'host_work_required':
                    try:
                        require(invocation['state'] == 'host_work_required' and attempt['host_request_refs'],
                                'RS05', '/host_request_refs', 'Waiting state has no actual request')
                        response_ref = self._response(attempt['host_request_refs'][-1], directory)
                        if response_ref is None:
                            return self._result(invocation)
                        require(response_ref not in attempt['host_response_refs'], 'RS05', '/host_response_refs', 'Host response was already consumed')
                        attempt['host_response_refs'].append(response_ref)
                        request = self.resolver.resolve(attempt['host_request_refs'][-1], 'investment.host_request')
                        attempt['state'] = 'processing' if request['role'] == 'producer' else 'validating'
                        invocation['state'] = 'running'
                        _save(self.resolver, manifest)
                    except (ContractError, OSError, ValueError, KeyError) as error:
                        self._finish_failure(manifest, invocation, attempt, base, incoming, 'host', error)
                elif (directory / 'sealed.json').exists():
                    self._seal(manifest, invocation, attempt, base, incoming, method)
                    return self._result(invocation)
                else:
                    error = ContractError('RS04', '/attempts', 'Controller interrupted before persisted attempt finished', 'execution_failed')
                    self._finish_failure(manifest, invocation, attempt, base, incoming, 'recovery', error)
            while invocation['state'] not in ('completed', 'failed'):
                if not invocation['attempts'] or invocation['attempts'][-1]['state'] == 'failed':
                    require(len(invocation['attempts']) < 3, 'RS04', '/attempts', 'Attempt budget exhausted')
                    attempt = self._new_attempt(invocation, manifest)
                else:
                    attempt = invocation['attempts'][-1]
                directory = base / f"attempt-{attempt['attempt_no']:02}"
                role = 'producer' if attempt['state'] == 'processing' else 'validator'
                phase = 'processing' if role == 'producer' else 'validation'
                try:
                    require(attempt['state'] in ('processing', 'validating'), 'RS04', '/state', 'Unexpected live attempt state')
                    process = self._invoke(base, invocation, attempt, role, method)
                    require(process.returncode in (0, 1, 3), 'RS04', '/process',
                            'Stage process could not execute: ' + process.stderr.decode('utf-8', errors='replace')[:1600], 'execution_failed')
                    result = parse_news(process.stdout)
                    if process.returncode == 3:
                        request_ref = self._request(result, invocation, attempt, role, base)
                        require(request_ref not in attempt['host_request_refs'], 'RS05', '/request_id', 'A completed request cannot be resubmitted', 'identity_mismatch')
                        attempt['host_request_refs'].append(request_ref)
                        attempt['state'] = invocation['state'] = 'host_work_required'
                        _save(self.resolver, manifest)
                        return self._result(invocation)
                    if process.returncode == 1 and isinstance(result, dict) and 'failed_at' in result:
                        self.schemas.validate('investment.research_failure', result)
                        require(all(result[key] == value for key, value in
                                    {'run_id': run_id, 'stage_id': self.stage_id, 'invocation_id': invocation_id,
                                     'attempt_no': attempt['attempt_no'], 'input_ref': invocation['frozen_input_ref'],
                                     'candidate_ref': attempt['candidate_ref']}.items()),
                                'RS03', '/failure', 'Stage failure identity differs', 'identity_mismatch')
                        raise ContractError(result['rule_id'], result['pointer'], result['reason'], result['code'])
                    self._required_work(invocation, attempt, role, directory)
                    if role == 'producer':
                        require(process.returncode == 0, 'RS06', '/candidate', 'Producer returned no completed candidate', 'execution_failed')
                        _candidate(self.resolver, result, incoming, invocation['method_ref'], method['runtime'], attempt_no=attempt['attempt_no'])
                        reference = _put(self.resolver, directory / 'candidate.json', result, 'investment.candidate_output')
                        attempt.update(candidate_ref=known(reference), state='validating')
                        _save(self.resolver, manifest)
                        continue
                    permitted = _receipt(self.resolver, result, incoming, attempt['candidate_ref']['value'], method)
                    reference = _put(self.resolver, directory / 'receipt.json', result, 'investment.validation_receipt')
                    attempt['receipt_ref'] = known(reference)
                    _save(self.resolver, manifest)
                    if process.returncode != 0 or not permitted:
                        blocker = result['blockers'][0] if result['blockers'] else None
                        failed = next((item for item in result['checks'] if item['verdict'] != 'passed'), None)
                        raise ContractError(blocker['rule_id'] if blocker else failed['rule_id'] if failed else 'RS07',
                                            blocker['field_or_object'] if blocker else '/allowed_uses',
                                            blocker['reason'] if blocker else failed['reason'] if failed else 'Independent receipt permits no handoff',
                                            'unsupported_output')
                    phase = 'seal'
                    self._seal(manifest, invocation, attempt, base, incoming, method)
                except (ContractError, OSError, ValueError, KeyError, TypeError) as error:
                    self._finish_failure(manifest, invocation, attempt, base, incoming, phase, error)
            return self._result(invocation)
