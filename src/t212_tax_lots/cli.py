"""Command-line interface for analysing Trading 212 transaction exports."""

from pathlib import Path
from typing import Annotated, Any

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
    cash_movements_frame,
    disposal_matches_frame,
    disposal_summary_frame,
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


def _format_money(value: float | None) -> str:
    """Format monetary values while keeping unknown amounts visible."""
    if value is None:
        return "unknown"
    return f"{value:,.2f}"


def _format_money_with_currency(value: float | None, currency: str | None) -> str:
    """Format a known amount with its currency without labeling unknown values."""
    if value is None:
        return "unknown"
    return f"{_format_money(value)} {currency or ''}".rstrip()


def _format_holding(
    holding_days: int | None,
    above_threshold: bool | None,
    threshold_months: int,
) -> str:
    """Format holding-period status with terminal color for quick scanning."""
    if holding_days is None or above_threshold is None:
        return "unknown"

    status = "above" if above_threshold else "below"
    color = "green" if above_threshold else "red"
    return f"[{color}]{holding_days}d ({status} {threshold_months}m)[/{color}]"


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


@app.command()
def disposals(
    input_path: InputPath,
    threshold_months: int = typer.Option(
        6,
        "--threshold-months",
        min=0,
        help="Calendar-month holding threshold used to classify matched lots.",
    ),
    ticker: str | None = typer.Option(
        None,
        "--ticker",
        "-t",
        help="Only show disposals for this ticker.",
    ),
    reporting_currency: str = typer.Option(
        "EUR",
        "--reporting-currency",
        "-c",
        help="Currency used for authoritative realized gain/loss totals.",
    ),
) -> None:
    """Show realized FIFO disposal matches, holding periods, and totals."""
    df = _read_input(input_path, report_unsupported=True)
    try:
        matches = _filter_ticker(
            disposal_matches_frame(
                df,
                threshold_months=threshold_months,
                reporting_currency=reporting_currency,
            ),
            ticker,
        )
        summary = disposal_summary_frame(matches)
        cash_movements = cash_movements_frame(matches)
    except ValueError as error:
        _calculation_error(error)

    match_rows = list(matches.iter_rows(named=True))
    rows_by_disposal: dict[str, list[dict[str, Any]]] = {}
    for row in match_rows:
        rows_by_disposal.setdefault(str(row["disposal_id"]), []).append(row)

    disposals_table = Table(title="Share Disposals (FIFO)")
    disposals_table.add_column("Disposal")
    disposals_table.add_column("Ticker")
    disposals_table.add_column("Name")
    disposals_table.add_column("ISIN")
    disposals_table.add_column("Sell date")
    disposals_table.add_column("Quantity", justify="right")
    disposals_table.add_column("Proceeds", justify="right")

    for disposal_id, rows in rows_by_disposal.items():
        first = rows[0]
        proceeds = [row["sell_proceeds"] for row in rows]
        total_proceeds = (
            None
            if any(value is None for value in proceeds)
            else sum(float(value) for value in proceeds)
        )
        disposals_table.add_row(
            disposal_id,
            str(first["ticker"] or ""),
            str(first["name"] or ""),
            str(first["isin"] or ""),
            str(first["sell_date"]),
            _format_float(first["sold_shares"]),
            _format_money_with_currency(total_proceeds, first["currency"]),
        )

    console.print(disposals_table)

    matches_table = Table(title="Matched Acquisition Lots")
    matches_table.add_column("Disposal")
    matches_table.add_column("Ticker")
    matches_table.add_column("Buy date")
    matches_table.add_column("Matched", justify="right")
    matches_table.add_column("Basis", justify="right")
    matches_table.add_column("Net proceeds", justify="right")
    matches_table.add_column("Gain/loss", justify="right")
    matches_table.add_column("Orig basis", justify="right")
    matches_table.add_column("Original G/L", justify="right")
    matches_table.add_column("Holding")

    for row in match_rows:
        matches_table.add_row(
            str(row["disposal_id"]),
            str(row["ticker"] or ""),
            str(row["buy_date"] or ""),
            _format_float(row["matched_shares"]),
            _format_money_with_currency(
                row["cost_basis_reporting"], row["reporting_currency"]
            ),
            _format_money_with_currency(
                row["net_proceeds_reporting"], row["reporting_currency"]
            ),
            _format_money_with_currency(
                row["realized_gain_loss_reporting"], row["reporting_currency"]
            ),
            _format_money_with_currency(row["cost_basis"], row["currency"]),
            _format_money_with_currency(
                row["original_gain_loss"], row["original_gain_loss_currency"]
            ),
            _format_holding(
                row["holding_days"], row["above_threshold"], threshold_months
            ),
        )

    console.print(matches_table)

    summary_rows = list(summary.iter_rows(named=True))
    summary_currencies = sorted(
        {row["currency"] for row in summary_rows},
        key=lambda value: str(value),
    )
    for currency in summary_currencies:
        currency_label = str(currency or "unknown currency")
        summary_table = Table(title=f"Disposal Summary ({currency_label})")
        summary_table.add_column("Scope")
        summary_table.add_column("Ticker")
        summary_table.add_column("Disposals", justify="right")
        summary_table.add_column("Net proceeds", justify="right")
        summary_table.add_column("Cost basis", justify="right")
        summary_table.add_column("Gain/loss", justify="right")
        summary_table.add_column("Shortest")
        summary_table.add_column("Longest")

        for row in summary_rows:
            if row["currency"] != currency:
                continue

            summary_table.add_row(
                str(row["scope"]),
                str(row["ticker"] or ""),
                str(row["disposals"]),
                _format_money_with_currency(
                    row["total_net_proceeds_reporting"], row["reporting_currency"]
                ),
                _format_money_with_currency(
                    row["total_cost_basis_reporting"], row["reporting_currency"]
                ),
                _format_money_with_currency(
                    row["total_realized_gain_loss_reporting"],
                    row["reporting_currency"],
                ),
                (
                    "unknown"
                    if row["shortest_holding_days"] is None
                    else f"{row['shortest_holding_days']}d"
                ),
                (
                    "unknown"
                    if row["longest_holding_days"] is None
                    else f"{row['longest_holding_days']}d"
                ),
            )

        console.print(summary_table)

    overall_reporting_rows = [
        row
        for row in summary_rows
        if row["scope"] == "overall"
        and row["total_realized_gain_loss_reporting"] is not None
    ]
    if overall_reporting_rows:
        total_gain = sum(
            max(0.0, float(row["total_realized_gain_loss_reporting"]))
            for row in overall_reporting_rows
        )
        total_loss = sum(
            min(0.0, float(row["total_realized_gain_loss_reporting"]))
            for row in overall_reporting_rows
        )
        reporting_label = reporting_currency.upper()
        totals_table = Table(title=f"Authoritative Realized Result ({reporting_label})")
        totals_table.add_column("Metric")
        totals_table.add_column("Value", justify="right")
        totals_table.add_row(
            "Total gains", _format_money_with_currency(total_gain, reporting_label)
        )
        totals_table.add_row(
            "Total losses", _format_money_with_currency(total_loss, reporting_label)
        )
        totals_table.add_row(
            "Net gain/loss",
            _format_money_with_currency(total_gain + total_loss, reporting_label),
        )
        totals_table.add_row(
            "Sell transactions",
            str(len({row["disposal_id"] for row in match_rows})),
        )
        totals_table.add_row("Matched lots", str(len(match_rows)))
        totals_table.add_row(
            "Warnings",
            str(sum(1 for row in match_rows if row["warning"])),
        )
        console.print(totals_table)

    if not cash_movements.is_empty():
        cash_table = Table(title="Cash Movements By Currency")
        cash_table.add_column("Currency")
        cash_table.add_column("Cash impact", justify="right")
        for row in cash_movements.iter_rows(named=True):
            cash_table.add_row(
                str(row["currency"]),
                _format_money_with_currency(row["cash_impact"], row["currency"]),
            )
        console.print(cash_table)

    warning_contexts: dict[str, set[str]] = {}
    for row in match_rows:
        if row["warning"]:
            warning_contexts.setdefault(str(row["warning"]), set()).add(
                str(row["disposal_id"])
            )
    for row in summary_rows:
        if row["warning"]:
            warning_contexts.setdefault(str(row["warning"]), set()).add(
                f"{row['scope']} {row['ticker'] or ''}".rstrip()
            )

    if warning_contexts:
        warnings_table = Table(title="Disposal Warnings")
        warnings_table.add_column("Context")
        warnings_table.add_column("Warning")
        for warning, contexts in warning_contexts.items():
            context = (
                ", ".join(sorted(contexts))
                if len(contexts) <= 3
                else f"{len(contexts)} disposals"
            )
            warnings_table.add_row(context, warning)
        console.print(warnings_table)
