"""Validated UTC market events. A trading-day label is not a local midnight."""
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

UTC = timezone.utc


class DataError(ValueError):
    pass


def number(value, *, positive=False):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DataError("Invalid decimal") from exc
    if not result.is_finite() or (positive and result <= 0):
        raise DataError("Nonfinite or nonpositive value")
    return result


def instant(value):
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None or result.utcoffset() is None:
            raise DataError("Timestamp must include UTC offset")
        return result.astimezone(UTC)
    except (ValueError, TypeError, AttributeError) as exc:
        raise DataError("Invalid timezone-aware timestamp") from exc


def iso(value):
    return instant(value).isoformat(timespec='microseconds').replace('+00:00', 'Z')


@dataclass(frozen=True)
class Bar:
    day: date
    time: datetime
    closed_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    complete: bool = True

    def __post_init__(self):
        if type(self.day) is not date or type(self.complete) is not bool:
            raise DataError("Invalid trading day/completion flag")
        for name in ('time', 'closed_at'):
            object.__setattr__(self, name, instant(getattr(self, name)))
        for name in ('open', 'high', 'low', 'close'):
            object.__setattr__(self, name, number(getattr(self, name), positive=True))
        if not (self.low <= self.open <= self.high and self.low <= self.close <= self.high):
            raise DataError("Invalid OHLC")
        if type(self.volume) is not int or self.volume <= 0 or self.closed_at <= self.time:
            raise DataError("Invalid volume/candle interval")

    def payload(self):
        return dict(day=self.day.isoformat(), time=iso(self.time), closed_at=iso(self.closed_at),
                    open=str(self.open), high=str(self.high), low=str(self.low), close=str(self.close),
                    volume=self.volume, complete=self.complete)


@dataclass(frozen=True)
class Quote:
    time: datetime
    price: Decimal

    def __post_init__(self):
        object.__setattr__(self, 'time', instant(self.time))
        object.__setattr__(self, 'price', number(self.price, positive=True))
