# t212-tax-lots

A small Python CLI tool for analysing Trading 212 CSV transaction exports.

It helps you inspect exports, calculate current positions, review the underlying
open buy lots, and estimate how many shares are at least six calendar months
old.

> [!IMPORTANT]
> This project is an analysis aid, not tax or financial advice. Verify its
> assumptions and results against the rules that apply to you before making
> decisions.

## What It Does

The tool currently:

- Reads one Trading 212 CSV export or combines all `.csv` files in one
  directory.
- Normalizes common Trading 212 column names and data types.
- Summarizes files, transaction types, buys, and sells.
- Calculates open positions by matching recognized sells against buys using
  FIFO: the oldest available buy lot is consumed first.
- Shows the remaining buy lots behind each position.
- Reports shares bought on or before the date six calendar months before a
  chosen `as-of` date.
- Uses ISIN to identify an asset when available, falling back to ticker.
- Validates required columns, timestamps, trade quantities, and asset
  identifiers before calculation.
- Detects overlap duplicates using Trading 212 transaction IDs when available,
  with a conservative cross-file fallback when IDs are absent.
- Reports unsupported actions and excludes them from tax-lot calculations.
- Rejects a sell that exceeds the available recognized buys with guidance to
  upload the missing earlier acquisition history.

Recognized trades are currently:

- `Market buy`
- `Limit buy`
- `Market sell`
- `Limit sell`

Other transaction types may appear in `inspect`, but they are not included in
position or lot calculations. Position-related commands report their names and
counts before continuing with recognized buys and sells.

## Setup

The project requires Python 3.10 or newer and uses
[uv](https://docs.astral.sh/uv/) for dependency management.

Install the project and its dependencies:

```bash
uv sync
```

Show the available commands:

```bash
uv run t212-tax-lots --help
```

Every command accepts `INPUT_PATH` as either:

- A single Trading 212 CSV export.
- A directory containing Trading 212 CSV exports. Only `.csv` files directly
  inside that directory are read.

For example:

```text
data/
├── export-2024.csv
└── export-2025.csv
```

Use `data/` as the input path to analyse both exports together.

Keep real exports outside version control because they contain private financial
data.

## Usage

### Inspect Exports

Check what an export or directory contains before calculating positions:

```bash
uv run t212-tax-lots inspect data/
```

This displays:

- The number of processed CSV files, rows, columns, and recognized trades.
- The number of duplicate rows removed from overlapping exports.
- Buy and sell transaction counts.
- Row counts for each source file.
- Counts for every transaction action and whether tax-lot processing recognizes
  it.

This is useful for noticing unexpected transaction types or accidentally
included files.

### Show Current Positions

Calculate current positions after applying all recognized buys and sells:

```bash
uv run t212-tax-lots positions data/
```

Show only one ticker:

```bash
uv run t212-tax-lots positions data/ --ticker AAPL
```

The output includes total remaining shares, the number of open lots, and the
oldest and newest remaining buy dates. "Current" means the position after all
transactions present in the supplied exports; the tool does not query Trading
212 or live market data.

### Show Shares Older Than Six Months

Estimate how many currently held shares were bought on or before the date six
calendar months before today:

```bash
uv run t212-tax-lots eligible-to-sell data/
```

Use an explicit date for reproducible results:

```bash
uv run t212-tax-lots eligible-to-sell data/ --as-of 2026-06-05
```

Filter the result to one ticker:

```bash
uv run t212-tax-lots eligible-to-sell data/ --ticker AAPL
```

For an `--as-of` date of `2026-06-05`, the cutoff is `2025-12-05`. Remaining
shares from lots bought on or before the cutoff are counted as eligible.

The six-month check uses calendar months, not a fixed number of days.

### Show Open Lots

Review the remaining FIFO buy lots used by the position and eligibility
calculations:

```bash
uv run t212-tax-lots open-lots data/
```

Show lots for one ticker:

```bash
uv run t212-tax-lots open-lots data/ --ticker AAPL
```

The output includes each lot's buy date, remaining shares, original
price-per-share information, and source file.

Use command-specific help for all options:

```bash
uv run t212-tax-lots eligible-to-sell --help
```

## How Calculations Work

Transactions are sorted by time. Each recognized buy creates a lot. Each
recognized sell consumes shares from the oldest open lot for the same asset
until the sell is fully matched.

For example:

1. Buy 10 shares on January 1.
2. Buy 5 shares on February 1.
3. Sell 12 shares on March 1.

The January lot is fully consumed and 2 shares are consumed from the February
lot, leaving 3 shares bought on February 1.

Asset matching prefers ISIN because it is generally more stable than ticker. If
an ISIN is missing, ticker is used instead.

## Important Limitations

- The tool assumes FIFO lot matching. This may not match the rules or elections
  relevant to your jurisdiction.
- It does not calculate capital gains, losses, taxes, fees, or currency-adjusted
  cost basis.
- It does not account for unrecognized trade action names, transfers, corporate
  actions, stock splits, mergers, or other events that may affect holdings.
- Duplicate detection prefers Trading 212 transaction IDs. For rows without an
  ID, exact matching transaction details are deduplicated only across different
  files; review the import notice when combining exports.
- Calculations require complete acquisition history for every recognized sell.
  If an earlier buy is absent, upload exports covering purchases before the
  reported sell timestamp.
- It relies on the contents and column format of the supplied Trading 212
  exports.
- Share quantities and prices currently use floating-point numbers.
- The eligibility report only measures lot age. It does not determine whether a
  sale is legally or tax-wise eligible.

Always inspect the inputs and validate important results independently.

## Possible Future Work

Potential improvements include:

- Supporting transfers, stock splits, and additional Trading 212 trade actions.
- Calculating realized gains and losses with fees and currency conversion.
- Using decimal arithmetic where exact monetary precision is required.
- Exporting calculated positions and lots to CSV or another structured format.
- Adding configurable lot-matching methods and jurisdiction-specific reports.

These are ideas rather than committed features.

## Development

Run the full quality suite:

```bash
uv run ruff check .
uv run mypy src tests
uv run pytest
```

See [`AGENTS.md`](AGENTS.md) for project-specific engineering and safety
guidelines.
