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

    two = technicals.get("two_hour", {})
    fifteen = technicals.get("fifteen_min", {})
    five = technicals.get("five_min", {})
    atm = technicals.get("atm_option_flow", {})
    institutional = technicals.get("institutional_flow", {}) or {}
    weighted = option_summary.get("weighted_alignment", {}) or {}

    two_bias = two.get("bias")
    two_confidence = two.get("confidence")
    fifteen_bias = fifteen.get("bias")
    fifteen_momentum = int(fifteen.get("momentum_score") or 0)
    five_bias = five.get("bias")
    weighted_grade = weighted.get("grade")
    weighted_score = float(weighted.get("score") or 0)

    institutional_conflicts = (
        institutional.get("confidence") == "HIGH"
        and institutional.get("bias") in {"BULLISH", "BEARISH"}
        and institutional.get("bias") != option_bias
    )

    if option_confidence != "HIGH":
        return {**DEFAULT_DECISION, "reason": "Option chain confidence is not HIGH"}

    if option_bias not in {"BULLISH", "BEARISH"}:
        return {**DEFAULT_DECISION, "reason": "Option chain is not directional"}

    if institutional_conflicts:
        return {
            **DEFAULT_DECISION,
            "reason": (
                "High-confidence institutional footprint conflicts with the "
                f"option-chain direction: option={option_bias}, "
                f"institutional={institutional.get('bias')}"
            ),
        }

    aligned_fifteen_momentum = (
        fifteen_momentum
        if option_bias == "BULLISH"
        else -fifteen_momentum
    )

    if fifteen_bias not in {option_bias, "NEUTRAL"}:
        return {
            **DEFAULT_DECISION,
            "reason": f"15M candle is opposite to option chain: option={option_bias}, 15M={fifteen_bias}",
        }

    if five_bias not in {option_bias, "NEUTRAL"}:
        return {
            **DEFAULT_DECISION,
            "reason": f"5M candle is opposite to option chain: option={option_bias}, 5M={five_bias}",
        }

    if weighted_grade == "SKIP" and not option_summary.get("cautious_override"):
        return {
            **DEFAULT_DECISION,
            "reason": f"Weighted score is below the trade threshold: {weighted_score}",
        }

    atm_close = atm.get("close")
    atm_vwap = atm.get("vwap")
    atm_below_vwap = (
        atm_close is not None
        and atm_vwap is not None
        and float(atm_close) < float(atm_vwap)
    )

    if weighted_grade == "CAUTIOUS_TRADE" and (
        five_bias != option_bias or atm.get("bias") == "BEARISH" or atm_below_vwap
    ):
        return {
            **DEFAULT_DECISION,
            "reason": "Cautious setup lacks aligned 5M and supportive ATM option premium flow.",
        }

    two_conflicts = (
        two_confidence in {"MEDIUM", "HIGH"}
        and two_bias in {"BULLISH", "BEARISH"}
        and two_bias != option_bias
    )

    if two_conflicts:
        if (
            fifteen_bias == option_bias
            and aligned_fifteen_momentum >= 3
            and five_bias == option_bias
            and not atm_below_vwap
        ):
            return {
                "execute_trade": True,
                "decision": option_bias,
                "confidence": "MEDIUM",
                "target_price": option_summary.get("target_price"),
                "stop_loss_price": option_summary.get("stop_loss_price"),
                "reason": "Cautious reversal trade: option chain is HIGH and 15M strongly confirms despite 2H conflict.",
            }

        return {
            **DEFAULT_DECISION,
            "reason": (
                "2H conflicts with option chain and 15M confirmation is not "
                f"strong enough. Direction-adjusted 15M momentum={aligned_fifteen_momentum}"
            ),
        }

    if fifteen_bias in {option_bias, "NEUTRAL"} and five_bias in {option_bias, "NEUTRAL"}:
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
    two = technicals.get("two_hour", {}) or {}
    fifteen = technicals.get("fifteen_min", {}) or {}
    five = technicals.get("five_min", {}) or {}
    atm = technicals.get("atm_option_flow", {}) or {}
    weighted = option_summary.get("weighted_alignment", {}) or {}
    trend = option_summary.get("option_chain_trend", {}) or {}
    institutional = technicals.get("institutional_flow", {}) or {}
    feasibility = technicals.get("trade_feasibility", {}) or {}
    direction = option_summary.get("bias")

    atm_close = atm.get("close")
    atm_vwap = atm.get("vwap")

    return {
        "symbol": symbol,
        "direction": direction,
        "trade_action": "BUY_OPTION",
        "selected_option_type": "CE" if direction == "BULLISH" else "PE" if direction == "BEARISH" else None,
        "option_chain_confidence": option_summary.get("confidence"),
        "option_chain_score": option_summary.get("score"),
        "entry_price": option_summary.get("entry_price"),
        "target_price": option_summary.get("target_price"),
        "stop_loss_price": option_summary.get("stop_loss_price"),
        "weighted_score": weighted.get("score"),
        "weighted_grade": weighted.get("grade"),
        "cautious_trade": bool(option_summary.get("cautious_trade")),
        "cautious_override": bool(option_summary.get("cautious_override")),
        "option_chain_trend_bias": trend.get("bias"),
        "option_chain_trend_aligns": trend.get("aligns_with_option_signal"),
        "two_hour_bias": two.get("bias"),
        "two_hour_aligns": two.get("bias") in {direction, "NEUTRAL", None},
        "two_hour_confidence": two.get("confidence"),
        "two_hour_score": two.get("score"),
        "two_hour_pivot": two.get("pivot"),
        "two_hour_middle_band": two.get("middle_band"),
        "two_hour_upper_band": two.get("upper_band"),
        "two_hour_lower_band": two.get("lower_band"),
        "two_hour_option_target_price": two.get("option_target_price"),
        "two_hour_option_stop_loss_price": two.get("option_stop_loss_price"),
        "fifteen_min_bias": fifteen.get("bias"),
        "fifteen_min_aligns": fifteen.get("bias") in {direction, "NEUTRAL", None},
        "fifteen_min_confidence": fifteen.get("confidence"),
        "fifteen_min_momentum": fifteen.get("momentum_score"),
        "fifteen_min_pivot": fifteen.get("pivot"),
        "fifteen_min_middle_band": fifteen.get("middle_band"),
        "fifteen_min_upper_band": fifteen.get("upper_band"),
        "fifteen_min_lower_band": fifteen.get("lower_band"),
        "fifteen_min_option_target_price": fifteen.get("option_target_price"),
        "fifteen_min_option_stop_loss_price": fifteen.get("option_stop_loss_price"),
        "five_min_bias": five.get("bias"),
        "five_min_aligns": five.get("bias") in {direction, "NEUTRAL", None},
        "five_min_confidence": five.get("confidence"),
        "five_min_momentum": five.get("momentum_score"),
        "five_min_pivot": five.get("pivot"),
        "five_min_middle_band": five.get("middle_band"),
        "five_min_upper_band": five.get("upper_band"),
        "five_min_lower_band": five.get("lower_band"),
        "atm_option_contract": atm.get("label"),
        "atm_option_premium_bias": atm.get("bias"),
        "atm_option_supports_long_entry": atm.get("bias") == "BULLISH",
        "atm_option_weakens_long_entry": atm.get("bias") == "BEARISH",
        "atm_option_confidence": atm.get("confidence"),
        "atm_option_close": atm_close,
        "atm_option_vwap": atm_vwap,
        "atm_option_above_vwap": (
            atm_close is not None and atm_vwap is not None and float(atm_close) >= float(atm_vwap)
        ),
        "atm_option_volume_confirmed": bool(atm.get("volume_confirmed")),
        "atm_option_volume_ratio": atm.get("volume_ratio"),
        "institutional_footprint_bias": institutional.get("bias"),
        "institutional_footprint_confidence": institutional.get("confidence"),
        "institutional_footprint_score": institutional.get("score"),
        "institutional_footprint_aligns": institutional.get("bias") in {direction, "NEUTRAL", None},
        "institutional_futures_component": institutional.get("futures_component"),
        "institutional_options_component": institutional.get("options_component"),
        "institutional_basis_component": institutional.get("basis_component"),
        "institutional_persistence_component": institutional.get("persistence_component"),
        "institutional_reasons": (institutional.get("reasons") or [])[:6],
        "technical_feasibility_allowed": feasibility.get("allowed"),
        "technical_reward_risk": feasibility.get("technical_reward_risk"),
        "technical_headroom_percent": feasibility.get("technical_headroom_percent"),
        "technical_limiting_timeframe": feasibility.get("limiting_timeframe"),
        "entry_extension_percent": feasibility.get("entry_extension_percent"),
        "technical_feasibility_reasons": feasibility.get("reasons", []),
    }


