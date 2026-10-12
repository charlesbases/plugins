"""SC02 运行互斥、不可变产物与三次尝试的持久化约束。"""
from __future__ import annotations

import html
import os
from contextlib import contextmanager
from pathlib import Path

from investment.contracts import artifact, canonical, instant, is_known, require
from investment.references import safe_path
from investment.runtime.runs import atomic


@contextmanager
def root_lock(root: Path):
    directory = safe_path(root, 'runs', 'sc02')
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


def put(root, path, value, schema_id, schemas):
    schemas.validate(schema_id, value)
    selected = safe_path(root, *Path(path).resolve().relative_to(root).parts)
    relative = selected.relative_to(root / 'runs' / 'sc02').as_posix()
    raw = canonical(value)
    atomic(selected, raw, immutable=True)
    return artifact(raw, schema_id, 'investment-run://sc02/' + relative)


def load_manifest(root, day, schemas):
    path = safe_path(root, 'runs', 'sc02', day, 'manifest.json')
    if path.exists():
        return schemas.document('investment.sc02_daily_manifest', path.read_bytes())
    return {'schema_version': 1, 'stage_id': 'SC02', 'execution_date': day,
            'timezone': 'Asia/Shanghai', 'invocations': []}


def verify_manifest(manifest, resolver):
    """清单是预算权威；不得用恢复重置已开始的尝试或删除既有失败。"""
    resolver.schemas.validate('investment.sc02_daily_manifest', manifest)
    identities = [item['invocation_id'] for item in manifest['invocations']]
    require(len(identities) == len(set(identities)), 'N03', '/invocations', 'Duplicate invocation identity')
    for invocation in manifest['invocations']:
        prefix = f"investment-run://sc02/{manifest['execution_date']}/{invocation['invocation_id']}/"
        incoming = resolver.resolve(invocation['frozen_input_ref'], 'investment.stage_input')
        require(invocation['frozen_input_ref']['uri'] == prefix + 'input.json'
                and (incoming['run_id'], incoming['invocation_id'], incoming['stage_id'])
                == (invocation['run_id'], invocation['invocation_id'], 'SC02')
                and incoming['parameters_ref'] == invocation['parameters_ref'],
                'N03', '/frozen_input_ref', 'Manifest/frozen input identity differs', 'identity_mismatch')
        attempts = invocation['attempts']
        require([item['attempt_no'] for item in attempts] == list(range(1, len(attempts) + 1))
                and len(attempts) <= 3, 'N03', '/attempts', 'Attempt budget/order differs')
        states = [item['processing_state'] for item in attempts]
        state = invocation['processing_state']
        expected = {'pending': [], 'running': ['failed'] * max(0, len(states) - 1) + ['running'],
                    'completed': ['failed'] * max(0, len(states) - 1) + ['completed'],
                    'failed': ['failed'] * len(states)}
        valid_states = states == expected[state] or state == 'running' and states == ['failed'] * len(states)
        require(valid_states and (state == 'pending' or bool(states)),
                'N03', '/attempts', 'Invocation/attempt states differ')
        active = state in ('pending', 'running')
        require(invocation['finished_at']['status'] == ('unknown' if active else 'known'),
                'N03', '/finished_at', 'Invocation terminal time differs')
        require(invocation['started_at']['status'] == ('unknown' if state == 'pending' else 'known'),
                'N03', '/started_at', 'Invocation start time differs')
        previous = instant(invocation['started_at']['value']) if is_known(invocation['started_at']) else None
        for attempt in attempts:
            require(previous is None or instant(attempt['started_at']) >= previous,
                    'N03', '/attempts/started_at', 'Attempt time moved backwards')
            attempt_prefix = prefix + f"attempt-{attempt['attempt_no']:02}/"
            for key, filename, schema_id in (
                ('capture_log_ref', 'capture-log.json', 'investment.news_capture_log'),
                ('candidate_ref', 'candidate.json', 'investment.candidate_output'),
                ('receipt_ref', 'receipt.json', 'investment.validation_receipt'),
                ('failure_ref', 'failure.json', 'investment.sc02_failure'),
            ):
                if is_known(attempt[key]):
                    reference = attempt[key]['value']
                    require(reference['uri'] == attempt_prefix + filename,
                            'N03', '/' + key, 'Attempt artifact path differs', 'identity_mismatch')
                    value = resolver.resolve(reference, schema_id)
                    if key in ('capture_log_ref', 'failure_ref'):
                        require((value['run_id'], value['invocation_id'], value['attempt_no'])
                                == (invocation['run_id'], invocation['invocation_id'], attempt['attempt_no']),
                                'N03', '/' + key, 'Attempt artifact identity differs', 'identity_mismatch')
                    if key == 'failure_ref':
                        require(value['input_ref'] == invocation['frozen_input_ref']
                                and value['candidate_ref'] == attempt['candidate_ref'],
                                'N03', '/failure_ref', 'Failure/frozen artifacts differ')
                    if key == 'receipt_ref':
                        require(is_known(attempt['candidate_ref'])
                                and value['candidate_output_ref'] == attempt['candidate_ref']['value']
                                and value['input_refs'] == incoming['input_refs']
                                and value['run_id'] == invocation['run_id'] and value['stage_id'] == 'SC02',
                                'N03', '/receipt_ref', 'Receipt/frozen artifacts differ')
            terminal = attempt['processing_state'] != 'running'
            require(is_known(attempt['finished_at']) == terminal,
                    'N03', '/attempts/finished_at', 'Attempt terminal time differs')
            require(is_known(attempt['failure_ref']) == (attempt['processing_state'] == 'failed'),
                    'N03', '/failure_ref', 'Attempt failure state differs')
            if terminal:
                previous = instant(attempt['finished_at']['value'])
                require(previous >= instant(attempt['started_at']), 'N03', '/finished_at', 'Attempt ends before start')
            if attempt['processing_state'] == 'completed':
                require(all(is_known(attempt[key]) for key in ('capture_log_ref', 'candidate_ref', 'receipt_ref')),
                        'N03', '/attempts', 'Completed attempt lacks actual artifacts')
        require(is_known(invocation['sealed_output_ref']) == (state == 'completed')
                and is_known(invocation['failure_summary_ref']) == (state == 'failed'),
                'N03', '/processing_state', 'Invocation terminal artifact differs')
        if state == 'completed':
            seal = resolver.resolve(invocation['sealed_output_ref']['value'], 'investment.sealed_output')
            require(invocation['sealed_output_ref']['value']['uri'] == prefix + f'attempt-{len(attempts):02}/sealed.json'
                    and seal['run_id'] == invocation['run_id'] and seal['stage_id'] == 'SC02'
                    and seal['candidate_output_ref'] == attempts[-1]['candidate_ref']['value']
                    and seal['validation_receipt_ref'] == attempts[-1]['receipt_ref']['value'],
                    'N03', '/sealed_output_ref', 'Seal does not bind current accepted attempt')
        if state == 'failed':
            summary = resolver.resolve(invocation['failure_summary_ref']['value'], 'investment.sc02_failure_summary')
            require(len(attempts) == 3 and summary['attempt_count'] == len(attempts)
                    and summary['attempt_failure_refs'] == [item['failure_ref']['value'] for item in attempts]
                    and (summary['run_id'], summary['invocation_id']) == (invocation['run_id'], invocation['invocation_id']),
                    'N03', '/failure_summary_ref', 'Terminal failure budget/bindings differ')


