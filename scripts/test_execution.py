"""Execution regression tests use a temporary database and no network."""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import database


class ExecutionTests(unittest.TestCase):
    def test_old_stop_checked_before_trailing_and_costs(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(database, "DB_PATH", str(Path(directory)/"test.db")):
            from trading import paper_trading as paper
            database.init_db()
            from core.settings import cfg
            saved = cfg.model_copy(deep=True)
            try:
                cfg.enable_trail = cfg.enable_breakeven = True
                cfg.paper_balance = 10000
                cfg.paper_trade_size_pct = 10
                cfg.max_active_positions = 5
                signal = dict(signal="LONG", entry=100, sl=99, tp=103, rr=3, strategy_version="structure_v2")
                self.assertTrue(paper.open_paper_trade("TESTUSDT", signal))
                self.assertFalse(paper.open_paper_trade("TESTUSDT", signal))
                with patch.object(paper, "save_settings"), patch.object(paper, "log_info"):
                    asyncio.run(paper.check_active_trades(None, "TESTUSDT", 102, 98))
                trade = database.get_history_signals()[0]
                self.assertEqual(trade["outcome"], "LOSS")
                self.assertEqual(trade["exit_price"], 99)
                self.assertLess(trade["pnl_pct"], -1)
            finally:
                for field in type(cfg).model_fields:
                    setattr(cfg, field, getattr(saved, field))

    def test_gap_stop_uses_observed_worse_price(self):
        from trading import paper_trading as paper
        from core.settings import cfg
        saved = cfg.paper_balance
        trade = dict(id=1, symbol="TESTUSDT", side="LONG", entry=100, sl=99, tp=103,
                     ml_features=json.dumps({"position_size_usdt": 1000}))
        try:
            with patch.object(paper, "get_active_signals", return_value=[trade]), patch.object(paper, "settle", return_value=(-30,saved-30)) as close, patch.object(paper, "save_settings"):
                asyncio.run(paper.check_active_trades(None, "TESTUSDT", 97, 97))
                self.assertEqual(close.call_args.args[2], 97)
        finally:
            cfg.paper_balance = saved


if __name__ == "__main__":
    # Import the module with its legacy init_db side effect isolated too.
    with tempfile.TemporaryDirectory() as directory, patch.object(database, "DB_PATH", str(Path(directory)/"import.db")):
        from trading import paper_trading
        unittest.main()
