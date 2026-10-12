"""标准SC09生产子进程入口。"""
from investment.chains.recommendation.chain import produce
from investment.research.controller import stage_main

if __name__ == '__main__':
    raise SystemExit(stage_main('SC09', 'producer', produce))
