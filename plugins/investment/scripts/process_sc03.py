"""SC03生产CLI：来源并发子session分析及完整候选装配。"""
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from investment.research.controller import stage_main
from investment.chains.news_assessment.chain import produce

if __name__ == '__main__':
    raise SystemExit(stage_main('SC03', 'producer', produce))
