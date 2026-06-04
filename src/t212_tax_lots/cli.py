"""Command-line interface for analysing Trading 212 transaction exports."""

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from t212_tax_lots.parser import read_transactions

app = typer.Typer(
    help="Analyse Trading 212 CSV exports and track share lots older than 6 months."
)

console = Console()


@app.callback()
def main() -> None:
    """Analyse Trading 212 transaction exports."""
    # This callback exists so Typer treats the app as a command group.
    # That allows commands like:
    #
    #   t212-tax-lots inspect path/to/file.csv
    #
    # instead of collapsing the only command into the root command.
    pass


@app.command()
def inspect(csv_path: Path) -> None:
    """Inspect a Trading 212 CSV export."""
    df = read_transactions(csv_path)

    table = Table(title="Trading 212 CSV Overview")
    table.add_column("Property")
    table.add_column("Value")

    table.add_row("Rows", str(df.height))
    table.add_row("Columns", str(df.width))
    table.add_row("Column names", ", ".join(df.columns))

    console.print(table)