import tempfile
import unittest
from pathlib import Path

from scripts.sync_core_env import BLOCK_END, BLOCK_START, CORE_VALUES, normalized_lines


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
        self.assertIn("VAMSI_UNIFIED_SCORE_RANGES=65-69", updated)
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


if __name__ == "__main__":
    unittest.main()
