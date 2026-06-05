from pathlib import Path

import polars as pl
import pytest

from t212_tax_lots.parser import (
    find_csv_files,
    get_buy_transactions,
    get_sell_transactions,
    get_trade_transactions,
    read_single_csv,
)


def test_find_csv_files_returns_sorted_directory_matches(tmp_path: Path) -> None:
    (tmp_path / "b.csv").write_text("Action\nMarket buy\n")
    (tmp_path / "notes.txt").write_text("ignored\n")
    (tmp_path / "a.csv").write_text("Action\nMarket sell\n")

    assert find_csv_files(tmp_path) == [tmp_path / "a.csv", tmp_path / "b.csv"]


def test_find_csv_files_rejects_non_csv_file(tmp_path: Path) -> None:
    path = tmp_path / "transactions.txt"
    path.write_text("not,csv\n")

    with pytest.raises(ValueError, match="Expected a CSV file"):
        find_csv_files(path)


def test_read_single_csv_normalizes_names_types_and_source(tmp_path: Path) -> None:
    path = tmp_path / "export.csv"
    path.write_text(
        "\n".join(
            [
                "Action,Time,Ticker,No. of shares,Price / share",
                "Market buy,2025-01-02 03:04:05,AAPL,1.5,123.45",
            ]
        )
    )

    df = read_single_csv(path)

    assert df.columns == [
        "action",
        "time",
        "ticker",
        "shares",
        "price_per_share",
        "source_file",
    ]
    assert df.schema["time"] == pl.Datetime
    assert df.schema["shares"] == pl.Float64
    assert df.row(0, named=True)["source_file"] == "export.csv"


def test_trade_filters_share_action_constants() -> None:
    df = pl.DataFrame(
        {
            "action": [
                "Market buy",
                "Limit sell",
                "Dividend",
                "Interest on cash",
            ]
        }
    )

    assert get_trade_transactions(df)["action"].to_list() == [
        "Market buy",
        "Limit sell",
    ]
    assert get_buy_transactions(df)["action"].to_list() == ["Market buy"]
    assert get_sell_transactions(df)["action"].to_list() == ["Limit sell"]
