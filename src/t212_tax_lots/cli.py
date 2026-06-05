"""Command-line interface for analysing Trading 212 transaction exports."""

from pathlib import Path
from typing import Annotated

import polars as pl
import typer
from rich.console import Console
from rich.table import Table

from t212_tax_lots.parser import (
    find_csv_files,
    get_buy_transactions,
    get_sell_transactions,
    get_trade_transactions,
    import_summary,
    read_transactions,
)
from t212_tax_lots.portfolio import (
    eligible_to_sell_frame,
    open_lots_frame,
    positions_frame,
)

app = typer.Typer(
    help=(
        "Analyse Trading 212 CSV exports, calculate open positions using FIFO, "
        "and track share lots older than six calendar months."
    ),
    no_args_is_help=True,
    epilog=(
        "INPUT_PATH may be one Trading 212 CSV export or a directory containing "
        "CSV exports. This tool assists analysis and does not provide tax advice."
    ),
)

console = Console()

InputPath = Annotated[
    Path,
    typer.Argument(
        help=(
            "Trading 212 CSV export, or a directory containing CSV exports to combine."
        ),
    ),
]


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


def _read_input(input_path: Path, *, report_unsupported: bool) -> pl.DataFrame:
    """Read validated input and report decisions that affect calculations."""
    try:
        df = read_transactions(input_path)
    except (FileNotFoundError, ValueError) as error:
        console.print(f"[bold red]Input error:[/bold red] {error}")
        raise typer.Exit(code=1) from error

    summary = import_summary(df)
    if summary.duplicate_rows_removed:
        console.print(
            "[yellow]Import notice:[/yellow] removed "
            f"{summary.duplicate_rows_removed} duplicate transaction row(s) from "
            "overlapping exports."
        )

    if report_unsupported and summary.unsupported_action_counts:
        actions = ", ".join(
            f"{action} ({count})"
            for action, count in sorted(summary.unsupported_action_counts.items())
        )
        console.print(
            "[yellow]Unsupported actions:[/yellow] "
            f"{actions}. These rows are not included in tax-lot calculations; "
            "recognized buys and sells will continue to be processed."
        )

    return df


def _calculation_error(error: ValueError) -> None:
    """Print a concise processing error and terminate the command."""
    console.print(f"[bold red]Processing error:[/bold red] {error}")
    raise typer.Exit(code=1) from error


@app.command()
def inspect(input_path: InputPath) -> None:
    """Summarize files, rows, columns, trades, and transaction types."""
    try:
        csv_files = find_csv_files(input_path)
    except (FileNotFoundError, ValueError) as error:
        console.print(f"[bold red]Input error:[/bold red] {error}")
        raise typer.Exit(code=1) from error
    df = _read_input(input_path, report_unsupported=False)

    trades = get_trade_transactions(df)
    buys = get_buy_transactions(df)
    sells = get_sell_transactions(df)
    summary = import_summary(df)

    overview_table = Table(title="Trading 212 CSV Overview")
    overview_table.add_column("Property")
    overview_table.add_column("Value")

    overview_table.add_row("CSV files", str(len(csv_files)))
    overview_table.add_row("Rows", str(df.height))
    overview_table.add_row("Columns", str(df.width))
    overview_table.add_row("Trades", str(trades.height))
    overview_table.add_row("Buy transactions", str(buys.height))
    overview_table.add_row("Sell transactions", str(sells.height))
    overview_table.add_row(
        "Duplicate rows removed", str(summary.duplicate_rows_removed)
    )
    overview_table.add_row(
        "Unsupported action types", str(len(summary.unsupported_action_counts))
    )
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
    action_table.add_column("Tax-lot processing")

    for row in action_counts.iter_rows(named=True):
        action = str(row["action"])
        processing = (
            "recognized"
            if action not in summary.unsupported_action_counts
            else "ignored"
        )
        action_table.add_row(action, str(row["len"]), processing)

    console.print(action_table)


@app.command()
def positions(
    input_path: InputPath,
    ticker: str | None = typer.Option(
        None,
        "--ticker",
        "-t",
        help="Only show positions for this ticker.",
    ),
) -> None:
    """Show current positions after matching sells against buys using FIFO."""
    df = _read_input(input_path, report_unsupported=True)
    try:
        positions = _filter_ticker(positions_frame(df), ticker)
    except ValueError as error:
        _calculation_error(error)

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
    input_path: InputPath,
    as_of: str | None = typer.Option(
        None,
        "--as-of",
        help=(
            "Date used for the six-calendar-month check, formatted as "
            "YYYY-MM-DD. Lots bought on or before the cutoff are included. "
            "Defaults to today."
        ),
    ),
    ticker: str | None = typer.Option(
        None,
        "--ticker",
        "-t",
        help="Only show eligibility for this ticker.",
    ),
) -> None:
    """Show shares bought on or before the six-calendar-month cutoff."""
    df = _read_input(input_path, report_unsupported=True)
    try:
        eligibility = _filter_ticker(
            eligible_to_sell_frame(df, as_of=as_of),
            ticker,
        )
    except ValueError as error:
        _calculation_error(error)

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
    input_path: InputPath,
    ticker: str | None = typer.Option(
        None,
        "--ticker",
        "-t",
        help="Only show open lots for this ticker.",
    ),
) -> None:
    """Show remaining FIFO buy lots after applying all recognized sells."""
    df = _read_input(input_path, report_unsupported=True)
    try:
        lots = _filter_ticker(open_lots_frame(df), ticker)
    except ValueError as error:
        _calculation_error(error)

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
