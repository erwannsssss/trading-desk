"""The risk manager. Every order passes through `check_order` before it is sent.

The AI is free to trade however it wants, but these checks are plain code and
cannot be talked out of. If a check fails, the order is blocked and logged.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from desk import config


@dataclass
class Book:
    """A snapshot of everything the bot owns, measured against its $10k budget."""
    alpaca_equity: float
    oanda_nav: float
    baseline_alpaca: float
    baseline_oanda: float
    stock_gross_exposure: float = 0.0   # $ value of all stock/crypto/option positions (long + short)
    forex_gross_notional: float = 0.0   # $ value of all forex positions
    open_positions: int = 0
    pending_exposure: float = 0.0       # $ tied up in unfilled entry orders

    @property
    def equity(self) -> float:
        """Bot equity = $10k plus whatever profit/loss it has made in both accounts."""
        pnl = (self.alpaca_equity - self.baseline_alpaca) + (self.oanda_nav - self.baseline_oanda)
        return config.STARTING_CAPITAL + pnl


@dataclass
class OrderIdea:
    symbol: str
    venue: str              # "alpaca" or "oanda"
    side: str               # "buy" or "sell"
    notional: float         # $ size of the position
    entry: float
    stop: float | None = None
    target: float | None = None
    opens_position: bool = True


@dataclass
class Verdict:
    ok: bool
    reasons: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return "approved" if self.ok else "blocked: " + "; ".join(self.reasons)


def daily_loss_pct(book: Book, day_start_equity: float) -> float:
    if not day_start_equity:
        return 0.0
    return (day_start_equity - book.equity) / day_start_equity * 100


def daily_limit_hit(book: Book, day_start_equity: float) -> bool:
    return daily_loss_pct(book, day_start_equity) >= config.MAX_DAILY_LOSS_PCT


def in_blackout(market_open: datetime | None, market_close: datetime | None, now: datetime) -> bool:
    """True during the first/last few minutes of the stock session."""
    if market_open and 0 <= (now - market_open).total_seconds() < config.NO_TRADE_OPEN_MINUTES * 60:
        return True
    if market_close and 0 <= (market_close - now).total_seconds() < config.NO_TRADE_CLOSE_MINUTES * 60:
        return True
    return False


def check_order(idea: OrderIdea, book: Book, state: dict, *, stock_blackout: bool = False) -> Verdict:
    reasons: list[str] = []

    # Closing or reducing a position is always allowed - never trap the bot in a loser.
    if not idea.opens_position:
        return Verdict(True)

    eq = book.equity
    if state.get("paused"):
        reasons.append("trading is paused (kill switch)")
    if state.get("halted_for_day"):
        reasons.append(f"daily loss limit of {config.MAX_DAILY_LOSS_PCT}% already hit")
    if state.get("trades_today", 0) >= config.MAX_TRADES_PER_DAY:
        reasons.append(f"already made {config.MAX_TRADES_PER_DAY} trades today")
    if book.open_positions >= config.MAX_OPEN_POSITIONS:
        reasons.append(f"already holding {config.MAX_OPEN_POSITIONS} positions")
    if idea.venue == "alpaca" and stock_blackout:
        reasons.append("too close to the market open/close")
    if eq <= 0:
        reasons.append("bot equity is gone")

    if idea.notional <= 0 or idea.entry <= 0:
        reasons.append("order size and price must be positive")

    max_pos = eq * (config.MAX_FOREX_POSITION_X if idea.venue == "oanda" else config.MAX_POSITION_PCT / 100)
    if idea.notional > max_pos + 0.01:
        reasons.append(f"position ${idea.notional:,.0f} is over the ${max_pos:,.0f} single-position limit")

    if config.REQUIRE_STOP_LOSS and not idea.stop:
        reasons.append("no stop-loss")
    if idea.stop and idea.entry > 0:
        wrong_side = (idea.side == "buy" and idea.stop >= idea.entry) or (idea.side == "sell" and idea.stop <= idea.entry)
        if wrong_side:
            reasons.append("stop-loss is on the wrong side of the entry price")
        loss_if_stopped = abs(idea.entry - idea.stop) / idea.entry * idea.notional
        max_risk = eq * config.MAX_RISK_PER_TRADE_PCT / 100
        if loss_if_stopped > max_risk + 0.01:
            reasons.append(f"risks ${loss_if_stopped:,.0f} if stopped out, over the ${max_risk:,.0f} limit")
    if idea.target and idea.entry > 0:
        wrong_side = (idea.side == "buy" and idea.target <= idea.entry) or (idea.side == "sell" and idea.target >= idea.entry)
        if wrong_side:
            reasons.append("target is on the wrong side of the entry price")

    if idea.venue == "alpaca":
        after = book.stock_gross_exposure + book.pending_exposure + idea.notional
        cap = eq * config.MAX_GROSS_LEVERAGE * (1 - config.MIN_CASH_RESERVE_PCT / 100)
        if after > cap + 0.01:
            reasons.append(f"total exposure would be ${after:,.0f}, over the ${cap:,.0f} limit")
    else:
        after = book.forex_gross_notional + idea.notional
        cap = eq * config.MAX_FOREX_NOTIONAL_X
        if after > cap + 0.01:
            reasons.append(f"forex exposure would be ${after:,.0f}, over the ${cap:,.0f} limit")

    return Verdict(not reasons, reasons)
