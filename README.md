# Erwann's Trading Desk

An autonomous AI trading agent that paper-trades a **$10,000** budget across US stocks, ETFs,
options, crypto (Alpaca paper) and forex (OANDA practice). Fake money only; both accounts
point at paper/practice endpoints and the code has no live-trading URL in it.

## How it runs

GitHub Actions runs the bot on a schedule for free (public repo):

| Workflow | When | What it does |
|---|---|---|
| **Trading desk tick** | every 15 minutes, 24/7 | Reads your Telegram commands, checks risk limits, saves state |
| **Research team** | morning 7:40am NY weekdays; intraday 10:20, 11:50, 1:20, 2:50 NY; evening 6:10pm daily; overnight 10:10pm and 2:10am | Scans the market, runs the AI analysts, sends ideas to Telegram |
| **Weekly review** | Sundays 7:30pm NY | Backtest, strategy tournament, fund letter, new rules |
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
3. **Trading engine** (done): see below
4. **Telegram** (done): trade alerts, briefs, evening recap, `/letter`, `/rules`, and free-text chat with the desk
5. **Dashboard** (done): https://erwannsssss.github.io/trading-desk/
6. **Learning** (done): trade post-mortems, strategy tournament, weekly review and fund letter, rulebook,
   confidence calibration, passed-ideas tracking
7. **Backtesting** (done): weekly backtest of every chart setup on ~2 years of data
8. **Options** (done): buys calls and puts for strong views (spreads and option selling are not automated)

## More trades

Fast sessions use a one-call analyst (all four specialists, bull, bear and portfolio manager in one prompt) so the
desk can research every ~90 minutes during market hours and through the night without running out of free AI
calls. The portfolio manager is told the desk aims for 20-30 trades a day and should take small, low-conviction
positions on modest edges instead of passing. Symbols researched in the last 3 hours are skipped.

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

## The trading engine

Runs on every tick (about every 15 minutes), in `desk/trading/engine.py`:

- **Entries:** takes each approved idea from today's plans, re-checks the live price (skips it if the stop or
  target already hit, waits if the price ran away from the entry), re-sizes with current equity, runs the
  risk manager, then places the order. Stock/ETF ideas (long or short) wait for the market to open.
- **Stocks and ETFs** go in as bracket orders: the broker holds the stop-loss and take-profit and fills them
  the instant price gets there, even between runs. **Forex** gets its stop and target attached on fill.
  **Crypto** gets a protective stop order; the engine takes profit at the target (crypto is long-only on Alpaca).
- **Managing trades:** stop to breakeven once a trade is up 1R, then trails 1R behind price once it's up 2R.
  Forex and crypto take half off at +1.5R. Trades are closed when they outlast their horizon
  (intraday by 3:50pm, "1-5 days" after a week, "1-4 weeks" after a month). Unfilled entries expire.
- Every fill, stop move, partial profit and exit goes to Telegram and the trade journal (`data/trades.json`).

## The dashboard

`docs/` is a static site on GitHub Pages. The bot rewrites `docs/data.json` after every run. Tabs:
overview with the equity curve against the S&P 500, open positions, the full trade log (tap a trade
to read its research memo: bull, bear, specialists, pre-mortem, fact-check), today's game plan,
performance stats (win rate, profit factor, drawdown, Sharpe, results by bucket/style/market,
confidence calibration), passed ideas and how they did, and the rulebook/fund letters.

## The trading floor

