"""SC06 独立验证 CLI；共享入口核真实冻结上下文及失败/等待。"""
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from investment.research.controller import stage_main
from investment.chains.forecast.validation import validate

if __name__ == '__main__':
    raise SystemExit(stage_main('SC06', 'validator', validate))
