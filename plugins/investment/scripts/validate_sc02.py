"""SC02 只读独立验收入口；公共凭证写 stdout，启动拒绝写 stderr。"""
import argparse
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from investment.contracts import ContractError, canonical, require
from investment.chains.news.contracts import NewsResolver, NewsSchemas, guard_namespace
from investment.chains.news.validation import validate_output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--invocation-id', required=True)
    parser.add_argument('--attempt-no', type=int, choices=(1,2,3), required=True)
    parser.add_argument('--mode', choices=('formal','test'), required=True)
    parser.add_argument('--network-origin', choices=('live','simulated'), default='live')
    parser.add_argument('--plugins-dir', type=Path)
    args = parser.parse_args()
    root,plugins = guard_namespace(args.root,args.mode,args.network_origin,args.plugins_dir)
    # 两个入口均先校验命名空间，再读取其中任意文件。
    incoming,candidate = args.input.resolve(),args.candidate.resolve()
    run_root = root/'runs/sc02'
    require(incoming.is_relative_to(run_root) and candidate.is_relative_to(run_root),
            'N01','/input','Entries must be within selected SC02 run namespace','use_not_allowed')
    parts = incoming.relative_to(run_root).parts
    require(len(parts) == 3 and parts[1:] == (args.invocation_id,'input.json')
            and candidate == incoming.parent/f'attempt-{args.attempt_no:02}'/'candidate.json',
            'N01','/input','Frozen input/current candidate layout required','use_not_allowed')
    schemas = NewsSchemas()
    resolver = NewsResolver(root,schemas,mode=args.mode,network_origin=args.network_origin,plugins_dir=plugins)
    resolver.run_parts('investment-run://sc02/'+'/'.join(parts))
    input_ref = resolver.path_ref(incoming,'investment.stage_input')
    candidate_ref = resolver.path_ref(candidate,'investment.candidate_output')
    receipt = validate_output(input_ref,candidate_ref,resolver,run_id=args.run_id,
                              invocation_id=args.invocation_id,attempt_no=args.attempt_no)
    sys.stdout.buffer.write(canonical(receipt))
    sys.stdout.buffer.flush()
    return 0 if receipt['business_result'] in ('usable','observation_only') else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ContractError,OSError,ValueError) as error:
        print(f'SC02 validation cannot start: {error}',file=sys.stderr)
        raise SystemExit(2)
