"""SC05 独立验收入口；只接收共享控制器的真实当前attempt。"""
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from investment.chains.research_dataset.validation import validate
from investment.research.controller import stage_main


if __name__ == '__main__':
    raise SystemExit(stage_main('SC05', 'validator', validate))
