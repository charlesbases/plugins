"""SC04 正式控制入口；冻结、独立验证、三次预算和封存由共享控制器负责。"""
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from investment.research.controller import run_stage_cli


if __name__ == '__main__':
    raise SystemExit(run_stage_cli('SC04'))
