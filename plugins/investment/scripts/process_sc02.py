"""SC02流程入口：仅产生本次候选或强格式失败，不调用独立验证器。"""
import argparse
import asyncio
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from investment.contracts import ContractError, canonical, na, now, require
from investment.chains.news.chain import collect_all, load_process_input
from investment.chains.news.contracts import NewsResolver, NewsSchemas, guard_namespace
from investment.runtime.paths import validate_input_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--mode', choices=('formal', 'test'), required=True)
    parser.add_argument('--network-origin', choices=('live', 'simulated'), required=True)
    parser.add_argument('--plugins-dir', type=Path)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--invocation-id', required=True)
    parser.add_argument('--attempt-no', type=int, choices=(1, 2, 3), required=True)
    args = parser.parse_args()
    # 根、模式、插件根与输入命名空间必须先于Schema/输入读取及任何目录创建。
    root, plugins = guard_namespace(args.root, args.mode, args.network_origin, args.plugins_dir)
    input_path = validate_input_path(root, args.input)
    run_root = root / 'runs' / 'sc02'
    require(input_path.is_relative_to(run_root), 'N01', '/input', 'SC02 input must be inside selected runs namespace', 'use_not_allowed')
    schemas = NewsSchemas()
    resolver = NewsResolver(root, schemas, mode=args.mode, network_origin=args.network_origin, plugins_dir=plugins)
    input_uri = 'investment-run://sc02/' + input_path.relative_to(run_root).as_posix()
    parts = resolver.run_parts(input_uri)
    require(parts[1:] == [args.invocation_id, 'input.json'], 'N03', '/input', 'Current invocation frozen input required', 'identity_mismatch')
    input_ref = resolver.path_ref(input_path, 'investment.stage_input')
    phase = 'input'
    try:
        bundle = load_process_input(input_ref, resolver, run_id=args.run_id,
                                    invocation_id=args.invocation_id, attempt_no=args.attempt_no)
        base = input_path.parent / f'attempt-{args.attempt_no:02}'
        require(not base.exists() or not any(base.iterdir()), 'N03', '/attempts',
                'Current production attempt already contains artifacts', 'use_not_allowed')
        base.mkdir(parents=True, exist_ok=True)
        prefix = input_uri.rsplit('/', 1)[0] + f'/attempt-{args.attempt_no:02}/'
        phase = 'collection'
        candidate_ref, _ = asyncio.run(collect_all(bundle, resolver, base, prefix, run_id=args.run_id,
                                        invocation_id=args.invocation_id, attempt_no=args.attempt_no))
        sys.stdout.buffer.write(canonical(candidate_ref))
        return 0
    except (ContractError, OSError, ValueError) as error:
        codes = {'schema_violation', 'invalid_reference', 'identity_mismatch', 'unsupported_output',
                 'missing_evidence', 'resource_limit', 'use_not_allowed', 'execution_failed'}
        code = getattr(error, 'code', 'execution_failed')
        failure = {'run_id': args.run_id, 'stage_id': 'SC02', 'invocation_id': args.invocation_id,
                   'attempt_no': args.attempt_no, 'failed_at': now(), 'phase': phase,
                   'rule_id': getattr(error, 'rule', 'N04'), 'code': code if code in codes else 'execution_failed',
                   'pointer': getattr(error, 'pointer', '/'), 'reason': (str(error) or type(error).__name__)[:2048],
                   'input_ref': input_ref, 'candidate_ref': na('candidate_not_produced'),
                   'evidence_refs': [input_ref] + getattr(error, 'evidence_refs', []),
                   'retryable': phase == 'collection' and code not in ('identity_mismatch', 'use_not_allowed')}
        schemas.validate('investment.sc02_failure', failure)
        sys.stdout.buffer.write(canonical(failure))
        return 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ContractError, OSError, ValueError) as error:
        print(f'Process cannot start: {error}', file=sys.stderr)
        raise SystemExit(2)
