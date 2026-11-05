"""Daily SMA crossover signals; pure functions, no broker access."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Sequence


@dataclass(frozen=True)
class DailyClose:
    day: date
    close: float


@dataclass(frozen=True)
class Signal:
    day: date
    target: int  # +1 long, -1 short, 0 no new signal


def sma(values: Sequence[float], window: int) -> float:
    if window < 1 or len(values) < window:
        raise ValueError("Insufficient prices or invalid window")
    return sum(values[-window:]) / window


def crossover(closes: Sequence[DailyClose], fast: int = 20, slow: int = 50) -> Signal:
    """Signal on completed daily bars only; 0 means no new crossing."""
    if not (0 < fast < slow):
        raise ValueError("Expected 0 < fast < slow")
    if len(closes) < slow + 1:
        raise ValueError("Need at least slow+1 completed days")
    days = [bar.day for bar in closes]
    if days != sorted(set(days)):
        raise ValueError("Daily bars must be unique and chronological")
    prices = [bar.close for bar in closes]
    if any(not (0 < p < float("inf")) for p in prices):
        raise ValueError("Invalid close price")
    previous_fast = sma(prices[:-1], fast)
    previous_slow = sma(prices[:-1], slow)
    current_fast = sma(prices, fast)
    current_slow = sma(prices, slow)
    if previous_fast <= previous_slow and current_fast > current_slow:
        target = 1
    elif previous_fast >= previous_slow and current_fast < current_slow:
        target = -1
    else:
        target = 0
    return Signal(day=closes[-1].day, target=target)
