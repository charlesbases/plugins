"""SC03运行CLI：共享控制器管理真实进程、独立验收、单次执行与封存。"""
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from investment.chains.news_assessment.runner import run_cli

if __name__ == '__main__':
    raise SystemExit(run_cli())
