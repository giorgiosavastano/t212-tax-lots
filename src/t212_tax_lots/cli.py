"""Command-line interface for analysing Trading 212 transaction exports."""

from pathlib import Path

import polars as pl
import typer
from rich.console import Console
from rich.table import Table

from t212_tax_lots.parser import (
    find_csv_files,
    get_buy_transactions,
    get_sell_transactions,
    get_trade_transactions,
    read_transactions,
)
from t212_tax_lots.portfolio import (
    eligible_to_sell_frame,
    open_lots_frame,
    positions_frame,
)

app = typer.Typer(
    help="Analyse Trading 212 CSV exports and track share lots older than 6 months."
)

console = Console()


@app.callback()
def main() -> None:
    """Analyse Trading 212 transaction exports."""
    pass


def _format_float(value: float | None) -> str:
    """Format share quantities in a compact but readable way."""
    if value is None:
        return ""

    return f"{value:.8f}".rstrip("0").rstrip(".")


def _filter_ticker(df: pl.DataFrame, ticker: str | None) -> pl.DataFrame:
    """Optionally filter a DataFrame to a single ticker."""
    if ticker is None:
        return df

    return df.filter(pl.col("ticker") == ticker)


@app.command()
def inspect(input_path: Path) -> None:
    """Inspect one Trading 212 CSV export or a folder of CSV exports."""
    csv_files = find_csv_files(input_path)
    df = read_transactions(input_path)

    trades = get_trade_transactions(df)
    buys = get_buy_transactions(df)
    sells = get_sell_transactions(df)

    overview_table = Table(title="Trading 212 CSV Overview")
    overview_table.add_column("Property")
    overview_table.add_column("Value")

    overview_table.add_row("CSV files", str(len(csv_files)))
    overview_table.add_row("Rows", str(df.height))
    overview_table.add_row("Columns", str(df.width))
    overview_table.add_row("Trades", str(trades.height))
    overview_table.add_row("Buy transactions", str(buys.height))
    overview_table.add_row("Sell transactions", str(sells.height))
    overview_table.add_row("Column names", ", ".join(df.columns))

    console.print(overview_table)

    files_table = Table(title="Processed Files")
    files_table.add_column("File")
    files_table.add_column("Rows", justify="right")

    file_counts = df.group_by("source_file").len().sort("source_file")

    for row in file_counts.iter_rows(named=True):
        files_table.add_row(str(row["source_file"]), str(row["len"]))

    console.print(files_table)

    action_counts = df.group_by("action").len().sort("len", descending=True)

    action_table = Table(title="Transaction Types")
    action_table.add_column("Action")
    action_table.add_column("Rows", justify="right")

    for row in action_counts.iter_rows(named=True):
        action_table.add_row(str(row["action"]), str(row["len"]))

    console.print(action_table)


@app.command()
def positions(
    input_path: Path,
    ticker: str | None = typer.Option(
        None,
        "--ticker",
        "-t",
        help="Only show positions for this ticker.",
    ),
) -> None:
    """Show current open positions after applying buys and sells."""
    df = read_transactions(input_path)
    positions = _filter_ticker(positions_frame(df), ticker)

    table = Table(title="Current Positions")
    table.add_column("Ticker")
    table.add_column("Name")
    table.add_column("Shares", justify="right")
    table.add_column("Open lots", justify="right")
    table.add_column("Oldest buy")
    table.add_column("Newest buy")

    for row in positions.iter_rows(named=True):
        table.add_row(
            str(row["ticker"]),
            str(row["name"]),
            _format_float(row["shares"]),
            str(row["open_lots"]),
            str(row["oldest_buy_date"]),
            str(row["newest_buy_date"]),
        )

    console.print(table)


@app.command("eligible-to-sell")
def eligible_to_sell(
    input_path: Path,
    as_of: str | None = typer.Option(
        None,
        "--as-of",
        help="Date used for the 6-month check, formatted as YYYY-MM-DD. Defaults to today.",
    ),
    ticker: str | None = typer.Option(
        None,
        "--ticker",
        "-t",
        help="Only show eligibility for this ticker.",
    ),
) -> None:
    """Show how many shares are older than 6 calendar months."""
    df = read_transactions(input_path)
    eligibility = _filter_ticker(
        eligible_to_sell_frame(df, as_of=as_of),
        ticker,
    )

    table = Table(title="Shares Older Than 6 Months")
    table.add_column("Ticker")
    table.add_column("Name")
    table.add_column("Eligible shares", justify="right")
    table.add_column("Not yet eligible", justify="right")
    table.add_column("Total shares", justify="right")
    table.add_column("Cutoff date")

    for row in eligibility.iter_rows(named=True):
        table.add_row(
            str(row["ticker"]),
            str(row["name"]),
            _format_float(row["eligible_shares"]),
            _format_float(row["not_yet_eligible_shares"]),
            _format_float(row["total_shares"]),
            str(row["six_month_cutoff"]),
        )

    console.print(table)


@app.command("open-lots")
def open_lots(
    input_path: Path,
    ticker: str | None = typer.Option(
        None,
        "--ticker",
        "-t",
        help="Only show open lots for this ticker.",
    ),
) -> None:
    """Show the underlying open buy lots used by the calculations."""
    df = read_transactions(input_path)
    lots = _filter_ticker(open_lots_frame(df), ticker)

    table = Table(title="Open Lots")
    table.add_column("Ticker")
    table.add_column("Name")
    table.add_column("Buy date")
    table.add_column("Remaining shares", justify="right")
    table.add_column("Price/share", justify="right")
    table.add_column("Currency")
    table.add_column("Source file")

    for row in lots.iter_rows(named=True):
        table.add_row(
            str(row["ticker"]),
            str(row["name"]),
            str(row["buy_date"]),
            _format_float(row["remaining_shares"]),
            _format_float(row["price_per_share"]),
            str(row["price_currency"]),
            str(row["source_file"]),
        )

    console.print(table)
