# Erwann's Trading Desk

An autonomous AI trading agent that paper-trades a **$10,000** budget across US stocks, ETFs,
options, crypto (Alpaca paper) and forex (OANDA practice). Fake money only; both accounts
point at paper/practice endpoints and the code has no live-trading URL in it.

## How it runs

GitHub Actions runs the bot on a schedule for free (public repo):

| Workflow | When | What it does |
|---|---|---|
| **Trading desk tick** | every 15 minutes, 24/7 | Reads your Telegram commands, checks risk limits, saves state |
| **Research team** | 7:40am NY on weekdays (morning) and 6:10pm NY daily (evening) | Scans the market, runs the AI analyst team, sends the game plan to Telegram |
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
2. **Research team** (done): see below
3. Trading engine: bracket orders, hourly loop, trailing stops, scaling in/out, options, forex
4. Telegram: trade alerts, morning brief, evening recap, chat with the bot
5. Dashboard
6. Learning: strategy tournament, weekly review, rulebook, confidence calibration, ghost portfolio
7. Backtesting

## The research team

Morning session: ~18 stocks/ETFs + 3 cryptos + 4 currency pairs. Evening session: 4 cryptos + 5 currency pairs.

1. **Screener** (code): scans ~150 stocks and ETFs (big caps, sectors, international, bonds, commodities,
   inverse ETFs, today's most-active and biggest movers, Reddit's hottest tickers, upcoming earnings),
   19 cryptos and 16 currency pairs. Flags breakouts, breakdowns, oversold bounces, trend pullbacks,
   volume surges and pre-market gaps, and keeps 5 slots for big-cap names.
2. **Macro strategist** (AI): reads rates, inflation, the VIX, every ETF's trend, overnight moves, crypto,
   forex, headlines, the economic and Fed calendar, and sets the regime, aggression and bucket split.
3. **Specialists** (AI): technical, fundamental, news/sentiment and smart-money reads on each finalist,
   using price data, valuation and growth, analyst ratings, earnings surprises, the latest earnings press
   release from the SEC, insider buying/selling, which of 14 big hedge funds own it, and Reddit chatter.
4. **Bull vs. bear** (AI): two separate analysts argue for and against the trade.
5. **Portfolio manager** (AI): runs a pre-mortem, decides long/short/pass, sets entry, stop, target,
   conviction and bucket, and cites the numbers it relied on.
6. **Fact-checker** (code): rejects any plan that cites numbers not in the data, has prices on the wrong
   side, enters too far from the market, has reward/risk under 1.2, or fails its own pre-mortem.
7. **Sizing** (code): risks 0.75% to 4% of the account depending on conviction, within the hard limits.

Plans are saved in `data/plans/`. Congressional trades and earnings-call transcripts aren't available for
free, so the desk uses insider filings and earnings press releases instead.

## Secrets

Add these in **Settings → Secrets and variables → Actions**:
`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `OANDA_ACCOUNT_ID`, `OANDA_API_TOKEN`,
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GEMINI_API_KEY`, `FINNHUB_API_KEY`, `FRED_API_KEY`
