from pathlib import Path

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


def test_inspect_reports_duplicates_and_unsupported_actions(tmp_path: Path) -> None:
    header = "Action,Time,ID,ISIN,Ticker,No. of shares\n"
    duplicate = "Market buy,2025-01-02 03:04:05,TX-1,ISIN-A,AAPL,1\n"
    (tmp_path / "first.csv").write_text(header + duplicate)
    (tmp_path / "second.csv").write_text(
        header
        + duplicate
        + "Dividend,2025-02-02 03:04:05,TX-2,ISIN-A,AAPL,\n"
    )

    result = runner.invoke(app, ["inspect", str(tmp_path)])

    assert result.exit_code == 0
    assert "removed 1 duplicate transaction row(s)" in result.stdout
    assert "Unsupported action types" in result.stdout
    assert "Dividend" in result.stdout
    assert "ignored" in result.stdout


def test_positions_warns_that_unsupported_actions_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "export.csv"
    path.write_text(
        "\n".join(
            [
                "Action,Time,ID,ISIN,Ticker,Name,No. of shares",
                "Market buy,2025-01-02 03:04:05,TX-1,ISIN-A,AAPL,Apple,1",
                "Dividend,2025-02-02 03:04:05,TX-2,ISIN-A,AAPL,Apple,",
            ]
        )
    )

    result = runner.invoke(app, ["positions", str(path)])
    normalized_output = " ".join(result.stdout.split())

    assert result.exit_code == 0
    assert "Unsupported actions:" in normalized_output
    assert "Dividend (1)" in normalized_output
    assert "not included in tax-lot calculations" in normalized_output


def test_positions_explains_missing_acquisition_history(tmp_path: Path) -> None:
    path = tmp_path / "partial.csv"
    path.write_text(
        "\n".join(
            [
                "Action,Time,ID,ISIN,Ticker,No. of shares",
                "Market sell,2025-02-02 03:04:05,TX-2,ISIN-A,AAPL,1",
            ]
        )
    )

    result = runner.invoke(app, ["positions", str(path)])
    normalized_output = " ".join(result.stdout.split())

    assert result.exit_code == 1
    assert "Processing error:" in normalized_output
    assert "missing from the supplied acquisition history" in normalized_output
    assert (
        "Upload exports covering purchases before 2025-02-02 03:04:05"
        in normalized_output
    )


def test_positions_reports_invalid_export_cleanly(tmp_path: Path) -> None:
    path = tmp_path / "invalid.csv"
    path.write_text("Action,Ticker\nMarket buy,AAPL\n")

    result = runner.invoke(app, ["positions", str(path)])

    assert result.exit_code == 1
    assert "Input error:" in result.stdout
    assert "missing required Trading 212 columns: Time" in result.stdout
