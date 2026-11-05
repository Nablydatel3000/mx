# GLDRUBF Trading Bot — development prototype

**Status:** offline SMA20/50 signal engine and persistent paper-position simulation only.
There is **no T-Investments API integration and no real order execution**.

## Run on Windows (Python 3.11+)

From the repository root:

```bat
python -m unittest discover -s tests -v
python -m src.gldrubf_bot.cli --csv daily.csv --db paper.sqlite3
```

Input `daily.csv` must have `day,close` columns, one completed trading day per row,
sorted ascending, e.g. `2026-01-01,100.0`. The program does **not** yet convert
the GLDRUBF minute CSV into official MOEX daily trading sessions.

## Important limitations

- PAPER records *target positions*, not real or simulated fills. Prices are only
  recorded as reference prices. P&L, fees, slippage, funding and risk controls
  are **not implemented**.
- SMA20/50 signals require 51 completed daily bars. Crossing up means target +1;
  crossing down means target -1; otherwise target 0 means maintain position.
- Replaying the same database does not duplicate events. Conflicting replay fails.
- This is a technical prototype, **not a validated profitable strategy**.
- No tokens or account credentials are needed or should be supplied.
