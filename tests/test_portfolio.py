from datetime import datetime

import polars as pl
import pytest

from t212_tax_lots.portfolio import (
    MissingAcquisitionHistoryError,
    build_open_lots,
    eligible_to_sell_frame,
    open_lots_frame,
    positions_frame,
)


def _transactions(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(rows)


def test_build_open_lots_matches_sells_fifo_by_asset() -> None:
    transactions = _transactions(
        [
            {
                "action": "Market buy",
                "time": datetime(2025, 1, 1, 9),
                "shares": 10.0,
                "ticker": "AAA",
                "isin": "ISIN-AAA",
                "name": "Alpha",
                "price_per_share": 10.0,
                "price_currency": "EUR",
                "source_file": "one.csv",
            },
            {
                "action": "Market buy",
                "time": datetime(2025, 2, 1, 9),
                "shares": 5.0,
                "ticker": "AAA",
                "isin": "ISIN-AAA",
                "name": "Alpha",
                "price_per_share": 12.0,
                "price_currency": "EUR",
                "source_file": "one.csv",
            },
            {
                "action": "Market buy",
                "time": datetime(2025, 1, 15, 9),
                "shares": 3.0,
                "ticker": "BBB",
                "isin": "ISIN-BBB",
                "name": "Beta",
                "price_per_share": 20.0,
                "price_currency": "EUR",
                "source_file": "one.csv",
            },
            {
                "action": "Market sell",
                "time": datetime(2025, 3, 1, 9),
                "shares": 12.0,
                "ticker": "AAA",
                "isin": "ISIN-AAA",
                "name": "Alpha",
            },
        ]
    )

    lots = build_open_lots(transactions)

    remaining_by_ticker = {lot.ticker: lot.remaining_shares for lot in lots}

    assert remaining_by_ticker == {"AAA": 3.0, "BBB": 3.0}
    assert next(lot for lot in lots if lot.ticker == "AAA").buy_time == datetime(
        2025, 2, 1, 9
    )


def test_build_open_lots_rejects_oversold_asset() -> None:
    transactions = _transactions(
        [
            {
                "action": "Market buy",
                "time": datetime(2025, 1, 1),
                "shares": 1.0,
                "ticker": "AAA",
                "isin": "ISIN-AAA",
            },
            {
                "action": "Market sell",
                "time": datetime(2025, 1, 2),
                "shares": 2.0,
                "ticker": "AAA",
                "isin": "ISIN-AAA",
            },
        ]
    )

    with pytest.raises(
        MissingAcquisitionHistoryError,
        match="missing from the supplied acquisition history.*Upload exports",
    ):
        build_open_lots(transactions)


def test_positions_frame_aggregates_open_lots() -> None:
    transactions = _transactions(
        [
            {
                "action": "Market buy",
                "time": datetime(2025, 1, 1),
                "shares": 2.0,
                "ticker": "AAA",
                "isin": "ISIN-AAA",
                "name": "Alpha",
            },
            {
                "action": "Limit buy",
                "time": datetime(2025, 2, 1),
                "shares": 3.0,
                "ticker": "AAA",
                "isin": "ISIN-AAA",
                "name": "Alpha",
            },
        ]
    )

    row = positions_frame(transactions).row(0, named=True)

    assert row["shares"] == 5.0
    assert row["open_lots"] == 2
    assert row["oldest_buy_date"].isoformat() == "2025-01-01"
    assert row["newest_buy_date"].isoformat() == "2025-02-01"


def test_eligible_to_sell_frame_splits_old_and_new_lots() -> None:
    transactions = _transactions(
        [
            {
                "action": "Market buy",
                "time": datetime(2025, 1, 1),
                "shares": 2.0,
                "ticker": "AAA",
                "isin": "ISIN-AAA",
                "name": "Alpha",
            },
            {
                "action": "Market buy",
                "time": datetime(2025, 5, 1),
                "shares": 3.0,
                "ticker": "AAA",
                "isin": "ISIN-AAA",
                "name": "Alpha",
            },
        ]
    )

    row = eligible_to_sell_frame(transactions, as_of="2025-08-01").row(
        0, named=True
    )

    assert row["six_month_cutoff"].isoformat() == "2025-02-01"
    assert row["eligible_shares"] == 2.0
    assert row["not_yet_eligible_shares"] == 3.0
    assert row["total_shares"] == 5.0


def test_empty_frames_keep_function_specific_schemas() -> None:
    transactions = pl.DataFrame(
        schema={
            "action": pl.String,
            "time": pl.Datetime,
            "shares": pl.Float64,
            "ticker": pl.String,
            "isin": pl.String,
        }
    )

    assert open_lots_frame(transactions).columns == [
        "asset_key",
        "ticker",
        "name",
        "isin",
        "buy_time",
        "buy_date",
        "remaining_shares",
        "price_per_share",
        "price_currency",
        "source_file",
    ]
    assert positions_frame(transactions).columns == [
        "asset_key",
        "ticker",
        "name",
        "isin",
        "shares",
        "oldest_buy_date",
        "newest_buy_date",
        "open_lots",
    ]
    assert eligible_to_sell_frame(transactions, as_of="2025-08-01").columns == [
        "asset_key",
        "ticker",
        "name",
        "isin",
        "as_of_date",
        "six_month_cutoff",
        "eligible_shares",
        "total_shares",
        "oldest_buy_date",
        "newest_buy_date",
        "not_yet_eligible_shares",
    ]
