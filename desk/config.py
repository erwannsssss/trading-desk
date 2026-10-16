"""All settings in one place.

Secrets come from environment variables (GitHub Secrets when running on GitHub).
The RISK LIMITS below are hard-coded on purpose: the AI decides what to trade,
but it can never change these numbers.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"


def _env(name: str, required: bool = True) -> str:
    value = os.environ.get(name, "").strip()
    if required and not value:
        raise RuntimeError(f"Missing secret: {name}. Add it in GitHub -> Settings -> Secrets and variables -> Actions.")
    return value


# ---- Secrets -----------------------------------------------------------------
def alpaca_key() -> str: return _env("ALPACA_API_KEY")
def alpaca_secret() -> str: return _env("ALPACA_SECRET_KEY")
def oanda_account() -> str: return _env("OANDA_ACCOUNT_ID")
def oanda_token() -> str: return _env("OANDA_API_TOKEN")
def telegram_token() -> str: return _env("TELEGRAM_BOT_TOKEN")
def telegram_chat() -> str: return _env("TELEGRAM_CHAT_ID")
def gemini_key() -> str: return _env("GEMINI_API_KEY")
def groq_key() -> str: return os.environ.get("GROQ_API_KEY", "").strip()   # optional backup AI provider
def finnhub_key() -> str: return _env("FINNHUB_API_KEY")
def fred_key() -> str: return _env("FRED_API_KEY")


# ---- Endpoints (paper / practice only - never live) ---------------------------
ALPACA_TRADING_URL = "https://paper-api.alpaca.markets/v2"
ALPACA_DATA_URL = "https://data.alpaca.markets"
OANDA_URL = "https://api-fxpractice.oanda.com/v3"

# Free-tier AI models, tried in order until one answers. Gemini first; Groq (its own separate free limits)
# takes over when Gemini's daily caps run out. "groq:" models are skipped if GROQ_API_KEY isn't set.
GEMINI_MODELS = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-flash-latest"]
GEMINI_BACKUP = ["gemini-3.5-flash-lite", "gemma-4-31b-it"]
GROQ_MODELS = ["groq:openai/gpt-oss-120b", "groq:qwen/qwen3.8-27b", "groq:openai/gpt-oss-20b"]
AI_MODELS = GEMINI_MODELS + GROQ_MODELS + GEMINI_BACKUP
# Everyday chatter (Telegram chat, intern picks, huddles, HR) goes to Groq first to save Gemini for research.
AI_LIGHT = GROQ_MODELS + GEMINI_MODELS + GEMINI_BACKUP

# ---- Budget ------------------------------------------------------------------
STARTING_CAPITAL = 10_000.0  # total budget across BOTH accounts

# ---- HARD RISK LIMITS (the AI cannot override these) ---------------------------
MAX_DAILY_LOSS_PCT = 6.0         # stop trading for the day after losing this much
MAX_TRADES_PER_DAY = 30          # a cap, not a quota
MAX_OPEN_POSITIONS = 20
MAX_POSITION_PCT = 100.0         # largest single stock/crypto position, % of the firm's own equity (notional)
MAX_RISK_PER_TRADE_PCT = 5.0     # max loss if the stop-loss hits, % of bot equity
# Total exposure (stocks + crypto + options + forex) can reach the firm's equity plus the bank credit line (bank.py).
MAX_FOREX_POSITION_X = 6.0       # one forex position's size vs bot equity (forex moves are small, so sizes run bigger)
MAX_TOTAL_OPEN_RISK_PCT = 30.0   # all open trades together can't lose more than this % of the firm's own money at their stops
MAX_OPTIONS_PREMIUM_PCT = 6.0    # most premium to pay on one options trade, % of bot equity (the max loss)
MAX_OPTIONS_TOTAL_PCT = 15.0     # all open options positions combined
MIN_REWARD_TO_RISK = 1.2         # target must be at least this many times farther than the stop
REQUIRE_STOP_LOSS = True         # every new trade must have a stop
NO_TRADE_OPEN_MINUTES = 5        # skip the first minutes after the stock market opens
NO_TRADE_CLOSE_MINUTES = 5       # and the last minutes before it closes

# Start in dry-run: the bot analyzes and logs but does not send orders.
# Flip to live paper trading with the Telegram command /golive.
DEFAULT_DRY_RUN = True

TIMEZONE = "America/New_York"
