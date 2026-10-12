"""SC02 主控：冻结、真实流程、独立验收、封存及不可重置的尝试预算。"""
from __future__ import annotations

import copy
import os
from pathlib import Path
import subprocess
import sys

from investment.contracts import ContractError, SPEC, artifact, canonical, digest, instant, is_known, known, na, now, require, unknown
from investment.chains.news.contracts import ROLES, REVIEW_USES, RULES, NewsResolver, NewsSchemas, guard_namespace, parse_news
from investment.chains.news.plugins import discover
from investment.references import safe_path
from investment.runtime.paths import validate_input_path
from investment.runtime import news_runs as runs

PLUGIN = Path(__file__).resolve().parents[3]


class NewsController:
    def __init__(self, root: Path, *, mode: str, network_origin: str, plugins_dir: Path | None = None):
        self.root, self.plugins = guard_namespace(root, mode, network_origin, plugins_dir)
        self.mode, self.network_origin = mode, network_origin

    def run(self, input_path: Path, *, run_id: str, invocation_id: str):
        # 先检查两个命名空间，再读取任意输入或取得可能创建目录的锁。
        submitted = validate_input_path(self.root, input_path)
        run_root = self.root / 'runs' / 'sc02'
        require(submitted.is_relative_to(run_root), 'N01', '/input', 'SC02 input must be inside selected runs namespace', 'use_not_allowed')
        self.schemas = NewsSchemas()
        self.resolver = NewsResolver(self.root, self.schemas, mode=self.mode,
                                    network_origin=self.network_origin, plugins_dir=self.plugins)
        submitted_ref = self.resolver.path_ref(submitted, 'investment.stage_input')
        parts = self.resolver.run_parts(submitted_ref['uri'])
        require(len(parts) == 3 and parts[1] == invocation_id, 'N03', '/input', 'Current invocation input required', 'identity_mismatch')
        incoming = self.resolver.resolve(submitted_ref, 'investment.stage_input')
        require((incoming['run_id'], incoming['stage_id'], incoming['invocation_id']) == (run_id, 'SC02', invocation_id),
                'N03', '/run_id', 'CLI and StageInput identity differ', 'identity_mismatch')
        require([row['role'] for row in incoming['input_refs']] == [role for role, _ in ROLES],
                'N03', '/input_refs', 'Exact ordered SC02 roles required', 'identity_mismatch')
        require(incoming['scope_ref'] == incoming['input_refs'][2]['ref'],
                'N03', '/scope_ref', 'Submitted scope reference differs from source role', 'identity_mismatch')
        parameters = self.resolver.resolve(incoming['parameters_ref'], 'investment.sc02_parameters')
        require(parameters['mode'] == self.mode and parameters['network_origin'] == self.network_origin
                and Path(parameters['plugins_dir']).resolve() == self.plugins,
                'N01', '/parameters_ref', 'CLI/frozen parameter namespace differs', 'use_not_allowed')
        with runs.root_lock(self.root):
            for path in sorted((self.root / 'runs' / 'sc02').glob('*/manifest.json')):
                previous = self.schemas.document('investment.sc02_daily_manifest', path.read_bytes())
                if any(row['invocation_id'] == invocation_id for row in previous['invocations']):
                    require(path.parent.name == parts[0], 'N03', '/invocation_id',
                            'Existing invocation cannot move to another day and reset its budget', 'identity_mismatch')
            manifest = runs.load_manifest(self.root, parts[0], self.schemas)
            if manifest['invocations']:
                runs.verify_manifest(manifest, self.resolver)
            base = safe_path(self.root, 'runs', 'sc02', parts[0], invocation_id)
            matches = [row for row in manifest['invocations'] if row['invocation_id'] == invocation_id]
            frozen = self._freeze_input(incoming, parameters, base)
            if matches:
                invocation = matches[0]
                require(invocation['run_id'] == run_id and invocation['frozen_input_ref'] == frozen,
                        'N03', '/frozen_input_ref', 'Existing invocation facts cannot change', 'identity_mismatch')
            else:
                invocation = self._new_invocation(incoming, frozen, base)
                manifest['invocations'].append(invocation)
                runs.save_manifest(self.root, manifest, self.resolver)
            if invocation['processing_state'] in ('completed', 'failed'):
                return self._result(invocation, base)
            if invocation['attempts'] and invocation['attempts'][-1]['processing_state'] == 'running':
                attempt = invocation['attempts'][-1]
                directory = base / f"attempt-{attempt['attempt_no']:02}"
                if (directory / 'sealed.json').exists():
                    self._recover_seal(manifest, invocation, attempt, directory)
                    return self._result(invocation, base)
                binding_error = self._bind_available(attempt, directory, tolerate_unbound=True)
                failure_path = directory / 'failure.json'
                if failure_path.exists():
                    failure = self.resolver.resolve(self.resolver.path_ref(failure_path, 'investment.sc02_failure'), 'investment.sc02_failure')
                else:
                    error = binding_error or ContractError('N04', '/attempts', 'Controller interrupted before attempt finished', 'execution_failed')
                    failure = self._failure(invocation, attempt, 'validation' if is_known(attempt['candidate_ref']) else 'collection', error)
                self._finish_failure(manifest, invocation, attempt, directory, failure)
            while len(invocation['attempts']) < 3:
                stamp = now()
                attempt = {'attempt_no': len(invocation['attempts']) + 1, 'processing_state': 'running',
                           'started_at': stamp, 'finished_at': unknown('running'),
                           'capture_log_ref': na('capture_log_not_produced'), 'candidate_ref': na('candidate_not_produced'),
                           'receipt_ref': na('validation_not_run'), 'failure_ref': na('attempt_not_failed')}
                invocation['attempts'].append(attempt)
                invocation.update(processing_state='running', started_at=invocation['started_at'] if is_known(invocation['started_at']) else known(stamp))
                # 已开始预算先落盘；进程中断也不能重用此编号。
                runs.save_manifest(self.root, manifest, self.resolver)
                directory = base / f"attempt-{attempt['attempt_no']:02}"
                phase = 'collection'
                try:
                    process = self._invoke('process_sc02.py', invocation, attempt, base)
                    self._logs(directory, 'process', process)
                    if process.returncode == 1:
                        failure = self.schemas.document('investment.sc02_failure', process.stdout)
                        require((failure['run_id'], failure['invocation_id'], failure['attempt_no'], failure['input_ref'])
                                == (run_id, invocation_id, attempt['attempt_no'], frozen),
                                'N03', '/failure_ref', 'Producer failure identity differs', 'identity_mismatch')
                        self._bind_available(attempt, directory)
                        require(failure['candidate_ref'] == attempt['candidate_ref'], 'N03', '/candidate_ref', 'Producer failure candidate differs')
                    else:
                        require(process.returncode == 0, 'N04', '/process', 'Producer cannot start: ' + process.stderr.decode('utf-8', errors='replace')[:1700], 'execution_failed')
                        candidate_ref = self.resolver.path_ref(directory / 'candidate.json', 'investment.candidate_output')
                        require(parse_news(process.stdout) == candidate_ref, 'N03', '/candidate_ref', 'Producer stdout does not bind actual candidate', 'identity_mismatch')
                        self._bind_available(attempt, directory)
                        runs.save_manifest(self.root, manifest, self.resolver)
                        phase = 'validation'
                        checked = self._invoke('validate_sc02.py', invocation, attempt, base)
                        self._logs(directory, 'validation', checked)
                        require(checked.returncode in (0, 1), 'N04', '/receipt', 'Validator cannot start: ' + checked.stderr.decode('utf-8', errors='replace')[:1700], 'execution_failed')
                        receipt = self.schemas.document('investment.validation_receipt', checked.stdout)
                        self._receipt_identity(receipt, invocation, attempt)
                        attempt['receipt_ref'] = known(runs.put(self.root, directory / 'receipt.json', receipt, 'investment.validation_receipt', self.schemas))
                        runs.save_manifest(self.root, manifest, self.resolver)
                        if checked.returncode == 0 and self._permitted(receipt):
                            phase = 'seal'
                            self._seal(manifest, invocation, attempt, directory, receipt)
                            return self._result(invocation, base)
                        check = next((row for row in receipt['checks'] if row['verdict'] not in ('passed', 'not_applicable')), None)
                        blocker = receipt['blockers'][0] if receipt['blockers'] else None
                        error = ContractError(check['rule_id'] if check else 'N12', blocker['field_or_object'] if blocker else '/allowed_uses',
                                              blocker['reason'] if blocker else check['reason'] if check else 'Independent receipt does not permit research handoff',
                                              'missing_evidence' if receipt['business_result'] == 'blocked' else 'unsupported_output')
                        failure = self._failure(invocation, attempt, phase, error)
                except (ContractError, OSError, ValueError) as error:
                    binding_error = self._bind_available(attempt, directory, tolerate_unbound=True)
                    failure = self._failure(invocation, attempt, phase, binding_error or error)
                self._finish_failure(manifest, invocation, attempt, directory, failure)
            require(len(invocation['attempts']) == 3 and all(row['processing_state'] == 'failed' and is_known(row['failure_ref'])
                    for row in invocation['attempts']), 'N03', '/attempts', 'Terminal summary requires three actual failures')
            expected = {'run_id': run_id, 'stage_id': 'SC02', 'invocation_id': invocation_id, 'status': 'failed',
                        'attempt_count': 3, 'attempt_failure_refs': [row['failure_ref']['value'] for row in invocation['attempts']],
                        'downstream_allowed': False}
            summary_path = base / 'failure-summary.json'
            if summary_path.exists():
                # 摘要先于终态清单落盘时，严格复用其原始时刻和字节。
                summary_ref = self.resolver.path_ref(summary_path, 'investment.sc02_failure_summary')
                summary = self.resolver.resolve(summary_ref, 'investment.sc02_failure_summary')
                require(all(summary[key] == value for key, value in expected.items()),
                        'N03', '/failure_summary_ref', 'Durable summary identity/failure bindings differ', 'identity_mismatch')
                require(instant(invocation['attempts'][-1]['finished_at']['value']) <= instant(summary['finished_at']) <= instant(now()),
                        'N03', '/failure_summary_ref/finished_at', 'Durable summary time differs from actual failed attempts', 'identity_mismatch')
            else:
                summary = dict(expected, finished_at=now())
                summary_ref = runs.put(self.root, summary_path, summary, 'investment.sc02_failure_summary', self.schemas)
            invocation.update(processing_state='failed', finished_at=known(summary['finished_at']),
                              failure_summary_ref=known(summary_ref))
            runs.save_manifest(self.root, manifest, self.resolver)
            return self._result(invocation, base)

    def _freeze_input(self, incoming, parameters, base):
        frozen = copy.deepcopy(incoming)
        filenames = ('clock.json', 'window.json', 'scope.json', 'query.json')
        for index, ((role, schema_id), filename) in enumerate(zip(ROLES, filenames)):
            value = self.resolver.resolve(incoming['input_refs'][index]['ref'], schema_id)
            frozen['input_refs'][index]['ref'] = runs.put(self.root, base / filename, value, schema_id, self.schemas)
        frozen['scope_ref'] = frozen['input_refs'][2]['ref']
        frozen['parameters_ref'] = runs.put(self.root, base / 'parameters.json', parameters, 'investment.sc02_parameters', self.schemas)
        return runs.put(self.root, base / 'input.json', frozen, 'investment.stage_input', self.schemas)

    def _new_invocation(self, incoming, input_ref, base):
        scope = self.resolver.resolve(incoming['input_refs'][2]['ref'], 'investment.source_scope')
        source_ids = [row['source_id'] for row in scope['entries']]
        require(source_ids == sorted(set(source_ids)), 'N03', '/source_scope_ref', 'Source IDs must be unique and ordered')
        installed, bindings = discover(self.plugins), []
        for source_id in source_ids:
            binding = {'source_id': source_id, 'manifest_ref': unknown('no_installed_profile'), 'profile_ref': unknown('no_installed_profile')}
            if source_id in installed:
                source = safe_path(self.plugins, source_id)
                manifest = self.schemas.document('investment.news_manifest', safe_path(source, 'manifest.json').read_bytes())
                profile = self.schemas.document('investment.news_profile', safe_path(source, 'extraction.json').read_bytes())
                require(manifest['source_id'] == profile['source_id'] == source_id and manifest['profile_sha256'] == digest(canonical(profile)),
                        'N03', '/profile_bindings', 'Installed source identity/hash differs', 'identity_mismatch')
                binding['manifest_ref'] = known(runs.put(self.root, base / f'plugin-{source_id}.json', manifest, 'investment.news_manifest', self.schemas))
                binding['profile_ref'] = known(runs.put(self.root, base / f'profile-{source_id}.json', profile, 'investment.news_profile', self.schemas))
            bindings.append(binding)
        return {'run_id': incoming['run_id'], 'invocation_id': incoming['invocation_id'], 'description': 'Configured source evidence collection',
                'frozen_input_ref': input_ref, 'parameters_ref': self.resolver.resolve(input_ref)['parameters_ref'],
                'method_ref': artifact((SPEC / 'method-sc02.json').read_bytes(), 'investment.sc02_method', 'investment-spec://v1/method-sc02.json'),
                'profile_bindings': bindings, 'processing_state': 'pending', 'started_at': unknown('not_started'), 'finished_at': unknown('not_finished'),
                'attempts': [], 'sealed_output_ref': na('not_sealed'), 'failure_summary_ref': na('not_failed'),
                'retention': {'state': 'in_use', 'reason': 'Frozen evidence required for current referenced research uses', 'required_by': [incoming['run_id']]}}

    def _invoke(self, script, invocation, attempt, base):
        command = [sys.executable, '-B', str(PLUGIN / 'scripts' / script), '--root', str(self.root), '--mode', self.mode,
                   '--network-origin', self.network_origin, '--plugins-dir', str(self.plugins), '--input', str(base / 'input.json'),
                   '--run-id', invocation['run_id'], '--invocation-id', invocation['invocation_id'], '--attempt-no', str(attempt['attempt_no'])]
        if script == 'validate_sc02.py':
            command += ['--candidate', str(base / f"attempt-{attempt['attempt_no']:02}" / 'candidate.json')]
        return subprocess.run(command, cwd=PLUGIN, env=dict(os.environ, PYTHONPATH=str(PLUGIN / 'src'), PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8'), capture_output=True)

    def _logs(self, directory, name, process):
        runs.atomic(directory / (name + '.stdout.log'), process.stdout, immutable=True)
        runs.atomic(directory / (name + '.stderr.log'), process.stderr, immutable=True)

    def _bind_available(self, attempt, directory, *, tolerate_unbound=False):
        if tolerate_unbound:
            require(attempt['processing_state'] == 'running', 'N03', '/attempts', 'Only current running artifacts may be incomplete')
        errors = []
        for key, filename, schema_id in (('capture_log_ref', 'capture-log.json', 'investment.news_capture_log'),
                                        ('candidate_ref', 'candidate.json', 'investment.candidate_output')):
            if is_known(attempt[key]):
                # 已绑定依据仍严格核对原引用，不用新摘要替换旧身份。
                self.resolver.resolve(attempt[key]['value'], schema_id)
                continue
            path = directory / filename
            if path.exists():
                try:
                    reference = self.resolver.path_ref(path, schema_id)
                    self.resolver.resolve(reference, schema_id)
                except (ContractError, OSError, ValueError) as error:
                    issue = ContractError(getattr(error, 'rule', 'N04'), '/' + key,
                                          filename + ': ' + (str(error) or type(error).__name__), getattr(error, 'code', 'execution_failed'))
                    if not tolerate_unbound:
                        raise issue from error
                    # 保留未绑定的坏字节和真实原因；不授予known引用，也不阻止尝试收尾。
                    errors.append(issue)
                    continue
                attempt[key] = known(reference)
        if errors:
            return ContractError(errors[0].rule, errors[0].pointer,
                                 '; '.join(error.pointer + ': ' + str(error) for error in errors), errors[0].code)
        return None

    def _receipt_identity(self, receipt, invocation, attempt):
        incoming = self.resolver.resolve(invocation['frozen_input_ref'], 'investment.stage_input')
        require(receipt['validator_id'] == 'sc02-validator-v1' and receipt['validator_version'] == 'v1'
                and receipt['run_id'] == invocation['run_id'] and receipt['stage_id'] == 'SC02'
                and receipt['candidate_output_ref'] == attempt['candidate_ref']['value'] and receipt['input_refs'] == incoming['input_refs']
                and [check['rule_id'] for check in receipt['checks']] == list(RULES),
                'N03', '/receipt_ref', 'Independent receipt identity/rule set differs', 'identity_mismatch')

    @staticmethod
    def _permitted(receipt):
        return (receipt['overall_verdict'] in ('passed', 'partial') and receipt['business_result'] in ('usable', 'observation_only')
                and receipt['allowed_uses'] == REVIEW_USES
                and all(check['verdict'] == 'passed' or check['rule_id'] == 'N10' and check['verdict'] == 'insufficient_evidence'
                        for check in receipt['checks']))

    def _failure(self, invocation, attempt, phase, error):
        code = getattr(error, 'code', 'execution_failed')
        allowed = {'schema_violation', 'invalid_reference', 'identity_mismatch', 'unsupported_output', 'missing_evidence', 'resource_limit', 'use_not_allowed', 'execution_failed'}
        refs = [invocation['frozen_input_ref']] + [attempt[key]['value'] for key in ('capture_log_ref', 'candidate_ref', 'receipt_ref') if is_known(attempt[key])]
        return {'run_id': invocation['run_id'], 'stage_id': 'SC02', 'invocation_id': invocation['invocation_id'], 'attempt_no': attempt['attempt_no'],
                'failed_at': now(), 'phase': phase, 'rule_id': getattr(error, 'rule', 'N04'), 'code': code if code in allowed else 'execution_failed',
                'pointer': getattr(error, 'pointer', '/'), 'reason': (str(error) or type(error).__name__)[:2048],
                'input_ref': invocation['frozen_input_ref'], 'candidate_ref': attempt['candidate_ref'], 'evidence_refs': refs,
                'retryable': code not in ('identity_mismatch', 'use_not_allowed')}

    def _finish_failure(self, manifest, invocation, attempt, directory, failure):
        reference = runs.put(self.root, directory / 'failure.json', failure, 'investment.sc02_failure', self.schemas)
        attempt.update(processing_state='failed', finished_at=known(now()), failure_ref=known(reference))
        # 两次尝试之间仍是running调用，最后失败暂作为已完成失败前缀保存。
        invocation['processing_state'] = 'running'
        runs.save_manifest(self.root, manifest, self.resolver)

    def _seal(self, manifest, invocation, attempt, directory, receipt):
        self._receipt_identity(receipt, invocation, attempt)
        require(self._permitted(receipt), 'N12', '/allowed_uses', 'Receipt does not permit research handoff', 'use_not_allowed')
        candidate = self.resolver.resolve(attempt['candidate_ref']['value'], 'investment.candidate_output')
        stamp = now()
        seal = {'run_id': invocation['run_id'], 'stage_id': 'SC02', 'output_id': candidate['output_id'],
                'candidate_output_ref': attempt['candidate_ref']['value'], 'validation_receipt_ref': attempt['receipt_ref']['value'], 'sealed_at': stamp}
        sealed_ref = runs.put(self.root, directory / 'sealed.json', seal, 'investment.sealed_output', self.schemas)
        attempt.update(processing_state='completed', finished_at=known(stamp))
        invocation.update(processing_state='completed', finished_at=known(stamp), sealed_output_ref=known(sealed_ref))
        runs.save_manifest(self.root, manifest, self.resolver)

    def _recover_seal(self, manifest, invocation, attempt, directory):
        self._bind_available(attempt, directory)
        receipt_ref = self.resolver.path_ref(directory / 'receipt.json', 'investment.validation_receipt')
        receipt = self.resolver.resolve(receipt_ref, 'investment.validation_receipt')
        attempt['receipt_ref'] = known(receipt_ref)
        self._receipt_identity(receipt, invocation, attempt)
        require(self._permitted(receipt), 'N12', '/allowed_uses', 'Interrupted seal lacks permitted receipt', 'use_not_allowed')
        seal_ref = self.resolver.path_ref(directory / 'sealed.json', 'investment.sealed_output')
        seal = self.resolver.resolve(seal_ref, 'investment.sealed_output')
        candidate = self.resolver.resolve(attempt['candidate_ref']['value'], 'investment.candidate_output')
        require((seal['run_id'], seal['stage_id'], seal['output_id'], seal['candidate_output_ref'], seal['validation_receipt_ref'])
                == (invocation['run_id'], 'SC02', candidate['output_id'], attempt['candidate_ref']['value'], receipt_ref),
                'N03', '/sealed_output_ref', 'Interrupted seal identity differs', 'identity_mismatch')
        attempt.update(processing_state='completed', finished_at=known(seal['sealed_at']))
        invocation.update(processing_state='completed', finished_at=known(seal['sealed_at']), sealed_output_ref=known(seal_ref))
        runs.save_manifest(self.root, manifest, self.resolver)

    def _result(self, invocation, base):
        sealed = invocation['processing_state'] == 'completed'
        uses, goal = [], 'unmet'
        if sealed:
            attempt = invocation['attempts'][-1]
            receipt = self.resolver.resolve(attempt['receipt_ref']['value'], 'investment.validation_receipt')
            self._receipt_identity(receipt, invocation, attempt)
            require(self._permitted(receipt), 'N12', '/allowed_uses', 'Stored receipt does not permit handoff', 'use_not_allowed')
            candidate = self.resolver.resolve(attempt['candidate_ref']['value'], 'investment.candidate_output')
            corpus = self.resolver.resolve(candidate['payload_ref'], 'investment.evidence_corpus')
            coverage = self.resolver.resolve(corpus['coverage_ref'], 'investment.news_coverage')['rows']
            uses = receipt['allowed_uses']
            if self.network_origin == 'live' and not candidate['gaps'] and all(row['window_coverage'] == 'complete' for row in coverage):
                goal = 'achieved'
        result = {'run_id': invocation['run_id'], 'stage_id': 'SC02', 'invocation_id': invocation['invocation_id'],
                  'status': 'sealed' if sealed else 'failed', 'attempt_count': len(invocation['attempts']),
                  'sealed_output_ref': invocation['sealed_output_ref'], 'failure_summary_ref': invocation['failure_summary_ref'],
                  'goal_status': goal, 'allowed_uses': uses}
        runs.put(self.root, base / 'result.json', result, 'investment.sc02_run_result', self.schemas)
        manifest = runs.load_manifest(self.root, base.parent.name, self.schemas)
        runs.save_manifest(self.root, manifest, self.resolver)
        return (0 if sealed else 1), result
