import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import vamsi_kb_daily_plan as planner


class VamsiKnowledgeDailyPlanTests(unittest.TestCase):
    def test_selects_one_gate_from_prior_completed_days(self):
        rows = []
        for index in range(6):
            rows.append(
                {
                    "trading_date": "2026-08-17" if index < 3 else "2026-08-18",
                    "symbol": "NIFTY",
                    "categories_json": json.dumps(
                        ["SCORE 80-89", "REJECT · OPTION CHAIN"]
                    ),
                    "favorable_points_before_stop": 35 if index < 4 else 15,
                    "adverse_points_before_stop": 12,
                    "target_hit_before_stop": index < 4,
                    "stop_hit": index >= 4,
                }
            )
        # Same-day data must never leak into the pre-market plan.
        rows.append(
            {
                "trading_date": "2026-08-19",
                "symbol": "NIFTY",
                "categories_json": json.dumps(
                    ["SCORE 80-89", "REJECT · OPTION FLOW / VOLUME"]
                ),
                "favorable_points_before_stop": 100,
                "adverse_points_before_stop": 0,
                "target_hit_before_stop": True,
                "stop_hit": False,
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.csv"
            pd.DataFrame(rows).to_csv(path, index=False)
            with patch.dict(
                planner.os.environ,
                {
                    "VAMSI_KB_PLAN_MIN_SAMPLES": "6",
                    "VAMSI_KB_PLAN_MIN_TRADING_DAYS": "2",
                    "VAMSI_KB_PLAN_MIN_TARGET_HIT_RATE": "35",
                },
            ):
                plan = planner.build_daily_plan(date(2026, 8, 19), path)

        nifty = plan["symbols"]["NIFTY"]
        self.assertEqual(plan["trainingThrough"], "2026-08-18")
        self.assertEqual(nifty["relaxedGates"], ["option_chain"])
        self.assertEqual(nifty["eligibleScoreBuckets"], ["80-89", "90-100"])
        self.assertEqual(nifty["maximumRelaxedFailuresPerCandidate"], 1)
        self.assertEqual(plan["symbols"]["BANKNIFTY"]["mode"], "STRICT")

    def test_multiple_failed_gates_are_not_used_to_promote_one_gate(self):
        rows = [
            {
                "trading_date": "2026-08-17",
                "symbol": "NIFTY",
                "categories_json": json.dumps(
                    [
                        "SCORE 70-79",
                        "REJECT · OPTION CHAIN",
                        "REJECT · CONSTITUENT BREADTH",
                    ]
                ),
                "favorable_points_before_stop": 100,
                "adverse_points_before_stop": 0,
                "target_hit_before_stop": True,
                "stop_hit": False,
            }
            for _ in range(10)
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.csv"
            pd.DataFrame(rows).to_csv(path, index=False)
            plan = planner.build_daily_plan(date(2026, 8, 18), path)

        self.assertEqual(plan["symbols"]["NIFTY"]["mode"], "STRICT")


if __name__ == "__main__":
    unittest.main()
