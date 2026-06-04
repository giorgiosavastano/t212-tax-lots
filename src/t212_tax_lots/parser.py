"""Parsing utilities for Trading 212 CSV transaction exports."""

from pathlib import Path

import polars as pl


def read_transactions(csv_path: Path) -> pl.DataFrame:
    """Read a Trading 212 CSV export into a Polars DataFrame.

    The goal of this function is to provide one clean entry point for loading
    Trading 212 exports. Later we can add column normalization, date parsing,
    transaction filtering, and validation here.
    """
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV file does not exist: {csv_path}")

    return pl.read_csv(
        csv_path,
        infer_schema_length=10_000,
        try_parse_dates=True,
    )