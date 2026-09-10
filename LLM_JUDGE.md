# Live NIFTY veto judge

`NIFTY_LLM_JUDGE_ENABLED=true` adds a veto-only review to the existing NIFTY
engine. There is no shadow mode or automatic relaxation of strategy gates.
Default local/example setting is false; Vamsi AWS explicitly enables it.

Model: `NIFTY_LLM_JUDGE_MODEL=gpt-5.6-luna`. A strict structured response must
contain PASS, VETO or ABSTAIN, a short reason and references to actual supplied
fields. Only PASS proceeds. The model cannot set size, direction, price levels
or broker actions. Its judgment is not a calibrated probability or proven edge.

Only an allowlisted market snapshot is sent: completed candles, structure,
participation proxies, chain direction/contract quality, and proposed levels.
Account identity, tokens, capital and aggregate entry score are not sent.
Requests use the fixed OpenAI HTTPS endpoint, no tools, no redirects, no SDK
retries and `store=false`. That setting does not constitute a zero-retention
guarantee. HTTP errors, incomplete/refused output, unknown evidence fields,
missing credentials and timeouts skip the entry. Raw API errors are not logged.

The entry scanner performs the request outside the position-monitor process.
After PASS, the same strike/expiry is refreshed and normal gates rerun. Skip
if premium moves more than 2%, underlying moves more than 0.25 ATR, remaining
reward/risk fails or the time window ends. Absolute underlying target/stop
stay fixed; distances are rebased to the refreshed spot. The execution path
checks that the approved contract/levels match and approval age is <=45 sec.
No judge call or decision can delay exits, trailing or two-second monitoring.

Evidence/reviews are in `data/vamsi_nifty_option_buy/llm_judge.csv` and the
existing decision ledger; veto reasons appear in normal scan logs/dashboard.
Failed normal gates and an already-active position do not incur an API call.

## Credential setup (on AWS terminal)

```sh
cd /home/ubuntu/trading-app
venv/bin/python scripts/configure_openai_key.py
```

The hidden prompt writes `OPENAI_API_KEY` privately to `.env` (mode 0600).
Do not paste credentials into chat, command arguments, source or Git. Revoke
any key exposed in chat and rotate through this prompt. Cron scanners load the
new key on their next invocation. Never print `.env` during diagnostics.

Official references: [Responses structured output](https://developers.openai.com/api/docs/guides/structured-outputs),
[model](https://developers.openai.com/api/docs/models/gpt-5.6-luna).
