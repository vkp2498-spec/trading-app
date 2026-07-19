from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from stock_screener import _completed_candles
from stock_screener import _evaluate
from stock_screener import _refresh_invested
from stock_screener import _position_review
from stock_screener import _wilder_atr


IST = ZoneInfo("Asia/Kolkata")


def _frame(index, closes, volumes=None):
    volumes = volumes or [100.0] * len(closes)
    return pd.DataFrame(
        {
            "open": [value - 0.2 for value in closes],
            "high": [value + 1.0 for value in closes],
            "low": [value - 1.0 for value in closes],
            "close": closes,
            "volume": volumes,
        },
        index=pd.DatetimeIndex(index),
    )


def test_daily_candle_is_available_after_market_close_only():
    today = datetime(2026, 7, 17, 9, 15, tzinfo=IST)
    data = _frame(
        [today - timedelta(days=1), today],
        [100.0, 101.0],
    )

    before_close = _completed_candles(
        data, "days", 1, datetime(2026, 7, 17, 14, 0, tzinfo=IST)
    )
    after_close = _completed_candles(
        data, "days", 1, datetime(2026, 7, 17, 15, 31, tzinfo=IST)
    )

    assert len(before_close) == 1
    assert len(after_close) == 2


def test_true_atr_includes_overnight_gap():
    index = pd.date_range("2026-06-01", periods=20, tz=IST)
    closes = [100.0] * 19 + [120.0]
    data = _frame(index, closes)
    data.iloc[-1, data.columns.get_loc("low")] = 119.0
    data.iloc[-1, data.columns.get_loc("high")] = 121.0

    assert _wilder_atr(data) > 2.0


def test_signal_volume_baseline_excludes_latest_candle(monkeypatch):
    monkeypatch.setenv("STOCK_SCREENER_MIN_REWARD_RISK", "0")
    monkeypatch.setenv("STOCK_SCREENER_MIN_SCORE", "0")
    monkeypatch.setenv("STOCK_SCREENER_MIN_AVG_TURNOVER", "0")
    now = datetime(2026, 7, 17, 16, 0, tzinfo=IST)
    daily_index = pd.date_range(end=now.replace(hour=9, minute=15), periods=100, freq="D")
    daily_close = [100 + index * 0.2 for index in range(100)]
    daily = _frame(daily_index, daily_close, [100.0] * 99 + [1000.0])
    four_index = pd.date_range(end=now - timedelta(hours=5), periods=60, freq="4h")
    four_hour = _frame(four_index, [100 + index * 0.2 for index in range(60)])
    benchmark = _frame(daily_index, [100 + index * 0.1 for index in range(100)])

    result = _evaluate(
        {"symbol": "TEST", "name": "Test", "instrumentKey": "NSE_EQ|TEST", "isETF": False},
        daily,
        four_hour,
        benchmark,
        now,
    )

    assert result is not None
    assert result["volumeRatio"] == 10.0
    assert result["probabilityUp"] == result["setupStrength"]
    assert result["probabilityAvailable"] is False


def test_invested_levels_do_not_move_with_new_recommendation():
    items = [
        {
            "symbol": "TEST",
            "instrumentKey": "NSE_EQ|TEST",
            "entryPrice": 100.0,
            "quantity": 10,
            "originalTargetPrice": 110.0,
            "originalStopLossPrice": 95.0,
        }
    ]
    recommendations = [
        {
            "symbol": "TEST",
            "instrumentKey": "NSE_EQ|TEST",
            "targetPrice": 120.0,
            "stopLossPrice": 90.0,
            "setupStrength": 75.0,
            "trend": "BULLISH",
            "rsi14": 60.0,
        }
    ]

    result = _refresh_invested(
        items,
        [],
        recommendations,
        {"NSE_EQ|TEST": 108.0},
    )[0]

    assert result["targetPrice"] == 110.0
    assert result["stopLossPrice"] == 95.0
    assert result["lastPrice"] == 108.0
    assert result["unrealizedPnl"] == 80.0
    assert result["status"] == "ACTIVE"


def _position(last_price, tracked_at="2026-07-17T09:30:00+05:30"):
    return {
        "lastPrice": last_price,
        "entryPrice": 100.0,
        "targetPrice": 110.0,
        "stopLossPrice": 95.0,
        "trackedAt": tracked_at,
    }


def test_position_review_holds_fresh_qualified_setup():
    result = _position_review(
        _position(103.0),
        {"atr14": 2.0},
        signal_fresh=True,
        signal_age_hours=2.0,
        now=datetime(2026, 7, 17, 12, 0, tzinfo=IST),
    )

    assert result["recommendationAction"] == "HOLD"
    assert result["targetProgressPercent"] == 30.0
    assert result["currentQualified"] is True


def test_position_review_trails_to_breakeven_after_40_percent_progress():
    result = _position_review(
        _position(105.0),
        {"atr14": 2.0},
        signal_fresh=True,
        signal_age_hours=2.0,
        now=datetime(2026, 7, 17, 12, 0, tzinfo=IST),
    )

    assert result["recommendationAction"] == "TRAIL STOP"
    assert result["suggestedStopPrice"] == 100.0


def test_position_review_exits_at_original_levels():
    now = datetime(2026, 7, 17, 12, 0, tzinfo=IST)
    target = _position_review(_position(110.0), {}, True, 1.0, now)
    stop = _position_review(_position(94.0), {}, True, 1.0, now)

    assert target["recommendationAction"] == "EXIT"
    assert "target" in target["actionReason"].lower()
    assert stop["recommendationAction"] == "EXIT"
    assert "stop-loss" in stop["actionReason"].lower()


def test_position_review_requires_review_when_signal_is_stale_or_missing():
    now = datetime(2026, 7, 17, 12, 0, tzinfo=IST)
    stale = _position_review(_position(103.0), {"atr14": 2.0}, False, 120.0, now)
    missing = _position_review(_position(103.0), None, True, 1.0, now)

    assert stale["recommendationAction"] == "REVIEW"
    assert "stale" in stale["actionReason"].lower()
    assert missing["recommendationAction"] == "REVIEW"
    assert "top-10" in missing["actionReason"].lower()


def test_position_review_flags_completed_five_session_horizon():
    result = _position_review(
        _position(103.0, "2026-07-10T09:30:00+05:30"),
        {"atr14": 2.0},
        signal_fresh=True,
        signal_age_hours=1.0,
        now=datetime(2026, 7, 17, 12, 0, tzinfo=IST),
    )

    assert result["holdingDays"] == 5
    assert result["recommendationAction"] == "REVIEW"
    assert "five-session" in result["actionReason"].lower()
