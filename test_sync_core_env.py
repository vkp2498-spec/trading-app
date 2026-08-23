import tempfile
import unittest
from pathlib import Path

from scripts.sync_core_env import (
    BLOCK_END,
    BLOCK_START,
    CORE_VALUES,
    deployment_role_overrides,
    normalized_lines,
    parse_instance_overrides,
)


class SyncCoreEnvTests(unittest.TestCase):
    def test_preserves_secrets_and_replaces_duplicates_and_deprecated_values(self):
        existing = "\n".join(
            [
                "UPSTOX_API_KEY=keep-me",
                "UPSTOX_ACCESS_TOKEN=keep-token",
                "GANESH_API_KEY=also-keep-me",
                "VAMSI_UNIFIED_SCORE_RANGES=50-59",
                "VAMSI_UNIFIED_SCORE_RANGES=80-89",
                "T20_MODE_ENABLED=true",
                "GANESH_MAX_TRADES=2",
                "TRADING_ENGINE=GANESH",
            ]
        )

        updated = "\n".join(normalized_lines(existing))

        self.assertIn("UPSTOX_API_KEY=keep-me", updated)
        self.assertIn("UPSTOX_ACCESS_TOKEN=keep-token", updated)
        self.assertIn("GANESH_API_KEY=also-keep-me", updated)
        self.assertNotIn("VAMSI_UNIFIED_SCORE_RANGES", updated)
        self.assertIn("TRADING_ENGINE=VAMSI_KB_INTRADAY_V1", updated)
        self.assertIn("ENABLE_LIVE_TRADING=false", updated)
        self.assertIn("ML_SHADOW_PAPER_ENABLED=false", updated)
        self.assertIn("OPTION_CAPITAL_PER_ENTRY=1", updated)
        self.assertIn("MAX_INDEX_TRADES_PER_DAY=1", updated)
        self.assertIn("TRADE_BANK_NIFTY=true", updated)
        self.assertIn("TRADE_SENSEX=true", updated)
        self.assertIn("VAMSI_KB_TARGET_POINTS=30", updated)
        self.assertIn("VAMSI_KB_STOP_POINTS=30", updated)
        self.assertIn("VAMSI_KB_NIFTY_TARGET_POINTS=30", updated)
        self.assertIn("VAMSI_KB_NIFTY_STOP_POINTS=30", updated)
        self.assertIn("VAMSI_KB_BANKNIFTY_TARGET_POINTS=60", updated)
        self.assertIn("VAMSI_KB_BANKNIFTY_STOP_POINTS=60", updated)
        self.assertIn("VAMSI_KB_SENSEX_TARGET_POINTS=40", updated)
        self.assertIn("VAMSI_KB_SENSEX_STOP_POINTS=40", updated)
        self.assertIn("VAMSI_KB_BANKNIFTY_MIN_BREADTH_COVERAGE=3", updated)
        self.assertIn("VAMSI_KB_SENSEX_MIN_BREADTH_COVERAGE=20", updated)
        self.assertIn("VAMSI_KB_WEEKLY_MANUAL_PLAN_ENABLED=true", updated)
        self.assertIn("VAMSI_KB_NIFTY_LIVE_SCORE_BUCKETS=50-59", updated)
        self.assertIn(
            "VAMSI_KB_NIFTY_LIVE_RELAXED_GATES=setup,completed_candles,breadth,option_flow",
            updated,
        )
        self.assertIn("VAMSI_KB_BANKNIFTY_LIVE_SCORE_BUCKETS=", updated)
        self.assertIn("VAMSI_KB_SENSEX_LIVE_SCORE_BUCKETS=", updated)
        self.assertIn("VAMSI_KB_DAILY_PLAN_ENABLED=false", updated)
        self.assertIn("VAMSI_KB_PLAN_MIN_TRADING_DAYS=2", updated)
        self.assertIn("VAMSI_KB_PLAN_EXPLORATION_ENABLED=true", updated)
        self.assertIn("CONCISE_TRADE_LOGS=true", updated)
        self.assertIn("ML_SHADOW_LIVE_TRADING_ENABLED=false", updated)
        self.assertIn("ML_SHADOW_V2_LIVE_ENABLED=false", updated)
        self.assertIn("ML_SHADOW_FORECAST_ONLY=true", updated)
        self.assertNotIn("T20_MODE_ENABLED", updated)
        self.assertNotIn("GANESH_MAX_TRADES", updated)
        self.assertEqual(updated.count(BLOCK_START), 1)
        self.assertEqual(updated.count(BLOCK_END), 1)

    def test_normalization_is_idempotent(self):
        first = "\n".join(normalized_lines("UPSTOX_API_KEY=secret"))
        second = "\n".join(normalized_lines(first))
        self.assertEqual(first, second)
        lines = second.splitlines()
        for key in CORE_VALUES:
            self.assertEqual(sum(line.startswith(f"{key}=") for line in lines), 1)

    def test_instance_overrides_replace_selected_core_values(self):
        overrides = parse_instance_overrides(
            "VAMSI_KB_MIN_BREADTH_SCORE=25\n"
            "MAX_CAPITAL_USE_PERCENT=90\n"
        )
        updated = "\n".join(normalized_lines("", overrides=overrides))

        self.assertIn("VAMSI_KB_MIN_BREADTH_SCORE=25", updated)
        self.assertIn("MAX_CAPITAL_USE_PERCENT=90", updated)

    def test_instance_overrides_reject_unknown_or_sensitive_keys(self):
        with self.assertRaises(ValueError):
            parse_instance_overrides("UPSTOX_ACCESS_TOKEN=do-not-store-here")

    def test_live_roles_separate_vamsi_max_from_ganesh_one_lot(self):
        vamsi = deployment_role_overrides("kb-live-max")
        ganesh = deployment_role_overrides("kb-live-one-lot")

        self.assertEqual(vamsi["ENABLE_LIVE_TRADING"], "true")
        self.assertEqual(vamsi["OPTION_CAPITAL_PER_ENTRY"], "MAX")
        self.assertEqual(vamsi["VAMSI_KB_FORCE_MAX_ALLOCATION"], "true")
        self.assertEqual(
            vamsi["ALLOW_BOT_WITH_UNTRACKED_DERIVATIVE_POSITIONS"], "false"
        )
        self.assertEqual(ganesh["ENABLE_LIVE_TRADING"], "true")
        self.assertEqual(ganesh["OPTION_CAPITAL_PER_ENTRY"], "1")
        self.assertEqual(ganesh["ACCOUNT_MAX_LOTS_PER_ENTRY"], "1")
        self.assertEqual(
            ganesh["ALLOW_BOT_WITH_UNTRACKED_DERIVATIVE_POSITIONS"], "true"
        )


if __name__ == "__main__":
    unittest.main()
