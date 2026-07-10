import json
import os

from openai import OpenAI

DEFAULT_DECISION = {
    "execute_trade": False,
    "decision": "NO_TRADE",
    "confidence": "LOW",
    "target_price": None,
    "stop_loss_price": None,
    "reason": "LLM decision disabled or unavailable",
}


def llm_enabled():
    return os.getenv("ENABLE_LLM_DECISION", "false").lower() == "true"


def build_rule_based_fallback(option_summary, technicals):
    option_bias = option_summary.get("bias")
    option_confidence = option_summary.get("confidence")

    four = technicals.get("four_hour", {})
    fifteen = technicals.get("fifteen_min", {})

    four_bias = four.get("bias")
    four_confidence = four.get("confidence")
    fifteen_bias = fifteen.get("bias")
    fifteen_momentum = int(fifteen.get("momentum_score") or 0)

    if option_confidence != "HIGH":
        return {**DEFAULT_DECISION, "reason": "Option chain confidence is not HIGH"}

    if option_bias not in {"BULLISH", "BEARISH"}:
        return {**DEFAULT_DECISION, "reason": "Option chain is not directional"}

    if fifteen_bias not in {option_bias, "NEUTRAL"}:
        return {
            **DEFAULT_DECISION,
            "reason": f"15M candle is opposite to option chain: option={option_bias}, 15M={fifteen_bias}",
        }

    four_conflicts = (
        four_confidence in {"MEDIUM", "HIGH"}
        and four_bias in {"BULLISH", "BEARISH"}
        and four_bias != option_bias
    )

    if four_conflicts:
        if fifteen_bias == option_bias and fifteen_momentum >= 3:
            return {
                "execute_trade": True,
                "decision": option_bias,
                "confidence": "MEDIUM",
                "target_price": option_summary.get("target_price"),
                "stop_loss_price": option_summary.get("stop_loss_price"),
                "reason": "Cautious reversal trade: option chain is HIGH and 15M strongly confirms despite 4H conflict.",
            }

        return {
            **DEFAULT_DECISION,
            "reason": f"4H conflicts with option chain and 15M confirmation is not strong enough. 15M momentum={fifteen_momentum}",
        }

    if fifteen_bias in {option_bias, "NEUTRAL"}:
        return {
            "execute_trade": True,
            "decision": option_bias,
            "confidence": "HIGH" if fifteen_bias == option_bias else "MEDIUM",
            "target_price": option_summary.get("target_price"),
            "stop_loss_price": option_summary.get("stop_loss_price"),
            "reason": "Option chain is HIGH confidence and technicals do not strongly conflict.",
        }

    return {**DEFAULT_DECISION, "reason": "No valid trade setup"}

def compact_decision_context(symbol, option_summary, technicals):
    four = technicals.get("four_hour", {}) or {}
    fifteen = technicals.get("fifteen_min", {}) or {}
    five = technicals.get("five_min", {}) or {}
    atm = technicals.get("atm_option_flow", {}) or {}
    weighted = option_summary.get("weighted_alignment", {}) or {}
    trend = option_summary.get("option_chain_trend", {}) or {}

    atm_close = atm.get("close")
    atm_vwap = atm.get("vwap")

    return {
        "symbol": symbol,
        "direction": option_summary.get("bias"),
        "option_chain_confidence": option_summary.get("confidence"),
        "option_chain_score": option_summary.get("score"),
        "entry_price": option_summary.get("entry_price"),
        "target_price": option_summary.get("target_price"),
        "stop_loss_price": option_summary.get("stop_loss_price"),
        "weighted_score": weighted.get("score"),
        "weighted_grade": weighted.get("grade"),
        "cautious_trade": bool(option_summary.get("cautious_trade")),
        "option_chain_trend_bias": trend.get("bias"),
        "option_chain_trend_aligns": trend.get("aligns_with_option_signal"),
        "four_hour_bias": four.get("bias"),
        "four_hour_confidence": four.get("confidence"),
        "fifteen_min_bias": fifteen.get("bias"),
        "fifteen_min_confidence": fifteen.get("confidence"),
        "fifteen_min_momentum": fifteen.get("momentum_score"),
        "five_min_bias": five.get("bias"),
        "five_min_confidence": five.get("confidence"),
        "five_min_momentum": five.get("momentum_score"),
        "atm_option_bias": atm.get("bias"),
        "atm_option_confidence": atm.get("confidence"),
        "atm_option_close": atm_close,
        "atm_option_vwap": atm_vwap,
        "atm_option_above_vwap": (
            atm_close is not None and atm_vwap is not None and float(atm_close) >= float(atm_vwap)
        ),
        "atm_option_volume_confirmed": bool(atm.get("volume_confirmed")),
        "atm_option_volume_ratio": atm.get("volume_ratio"),
    }