The dashboard's Trading floor tab shows every agent as a named person at one of three desks, what each one is
working on right now, and what they did in the last hour. `desk/activity.py` records each step and publishes a
small JSON file to the `live` branch about every 40 seconds (GitHub API with the workflow's built-in token), so
the page stays within a minute or two of live without waiting for GitHub Pages to rebuild.

| Desk | People |
|---|---|
| Research | Maya Chen (market scanner), Darius Webb (chief macro strategist), Ben Carter (research associate), Priya Natarajan (technical), Marcus Hale (fundamental), Sofia Reyes (news and sentiment), Theo Okafor (smart money), Jake Morrison (the bull), Nadia Volkov (the bear), Elena Castillo (portfolio manager), Sam Brooks (fact-checker), Iris Lindqvist (position sizer) |
| Trading | Kenji Watanabe (execution trader), Grace Adeyemi (risk manager), Leo Moretti (position manager), Ava Sinclair (options trader) |
| Learning | Ruth Klein (trade reviewer), Omar Haddad (performance analyst), Nina Petrova (quant researcher), Victor Ashford (chief investment officer), Riley Park (client desk, your Telegram) |

## The firm

`desk/staff.py` holds the org chart: Erwann (managing partner) at the top; Victor Ashford (CIO), Ashley Brennan
(Head of HR) and Riley Park (client desk) report to him; the research, trading and learning desks report to Victor.

- **Hit rates** (`desk/scorecard.py`): every researched idea is graded 3 trading days later, giving each analyst a
  hit rate (specialists' scores, the bull's and bear's calls, the PM's decisions, the macro regime call, the
  scanner's picks, the fact-checker's blocks). Trading desk numbers come from real trades.
- **HR** (`desk/hr.py`): Ashley writes weekly reviews with grades. Analysts with 20+ graded calls in 30 days and a
  hit rate under 42% are let go and replaced by a new hire with a different working approach, which goes into
  the AI prompts. Former staff are listed as alumni.
- **Interns** (`desk/interns.py`): three interns with their own personalities and $150 of pretend money pick
  trades after each morning and evening session. Reach $500 and they're hired as junior analysts whose best pick
  gets debated by the full team each session; drop to $30 and they're let go and replaced.
- **`/pitch TICKER`** (`desk/pitch.py`): the full team researches any stock, ETF, crypto or currency pair; an
  approved pitch is traded like any other idea.
- **Trade of the week** image card (`desk/cards.py`) and a monthly **season recap** from Victor.
- Dashboard: org chart floor with live status and hit rates, "Explain like I'm new" on every idea, and a
  tap-to-define glossary.

## Luck or skill?

`desk/edge.py` tests whether results are a real edge or a lucky streak, and the dashboard's **Luck or skill?**
tab charts it:

- **Expectancy** (average R per trade) with a t-test, a 90% confidence interval and a 4,000-sample bootstrap
- **Coin-flip permutation test:** same trade sizes, random direction; how often does luck do as well? Charted as
  a "luck zone" band around the desk's running total
- **Win rate** with a Wilson interval vs. the break-even win rate; rolling 20-trade win rate
- **Trades needed** before the current edge would be statistically significant
- **Versus the market:** beta, annualized alpha with its t-test, Sharpe, the probabilistic Sharpe ratio and the
  track record still needed; daily scatter vs. the S&P 500; drawdown chart
- **Edge by style** with confidence intervals, **conviction vs. result** correlation, **each analyst vs. a coin
  flip** (binomial test with Wilson intervals), and a **Monte Carlo** fan of the next 100 trades
- A verdict: too early (under 20 trades), no edge, promising, or evidence of a real edge (coin-flip p < 0.05
  and bootstrap P(edge > 0) >= 95%)

## Bank credit line

`desk/bank.py`: the firm can borrow up to $100,000 from "First Hoosier Bank" at 8% a year. A trade borrows only
what the firm's free cash can't cover; when it closes, the loan is repaid plus interest for the days it was out,
and interest comes out of the firm's equity. Hugo Brandt (treasurer) logs every loan and repayment. Borrowing
lets the desk hold more positions at once; it doesn't raise per-trade risk (still 5% of the firm's own money)
and total open risk across all trades is capped at 20%. Alpaca's own limits still apply (crypto is cash-only,
stocks get about 2x overnight), so trades are trimmed to fit. To let stocks use more of the line, raise the
Alpaca paper balance: with nothing open, the bot treats the change as a deposit, not profit.

## Making it skill, not luck

- **Shadow strategies** (`desk/shadow.py`): "always follow the bull", "only conviction 8+", "scanner's top
  pick" and "coin flip" trade the same research ideas in simulation; the Luck or skill tab compares them with
  the desk and tests whether the desk's lead is real (Welch t-test).
- **Breaking-news huddles** (`desk/alerts.py`): big moves or fresh headlines on an open position trigger a
  hold / close / tighten-the-stop decision; today's researched stocks that move 5%+ get re-researched.
- **Earnings season mode:** when 6+ big names report within 3 days, the scanner and analysts lean into earnings plays.
- **Sector heatmap** on the overview: sectors, themes, markets, world, rates and commodities over 1 day / week / month.

## Secrets

Add these in **Settings → Secrets and variables → Actions**:
`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `OANDA_ACCOUNT_ID`, `OANDA_API_TOKEN`,
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GEMINI_API_KEY`, `FINNHUB_API_KEY`, `FRED_API_KEY`

## Running the firm like a business

- **Levels, promotions and demotions.** Everyone has a level: Junior, Associate, Senior or Principal. In Ashley's Sunday review, people are promoted at 60%+ right over 15+ graded calls and demoted under 45%, at most once every 4 weeks. A Junior who stays under 45% is let go. The portfolio manager sees each analyst's level and hit rate and weighs proven people more.
- **Payroll and bonuses.** Staff are paid from fees, like a real hedge fund: base pay is 2% a year of firm equity, split by level, and bonuses are 20% of new profits above the high-water mark, split by level and hit rate and paid monthly. Erwann takes no salary, and interns are unpaid. Payroll, bonuses and bank interest all come out of firm equity.
- **Firm P&L tab.** The income statement since opening, by month and by day: trading gains minus interest, payroll and bonuses, which answers whether the firm is profitable.
- **Unlockable desks.** The first time equity reaches each milestone, a desk opens and stays open:

  | Equity | Unlock |
  |---|---|
  | $10,500 | Crypto desk |
  | $11,000 | Event desk |
  | $11,750 | FX desk |
  | $12,500 | Bank rate cut to 6.5% |
  | $13,500 | Quant desk |
  | $15,000 | 5 intern seats |

  The new desks give their own expert reads and add more ideas per session.
