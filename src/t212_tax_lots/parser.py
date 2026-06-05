"""Utilities for reading and normalizing Trading 212 CSV transaction exports."""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import polars as pl


COLUMN_MAPPING: dict[str, str] = {
    "Action": "action",
    "Time": "time",
    "ISIN": "isin",
    "Ticker": "ticker",
    "Name": "name",
    "Notes": "notes",
    "ID": "id",
    "No. of shares": "shares",
    "Price / share": "price_per_share",
    "Currency (Price / share)": "price_currency",
    "Exchange rate": "exchange_rate",
    "Result": "result",
    "Currency (Result)": "result_currency",
    "Total": "total",
    "Currency (Total)": "total_currency",
    "Withholding tax": "withholding_tax",
    "Currency (Withholding tax)": "withholding_tax_currency",
    "Stamp duty reserve tax": "stamp_duty_reserve_tax",
    "Currency (Stamp duty reserve tax)": "stamp_duty_reserve_tax_currency",
    "Currency conversion fee": "currency_conversion_fee",
    "Currency (Currency conversion fee)": "currency_conversion_fee_currency",
}


NUMERIC_COLUMNS: list[str] = [
    "shares",
    "price_per_share",
    "exchange_rate",
    "result",
    "total",
    "withholding_tax",
    "stamp_duty_reserve_tax",
    "currency_conversion_fee",
]

BUY_ACTIONS = frozenset({"Market buy", "Limit buy"})
SELL_ACTIONS = frozenset({"Market sell", "Limit sell"})
TRADE_ACTIONS = BUY_ACTIONS | SELL_ACTIONS

REQUIRED_EXPORT_COLUMNS = frozenset({"Action", "Time"})
IDENTITY_COLUMNS = (
    "action",
    "time",
    "isin",
    "ticker",
    "name",
    "shares",
    "price_per_share",
    "price_currency",
    "exchange_rate",
    "result",
    "result_currency",
    "total",
    "total_currency",
    "withholding_tax",
    "withholding_tax_currency",
    "stamp_duty_reserve_tax",
    "stamp_duty_reserve_tax_currency",
    "currency_conversion_fee",
    "currency_conversion_fee_currency",
)


class ExportValidationError(ValueError):
    """Raised when supplied CSV exports cannot be processed safely."""


@dataclass(frozen=True)
class ImportSummary:
    """A compact summary of import decisions relevant to the user."""

    duplicate_rows_removed: int
    unsupported_action_counts: dict[str, int]


def find_csv_files(path: Path) -> list[Path]:
    """Return CSV files from a single file path or from a directory.

    This allows CLI commands to work with either:

        data/private/export.csv

    or:

        data/private/

    When a directory is provided, only files ending in ".csv" are returned.
    The files are sorted so repeated runs process files in a stable order.
    """
    if not path.exists():
        raise FileNotFoundError(f"Path does not exist: {path}")

    if path.is_file():
        if path.suffix.lower() != ".csv":
            raise ValueError(f"Expected a CSV file, got: {path}")

        return [path]

    if path.is_dir():
        csv_files = sorted(path.glob("*.csv"))

        if not csv_files:
            raise FileNotFoundError(f"No CSV files found in directory: {path}")

        return csv_files

    raise ValueError(f"Path is neither a file nor a directory: {path}")


