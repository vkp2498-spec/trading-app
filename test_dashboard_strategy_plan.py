import os
import unittest
from unittest.mock import patch

import dashboard_data


class DashboardStrategyPlanTests(unittest.TestCase):
    def test_weekly_manual_plan_exposes_only_non_secret_entry_policy(self):
        values = {
            "VAMSI_KB_WEEKLY_MANUAL_PLAN_ENABLED": "true",
            "VAMSI_KB_WEEKLY_PLAN_LABEL": "WEEKLY_TEST",
            "VAMSI_KB_NIFTY_LIVE_SCORE_BUCKETS": "50-59,80-89",
            "VAMSI_KB_NIFTY_LIVE_RELAXED_GATES": "setup,breadth",
            "VAMSI_KB_NIFTY_MAX_RELAXED_FAILURES": "1",
            "VAMSI_KB_NIFTY_TARGET_POINTS": "32",
            "VAMSI_KB_NIFTY_STOP_POINTS": "28",
            "VAMSI_KB_BANKNIFTY_LIVE_SCORE_BUCKETS": "",
            "VAMSI_KB_SENSEX_LIVE_SCORE_BUCKETS": "",
        }
        with patch.dict(os.environ, values, clear=False):
            plan = dashboard_data.build_strategy_plan()

        self.assertEqual(plan["reviewLabel"], "WEEKLY_TEST")
        nifty = plan["symbols"]["NIFTY"]
        self.assertEqual(nifty["eligibleScoreBuckets"], ["50-59", "80-89"])
        self.assertEqual(nifty["relaxedGates"], ["setup", "breadth"])
        self.assertEqual(nifty["maximumRelaxedFailuresPerCandidate"], 1)
        self.assertEqual(nifty["targetPoints"], 32.0)
        self.assertEqual(nifty["stopPoints"], 28.0)
        self.assertEqual(
            plan["symbols"]["BANKNIFTY"]["mode"],
            "WEEKLY_MANUAL_PAPER_ONLY",
        )
        self.assertNotIn("UPSTOX_ACCESS_TOKEN", str(plan))


if __name__ == "__main__":
    unittest.main()
