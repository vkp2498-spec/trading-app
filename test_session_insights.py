import pandas as pd

from session_insights import blocker_category, build_session_insights, parse_log_evidence


def test_blocker_categories_are_stable():
    assert blocker_category("weighted score too low before LLM") == "WEIGHTED_SCORE_LOW"
    assert blocker_category("Technical reward/risk 0.37 is below required 1.00") == "TECHNICAL_RR_LOW"
    assert blocker_category("same-direction re-entry blocked after STOP_LOSS") == "LOSS_REENTRY_GUARD"


def test_log_parser_collects_signal_and_blocker():
    lines = [
        "2026-07-20 09:20:02 | NIFTY signal: BEARISH, confidence=HIGH, score=-4, strike=24200",
        "2026-07-20 09:20:03 | NIFTY no trade: weighted score too low before LLM",
    ]
    signals, blockers = parse_log_evidence(lines)
    assert signals.iloc[0]["direction"] == "BEARISH"
    assert blockers.iloc[0]["category"] == "WEIGHTED_SCORE_LOW"


def test_session_summary_reports_controls_and_regime():
    analysis = pd.DataFrame(
        [
            {"symbol": "NIFTY", "option_chain_bias": "BEARISH", "option_chain_confidence": "HIGH"},
            {"symbol": "NIFTY", "option_chain_bias": "BEARISH", "option_chain_confidence": "HIGH"},
            {"symbol": "NIFTY", "option_chain_bias": "NEUTRAL", "option_chain_confidence": "MEDIUM"},
        ]
    )
    report = build_session_insights(
        "2026-07-20",
        analysis,
        pd.DataFrame(),
        pd.DataFrame(),
        [],
        {"LOSS_REENTRY_MODE": "cooldown", "MIN_REENTRY_MINUTES": "0"},
    )
    assert "predominantly BEARISH / HIGH" in report["narrative"][0]
    assert report["controls"]["LOSS_REENTRY_MODE"] == "cooldown"
    assert "after both profitable and losing exits" in report["narrative"][-1]


def test_log_signal_mix_includes_checks_that_never_reached_analysis_journal():
    analysis = pd.DataFrame(
        [{"symbol": "NIFTY", "option_chain_bias": "BULLISH", "option_chain_confidence": "HIGH"}]
    )
    lines = [
        "2026-07-20 09:20:02 | NIFTY signal: BEARISH, confidence=HIGH, score=-4",
        "2026-07-20 09:25:02 | NIFTY signal: NEUTRAL, confidence=MEDIUM, score=-2",
    ]
    report = build_session_insights(
        "2026-07-20", analysis, pd.DataFrame(), pd.DataFrame(), lines, {}
    )
    directions = {row["direction"] for row in report["signal_mix"]}
    assert directions == {"BEARISH", "NEUTRAL"}
