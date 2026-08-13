import unittest
from pathlib import Path

from scripts.sync_trading_cron import BLOCK_END, BLOCK_START, normalized_crontab


class SyncTradingCronTests(unittest.TestCase):
    def test_replaces_old_bot_jobs_and_preserves_unrelated_jobs(self):
        existing = "\n".join(
            [
                "0 0 * * * /usr/local/bin/unrelated-job",
                "#* 4-9 * * 1-5 cd /old && python trade_bot.py --monitor",
                "30 3 * * 1-5 cd /old && python adaptive_score_calibration.py",
                BLOCK_START,
                "old managed content",
                BLOCK_END,
            ]
        )
        updated = normalized_crontab(existing, Path("/home/ubuntu/trading-app"))

        self.assertIn("/usr/local/bin/unrelated-job", updated)
        self.assertNotIn("cd /old", updated)
        self.assertEqual(updated.count(BLOCK_START), 1)
        self.assertEqual(updated.count(BLOCK_END), 1)
        self.assertIn("15 3 * * 1-5", updated)
        self.assertIn("47,52,57 3 * * 1-5", updated)
        self.assertIn("ml_shadow_v1.py --scan", updated)
        self.assertIn("46 7 * * 1-5", updated)
        self.assertIn("48 7 * * 1-5", updated)

    def test_disable_removes_managed_jobs_and_keeps_unrelated_jobs(self):
        existing = "\n".join(
            [
                "0 0 * * * /usr/local/bin/unrelated-job",
                *normalized_crontab("", Path("/home/ubuntu/trading-app")).splitlines(),
            ]
        )
        updated = normalized_crontab(
            existing,
            Path("/home/ubuntu/trading-app"),
            enabled=False,
        )

        self.assertEqual(updated, "0 0 * * * /usr/local/bin/unrelated-job\n")
        self.assertNotIn("trade_bot.py", updated)
        self.assertNotIn("ml_shadow_v1.py", updated)


if __name__ == "__main__":
    unittest.main()