def save_manifest(root, manifest, resolver):
    verify_manifest(manifest, resolver)
    directory = safe_path(root, 'runs', 'sc02', manifest['execution_date'])
    atomic(directory / 'manifest.json', canonical(manifest))
    lines = [f"# SC02：{manifest['execution_date']} 执行说明", '', '时区：Asia/Shanghai', '',
             '| invocation_id | 执行内容与文件 | 状态与尝试 | 保留条件 |', '|---|---|---|---|']
    for invocation in manifest['invocations']:
        base = directory / invocation['invocation_id']
        files = ', '.join(path.relative_to(base).as_posix() for path in sorted(base.rglob('*')) if path.is_file())
        causes = []
        for attempt in invocation['attempts']:
            if is_known(attempt['failure_ref']):
                failure = resolver.resolve(attempt['failure_ref']['value'], 'investment.sc02_failure')
                causes.append(f"{attempt['attempt_no']}:{failure['phase']}:{failure['rule_id']}:{failure['pointer']}:{failure['code']}:{failure['reason']}")
        columns = (invocation['invocation_id'], invocation['description'] + '; ' + files,
                   invocation['processing_state'] + f"; attempts={len(invocation['attempts'])}; " + '; '.join(causes),
                   invocation['retention']['state'] + ': ' + invocation['retention']['reason'])
        lines.append('| ' + ' | '.join(html.escape(value).replace('|', '&#124;').replace('\n', ' ') for value in columns) + ' |')
    lines += ['', '冻结输入、来源规则、全部请求原字节、候选及凭证共同支持引用；有效用途结束前保留。',
              '部分材料仅支持凭证许可的研究用途；处理成功与整窗目标达成分别表达。此入口不提供自动清理。', '']
    atomic(directory / 'README.md', '\n'.join(lines).encode('utf-8'))
