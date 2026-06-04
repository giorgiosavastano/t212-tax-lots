"""Utilities for reading and normalizing Trading 212 CSV transaction exports."""

from pathlib import Path

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

    df = pl.read_csv(
        csv_path,
        infer_schema_length=10_000,
        try_parse_dates=False,
    )

    existing_mapping = {
        raw_name: clean_name
        for raw_name, clean_name in COLUMN_MAPPING.items()
        if raw_name in df.columns
    }

    df = df.rename(existing_mapping)

    if "time" in df.columns:
        df = df.with_columns(
            pl.col("time")
            .str.strptime(pl.Datetime, format="%Y-%m-%d %H:%M:%S", strict=False)
            .alias("time")
        )

    numeric_columns_present = [
        column for column in NUMERIC_COLUMNS if column in df.columns
    ]

    df = df.with_columns(
        [
            pl.col(column).cast(pl.Float64, strict=False).alias(column)
            for column in numeric_columns_present
        ]
    )

    # Add the source file name so that later we can trace where each transaction
    # came from. This is useful when combining annual Trading 212 exports.
    df = df.with_columns(pl.lit(csv_path.name).alias("source_file"))

    return df


def read_transactions(path: Path) -> pl.DataFrame:
    """Read one or more Trading 212 CSV exports.

    The provided path can be either:

    - a single CSV file
    - a directory containing multiple CSV files

    All matching files are normalized and concatenated into one DataFrame.
    """
    csv_files = find_csv_files(path)

    dataframes = [read_single_csv(csv_file) for csv_file in csv_files]

    return pl.concat(
        dataframes,
        how="diagonal_relaxed",
    )


def get_trade_transactions(df: pl.DataFrame) -> pl.DataFrame:
    """Return only buy and sell transactions."""
    return df.filter(
        pl.col("action").is_in(
            [
                "Market buy",
                "Limit buy",
                "Market sell",
                "Limit sell",
            ]
        )
    )


def get_buy_transactions(df: pl.DataFrame) -> pl.DataFrame:
    """Return only buy transactions."""
    return df.filter(
        pl.col("action").is_in(
            [
                "Market buy",
                "Limit buy",
            ]
        )
    )


def get_sell_transactions(df: pl.DataFrame) -> pl.DataFrame:
    """Return only sell transactions."""
    return df.filter(
        pl.col("action").is_in(
            [
                "Market sell",
                "Limit sell",
            ]
        )
    )
