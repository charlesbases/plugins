"""SC09 独立验证 CLI，使用共享冻结上下文、正式等待与失败协议。"""
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from investment.research.controller import stage_main
from investment.chains.recommendation.validation import validate

if __name__ == '__main__':
    raise SystemExit(stage_main('SC09', 'validator', validate))