def read_single_csv(csv_path: Path) -> pl.DataFrame:
    """Read and normalize one Trading 212 CSV export."""
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV file does not exist: {csv_path}")

    try:
        df = pl.read_csv(
            csv_path,
            infer_schema_length=10_000,
            try_parse_dates=False,
        )
    except pl.exceptions.PolarsError as error:
        raise ExportValidationError(
            f"{csv_path.name}: could not read CSV ({error})"
        ) from error

    missing_columns = REQUIRED_EXPORT_COLUMNS - set(df.columns)
    if missing_columns:
        raise ExportValidationError(
            f"{csv_path.name}: missing required Trading 212 columns: "
            f"{', '.join(sorted(missing_columns))}"
        )

    if df.is_empty():
        raise ExportValidationError(f"{csv_path.name}: export contains no transactions")

    existing_mapping = {
        raw_name: clean_name
        for raw_name, clean_name in COLUMN_MAPPING.items()
        if raw_name in df.columns
    }

    df = df.rename(existing_mapping)

    for identifier_column in ("isin", "ticker"):
        if identifier_column not in df.columns:
            df = df.with_columns(pl.lit(None, dtype=pl.String).alias(identifier_column))

    df = df.with_row_index("_source_row", offset=2)
    df = df.with_columns(
        pl.col("time")
        .cast(pl.String)
        .str.strip_chars()
        .str.strptime(pl.Datetime, format="%Y-%m-%d %H:%M:%S", strict=False)
        .alias("time"),
        pl.col("action").cast(pl.String).str.strip_chars().alias("action"),
    )

    numeric_columns_present = [
        column for column in NUMERIC_COLUMNS if column in df.columns
    ]

    for column in numeric_columns_present:
        text_value = pl.col(column).cast(pl.String).str.strip_chars()
        invalid_rows = _invalid_rows(
            df,
            text_value.is_not_null()
            & (text_value != "")
            & text_value.cast(pl.Float64, strict=False).is_null(),
        )
        if invalid_rows:
            raise ExportValidationError(
                f"{csv_path.name}: {column!r} must be numeric at CSV row(s) "
                f"{_format_rows(invalid_rows)}"
            )

    df = df.with_columns(
        [
            pl.col(column).cast(pl.Float64, strict=False).alias(column)
            for column in numeric_columns_present
        ]
    )

    string_columns = [
        column
        for column, dtype in df.schema.items()
        if dtype == pl.String and column not in {"action"}
    ]
    if string_columns:
        df = df.with_columns(
            [
                pl.when(pl.col(column).str.strip_chars() == "")
                .then(None)
                .otherwise(pl.col(column).str.strip_chars())
                .alias(column)
                for column in string_columns
            ]
        )

    for column in numeric_columns_present:
        non_finite_rows = _invalid_rows(
            df, pl.col(column).is_not_null() & ~pl.col(column).is_finite()
        )
        if non_finite_rows:
            raise ExportValidationError(
                f"{csv_path.name}: {column!r} must be a finite number at CSV "
                f"row(s) {_format_rows(non_finite_rows)}"
            )

    # Add the source file name so that later we can trace where each transaction
    # came from. This is useful when combining annual Trading 212 exports.
    df = df.with_columns(pl.lit(csv_path.name).alias("source_file"))

    _validate_normalized_export(df, csv_path.name)

    return df.drop("_source_row")


def read_transactions(path: Path) -> pl.DataFrame:
    """Read one or more Trading 212 CSV exports.

    The provided path can be either:

    - a single CSV file
    - a directory containing multiple CSV files

    All matching files are normalized and concatenated into one DataFrame.
    """
    csv_files = find_csv_files(path)

    dataframes = [read_single_csv(csv_file) for csv_file in csv_files]

    transactions = pl.concat(
        dataframes,
        how="diagonal_relaxed",
    )
    _validate_transaction_id_consistency(transactions)
    return _deduplicate_transactions(transactions)


def _invalid_rows(df: pl.DataFrame, predicate: pl.Expr) -> list[int]:
    """Return source row numbers matching a validation predicate."""
    return df.filter(predicate)["_source_row"].to_list()


def _format_rows(rows: list[int]) -> str:
    """Format row numbers without flooding an error message."""
    displayed = ", ".join(str(row) for row in rows[:5])
    if len(rows) > 5:
        displayed += f", and {len(rows) - 5} more"
    return displayed


def _validate_normalized_export(df: pl.DataFrame, source_file: str) -> None:
    """Validate required values and formats in one normalized export."""
    empty_actions = _invalid_rows(
        df, pl.col("action").is_null() | (pl.col("action") == "")
    )
    if empty_actions:
        raise ExportValidationError(
            f"{source_file}: Action is empty at CSV row(s) {_format_rows(empty_actions)}"
        )

    invalid_times = _invalid_rows(df, pl.col("time").is_null())
    if invalid_times:
        raise ExportValidationError(
            f"{source_file}: Time must use YYYY-MM-DD HH:MM:SS at CSV row(s) "
            f"{_format_rows(invalid_times)}"
        )

    trades = df.filter(pl.col("action").is_in(TRADE_ACTIONS))
    if trades.is_empty():
        return

    required_trade_columns = {"shares"}
    missing_trade_columns = required_trade_columns - set(df.columns)
    if missing_trade_columns:
        raise ExportValidationError(
            f"{source_file}: recognized trades require columns: "
            f"{', '.join(sorted(missing_trade_columns))}"
        )

    invalid_shares = _invalid_rows(
        trades, pl.col("shares").is_null() | (pl.col("shares") <= 0)
    )
    if invalid_shares:
        raise ExportValidationError(
            f"{source_file}: recognized trades require a positive numeric "
            f"'No. of shares' value at CSV row(s) {_format_rows(invalid_shares)}"
        )

    missing_assets = _invalid_rows(
        trades, pl.col("isin").is_null() & pl.col("ticker").is_null()
    )
    if missing_assets:
        raise ExportValidationError(
            f"{source_file}: recognized trades require an ISIN or Ticker at CSV "
            f"row(s) {_format_rows(missing_assets)}"
        )