def llm_factual_issues(decision, decision_context):
    reason = str(decision.get("reason") or "").lower()
    issues = []

    if decision_context.get("atm_option_above_vwap") is True and "below vwap" in reason:
        issues.append("LLM said the selected option was below VWAP when it was above VWAP")

    if (
        decision_context.get("option_chain_confidence") == "HIGH"
        and "option-chain confidence is low" in reason
    ):
        issues.append("LLM changed HIGH option-chain confidence to LOW")

    if decision_context.get("atm_option_volume_confirmed") is True and (
        "volume is not confirmed" in reason or "volume_confirmed=false" in reason
    ):
        issues.append("LLM said option volume was unconfirmed when it was confirmed")

    if decision_context.get("fifteen_min_aligns") is True and "15m is opposite" in reason:
        issues.append("LLM called an aligned 15M signal opposite")

    if decision_context.get("five_min_aligns") is True and "5m is opposite" in reason:
        issues.append("LLM called an aligned 5M signal opposite")

    return issues


def reconcile_llm_decision(decision, decision_context, option_summary, technicals):
    fallback = build_rule_based_fallback(option_summary, technicals)
    issues = llm_factual_issues(decision, decision_context)

    # Deterministic rules are the safety boundary. The LLM cannot approve a
    # setup rejected by those rules, regardless of its narrative.
    if not fallback.get("execute_trade"):
        return {
            **fallback,
            "reason": f"Deterministic safety gate: {fallback.get('reason')}",
        }

    weighted_grade = (option_summary.get("weighted_alignment", {}) or {}).get("grade")

    # A fully qualified TRADE setup is already authorized by objective inputs.
    # The LLM remains advisory here and cannot invent a semantic CE/PE conflict.
    if weighted_grade == "TRADE":
        return {
            **fallback,
            "reason": (
                "Deterministic TRADE-grade setup approved. "
                f"Rule engine: {fallback.get('reason')}"
            ),
        }

    if not issues:
        return decision

    fallback_reason = fallback.get("reason") or "Rule-based fallback applied"
    return {
        **fallback,
        "reason": (
            f"LLM factual inconsistency detected: {'; '.join(issues)}. "
            f"Deterministic fallback used: {fallback_reason}"
        ),
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
    "Use only decision_context. Do not infer from missing fields.",
    "If option_chain_confidence is HIGH, never say it is LOW.",
    "If atm_option_volume_confirmed is true, never say volume is not confirmed.",

    "Option-chain HIGH confidence is mandatory for any trade.",
    "Reject if 15M bias is opposite to option-chain direction.",
    "Reject if 5M bias is opposite to option-chain direction.",
    "2H conflict is a risk penalty, not automatic rejection.",
    "Reject 2H only when it has MEDIUM or HIGH confidence and is opposite to option-chain direction.",
    "Do not reject only because 2H confidence is LOW or 2H bias is NEUTRAL.",

    "Use weighted_alignment score as important context.",
    "Scores >= 80 can be considered normal trade candidates.",
    "Scores 65 to 79 with CAUTIOUS_TRADE grade require 5M alignment and either 15M alignment or strong direction-adjusted 15M momentum. A neutral 15M signal does not count as confirmation when 2H is opposite.",
    "Scores below 65 should normally be rejected unless the bot explicitly marks cautious_override true.",

    "When cautious_trade is true, evaluate it as an already risk-reduced setup with smaller target and tighter stop.",
    "If cautious_trade is true and option-chain is HIGH, 15M is aligned, and 5M is aligned, do not reject only because 2H is NEUTRAL or LOW confidence.",

    "ATM option volume and VWAP are more important than index volume/VWAP for NIFTY/BANKNIFTY option entries.",
    "ATM option premium bias describes the selected option contract's price trend, not the underlying index direction.",
    "A BULLISH ATM option premium bias supports buying the selected CE or PE because that option premium is strengthening.",
    "A BEARISH ATM option premium bias weakens or blocks a long-option entry because that selected option premium is falling.",
    "Never interpret BULLISH PE premium flow as opposing a BEARISH index trade.",
    "Never interpret BULLISH CE premium flow as opposing a BULLISH index trade.",
    "Use atm_option_supports_long_entry and atm_option_weakens_long_entry directly; do not compare atm_option_premium_bias with the underlying direction.",
    "ATM option premium above VWAP with above-average volume strengthens a long option trade.",
    "ATM option premium below VWAP reduces confidence even when volume is strong; below-VWAP option premium means buyers are not yet in control.",
    "ATM option below VWAP is a significant risk penalty. If ATM option is below VWAP, cautious trade can still be rejected unless other signals are very strong.",
    "ATM option flow below VWAP is a risk penalty, but not automatic rejection when 15M and 5M are aligned.",

    "Volume confirmation means volume_confirmed=true in atm_option_flow. Do not call volume weak when volume_confirmed is true.",
    "Option-chain trend over recent snapshots is more reliable than one snapshot alone.",

    "Institutional footprint is inferred from futures price/OI, nearby-strike option OI, basis, VIX, and persistence. It does not prove that FIIs caused the move.",
    "Daily FII positioning is supporting context only and must never independently authorize an intraday trade.",
    "An aligned MEDIUM or HIGH institutional footprint strengthens an existing setup.",
    "A HIGH-confidence opposite institutional footprint is a serious blocker.",
    "A NEUTRAL or LOW-confidence institutional footprint is not a blocker by itself.",
    "Use institutional_footprint_aligns directly and do not invent institutional activity from missing fields.",

    "Buying a PE is still a long-option purchase that expresses a BEARISH underlying view; do not confuse selected-option premium direction with underlying direction.",
    "Use the explicit two_hour_aligns, fifteen_min_aligns, and five_min_aligns fields when describing agreement.",

    "Pivot and Bollinger fields are supporting technical context; converted technical option levels may be used to assess whether the default risk levels are realistic.",
    "technical_feasibility_allowed must be true. The deterministic bot rejects the setup before this call otherwise.",
    "Use technical_reward_risk, technical_headroom_percent, and technical_limiting_timeframe when explaining whether sufficient reachable reward remains.",
    "The bot owns execution prices deterministically. Return the supplied target_price and stop_loss_price unchanged when approving a trade.",
    "Do not invent prices. Target must be above entry premium and stop loss below entry premium.",

    "NIFTY requires stronger confirmation than BANKNIFTY.",
    "CAUTIOUS_TRADE requires symbol minimum score: NIFTY >= 70, BANKNIFTY >= 65.",
    "ATM option premium flow has higher importance after recent losses; atm_option_weakens_long_entry=true is a serious blocker.",
    "Do not reject a trade merely because atm_option_premium_bias is BULLISH while the underlying direction is BEARISH; a strengthening selected PE supports that trade.",
    "Prefer trades where ATM option premium is above VWAP or its premium bias is at least neutral with strong 15M and 5M alignment.",

    "When rejecting, state the actual blocker precisely: ATM option below VWAP, 2H opposite, 15M opposite, 5M opposite, weighted score too low, or option-chain confidence not HIGH.",
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
        decision = json.loads(response.output_text)
        return reconcile_llm_decision(
            decision,
            decision_context,
            option_summary,
            technicals,
        )
    except Exception:
        return {**DEFAULT_DECISION, "reason": f"Could not parse LLM response: {response.output_text[:300]}"}
