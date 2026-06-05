"""Portfolio and tax-lot calculations for Trading 212 exports.

The functions in this module intentionally use plain Python for lot matching.
That keeps the logic readable and auditable, which is important for tax-related
calculations.

Polars is still used for input/output tables and aggregation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

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

DISPOSAL_MATCHES_SCHEMA = {
    "disposal_id": pl.String,
    "ticker": pl.String,
    "name": pl.String,
    "isin": pl.String,
    "sell_time": pl.Datetime,
    "sell_date": pl.Date,
    "sold_shares": pl.Float64,
    "sell_proceeds": pl.Float64,
    "currency": pl.String,
    "buy_date": pl.Date,
    "matched_shares": pl.Float64,
    "cost_basis": pl.Float64,
    "realized_gain_loss": pl.Float64,
    "holding_days": pl.Int64,
    "threshold_months": pl.Int64,
    "above_threshold": pl.Boolean,
    "warning": pl.String,
}

DISPOSAL_SUMMARY_SCHEMA = {
    "scope": pl.String,
    "ticker": pl.String,
    "currency": pl.String,
    "total_proceeds": pl.Float64,
    "total_cost_basis": pl.Float64,
    "total_realized_gain_loss": pl.Float64,
    "disposals": pl.UInt32,
    "shortest_holding_days": pl.Int64,
    "longest_holding_days": pl.Int64,
    "warning": pl.String,
}


class MissingAcquisitionHistoryError(ValueError):
    """Raised when supplied exports do not contain enough buys for a sell."""


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


@dataclass
class DisposalLot:
    """A purchase lot carrying the values needed for disposal reporting."""

    asset_key: str
    ticker: str | None
    name: str | None
    isin: str | None
    buy_time: datetime
    remaining_shares: float
    amount_per_share: float | None
    currency: str | None
    warning: str | None


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


def _trade_amount(
    row: dict[str, Any], *, is_buy: bool
) -> tuple[float | None, str | None, list[str]]:
    """Return a comparable trade amount, preferring the instrument currency.

    Trading 212 multi-currency accounts can settle a buy and sell in different
    cash currencies. Price-per-share values remain comparable when both trades
    use the same instrument currency, while their Total values do not.
    """
    warnings: list[str] = []
    shares = row.get("shares")
    price_per_share = row.get("price_per_share")

    if (
        shares is not None
        and price_per_share is not None
        and row.get("price_currency") is not None
    ):
        amount = float(shares) * float(price_per_share)
        currency = row.get("price_currency")
    elif row.get("total") is not None:
        amount = abs(float(row["total"]))
        currency = row.get("total_currency")
    else:
        return None, None, ["missing monetary value"]

    excluded_fees = False
    for fee_column, fee_currency_column in (
        ("stamp_duty_reserve_tax", "stamp_duty_reserve_tax_currency"),
        ("currency_conversion_fee", "currency_conversion_fee_currency"),
    ):
        fee = row.get(fee_column)
        if fee is None or abs(float(fee)) <= FLOAT_TOLERANCE:
            continue

        fee_currency = row.get(fee_currency_column)
        if currency is None or fee_currency != currency:
            excluded_fees = True
            continue

        amount += abs(float(fee)) if is_buy else -abs(float(fee))

    if excluded_fees:
        warnings.append("fees in another currency excluded")

    return amount, currency, warnings


def _possible_duplicate_trade_keys(
    rows: list[dict[str, Any]],
) -> set[tuple[Any, ...]]:
    """Identify no-ID trades whose core details occur more than once."""
    counts: dict[tuple[Any, ...], int] = {}
    for row in rows:
        if row.get("id") is not None:
            continue
        key = (
            row.get("action"),
            row.get("time"),
            row.get("isin"),
            row.get("ticker"),
            row.get("shares"),
            row.get("total"),
        )
        counts[key] = counts.get(key, 0) + 1
    return {key for key, count in counts.items() if count > 1}


def _warning_text(warnings: list[str]) -> str | None:
    """Return stable, de-duplicated warning text."""
    unique = list(dict.fromkeys(warnings))
    return "; ".join(unique) if unique else None


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
        "time", nulls_last=True, maintain_order=True
    )

    open_lots: list[Lot] = []

    for row in trades.iter_rows(named=True):
        action = row["action"]
        shares = row["shares"]

        if row["time"] is None:
            raise ValueError("Recognized trade has an invalid or empty Time value")

        if shares is None or float(shares) <= 0:
            raise ValueError("Recognized trade has an invalid share quantity")

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
                sell_time = row["time"].strftime("%Y-%m-%d %H:%M:%S")
                raise MissingAcquisitionHistoryError(
                    f"Sell for {asset_key} at {sell_time} cannot be matched: "
                    f"{shares_to_sell:.8f} shares are missing from the supplied "
                    "acquisition history. Upload exports covering purchases before "
                    f"{sell_time} and try again."
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


def disposal_matches_frame(
    transactions: pl.DataFrame,
    *,
    threshold_months: int = 6,
) -> pl.DataFrame:
    """Return one row per FIFO acquisition-lot match for every recognized sell."""
    if threshold_months < 0:
        raise ValueError("Holding-period threshold must be zero months or greater")

    required_columns = {"action", "time", "shares", "ticker", "isin"}
    missing_columns = required_columns - set(transactions.columns)
    if missing_columns:
        raise ValueError(f"Missing required columns: {sorted(missing_columns)}")

    trades = transactions.filter(pl.col("action").is_in(list(TRADE_ACTIONS))).sort(
        "time", nulls_last=True, maintain_order=True
    )
    trade_rows = list(trades.iter_rows(named=True))
    possible_duplicates = _possible_duplicate_trade_keys(trade_rows)
    open_lots: list[DisposalLot] = []
    result_rows: list[dict[str, Any]] = []
    sell_number = 0

    for row in trade_rows:
        if row["time"] is None:
            raise ValueError("Recognized trade has an invalid or empty Time value")
        if row["shares"] is None or float(row["shares"]) <= 0:
            raise ValueError("Recognized trade has an invalid share quantity")

        asset_key = _asset_key(row)
        shares = float(row["shares"])
        amount, currency, amount_warnings = _trade_amount(
            row, is_buy=row["action"] in BUY_ACTIONS
        )
        duplicate_key = (
            row.get("action"),
            row.get("time"),
            row.get("isin"),
            row.get("ticker"),
            row.get("shares"),
            row.get("total"),
        )
        if duplicate_key in possible_duplicates:
            amount_warnings.append("possible ambiguous duplicate transaction")
        if int(row.get("duplicate_count") or 1) > 1:
            amount_warnings.append("overlap duplicate transaction removed")

        if row["action"] in BUY_ACTIONS:
            open_lots.append(
                DisposalLot(
                    asset_key=asset_key,
                    ticker=row.get("ticker"),
                    name=row.get("name"),
                    isin=row.get("isin"),
                    buy_time=row["time"],
                    remaining_shares=shares,
                    amount_per_share=None if amount is None else amount / shares,
                    currency=currency,
                    warning=_warning_text(amount_warnings),
                )
            )
            continue

        sell_number += 1
        disposal_id = str(row.get("id") or f"sell-{sell_number:06d}")
        shares_to_sell = shares
        proceeds_per_share = None if amount is None else amount / shares

        for lot in open_lots:
            if lot.asset_key != asset_key or shares_to_sell <= FLOAT_TOLERANCE:
                continue

            matched_shares = min(lot.remaining_shares, shares_to_sell)
            lot.remaining_shares -= matched_shares
            shares_to_sell -= matched_shares
            holding_days = (row["time"].date() - lot.buy_time.date()).days
            warnings = [*amount_warnings]
            if lot.warning:
                warnings.append(f"acquisition: {lot.warning}")

            sell_proceeds = (
                None
                if proceeds_per_share is None
                else proceeds_per_share * matched_shares
            )
            cost_basis = (
                None
                if lot.amount_per_share is None
                else lot.amount_per_share * matched_shares
            )
            if lot.currency != currency:
                cost_basis = None
                realized_gain_loss = None
                warnings.append("buy and sell currencies differ")
            elif currency is None:
                cost_basis = None
                realized_gain_loss = None
                warnings.append("missing currency")
            elif sell_proceeds is not None and cost_basis is not None:
                realized_gain_loss = sell_proceeds - cost_basis
            else:
                realized_gain_loss = None
                warnings.append("missing cost basis or proceeds")

            result_rows.append(
                {
                    "disposal_id": disposal_id,
                    "ticker": row.get("ticker") or lot.ticker,
                    "name": row.get("name") or lot.name,
                    "isin": row.get("isin") or lot.isin,
                    "sell_time": row["time"],
                    "sell_date": row["time"].date(),
                    "sold_shares": shares,
                    "sell_proceeds": sell_proceeds,
                    "currency": currency,
                    "buy_date": lot.buy_time.date(),
                    "matched_shares": matched_shares,
                    "cost_basis": cost_basis,
                    "realized_gain_loss": realized_gain_loss,
                    "holding_days": holding_days,
                    "threshold_months": threshold_months,
                    "above_threshold": lot.buy_time.date()
                    <= row["time"].date() - relativedelta(months=threshold_months),
                    "warning": _warning_text(warnings),
                }
            )

        if shares_to_sell > FLOAT_TOLERANCE:
            warnings = [
                *amount_warnings,
                f"unmatched sell: {shares_to_sell:.8f} shares lack acquisition history",
                "missing cost basis",
            ]
            result_rows.append(
                {
                    "disposal_id": disposal_id,
                    "ticker": row.get("ticker"),
                    "name": row.get("name"),
                    "isin": row.get("isin"),
                    "sell_time": row["time"],
                    "sell_date": row["time"].date(),
                    "sold_shares": shares,
                    "sell_proceeds": (
                        None
                        if proceeds_per_share is None
                        else proceeds_per_share * shares_to_sell
                    ),
                    "currency": currency,
                    "buy_date": None,
                    "matched_shares": shares_to_sell,
                    "cost_basis": None,
                    "realized_gain_loss": None,
                    "holding_days": None,
                    "threshold_months": threshold_months,
                    "above_threshold": None,
                    "warning": _warning_text(warnings),
                }
            )

    if not result_rows:
        return pl.DataFrame(schema=DISPOSAL_MATCHES_SCHEMA)
    return pl.DataFrame(result_rows, schema=DISPOSAL_MATCHES_SCHEMA)


def _complete_sum(rows: list[dict[str, Any]], column: str) -> float | None:
    """Sum a monetary column only when every component is known."""
    values = [row[column] for row in rows]
    if any(value is None for value in values):
        return None
    return sum(float(value) for value in values)


def disposal_summary_frame(matches: pl.DataFrame) -> pl.DataFrame:
    """Aggregate disposal matches by ticker/currency and across all disposals."""
    if matches.is_empty():
        return pl.DataFrame(schema=DISPOSAL_SUMMARY_SCHEMA)

    rows = list(matches.iter_rows(named=True))
    groups: dict[tuple[str | None, str | None], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault((row["ticker"], row["currency"]), []).append(row)

    summary_rows: list[dict[str, Any]] = []
    for scope, grouped_rows in [
        *(
            ("ticker", group_rows)
            for _, group_rows in sorted(
                groups.items(), key=lambda item: (str(item[0][0]), str(item[0][1]))
            )
        ),
        ("overall", rows),
    ]:
        holding_days = [
            int(row["holding_days"])
            for row in grouped_rows
            if row["holding_days"] is not None
        ]
        currencies = {row["currency"] for row in grouped_rows}
        warnings: list[str] = []
        if len(currencies) > 1:
            warnings.append("mixed currencies: monetary totals unavailable")

        summary_rows.append(
            {
                "scope": scope,
                "ticker": grouped_rows[0]["ticker"] if scope == "ticker" else "ALL",
                "currency": (next(iter(currencies)) if len(currencies) == 1 else None),
                "total_proceeds": (
                    _complete_sum(grouped_rows, "sell_proceeds")
                    if len(currencies) == 1
                    else None
                ),
                "total_cost_basis": (
                    _complete_sum(grouped_rows, "cost_basis")
                    if len(currencies) == 1
                    else None
                ),
                "total_realized_gain_loss": (
                    _complete_sum(grouped_rows, "realized_gain_loss")
                    if len(currencies) == 1
                    else None
                ),
                "disposals": len({row["disposal_id"] for row in grouped_rows}),
                "shortest_holding_days": min(holding_days) if holding_days else None,
                "longest_holding_days": max(holding_days) if holding_days else None,
                "warning": _warning_text(warnings),
            }
        )

    return pl.DataFrame(summary_rows, schema=DISPOSAL_SUMMARY_SCHEMA)