- **Compliance.** Diane Foster audits every trade after the fact: stops, risk at the actual fill, slippage, blackout windows, stops that were widened, oversized losses, holding periods, and firm-wide limits. Major findings go to Telegram, and every finding feeds the HR reviews.
- **Date pickers.** The trade log, game plan, passed ideas, compliance log, reviews, letters, cards and recaps show one day (or week, or month) at a time. Past days' plans are archived in `docs/days/`.
- **Staff photos.** AI headshots in suits live in `docs/faces/`. The photo job runs hourly within the free image service's limit until everyone has one.

## Making it more profitable

- **Realistic borrowing.** A forex position only ties up its margin (about 2 to 5% of its size), so that's all it borrows. Stock, crypto and options trades borrow from the bank only at conviction 7+; lower-conviction trades are sized to the firm's own free cash.
- **No doubled-up bets.** At most 2 positions leaning the same way on one currency, 3 crypto longs, or 3 stock positions with the same direction and style.
- **Better stocks.** Stocks must trade above $10 with at least $50M traded a day.
- **Risk-off rule.** When the market read is risk-off or panic, buying stocks needs conviction 6+.
- **Earnings.** No new stock trade is held through an earnings report unless it's an earnings play. Positions are closed in the afternoon before a report.
- **Debit spreads.** Options trades buy one option near the money and sell one at the target with the same expiry. That costs roughly 25 to 45% less than a single option, with the profit capped at the gap between the strikes.
- **Puts instead of shorts.** Alpaca doesn't allow shorting stocks on this account, so bearish stock views are traded with puts.
- **Results-driven sizing.** After 30+ closed trades, every bet is scaled from 0.7x to 1.2x by the measured edge. Any simple shadow rule that clearly beats the desk is shown to the portfolio manager.
- **AI budget.** Gemini comes first, with Groq as a backup that has its own free limits. Everyday chatter goes to Groq first, and the full team works on only the top 8 ideas per session.
- **`/ask` on Telegram.** Message anyone at the firm (for example, `/ask Nadia why ONDS?`) and they answer from their own records.
- **Trade replay.** Every trade's research drawer shows its price chart with the entry, each stop move, the target and the exit.

## Extra free data (desk/research/extra.py)

| Source | What it adds | Read by |
|---|---|---|
| edgartools | Latest quarter and year from SEC filings, plus insider Form 4 buys and sells | Fundamental analyst, smart money tracker |
| yfinance | Short interest (% of float, days to cover), analyst targets and ratings, ownership, beta | Sentiment analyst, the bear |
| FINRA short volume | Share of each day's trading that was short selling | Technical analyst |
| CFTC Commitments of Traders | Speculator positioning in currencies, gold, oil, stock indexes, Treasuries and bitcoin, with a 3-year percentile | Macro strategist, FX desk |
| alternative.me and DefiLlama | Crypto Fear & Greed, stablecoin supply and DeFi value locked | Crypto ideas, macro strategist |
| FinanceDatabase | Sector and industry for about 19,000 US stocks | Risk: at most 2 same-direction positions per industry |

Every source is cached and fails quietly. The health check tests all of them.

## Smarter trading, round 3

- **Second opinion.** Raj Iyer, an independent reviewer on a different AI model (Groq), checks every approved trade without seeing the team's debate. If he agrees, the bet is sized up 15%; if he disagrees, it's cut to 60%; if he strongly disagrees, the trade is vetoed.
- **Credit spreads.** When options are expensive (implied volatility 25%+ above what the stock actually moves), the desk sells a spread at the stop level instead of buying one, and keeps the premium if the stock stays on the right side.
- **Hedge desk.** When stock exposure is 30%+ of equity in a risk-off market, Grace buys an S&P 500 put spread with up to 1.5% of equity. It comes off when the market read improves or exposure drops under 10%.
- **Pair trades.** Each morning the desk looks for the widest 20-day split inside one industry: long the leader, puts on the laggard. Both legs open together and close together.
- **Auto-tuned exits.** The desk now tracks how far every trade ran and dipped. After 15+ trades in a style, targets move to where trades actually reach, and stops tighten if winners rarely dip.
- **Session budget.** Once there's enough data, research shifts toward the sessions whose ideas make money: 30% more ideas for the best session, 30% fewer for the worst.
- **Employee of the month** (best hit rate, 10+ calls, named on the 1st) and the **bull vs. bear rivalry** (weekly trash talk and a monthly trophy) on the Trading floor tab.
- **Bug fix.** P&L on bought puts was recorded with the wrong sign. Fixed before any put trade had closed.
