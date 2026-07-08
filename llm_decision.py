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
    four_bias = technicals.get("four_hour", {}).get("bias")
    fifteen_bias = technicals.get("fifteen_min", {}).get("bias")

    if option_confidence != "HIGH":
        return {**DEFAULT_DECISION, "reason": "Option chain confidence is not HIGH"}

    if option_bias in {"BULLISH", "BEARISH"} and four_bias == option_bias and fifteen_bias in {option_bias, "NEUTRAL"}:
        return {
            "execute_trade": True,
            "decision": option_bias,
            "confidence": "MEDIUM",
            "target_price": option_summary.get("target_price"),
            "stop_loss_price": option_summary.get("stop_loss_price"),
            "reason": "Rule fallback: option chain and 4H candle agree; 15M is not opposite.",
        }

    return {
        **DEFAULT_DECISION,
        "reason": "Rule fallback: option chain and candle signals do not align.",
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

    payload = {
        "symbol": symbol,
        "option_chain": option_summary,
        "technical_analysis": technicals,
        "rules": [
            "Return execute_trade=false if signals conflict strongly.",
            "Return execute_trade=false if 15M is opposite to option chain.",
            "Prefer option-chain target/stop_loss for option premium trade.",
            "Do not invent prices. Use provided target_price and stop_loss_price unless improving conservatively.",
            "This is intraday options buying. Stop loss must be below entry premium and target above entry premium.",
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