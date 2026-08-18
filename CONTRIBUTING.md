# Contributing

Contributions must preserve the audit-only boundary of the alpha release.

Before opening a pull request:

1. Describe the lifecycle or identity boundary being changed.
2. Add a regression test that fails before the change.
3. Keep ambiguous evidence labeled `report-only`; never describe a snapshot-window delta as ownership.
4. Run `PYTHONPATH=src python3 -m unittest discover -s tests -v`.
5. Run `ruff check .`, `ruff format --check .`, and `python3 scripts/validate-release.py`.
6. Keep platform-specific discovery behind `ProcessBackend` and below the outer Hook deadline.

Do not add transcript-clock, name-only, age-only, or duplicate-count-only
attribution. Changes that
send signals or invoke process-tree termination are out of scope until Codex
exposes an authoritative process ownership primitive and the design receives a
separate security review.
