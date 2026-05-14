# JARVIS — Multi-Agent Prediction & Trading System

Replaces the old `trading_assistant.py` SMS notifier with a full agent swarm:
**Research → Filter → Predict → Risk/Execute → Learn**, working across
prediction markets (Polymarket / Kalshi) AND equities/futures.

## The 5-step pipeline

```
┌──────────────────────────────────────────────────────────────────────┐
│ 1. RESEARCH    Twitter • Reddit • RSS • YouTube • news (parallel)    │
│                Sentiment vs. market odds                              │
├──────────────────────────────────────────────────────────────────────┤
│ 2. FILTER      300+ markets → liquidity → volume → time → edge       │
│                Survivors: ~5–20 high-quality opportunities            │
├──────────────────────────────────────────────────────────────────────┤
│ 3. PREDICT     XGBoost classifier → raw probability                  │
│                LLM calibrator (Claude) → calibrated true_prob         │
│                Compare vs. market price → edge%                       │
├──────────────────────────────────────────────────────────────────────┤
│ 4. RISK        Kelly Criterion sizing (full + half)                   │
│                Limits: max position %, max open, min edge, max DD     │
│                Approve / block; execute on-chain or alert             │
├──────────────────────────────────────────────────────────────────────┤
│ 5. LEARN       On every loss: 5-agent post-mortem                    │
│                (Data, Sentiment, Timing, Model, Risk)                │
│                Save incident to memory; auto-add rules               │
└──────────────────────────────────────────────────────────────────────┘
```

## Project layout

```
jarvis/
├── jarvis.py            CLI entrypoint (scan / research / predict / size / execute / postmortem / daemon)
├── storage.py           SQLite store: predictions, incidents, rules, bankroll
├── bankroll.py          Kelly Criterion + risk-limit checks
├── rules.py             Rules engine + memory store ("never repeat the same mistake")
├── llm.py               Anthropic Claude wrapper (graceful fallback if no key)
├── data.py              All external sources (Finnhub, Polymarket, X, Reddit, RSS, YouTube, calendars)
├── notify.py            SMS (Twilio) + push + multi-channel alerts
├── agents.py            Five agents (MarketFilter, ResearchSwarm, Prediction, Risk, PostMortem)
├── web.py               Optional FastAPI dashboard (serves prototype.html + JSON API)
├── prototype.html       Single-file COMMAND-style visual dashboard
├── config.example.json  Template for keys
└── requirements.txt
```

## Setup

```bash
cd jarvis
cp config.example.json config.json   # then fill in keys
pip install -r requirements.txt
python jarvis.py setup                # one-time: init DB, set bankroll
```

Required keys (edit `config.json`):

| Key                     | Used by                       | Optional? |
|-------------------------|-------------------------------|-----------|
| `anthropic_api_key`     | LLM calibrator, post-mortem   | recommended |
| `finnhub_api_key`       | Equity/futures snapshots      | for stocks |
| `polymarket_api_key`    | Prediction-market quotes      | for PM    |
| `twitter_bearer_token`  | X/Twitter scraping            | optional  |
| `reddit_client_id` / `reddit_secret` | Reddit API       | optional  |
| `youtube_api_key`       | YouTube uploads/transcripts   | optional  |
| `twilio_*`              | SMS alerts                    | optional  |
| `wallet_private_key`    | On-chain Polymarket execution | optional  |

Anything missing falls back to a stub — system stays runnable end-to-end.

## Quick commands

```bash
python jarvis.py scan                   # Step 2 — filter live markets
python jarvis.py research <ticker|slug> # Step 1 — sentiment swarm
python jarvis.py predict <slug>         # Steps 1+3 — full prediction
python jarvis.py size <slug> <prob>     # Step 4 — Kelly + risk
python jarvis.py execute <slug>         # Steps 1–4 end-to-end
python jarvis.py postmortem <pred_id>   # Step 5 — learn from a loss
python jarvis.py daemon                 # always-on (alerts + auto-research)
python jarvis.py serve                  # FastAPI dashboard on :8000
```

## Alerts

`notify.py` fans out to **SMS (Twilio), Twitter mention watcher, news feed, YouTube uploads** —
all of them produce events that flow back into the Research agent.

## Migration from `trading_assistant.py`

The old script's watchlist (CL/ES/NQ/GC/BTC/DXY) and morning/evening SMS briefs are subsumed
by `daemon` mode + the Prediction agent in equities domain. Old config keys are reused where
applicable. After verifying parity, the old file can be deleted.
