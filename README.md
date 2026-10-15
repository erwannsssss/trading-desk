# Erwann's Trading Desk

An autonomous AI trading agent that paper-trades a **$10,000** budget across US stocks, ETFs,
options, crypto (Alpaca paper) and forex (OANDA practice). Fake money only; both accounts
point at paper/practice endpoints and the code has no live-trading URL in it.

## How it runs

GitHub Actions runs the bot on a schedule for free (public repo):

| Workflow | When | What it does |
|---|---|---|
| **Trading desk tick** | every 15 minutes, 24/7 | Reads your Telegram commands, checks risk limits, saves state |
| **Health check** | by hand, or whenever the code changes | Runs the tests, checks every connection, messages you on Telegram |

The bot's memory lives in `data/` and is committed back to the repo after each run.

## Hard risk limits

The AI decides what to trade. These limits are plain code in `desk/config.py` and it can't override them:

- Stop trading for the day after a **6%** loss
- Max **30** trades a day (a cap, not a quota) and **20** open positions
- Max **25%** of the account in one position, max **5%** loss if a stop-loss hits
- Every new trade needs a stop-loss
- Max **2x** gross exposure for stocks/crypto/options, **5x** for forex
- Keep **10%** in reserve; no trades in the first/last 5 minutes of the stock session

## Telegram commands

`/status` `/positions` `/pause` `/resume` `/golive` `/dryrun` `/flatten confirm` `/help`

It starts in **dry-run** (analyze and log only). Send `/golive` to let it place paper trades.

## Build phases

1. **Foundation** (done): broker connections, budget, risk limits, kill switch, Telegram, AI and data clients
2. Research team: macro, screeners, fundamentals, technicals, news, insiders/13F/congress, Reddit, bull-vs-bear debate, pre-mortem, fact-check
3. Trading engine: bracket orders, hourly loop, trailing stops, scaling in/out, options, forex
4. Telegram: trade alerts, morning brief, evening recap, chat with the bot
5. Dashboard
6. Learning: strategy tournament, weekly review, rulebook, confidence calibration, ghost portfolio
7. Backtesting

## Secrets

Add these in **Settings → Secrets and variables → Actions**:
`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `OANDA_ACCOUNT_ID`, `OANDA_API_TOKEN`,
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GEMINI_API_KEY`, `FINNHUB_API_KEY`, `FRED_API_KEY`
