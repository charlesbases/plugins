"""SC04 独立验收入口；共享控制器负责合法上下文及已消耗预算。"""
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from investment.chains.fund_universe.validation import validate
from investment.research.controller import stage_main


if __name__ == '__main__':
    raise SystemExit(stage_main('SC04', 'validator', validate))

