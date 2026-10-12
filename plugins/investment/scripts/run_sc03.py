"""SC03调度CLI；按冻结预算执行真实生产、独立验证和封存。"""
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from investment.research.controller import run_stage_cli

if __name__ == '__main__':
    raise SystemExit(run_stage_cli('SC03'))
