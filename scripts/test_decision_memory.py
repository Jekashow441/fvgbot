import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from trading.decision_memory import assess_context, context_key, settings_key
from core.settings import cfg


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.signal = dict(strategy_version='research_v5',strategy_profile='balanced',execution_version='cost_aware_v1',settings_fingerprint='x',timeframe='5',setup='fvg_retest',regime='TREND',signal='LONG')

    def rows(self,n=30,days=6,value=-1):
        return [dict(pnl_pct=value,closed_ts=f'2026-09-{1+i%days:02d}',ml_features=json.dumps({'decision_context':context_key(self.signal)})) for i in range(n)]

    def test_sparse_evidence_is_neutral(self):
        self.assertEqual(assess_context(self.signal,self.rows(7))['status'],'INSUFFICIENT')

    def test_single_day_cluster_insufficient(self):
        self.assertEqual(assess_context(self.signal,self.rows(40,1))['status'],'INSUFFICIENT')

    def test_negative_context_detected(self):
        self.assertEqual(assess_context(self.signal,self.rows())['status'],'NEGATIVE_EVIDENCE')

    def test_direction_and_settings_are_isolated(self):
        rows = self.rows()
        self.signal['signal']='SHORT'
        self.assertEqual(assess_context(self.signal,rows)['trades'],0)

    def test_positive_evidence_is_not_score_bonus(self):
        result=assess_context(self.signal,self.rows(value=1))
        self.assertEqual(result['status'],'OBSERVING')
        self.assertEqual(result['score_bonus'],0)

    def test_exit_setting_changes_memory_key(self):
        settings=cfg.model_copy(deep=True)
        key=settings_key(settings)
        settings.trail_gap_r += .1
        self.assertNotEqual(settings_key(settings),key)


class BreakevenTests(unittest.TestCase):
    def move(self,side,price,cost=7,old=False):
        from trading import paper_trading as paper
        features={'execution_version':'legacy' if old else 'cost_aware_v1','fee_bps':cost,'slippage_bps':0,
                  'exit_policy':dict(enable_breakeven=True,breakeven_at_r=.8,enable_trail=False)}
        trade=dict(id=999,symbol='TEST',side=side,entry=100,sl=99 if side=='LONG' else 101,tp=103 if side=='LONG' else 97,initial_risk=1,ml_features=json.dumps(features))
        with patch.object(paper,'update_signal_levels'),patch.object(paper,'log_info'):
            return paper._manage_exit_levels(trade,price,price)[0]

    def test_long_stop_covers_stored_cost(self):
        self.assertAlmostEqual(self.move('LONG',100.9),100.14)

    def test_short_stop_covers_stored_cost(self):
        self.assertAlmostEqual(self.move('SHORT',99.1),99.86)

    def test_stop_cannot_be_above_observed_long_price(self):
        self.assertEqual(self.move('LONG',100.9,cost=100),99)

    def test_existing_execution_convention_preserved(self):
        self.assertEqual(self.move('LONG',100.9,old=True),100)


if __name__ == '__main__':
    unittest.main()
