"""Artificial technical fixtures, NOT GLDRUBF market data."""
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from src.gldrubf_bot.models import Bar, Quote, UTC
from src.gldrubf_bot.engine import Costs


def bars(prices):
    result = []
    for i, p in enumerate(prices):
        t = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=i)
        result.append(Bar(t.date(), t, t+timedelta(hours=18), D(p), D(p), D(p), D(p), 10))
    return result


def costs(**kw):
    values = dict(point_value_rub=D(2), tick=D(1), commission_rub=D('.5'), slippage_ticks=1,
                  initial_cash_rub=D(1000), funding_daily_rub=D(2))
    values.update(kw)
    return Costs(**values)


UP = [100]*30+[90]*20+[400]
DOWN = [100]*30+[101]*20+[1]
