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
        self.assertIn("51,56 3 * * 1-5", updated)
        self.assertIn("0 3 * * 1-5", updated)
        self.assertIn("vamsi_kb_daily_plan.py --generate", updated)
        self.assertIn("1-56/5 4-8 * * 1-5", updated)
        self.assertIn("vamsi_kb_intraday.py --scan", updated)
        self.assertIn("trade_bot.py --monitor", updated)
        self.assertIn("59 9 * * 1-5", updated)
        self.assertIn("trade_bot.py --squareoff", updated)
        self.assertIn("30 10 * * 1-5", updated)
        self.assertIn("post_market_score_audit.py --knowledge-engine-all-scans", updated)
        self.assertNotIn("ml_shadow_v1.py --scan", updated)

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

    def test_opening_pulse_schedule_has_only_one_entry_and_1500_squareoff(self):
        updated = normalized_crontab(
            "0 2 * * 1-5 /usr/local/bin/token-request",
            Path("/home/ubuntu/trading-app"),
            mode="opening-pulse",
        )

        self.assertIn("50 3 * * 1-5", updated)
        self.assertIn("vamsi_opening_pulse.py --scan", updated)
        self.assertIn("30 9 * * 1-5", updated)
        self.assertIn("vamsi_opening_pulse.py --squareoff", updated)
        self.assertIn("35 9 * * 1-5", updated)
        self.assertIn("sync_upstox_today_trades.py", updated)
        self.assertNotIn("vamsi_kb_intraday.py", updated)
        self.assertNotIn("trade_bot.py --monitor", updated)

    def test_nifty_option_buy_schedule_scans_15m_and_exits_at_1525(self):
        updated = normalized_crontab(
            "0 2 * * 1-5 /usr/local/bin/token-request",
            Path("/home/ubuntu/trading-app"),
            mode="nifty-option-buy",
        )

        self.assertIn("vamsi_nifty_option_buy.py --scan", updated)
        self.assertIn("1,16,31,46 4-8 * * 1-5", updated)
        self.assertIn("1,16 9 * * 1-5", updated)
        self.assertNotIn("1-56/5", updated)
        self.assertIn("trade_bot.py --monitor", updated)
        self.assertIn("* 9 * * 1-5", updated)
        self.assertIn("55 9 * * 1-5", updated)
        self.assertNotIn("30 9 * * 1-5", updated)
        self.assertIn("trade_bot.py --squareoff", updated)
        self.assertIn("0 10 * * 1-5", updated)
        self.assertIn("sync_upstox_today_trades.py", updated)
        self.assertIn("/usr/local/bin/token-request", updated)
        self.assertEqual(updated, normalized_crontab(updated, Path("/home/ubuntu/trading-app"), mode="nifty-option-buy"))
        self.assertNotIn("vamsi_opening_pulse.py", updated)
        self.assertNotIn("vamsi_kb_intraday.py", updated)


if __name__ == "__main__":
    unittest.main()
