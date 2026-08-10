import json
import tempfile
import unittest
from pathlib import Path
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from adaptive_live_policy import build_live_policy, policy_decision, runtime_policy, time_cell
from unified_entry_score import UNIFIED_SCORE_VERSION


IST = ZoneInfo("Asia/Kolkata")


def rows(count=40, start=date(2026, 6, 1)):
    result = []
    for index in range(count):
        day = start + timedelta(days=index)
        path = '[{"t":"%sT11:05:00+05:30","o":100,"h":122,"l":98,"c":115}]' % day
        result.append(
            {
                "observation_id": str(index),
                "trading_date": day.isoformat(),
                "signal_time": f"{day.isoformat()}T11:05:00+05:30",
                "symbol": "NIFTY",
                "score": 66,
                "score_version": UNIFIED_SCORE_VERSION,
                "action": "reject",
                "direction": "BULLISH",
                "reference_price": 100,
                "minute_path_json": path,
            }
        )
    return result


class AdaptiveLivePolicyTests(unittest.TestCase):
    def test_time_cells_are_deterministic(self):
        self.assertEqual(time_cell(datetime(2026, 8, 10, 11, 15, tzinfo=IST)), "11:00-12:59")
        self.assertEqual(time_cell(datetime(2026, 8, 10, 13, 55, tzinfo=IST)), "13:00-13:59")
        self.assertEqual(time_cell(datetime(2026, 8, 10, 14, 0, tzinfo=IST)), "14:00-15:25")

    def test_policy_requires_three_stable_calibrations_before_live(self):
        frame = pd.DataFrame(rows())
        first = build_live_policy(
            frame,
            date(2026, 7, 11),
            target_grid=(10, 20),
            stop_grid=(10, 20),
        )
        second_frame = pd.DataFrame(rows(41))
        second = build_live_policy(
            second_frame,
            date(2026, 7, 12),
            previous=first,
            target_grid=(10, 20),
            stop_grid=(10, 20),
        )
        third_frame = pd.DataFrame(rows(42))
        third = build_live_policy(
            third_frame,
            date(2026, 7, 13),
            previous=second,
            target_grid=(10, 20),
            stop_grid=(10, 20),
        )

        identifier = "11:00-12:59|65-69"
        self.assertEqual(first["cells"][identifier]["status"], "VALIDATING")
        self.assertEqual(second["cells"][identifier]["status"], "VALIDATING")
        self.assertEqual(third["cells"][identifier]["status"], "LIVE_ENABLED")
        self.assertEqual(third["global_status"], "LIVE_ENABLED")
        self.assertEqual(third["maximum_live_trades"], 1)

        decision = policy_decision(
            third, 66, datetime(2026, 7, 13, 11, 15, tzinfo=IST)
        )
        self.assertTrue(decision["adaptive_active"])
        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["cell"]["proposed_target_points"], 21.9)

    def test_once_live_a_failed_cell_suspends_instead_of_static_fallback(self):
        previous = {
            "ever_live_enabled": True,
            "effective_date": "2026-08-02",
            "cells": {
                "11:00-12:59|65-69": {
                    "status": "LIVE_ENABLED",
                    "consecutive_valid_calibrations": 3,
                }
            },
        }
        policy = build_live_policy(pd.DataFrame(), date(2026, 8, 3), previous=previous)
        self.assertEqual(policy["global_status"], "SUSPENDED")
        decision = policy_decision(
            policy, 66, datetime(2026, 8, 3, 11, 15, tzinfo=IST)
        )
        self.assertTrue(decision["adaptive_active"])
        self.assertFalse(decision["allowed"])

    def test_stale_previously_live_policy_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "policy.json"
            path.write_text(
                json.dumps(
                    {
                        "mode": "AUTO_ADAPTIVE_LIVE",
                        "score_version": UNIFIED_SCORE_VERSION,
                        "effective_date": "2026-08-02",
                        "global_status": "LIVE_ENABLED",
                        "ever_live_enabled": True,
                    }
                )
            )
            policy = runtime_policy(date(2026, 8, 3), path)

        self.assertEqual(policy["global_status"], "SUSPENDED")
        self.assertEqual(policy["maximum_live_trades"], 0)


if __name__ == "__main__":
    unittest.main()
