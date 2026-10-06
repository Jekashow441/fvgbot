import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd
from core.settings import cfg
from trading.market_reasoning import market_state
from trading.quality import summarize_outcomes
from trading.strategy import detect_fvgs
from trading.learning import learning_adjustment


class ReasoningTests(unittest.TestCase):
    def test_small_sample_cannot_establish_target(self):
        result = summarize_outcomes([1]*7, target=70, minimum=100)
        self.assertEqual(result["target_status"], "INSUFFICIENT")
        self.assertEqual(result["win_rate"], 100)

    def test_high_winrate_with_negative_expectancy_fails(self):
        result = summarize_outcomes([.1]*180+[-10]*20)
        self.assertEqual(result["win_rate"], 90)
        self.assertEqual(result["target_status"], "NOT_REACHED")

    def test_confidence_lower_bound_is_required(self):
        result = summarize_outcomes([1]*75+[-1]*25)
        self.assertLess(result["interval_95"][0], 70)
        self.assertEqual(result["target_status"], "NOT_REACHED")

    def test_observed_target_is_not_probability(self):
        result = summarize_outcomes([1]*900+[-1]*100)
        self.assertEqual(result["target_status"], "OBSERVED_TARGET")
        self.assertNotIn("prediction", result)

    def test_market_state_detects_shock(self):
        df = pd.DataFrame({"close":range(100,130), "open":range(100,130), "high":range(101,131), "low":range(99,129), "volume":[100]*30})
        self.assertEqual(market_state(df, 30, 2)["regime"], "TREND")
        df.loc[29,"high"] = 150
        self.assertEqual(market_state(df, 30, 2)["regime"], "SHOCK")

    def test_one_visit_is_not_three_independent_visits(self):
        df = pd.DataFrame([dict(timestamp=i*300000,open=100.,high=101.,low=99.,close=100.,volume=100.) for i in range(45)])
        df.loc[39,["open","high","low","close","volume"]] = [100,106,99.5,105.5,300]
        df.loc[40:,["open","high","low","close"]] = [104,107,103,105]
        settings = cfg.model_copy(deep=True)
        settings.strategy_profile = "contextual"
        settings.fvg_confirmations = 1
        zones = detect_fvgs(df,settings)
        self.assertTrue(zones)
        self.assertEqual(zones[0]["touches"],1)
        settings.strategy_profile = "balanced"
        self.assertEqual(detect_fvgs(df,settings)[0]["touches"],1)

    def test_correlated_factor_bonuses_do_not_stack(self):
        perf={name:{"sample_ok":True,"expectancy_lower":.1,"expectancy_upper":.2} for name in ["a","b","c"]}
        with patch("trading.learning.factor_performance",return_value=perf), patch.object(cfg,"enable_factor_learning",True), patch.object(cfg,"factor_score_adjustment_cap",15):
            self.assertEqual(learning_adjustment(["a","a","b","c"])[0],4)


if __name__ == "__main__":
    unittest.main()
