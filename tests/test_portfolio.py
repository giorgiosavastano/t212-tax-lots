from datetime import datetime

import polars as pl
import pytest

from t212_tax_lots.portfolio import (
    MissingAcquisitionHistoryError,
    build_open_lots,
    disposal_matches_frame,
    disposal_summary_frame,
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

    row = eligible_to_sell_frame(transactions, as_of="2025-08-01").row(0, named=True)

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
    assert disposal_matches_frame(transactions).columns == [
        "disposal_id",
        "ticker",
        "name",
        "isin",
        "sell_time",
        "sell_date",
        "sold_shares",
        "sell_proceeds",
        "currency",
        "buy_date",
        "matched_shares",
        "cost_basis",
        "realized_gain_loss",
        "holding_days",
        "threshold_months",
        "above_threshold",
        "warning",
    ]


def test_disposal_matches_simple_partial_sale_and_fee_adjusted_gain() -> None:
    transactions = _transactions(
        [
            {
                "action": "Market buy",
                "time": datetime(2025, 1, 1),
                "shares": 10.0,
                "ticker": "AAA",
                "isin": "ISIN-AAA",
                "name": "Alpha",
                "total": 100.0,
                "total_currency": "EUR",
                "currency_conversion_fee": 1.0,
                "currency_conversion_fee_currency": "EUR",
            },
            {
                "action": "Market sell",
                "time": datetime(2025, 8, 1),
                "shares": 4.0,
                "ticker": "AAA",
                "isin": "ISIN-AAA",
                "name": "Alpha",
                "total": 60.0,
                "total_currency": "EUR",
                "currency_conversion_fee": 0.4,
                "currency_conversion_fee_currency": "EUR",
            },
        ]
    )

    row = disposal_matches_frame(transactions).row(0, named=True)

    assert row["sold_shares"] == 4.0
    assert row["matched_shares"] == 4.0
    assert row["sell_proceeds"] == pytest.approx(59.6)
    assert row["cost_basis"] == pytest.approx(40.4)
    assert row["realized_gain_loss"] == pytest.approx(19.2)
    assert row["holding_days"] == 212
    assert row["above_threshold"] is True


def test_disposal_matches_one_sell_to_multiple_fifo_lots_with_gain_and_loss() -> None:
    transactions = _transactions(
        [
            {
                "action": "Market buy",
                "time": datetime(2025, 1, 1),
                "shares": 2.0,
                "ticker": "AAA",
                "isin": "ISIN-AAA",
                "total": 20.0,
                "total_currency": "EUR",
            },
            {
                "action": "Market buy",
                "time": datetime(2025, 7, 1),
                "shares": 3.0,
                "ticker": "AAA",
                "isin": "ISIN-AAA",
                "total": 60.0,
                "total_currency": "EUR",
            },
            {
                "action": "Market sell",
                "time": datetime(2025, 8, 1),
                "shares": 4.0,
                "ticker": "AAA",
                "isin": "ISIN-AAA",
                "total": 60.0,
                "total_currency": "EUR",
            },
        ]
    )

    matches = disposal_matches_frame(transactions)

    assert matches["buy_date"].to_list() == [
        datetime(2025, 1, 1).date(),
        datetime(2025, 7, 1).date(),
    ]
    assert matches["matched_shares"].to_list() == [2.0, 2.0]
    assert matches["realized_gain_loss"].to_list() == [10.0, -10.0]
    assert matches["above_threshold"].to_list() == [True, False]

    summary = disposal_summary_frame(matches)
    ticker_summary = summary.filter(pl.col("scope") == "ticker").row(0, named=True)
    overall = summary.filter(pl.col("scope") == "overall").row(0, named=True)
    assert ticker_summary["disposals"] == 1
    assert ticker_summary["total_proceeds"] == 60.0
    assert ticker_summary["total_cost_basis"] == 60.0
    assert ticker_summary["total_realized_gain_loss"] == 0.0
    assert overall["shortest_holding_days"] == 31
    assert overall["longest_holding_days"] == 212


def test_disposal_threshold_uses_calendar_month_boundary() -> None:
    transactions = _transactions(
        [
            {
                "action": "Market buy",
                "time": datetime(2025, 2, 28),
                "shares": 1.0,
                "ticker": "AAA",
                "isin": "ISIN-AAA",
                "total": 10.0,
                "total_currency": "EUR",
            },
            {
                "action": "Market sell",
                "time": datetime(2025, 8, 28),
                "shares": 1.0,
                "ticker": "AAA",
                "isin": "ISIN-AAA",
                "total": 12.0,
                "total_currency": "EUR",
            },
        ]
    )

    assert disposal_matches_frame(transactions).row(0, named=True)["above_threshold"]
    assert not disposal_matches_frame(transactions, threshold_months=7).row(
        0, named=True
    )["above_threshold"]


def test_disposal_reports_unmatched_sell_and_unknown_cost_basis() -> None:
    transactions = _transactions(
        [
            {
                "action": "Market sell",
                "time": datetime(2025, 8, 1),
                "shares": 2.0,
                "ticker": "AAA",
                "isin": "ISIN-AAA",
                "total": 30.0,
                "total_currency": "EUR",
            }
        ]
    )

    match = disposal_matches_frame(transactions).row(0, named=True)
    summary = disposal_summary_frame(disposal_matches_frame(transactions)).row(
        0, named=True
    )

    assert match["matched_shares"] == 2.0
    assert match["cost_basis"] is None
    assert match["realized_gain_loss"] is None
    assert "unmatched sell" in match["warning"]
    assert summary["total_proceeds"] == 30.0
    assert summary["total_cost_basis"] is None
    assert summary["total_realized_gain_loss"] is None


def test_disposal_uses_common_instrument_currency_when_cash_currencies_differ() -> None:
    transactions = _transactions(
        [
            {
                "action": "Market buy",
                "time": datetime(2025, 1, 1),
                "shares": 1.0,
                "ticker": "AAA",
                "isin": "ISIN-AAA",
                "price_per_share": 11.0,
                "price_currency": "USD",
                "total": 10.0,
                "total_currency": "EUR",
            },
            {
                "action": "Market sell",
                "time": datetime(2025, 8, 1),
                "shares": 1.0,
                "ticker": "AAA",
                "isin": "ISIN-AAA",
                "price_per_share": 12.0,
                "price_currency": "USD",
                "total": 12.0,
                "total_currency": "USD",
            },
        ]
    )

    row = disposal_matches_frame(transactions).row(0, named=True)

    assert row["sell_proceeds"] == 12.0
    assert row["cost_basis"] == 11.0
    assert row["realized_gain_loss"] == 1.0
    assert row["currency"] == "USD"
    assert row["warning"] is None


def test_disposal_keeps_basis_unknown_when_only_cash_currencies_differ() -> None:
    transactions = _transactions(
        [
            {
                "action": "Market buy",
                "time": datetime(2025, 1, 1),
                "shares": 1.0,
                "ticker": "AAA",
                "isin": "ISIN-AAA",
                "total": 10.0,
                "total_currency": "EUR",
            },
            {
                "action": "Market sell",
                "time": datetime(2025, 8, 1),
                "shares": 1.0,
                "ticker": "AAA",
                "isin": "ISIN-AAA",
                "total": 12.0,
                "total_currency": "USD",
            },
        ]
    )

    row = disposal_matches_frame(transactions).row(0, named=True)

    assert row["cost_basis"] is None
    assert row["realized_gain_loss"] is None
    assert "buy and sell currencies differ" in row["warning"]


def test_disposal_summary_reports_overall_totals_per_currency() -> None:
    transactions = _transactions(
        [
            {
                "action": "Market buy",
                "time": datetime(2025, 1, 1),
                "shares": 1.0,
                "ticker": "AAA",
                "isin": "ISIN-AAA",
                "price_per_share": 10.0,
                "price_currency": "USD",
            },
            {
                "action": "Market sell",
                "time": datetime(2025, 8, 1),
                "shares": 1.0,
                "ticker": "AAA",
                "isin": "ISIN-AAA",
                "price_per_share": 12.0,
                "price_currency": "USD",
            },
            {
                "action": "Market buy",
                "time": datetime(2025, 1, 2),
                "shares": 1.0,
                "ticker": "BBB",
                "isin": "ISIN-BBB",
                "price_per_share": 20.0,
                "price_currency": "EUR",
            },
            {
                "action": "Market sell",
                "time": datetime(2025, 8, 2),
                "shares": 1.0,
                "ticker": "BBB",
                "isin": "ISIN-BBB",
                "price_per_share": 18.0,
                "price_currency": "EUR",
            },
        ]
    )

    overall = disposal_summary_frame(disposal_matches_frame(transactions)).filter(
        pl.col("scope") == "overall"
    )
    overall_by_currency = {
        row["currency"]: row for row in overall.iter_rows(named=True)
    }

    assert set(overall_by_currency) == {"EUR", "USD"}
    assert overall_by_currency["USD"]["total_proceeds"] == 12.0
    assert overall_by_currency["USD"]["total_cost_basis"] == 10.0
    assert overall_by_currency["USD"]["total_realized_gain_loss"] == 2.0
    assert overall_by_currency["EUR"]["total_proceeds"] == 18.0
    assert overall_by_currency["EUR"]["total_cost_basis"] == 20.0
    assert overall_by_currency["EUR"]["total_realized_gain_loss"] == -2.0
