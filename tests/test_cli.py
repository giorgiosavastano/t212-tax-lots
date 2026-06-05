from typer.testing import CliRunner

from t212_tax_lots.cli import app


runner = CliRunner()


def test_root_help_explains_core_calculation_and_input() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "calculate open positions using FIFO" in result.stdout
    assert "INPUT_PATH may be one Trading 212 CSV export" in result.stdout


def test_eligible_to_sell_help_explains_cutoff_boundary() -> None:
    result = runner.invoke(app, ["eligible-to-sell", "--help"], terminal_width=120)
    normalized_help = " ".join(result.stdout.split())

    assert result.exit_code == 0
    assert (
        "Show shares bought on or before the six-calendar-month cutoff"
        in normalized_help
    )