def _identity_value(row: dict[str, Any], column: str) -> Any:
    """Return a hashable, normalized value used for duplicate identity."""
    value = row.get(column)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _transaction_signature(row: dict[str, Any]) -> tuple[Any, ...]:
    """Build a conservative fallback identity for transactions without an ID."""
    return tuple(_identity_value(row, column) for column in IDENTITY_COLUMNS)


def _validate_transaction_id_consistency(transactions: pl.DataFrame) -> None:
    """Reject reused stable IDs that describe different transactions."""
    if "id" not in transactions.columns:
        return

    rows_by_id: dict[str, list[dict[str, Any]]] = {}
    for row in transactions.iter_rows(named=True):
        transaction_id = row.get("id")
        if transaction_id is not None:
            rows_by_id.setdefault(str(transaction_id), []).append(row)

    for transaction_id, rows in rows_by_id.items():
        signatures = {_transaction_signature(row) for row in rows}
        if len(signatures) > 1:
            source_files = sorted({str(row["source_file"]) for row in rows})
            raise ExportValidationError(
                f"Transaction ID {transaction_id!r} has inconsistent data across "
                f"exports: {', '.join(source_files)}"
            )


def _deduplicate_transactions(transactions: pl.DataFrame) -> pl.DataFrame:
    """Remove overlap duplicates while retaining compact provenance."""
    kept_indices: list[int] = []
    duplicate_counts: list[int] = []
    duplicate_source_files: list[set[str]] = []
    row_index_by_identity: dict[tuple[Any, ...], int] = {}

    for source_index, row in enumerate(transactions.iter_rows(named=True)):
        transaction_id = row.get("id")
        if transaction_id is not None:
            identity = ("id", str(transaction_id))
        else:
            # A fallback signature is only deduplicated across different files,
            # because two identical transactions in one export may be legitimate.
            identity = ("signature", *_transaction_signature(row))

        existing_index = row_index_by_identity.get(identity)
        if existing_index is not None:
            existing_source = transactions["source_file"][kept_indices[existing_index]]
            if transaction_id is not None or existing_source != row["source_file"]:
                duplicate_counts[existing_index] += 1
                duplicate_source_files[existing_index].add(str(row["source_file"]))
                continue

        # Keep the first no-ID identity as the cross-file overlap reference.
        # Identical rows within the same file remain separate transactions.
        row_index_by_identity.setdefault(identity, len(kept_indices))
        kept_indices.append(source_index)
        duplicate_counts.append(1)
        duplicate_source_files.append({str(row["source_file"])})

    return transactions[kept_indices].with_columns(
        pl.Series("duplicate_count", duplicate_counts, dtype=pl.UInt32),
        pl.Series(
            "duplicate_source_files",
            [", ".join(sorted(files)) for files in duplicate_source_files],
            dtype=pl.String,
        ),
    )


def import_summary(df: pl.DataFrame) -> ImportSummary:
    """Summarize duplicates removed and actions unsupported by calculations."""
    duplicate_rows_removed = 0
    if "duplicate_count" in df.columns:
        duplicate_rows_removed = int((df["duplicate_count"] - 1).sum())

    unsupported_actions = df.filter(~pl.col("action").is_in(TRADE_ACTIONS))["action"]
    return ImportSummary(
        duplicate_rows_removed=duplicate_rows_removed,
        unsupported_action_counts=dict(Counter(unsupported_actions.to_list())),
    )


def get_trade_transactions(df: pl.DataFrame) -> pl.DataFrame:
    """Return only buy and sell transactions."""
    return df.filter(pl.col("action").is_in(TRADE_ACTIONS))


def get_buy_transactions(df: pl.DataFrame) -> pl.DataFrame:
    """Return only buy transactions."""
    return df.filter(pl.col("action").is_in(BUY_ACTIONS))


def get_sell_transactions(df: pl.DataFrame) -> pl.DataFrame:
    """Return only sell transactions."""
    return df.filter(pl.col("action").is_in(SELL_ACTIONS))
