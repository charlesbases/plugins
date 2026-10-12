"""SC04 正式生产入口；仅生成真实Host工作或业务候选。"""
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from investment.chains.fund_universe.chain import produce
from investment.research.controller import stage_main


if __name__ == '__main__':
    raise SystemExit(stage_main('SC04', 'producer', produce))
