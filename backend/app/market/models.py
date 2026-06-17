"""Data models for market data."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable snapshot of a single ticker's price at a point in time."""

    ticker: str
    price: float
    previous_price: float
    # Session-open / reference price, used for the *daily* change shown in the
    # watchlist. Defaults to 0.0 ("unset") so the daily-change fields read as
    # zero until the cache supplies a real baseline. See cache.PriceCache.
    reference_price: float = 0.0
    timestamp: float = field(default_factory=time.time)  # Unix seconds

    @property
    def change(self) -> float:
        """Absolute price change from the previous update (tick-to-tick)."""
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        """Percentage change from the previous update (tick-to-tick)."""
        if self.previous_price == 0:
            return 0.0
        return round((self.price - self.previous_price) / self.previous_price * 100, 4)

    @property
    def daily_change(self) -> float:
        """Absolute price change from the session-open reference price."""
        if self.reference_price <= 0:
            return 0.0
        return round(self.price - self.reference_price, 4)

    @property
    def daily_change_percent(self) -> float:
        """Percentage change from the session-open reference price.

        This is the "daily change %" surfaced in the watchlist, distinct from
        the sub-cent tick-to-tick ``change_percent``.
        """
        if self.reference_price <= 0:
            return 0.0
        return round((self.price - self.reference_price) / self.reference_price * 100, 4)

    @property
    def direction(self) -> str:
        """'up', 'down', or 'flat'."""
        if self.price > self.previous_price:
            return "up"
        elif self.price < self.previous_price:
            return "down"
        return "flat"

    def to_dict(self) -> dict:
        """Serialize for JSON / SSE transmission."""
        return {
            "ticker": self.ticker,
            "price": self.price,
            "previous_price": self.previous_price,
            "reference_price": self.reference_price,
            "timestamp": self.timestamp,
            "change": self.change,
            "change_percent": self.change_percent,
            "daily_change": self.daily_change,
            "daily_change_percent": self.daily_change_percent,
            "direction": self.direction,
        }
