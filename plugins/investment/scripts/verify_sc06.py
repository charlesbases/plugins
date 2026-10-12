"""SC06独立基础/真实流程入口；未就绪上游或真实Host待办不记通过。"""
import argparse
import io
import os
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / 'src'))
sys.path.insert(0, str(PLUGIN))

from investment.contracts import canonical, now
from investment.research.contracts import guard_namespace
from investment.runtime.paths import validate_input_path
from investment.runtime.runs import atomic
from tests import test_sc06_flow


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True, type=Path)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--foundation', action='store_true')
    group.add_argument('--selector', choices=('all', *test_sc06_flow.SELECTORS))
    parser.add_argument('--cases', type=Path)
    args = parser.parse_args()
    root = guard_namespace(args.root, 'test')
    cases = validate_input_path(root, args.cases) if args.cases else root / 'sc06-flow-cases.json'
    os.environ['INVESTMENT_SC06_FLOW_ROOT'] = str(root)
    os.environ['SC06_FLOW_CASES'] = str(cases)
    test_sc06_flow.ACTUAL_RUNS.clear()
    loader = unittest.TestLoader()
    if args.foundation:
        suite = loader.loadTestsFromTestCase(test_sc06_flow.SC06ProductionFoundation)
    else:
        names = test_sc06_flow.SELECTORS if args.selector == 'all' else (args.selector,)
        suite = unittest.TestSuite(test_sc06_flow.ActualSC06Flow('test_' + name) for name in names)
    log = io.StringIO()
    result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    pending = any(row['result']['status'] == 'host_work_required' for row in test_sc06_flow.ACTUAL_RUNS)
    status = ('host_work_required' if pending else 'failed' if not result.wasSuccessful()
              else 'not_executed' if len(result.skipped) == result.testsRun else 'partial' if result.skipped else 'passed')
    report = {'schema_version': 1, 'kind': 'mathematical_and_method_foundation' if args.foundation else 'actual_sc06_flow',
              'selector': 'foundation' if args.foundation else args.selector, 'finished_at': now(), 'status': status,
              'tests_run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
              'skipped': [{'test': str(test), 'reason': reason} for test, reason in result.skipped],
              'invocations': test_sc06_flow.ACTUAL_RUNS,
              'meaning': 'Foundations do not establish a sealed business run; absent legal upstream inputs or pending actual Host work are not passed selectors'}
    root.mkdir(parents=True, exist_ok=True)
    tag = ('foundation' if args.foundation else args.selector) + '-' + report['finished_at'].replace(':', '').replace('.', '')
    atomic(root / ('verify-sc06-' + tag + '.log.txt'), log.getvalue().encode('utf-8'), immutable=True)
    atomic(root / ('verify-sc06-' + tag + '.report.json'), canonical(report), immutable=True)
    sys.stdout.buffer.write(canonical(report))
    return 3 if pending else 1 if not result.wasSuccessful() else 4 if result.skipped else 0


if __name__ == '__main__':
    raise SystemExit(main())