def get_llm_decision(symbol, option_summary, technicals):
    if not llm_enabled():
        return build_rule_based_fallback(option_summary, technicals)

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return {**DEFAULT_DECISION, "reason": "OPENAI_API_KEY not set"}

    model = os.getenv("OPENAI_MODEL", "gpt-4.1-nano")
    client = OpenAI(api_key=api_key)

    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "execute_trade": {"type": "boolean"},
            "decision": {"type": "string", "enum": ["BULLISH", "BEARISH", "NO_TRADE"]},
            "confidence": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
            "target_price": {"type": ["number", "null"]},
            "stop_loss_price": {"type": ["number", "null"]},
            "reason": {"type": "string"},
        },
        "required": [
            "execute_trade",
            "decision",
            "confidence",
            "target_price",
            "stop_loss_price",
            "reason",
        ],
    }

    decision_context = compact_decision_context(symbol, option_summary, technicals)

    payload = {
        "decision_context": decision_context,
        "rules": [
            "Option-chain HIGH confidence is mandatory for any trade.",
            "Reject if 15M bias is opposite to option-chain direction.",
            "4H conflict is a risk penalty, not automatic rejection.",
            "If 4H conflicts but 15M momentum_score is >= 3 in option-chain direction, a cautious trade may be allowed.",
            "If 4H conflicts and 15M is neutral/low momentum, reject.",
            "Prefer smaller target and tighter stop when 4H conflicts.",
            "Use option-chain target/stop as default option premium levels.",
            "Use option_target_price and option_stop_loss_price from technical analysis only when they support the option-chain direction.",
            "Do not invent prices. Target must be above entry premium and stop loss below entry premium.",
            "Use weighted_alignment score as important context.",
            "Scores >= 80 can be considered normal trade candidates.",
            "Scores 60 to 79 with CAUTIOUS_TRADE grade should generally execute if option-chain is HIGH and 15M/5M are aligned, using smaller target and tighter stop.",
            "Scores below 60 should normally be rejected.",
            "Volume confirmation on 5M strengthens breakout quality.",
            "VWAP alignment strengthens intraday trend quality.",
            "Option-chain trend over rec"rules": [
            "Use only decision_context. Do not infer from missing fields.",
            "If option_chain_confidence is HIGH, never say it is LOW.",
            "If atm_option_volume_confirmed is true, never say volume is not confirmed.",

            "Option-chain HIGH confidence is mandatory for any trade.",
            "Reject if 15M bias is opposite to option-chain direction.",
            "Reject if 5M bias is opposite to option-chain direction.",
            "4H conflict is a risk penalty, not automatic rejection.",
            "Reject 4H only when it has MEDIUM or HIGH confidence and is opposite to option-chain direction.",
            "Do not reject only because 4H confidence is LOW or 4H bias is NEUTRAL.",

            "Use weighted_alignment score as important context.",
            "Scores >= 80 can be considered normal trade candidates.",
            "Scores 60 to 79 with CAUTIOUS_TRADE grade should generally execute if option-chain is HIGH and 15M/5M are aligned, using smaller target and tighter stop.",
            "Scores below 60 should normally be rejected.",

            "When cautious_trade is true, evaluate it as an already risk-reduced setup with smaller target and tighter stop.",
            "If cautious_trade is true and option-chain is HIGH, 15M is aligned, and 5M is aligned, do not reject only because 4H is NEUTRAL or LOW confidence.",

            "ATM option volume and VWAP are more important than index volume/VWAP for NIFTY/BANKNIFTY option entries.",
            "ATM option premium above VWAP with above-average volume strengthens a long option trade.",
            "ATM option premium below VWAP reduces confidence even when volume is strong; below-VWAP option premium means buyers are not yet in control.",
            "ATM option below VWAP is a significant risk penalty. If ATM option is below VWAP, cautious trade can still be rejected unless other signals are very strong.",
            "ATM option flow below VWAP is a risk penalty, but not automatic rejection when 15M and 5M are aligned.",

            "Volume confirmation means volume_confirmed=true in atm_option_flow. Do not call volume weak when volume_confirmed is true.",
            "Option-chain trend over recent snapshots is more reliable than one snapshot alone.",

            "Use option-chain target/stop as default option premium levels.",
            "Use option_target_price and option_stop_loss_price from technical analysis only when they support the option-chain direction.",
            "Do not invent prices. Target must be above entry premium and stop loss below entry premium.",

            "When rejecting, state the actual blocker precisely: ATM option below VWAP, 4H opposite, 15M opposite, 5M opposite, weighted score too low, or option-chain confidence not HIGH.",
        ],
    }

    response = client.responses.create(
        model=model,
        input=[
            {
                "role": "system",
                "content": (
                    "You are a strict trading risk filter. "
                    "You do not give financial advice. "
                    "You only classify the provided signals into a JSON trade/no-trade decision."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(payload, sort_keys=True),
            },
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "trade_decision",
                "schema": schema,
                "strict": True,
            }
        },
    )

    try:
        return json.loads(response.output_text)
    except Exception:
        return {**DEFAULT_DECISION, "reason": f"Could not parse LLM response: {response.output_text[:300]}"}