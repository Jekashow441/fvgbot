"""Quick project smoke checks.

Run:
    python scripts/smoke_check.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.settings import cfg, reload_settings
from core.database import init_db, get_stats
from trading.strategy import detect_fvg, validate_signal
from trading.paper_trading import get_paper_stats
from trading.learning import learning_summary, factor_performance


def main() -> None:
    reload_settings()
    init_db()
    assert cfg.max_active_positions >= 1, "max_active_positions must be >= 1"
    assert cfg.bybit_max_concurrency >= 1, "bybit_max_concurrency must be >= 1"
    assert callable(detect_fvg)
    assert callable(validate_signal)
    stats = get_stats()
    paper = get_paper_stats()
    learning = learning_summary()
    factors = factor_performance(min_trades=1)
    print("SMOKE OK")
    print(f"symbols={len(cfg.symbols)} timeframe={cfg.timeframe} running={cfg.is_running}")
    print(f"db_total={stats.get('total', 0)} active={paper.get('active_count', 0)} balance={paper.get('balance', 0):.2f}")
    print(f"learning={learning.get('enabled')} factors={len(factors)} cooldown={cfg.loss_cooldown_minutes}m")


if __name__ == "__main__":
    main()
