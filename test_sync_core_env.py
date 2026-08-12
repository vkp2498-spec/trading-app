import tempfile
import unittest
from pathlib import Path

from scripts.sync_core_env import (
    BLOCK_END,
    BLOCK_START,
    CORE_VALUES,
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
        self.assertEqual(updated.count("VAMSI_UNIFIED_SCORE_RANGES="), 1)
        self.assertIn("VAMSI_UNIFIED_SCORE_RANGES=80-89", updated)
        self.assertIn("MAX_INDEX_TRADES_PER_DAY=1", updated)
        self.assertIn("MAX_SIMULTANEOUS_PAPER_OBSERVATIONS=10", updated)
        self.assertIn("ACCOUNT_MAX_LOTS_PER_ENTRY=1", updated)
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
            "VAMSI_UNIFIED_SCORE_RANGES=80-100\n"
            "MAX_INDEX_TRADES_PER_DAY=0\n"
            "DAILY_PNL_GUARDS_ENABLED=false\n"
        )
        updated = "\n".join(normalized_lines("", overrides=overrides))

        self.assertIn("VAMSI_UNIFIED_SCORE_RANGES=80-100", updated)
        self.assertIn("MAX_INDEX_TRADES_PER_DAY=0", updated)
        self.assertIn("DAILY_PNL_GUARDS_ENABLED=false", updated)

    def test_instance_overrides_reject_unknown_or_sensitive_keys(self):
        with self.assertRaises(ValueError):
            parse_instance_overrides("UPSTOX_ACCESS_TOKEN=do-not-store-here")


if __name__ == "__main__":
    unittest.main()
