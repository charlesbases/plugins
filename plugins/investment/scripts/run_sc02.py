"""SC02 主控入口；stdout为登记的控制结果，启动拒绝只写stderr。"""
import argparse
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from investment.contracts import ContractError, canonical
from investment.runtime.news_controller import NewsController


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--mode', choices=('formal', 'test'), required=True)
    parser.add_argument('--network-origin', choices=('live', 'simulated'), required=True)
    parser.add_argument('--plugins-dir', type=Path)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--invocation-id', required=True)
    args = parser.parse_args()
    controller = NewsController(args.root, mode=args.mode, network_origin=args.network_origin, plugins_dir=args.plugins_dir)
    code, result = controller.run(args.input, run_id=args.run_id, invocation_id=args.invocation_id)
    sys.stdout.buffer.write(canonical(result))
    return code


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ContractError, OSError, ValueError) as error:
        print(f'SC02 controller cannot start: {error}', file=sys.stderr)
        raise SystemExit(2)
