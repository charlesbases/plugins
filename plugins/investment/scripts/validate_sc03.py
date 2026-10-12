"""SC03独立验证CLI；共享入口负责冻结身份、宿主等待和结构化失败。"""
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from investment.research.controller import stage_main
from investment.chains.news_assessment.validation import validate

if __name__ == "__main__":
    raise SystemExit(stage_main("SC03", "validator", validate))
