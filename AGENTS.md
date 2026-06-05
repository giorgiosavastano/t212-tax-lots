# Agent Instructions

These instructions apply to all automated agents and contributors working on
this repository.

## Project Purpose

`t212-tax-lots` is a small Python CLI for analysing Trading 212 CSV transaction
exports. It parses exports, calculates open positions using FIFO lot matching,
and reports shares older than six calendar months.

Treat calculation correctness, auditability, and user-data privacy as primary
requirements. The tool assists analysis; it must not present its output as
professional tax or financial advice.

## Repository Layout

- `src/t212_tax_lots/parser.py`: CSV discovery, parsing, normalization, and
  transaction filtering.
- `src/t212_tax_lots/portfolio.py`: FIFO lot matching and portfolio
  calculations.
- `src/t212_tax_lots/cli.py`: Typer commands and Rich output formatting.
- `tests/`: focused unit tests that mirror the source modules.
- `README.md`: user-facing setup and usage documentation.
- `pyproject.toml`: project metadata, dependencies, and development tools.

Keep these responsibilities separate. Calculation logic belongs in testable
domain functions, not in CLI rendering code.

## Working Principles

- Read the relevant code and tests before making changes.
- Make the smallest coherent change that fully solves the task.
- Prefer clear, explicit code over clever or premature abstractions.
- Follow existing naming, typing, module boundaries, and library conventions.
- Preserve public behavior unless a behavior change is intentional and
  documented.
- Do not mix unrelated refactors with a feature or bug fix.
- Remove dead code and temporary debugging output introduced during the work.
- Add comments only when they explain a non-obvious decision or invariant.

## Correctness And Safety

- Treat tax-lot calculations as high-risk logic. Document assumptions and add
  tests for every changed rule or edge case.
- Preserve FIFO ordering and per-asset isolation unless requirements explicitly
  change them.
- Use calendar-aware date operations for month-based rules. Do not approximate
  a month with a fixed number of days.
- Keep calculations deterministic. Sort inputs explicitly when ordering affects
  results.
- Validate required input columns and fail with clear, actionable errors.
- Never silently discard malformed or unsupported financial transactions.
- Be deliberate about numeric precision and tolerances. Do not introduce new
  rounding behavior without tests and a documented reason.
- Do not expose raw transaction data, account details, or other personal
  information in logs or errors unless strictly necessary.
- Never commit real Trading 212 exports, credentials, secrets, or private user
  data. Use small synthetic fixtures in tests and documentation.
- Avoid destructive file operations and do not overwrite input CSV files.

## Testing And Quality Checks

Use `uv` for dependency management and command execution.

Run the full verification suite before considering a change complete:

```bash
uv run ruff check .
uv run mypy src tests
uv run pytest
```

When relevant, also exercise the affected CLI command with synthetic data.

Tests should:

- Cover expected behavior, boundary conditions, and failure modes.
- Be deterministic and independent of the current date; pass an explicit
  `as_of` date when testing date-sensitive behavior.
- Use synthetic transaction data and descriptive assertions.
- Include a regression test for every bug fix.
- Check externally observable behavior rather than private implementation
  details where practical.

Do not weaken, skip, or delete tests merely to make a change pass.

## Code And Dependency Guidelines

- Support the Python version declared in `pyproject.toml`.
- Add type annotations to new and changed functions.
- Prefer standard-library features and existing dependencies.
- Add a dependency only when it provides clear value that cannot reasonably be
  achieved with the current stack; update `pyproject.toml` and `uv.lock`
  together.
- Use Polars for tabular input, output, and aggregation. Keep complex lot
  matching straightforward and auditable.
- Maintain stable, meaningful schemas for returned DataFrames, including empty
  results.
- Keep user-facing CLI messages concise and actionable.

## Documentation And Change Hygiene

- Update `README.md` when setup, commands, options, output, or user-facing
  behavior changes.
- Record important assumptions near the relevant code and tests.
- Review the final diff for accidental formatting churn, generated files,
  secrets, and unrelated edits.
- Do not revert or overwrite changes made by others.
- Leave the repository passing all quality checks, or clearly report any check
  that could not be run and why.

## Definition Of Done

A change is complete when it is focused, readable, documented where necessary,
covered by appropriate tests, safe for private financial data, and passes the
full verification suite.
