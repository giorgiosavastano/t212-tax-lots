from pathlib import Path

import polars as pl
import pytest

from t212_tax_lots.parser import (
    ExportValidationError,
    find_csv_files,
    get_buy_transactions,
    get_sell_transactions,
    get_trade_transactions,
    import_summary,
    read_single_csv,
    read_transactions,
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
        "isin",
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


def test_read_transactions_deduplicates_overlapping_exports_by_id(
    tmp_path: Path,
) -> None:
    header = "Action,Time,ID,ISIN,Ticker,No. of shares,Price / share,Total\n"
    duplicate = "Market buy,2025-01-02 03:04:05,TX-1,ISIN-A,AAPL,1,10,10\n"
    (tmp_path / "first.csv").write_text(header + duplicate)
    (tmp_path / "second.csv").write_text(
        header
        + duplicate
        + "Market sell,2025-02-02 03:04:05,TX-2,ISIN-A,AAPL,1,12,12\n"
    )

    df = read_transactions(tmp_path)

    assert df.height == 2
    assert import_summary(df).duplicate_rows_removed == 1
    duplicate_row = df.filter(pl.col("id") == "TX-1").row(0, named=True)
    assert duplicate_row["duplicate_count"] == 2
    assert duplicate_row["duplicate_source_files"] == "first.csv, second.csv"


def test_read_transactions_deduplicates_matching_rows_without_ids_across_files(
    tmp_path: Path,
) -> None:
    content = "\n".join(
        [
            "Action,Time,ISIN,Ticker,No. of shares,Price / share,Total",
            "Market buy,2025-01-02 03:04:05,ISIN-A,AAPL,1,10,10",
        ]
    )
    (tmp_path / "first.csv").write_text(content)
    (tmp_path / "second.csv").write_text(content)

    df = read_transactions(tmp_path)

    assert df.height == 1
    assert import_summary(df).duplicate_rows_removed == 1


def test_read_single_csv_rejects_missing_required_columns(tmp_path: Path) -> None:
    path = tmp_path / "export.csv"
    path.write_text("Action,Ticker\nMarket buy,AAPL\n")

    with pytest.raises(ExportValidationError, match="missing required.*Time"):
        read_single_csv(path)


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (
            "Market buy,not-a-time,ISIN-A,AAPL,1",
            "Time must use YYYY-MM-DD HH:MM:SS",
        ),
        (
            "Market buy,2025-01-02 03:04:05,ISIN-A,AAPL,not-a-number",
            "'shares' must be numeric",
        ),
        (
            "Market buy,2025-01-02 03:04:05,,,1",
            "require an ISIN or Ticker",
        ),
    ],
)
def test_read_single_csv_rejects_invalid_trade_values(
    tmp_path: Path, row: str, message: str
) -> None:
    path = tmp_path / "export.csv"
    path.write_text("Action,Time,ISIN,Ticker,No. of shares\n" + row + "\n")

    with pytest.raises(ExportValidationError, match=message):
        read_single_csv(path)


def test_read_single_csv_rejects_malformed_optional_numeric_value(
    tmp_path: Path,
) -> None:
    path = tmp_path / "export.csv"
    path.write_text(
        "\n".join(
            [
                "Action,Time,ISIN,Ticker,No. of shares,Price / share",
                "Market buy,2025-01-02 03:04:05,ISIN-A,AAPL,1,not-a-price",
            ]
        )
    )

    with pytest.raises(ExportValidationError, match="'price_per_share' must be numeric"):
        read_single_csv(path)


def test_read_transactions_rejects_inconsistent_duplicate_id(tmp_path: Path) -> None:
    header = "Action,Time,ID,ISIN,Ticker,No. of shares\n"
    (tmp_path / "first.csv").write_text(
        header + "Market buy,2025-01-02 03:04:05,TX-1,ISIN-A,AAPL,1\n"
    )
    (tmp_path / "second.csv").write_text(
        header + "Market buy,2025-01-02 03:04:05,TX-1,ISIN-A,AAPL,2\n"
    )

    with pytest.raises(ExportValidationError, match="inconsistent data"):
        read_transactions(tmp_path)


def test_import_summary_reports_unsupported_actions(tmp_path: Path) -> None:
    path = tmp_path / "export.csv"
    path.write_text(
        "\n".join(
            [
                "Action,Time,ISIN,Ticker,No. of shares",
                "Market buy,2025-01-02 03:04:05,ISIN-A,AAPL,1",
                "Dividend,2025-02-02 03:04:05,ISIN-A,AAPL,",
                "Interest on cash,2025-03-02 03:04:05,,,",
            ]
        )
    )

    summary = import_summary(read_transactions(path))

    assert summary.unsupported_action_counts == {"Dividend": 1, "Interest on cash": 1}


def test_deduplication_preserves_schema_when_later_optional_value_is_numeric(
    tmp_path: Path,
) -> None:
    rows = [
        f"Dividend,2025-01-01 00:{minute // 60:02}:{minute % 60:02},TX-{minute},"
        for minute in range(100)
    ]
    rows.append("Dividend,2025-01-01 01:40:00,TX-100,0.0")
    path = tmp_path / "export.csv"
    path.write_text("\n".join(["Action,Time,ID,Result", *rows]))

    df = read_transactions(path)

    assert df.schema["result"] == pl.Float64
    assert df["result"][-1] == 0.0
