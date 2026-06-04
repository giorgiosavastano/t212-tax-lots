"""Portfolio and tax-lot calculations for Trading 212 exports.

The functions in this module intentionally use plain Python for lot matching.
That keeps the logic readable and auditable, which is important for tax-related
calculations.

Polars is still used for input/output tables and aggregation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import polars as pl
from dateutil.relativedelta import relativedelta

from t212_tax_lots.parser import BUY_ACTIONS, SELL_ACTIONS, TRADE_ACTIONS

FLOAT_TOLERANCE = 1e-9

OPEN_LOTS_SCHEMA = {
    "asset_key": pl.String,
    "ticker": pl.String,
    "name": pl.String,
    "isin": pl.String,
    "buy_time": pl.Datetime,
    "buy_date": pl.Date,
    "remaining_shares": pl.Float64,
    "price_per_share": pl.Float64,
    "price_currency": pl.String,
    "source_file": pl.String,
}

POSITIONS_SCHEMA = {
    "asset_key": pl.String,
    "ticker": pl.String,
    "name": pl.String,
    "isin": pl.String,
    "shares": pl.Float64,
    "oldest_buy_date": pl.Date,
    "newest_buy_date": pl.Date,
    "open_lots": pl.UInt32,
}

ELIGIBILITY_SCHEMA = {
    "asset_key": pl.String,
    "ticker": pl.String,
    "name": pl.String,
    "isin": pl.String,
    "as_of_date": pl.Date,
    "six_month_cutoff": pl.Date,
    "eligible_shares": pl.Float64,
    "total_shares": pl.Float64,
    "oldest_buy_date": pl.Date,
    "newest_buy_date": pl.Date,
    "not_yet_eligible_shares": pl.Float64,
}


@dataclass
class Lot:
    """An open purchase lot.

    A lot represents shares bought in a single buy transaction that have not yet
    been fully consumed by later sell transactions.
    """

    asset_key: str
    ticker: str | None
    name: str | None
    isin: str | None
    buy_time: datetime
    remaining_shares: float
    price_per_share: float | None
    price_currency: str | None
    source_file: str | None


def _as_date(value: date | datetime | str | None) -> date:
    """Normalize an optional date-like value into a date.

    This lets CLI commands accept a simple YYYY-MM-DD string while the internal
    logic works with a proper date object.
    """
    if value is None:
        return date.today()

    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    return datetime.strptime(value, "%Y-%m-%d").date()


def _asset_key(row: dict) -> str:
    """Return a stable identifier for matching buys and sells.

    ISIN is preferred because it is usually more stable than ticker. If ISIN is
    missing, we fall back to ticker.
    """
    isin = row.get("isin")
    ticker = row.get("ticker")

    if isin:
        return str(isin)

    if ticker:
        return str(ticker)

    raise ValueError(f"Transaction has neither ISIN nor ticker: {row}")


def build_open_lots(transactions: pl.DataFrame) -> list[Lot]:
    """Build the currently open lots after applying buys and sells.

    Sells are matched against previous buy lots using FIFO:

    - oldest open lot first
    - only lots for the same asset are consumed
    - fully consumed lots are removed from the open position
    """
    required_columns = {"action", "time", "shares", "ticker", "isin"}

    missing_columns = required_columns - set(transactions.columns)
    if missing_columns:
        raise ValueError(f"Missing required columns: {sorted(missing_columns)}")

    trades = transactions.filter(pl.col("action").is_in(list(TRADE_ACTIONS))).sort(
        "time"
    )

    open_lots: list[Lot] = []

    for row in trades.iter_rows(named=True):
        action = row["action"]
        shares = row["shares"]

        if shares is None:
            continue

        asset_key = _asset_key(row)

        if action in BUY_ACTIONS:
            open_lots.append(
                Lot(
                    asset_key=asset_key,
                    ticker=row.get("ticker"),
                    name=row.get("name"),
                    isin=row.get("isin"),
                    buy_time=row["time"],
                    remaining_shares=float(shares),
                    price_per_share=row.get("price_per_share"),
                    price_currency=row.get("price_currency"),
                    source_file=row.get("source_file"),
                )
            )

        elif action in SELL_ACTIONS:
            shares_to_sell = float(shares)

            for lot in open_lots:
                if lot.asset_key != asset_key:
                    continue

                if shares_to_sell <= FLOAT_TOLERANCE:
                    break

                consumed_shares = min(lot.remaining_shares, shares_to_sell)

                lot.remaining_shares -= consumed_shares
                shares_to_sell -= consumed_shares

            if shares_to_sell > FLOAT_TOLERANCE:
                raise ValueError(
                    f"Sell transaction for {asset_key} exceeds available shares "
                    f"by {shares_to_sell:.8f}"
                )

    return [lot for lot in open_lots if lot.remaining_shares > FLOAT_TOLERANCE]


def open_lots_frame(transactions: pl.DataFrame) -> pl.DataFrame:
    """Return open lots as a Polars DataFrame."""
    lots = build_open_lots(transactions)

    rows = [
        {
            "asset_key": lot.asset_key,
            "ticker": lot.ticker,
            "name": lot.name,
            "isin": lot.isin,
            "buy_time": lot.buy_time,
            "buy_date": lot.buy_time.date(),
            "remaining_shares": lot.remaining_shares,
            "price_per_share": lot.price_per_share,
            "price_currency": lot.price_currency,
            "source_file": lot.source_file,
        }
        for lot in lots
    ]

    if not rows:
        return pl.DataFrame(schema=OPEN_LOTS_SCHEMA)

    return pl.DataFrame(rows)


def positions_frame(transactions: pl.DataFrame) -> pl.DataFrame:
    """Return current positions aggregated by asset."""
    lots = open_lots_frame(transactions)

    if lots.is_empty():
        return pl.DataFrame(schema=POSITIONS_SCHEMA)

    return (
        lots.group_by(["asset_key", "ticker", "name", "isin"])
        .agg(
            pl.col("remaining_shares").sum().alias("shares"),
            pl.col("buy_date").min().alias("oldest_buy_date"),
            pl.col("buy_date").max().alias("newest_buy_date"),
            pl.len().alias("open_lots"),
        )
        .sort(["ticker", "name"])
    )


def eligible_to_sell_frame(
    transactions: pl.DataFrame,
    *,
    as_of: date | datetime | str | None = None,
) -> pl.DataFrame:
    """Return shares older than 6 calendar months as of a given date."""
    as_of_date = _as_date(as_of)
    cutoff_date = as_of_date - relativedelta(months=6)

    lots = open_lots_frame(transactions)

    if lots.is_empty():
        return pl.DataFrame(schema=ELIGIBILITY_SCHEMA)

    return (
        lots.with_columns(
            pl.lit(as_of_date).alias("as_of_date"),
            pl.lit(cutoff_date).alias("six_month_cutoff"),
            (pl.col("buy_date") <= pl.lit(cutoff_date)).alias("older_than_6_months"),
        )
        .group_by(
            ["asset_key", "ticker", "name", "isin", "as_of_date", "six_month_cutoff"]
        )
        .agg(
            pl.col("remaining_shares")
            .filter(pl.col("older_than_6_months"))
            .sum()
            .alias("eligible_shares"),
            pl.col("remaining_shares").sum().alias("total_shares"),
            pl.col("buy_date").min().alias("oldest_buy_date"),
            pl.col("buy_date").max().alias("newest_buy_date"),
        )
        .with_columns(
            (pl.col("total_shares") - pl.col("eligible_shares")).alias(
                "not_yet_eligible_shares"
            )
        )
        .sort(["ticker", "name"])
    )
