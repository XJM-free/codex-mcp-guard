# Codex MCP Guard

[![Tests](https://github.com/XJM-free/codex-mcp-guard/actions/workflows/tests.yml/badge.svg)](https://github.com/XJM-free/codex-mcp-guard/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

> **Alpha safety boundary:** `v0.1.0-alpha.2` is audit-only. It never sends a
> signal to an observed Codex/MCP process, terminates a candidate, or offers an
> enforcement mode.

Codex MCP Guard is an unofficial Codex plugin and dependency-free CLI for
observing MCP helper processes around subagent lifecycle events.

It investigates the retained-runtime window discussed in
[openai/codex#17574](https://github.com/openai/codex/issues/17574). Upstream has
already added shutdown, resident-agent LRU, and process-tree cleanup improvements,
so this project does not claim that every current Codex build leaks indefinitely.

## Why it observes instead of killing

`SubagentStart` and `SubagentStop` identify a lifecycle, but they do not expose an
authoritative MCP runtime handle, launch generation, or process-ownership token.
Externally killing a matching process can race PID reuse and can leave a resident
subagent holding a dead MCP connection.

A safe cleanup fix belongs upstream, where Codex can atomically shut down its MCP
client and arrange reconnection. This plugin deliberately reports evidence only.

## Evidence model

Alpha 2 removes the unsafe assumption that the common Hook `transcript_path` is a
subagent creation clock. It is the parent session transcript.

The observer now uses two facts:

1. `SubagentStart` first registers a pending observation, then records a bounded
   baseline of MCP-like direct child roots.
2. `SubagentStop` reports only roots absent from that baseline and still live in a
   single, non-overlapping observation window.

This window delta is still not ownership. Concurrent subagent windows, multiple
cohorts, duplicate signatures, missing events, malformed state, and identity changes
all downgrade to `report-only` or `skipped`. Helpers that start before the Start
snapshot or exit before the Stop snapshot can be missed; this precision-first false
negative is intentional.

## What it does

- inventories candidate roots, ages, and process-group RSS with `doctor`
- records `SubagentStart` baselines and `SubagentStop` deltas through Codex Hooks
- keys observations by parent session, turn, and agent
- makes pending concurrent Starts visible and duplicate Stop events terminal-idempotent
- revalidates PID, parent, process group, start time, and command fingerprint
- provides a redacted summary with optional fresh identity revalidation
- bounds snapshot time, lock waits, identity arrays, state records, and serialized
  state size
- persists no raw command line, prompt, or transcript content

The bounded snapshot runner may stop the `ps` or PowerShell inventory child that it
started itself when that child times out or exceeds output limits. That owned utility
process is not an observed Codex/MCP candidate.

| Status or outcome | Meaning |
| --- | --- |
| `baseline-recorded` | Start snapshot saved; there is no retention claim yet. |
| `retained-candidate` | One window-delta cohort was live at Stop; ownership remains unproven. |
| `report-only` | Evidence was absent, incomplete, or ambiguous. |
| `skipped` | Stored host or process identity could not be safely revalidated. |
| `legacy-report-only` | Alpha 1 transcript-clock evidence was retired during state migration. |

## Install

Python 3.10 or newer is required. Install the published plugin:

```bash
codex plugin marketplace add XJM-free/codex-mcp-guard --ref v0.1.0-alpha.2
codex plugin add codex-mcp-guard@xjm-free
```

Start a new Codex thread. Review and trust the two exact command Hooks through the
normal `/hooks` UI; do not bypass Hook trust. The installed skill's canonical name is
`$codex-mcp-guard:codex-mcp-guard`, and natural-language diagnosis requests can also
trigger it.

### Upgrade a pinned Alpha 1 marketplace

A marketplace installed with `--ref` remains pinned to that ref. Replace it before
installing Alpha 2:

```bash
codex plugin remove codex-mcp-guard@xjm-free
codex plugin marketplace remove xjm-free
codex plugin marketplace add XJM-free/codex-mcp-guard --ref v0.1.0-alpha.2
codex plugin add codex-mcp-guard@xjm-free
```

Start a new thread after upgrading. Existing version 1 ledger evidence is migrated to
`legacy-report-only`; collect a fresh Start/Stop cycle.

## Diagnose

```bash
# Redacted current inventory; this does not prove lifecycle retention.
scripts/codex-mcp-guard doctor --summary

# PID-level local debugging only.
scripts/codex-mcp-guard doctor --json

# Redacted historical summary plus a fresh read-only identity check.
scripts/codex-mcp-guard status --summary --revalidate

# Machine-readable form for an agent or report generator.
scripts/codex-mcp-guard status --summary --json --revalidate
```

Plain `status` emits the private raw ledger for local debugging. Do not paste it into
a public issue: it contains session IDs, agent IDs, PIDs, and command fingerprints.

## Source-checkout Hook evaluation

This is an alternative to plugin installation, not an additional Hook source. Do not
install user Hooks while `codex-mcp-guard@xjm-free` is enabled; Codex runs matching
Hooks from both sources.

```bash
git clone https://github.com/XJM-free/codex-mcp-guard.git
cd codex-mcp-guard

python3 scripts/install-user-hooks.py --dry-run
python3 scripts/install-user-hooks.py
```

The installer merges unrelated handlers from the revision it reads, refuses symlinked
or malformed Hook files, backs up an existing `~/.codex/hooks.json`, and aborts when
its pre-commit identity/content check detects a concurrent edit. Do not intentionally
edit the file during installation; portable filesystems do not expose a true compare-
and-swap replace. It installs lifecycle Hooks only, not the bundled skill. Remove only
its handlers with:

```bash
python3 scripts/install-user-hooks.py --uninstall
```

Remove the plugin distribution with:

```bash
codex plugin remove codex-mcp-guard@xjm-free
codex plugin marketplace remove xjm-free
```

## Plugin layout

- `.codex-plugin/plugin.json` — plugin identity and explicit skill discovery
- `.agents/plugins/marketplace.json` — dedicated-repository marketplace
- `hooks/hooks.json` — default plugin Hook location
- `skills/codex-mcp-guard/SKILL.md` — agent diagnostic and cleanup-safety workflow
- `scripts/install-user-hooks.py` — optional, reversible user-Hook installer

The default `hooks/hooks.json` location is intentionally used instead of a manifest
`hooks` field so the package remains compatible with the validator bundled in the
tested Codex release. See the official
[Codex Hooks documentation](https://developers.openai.com/codex/hooks).

## Local data and privacy

Plugin Hooks and shell commands share `~/.codex-mcp-guard/state.json`. Ordinary skill
shell commands do not receive plugin-only `PLUGIN_DATA`, so using that directory for
the ledger would split writer and reader state.

On POSIX, the state directory and files must be private (`0700` and `0600`). Symlinked
roots/files are rejected. The ledger stores lifecycle identifiers, PIDs, timestamps,
process relationships, RSS observations, classifications, and SHA-256 command
fingerprints—never raw commands, prompts, or transcripts. A command hash can still
reveal equality and may be guessable for a known command, so keep the ledger private.
RSS totals include a coverage count and remain unknown when any contributing process
is unknown; they are not estimates of uniquely reclaimable physical memory.

## Compatibility

Developed against Codex CLI `0.148.0-alpha.9` on macOS, where Hooks and plugins report
as stable. CI exercises Python 3.10 and 3.14 on macOS, Linux, and Windows. Linux and
Windows discovery remains best effort; no platform has a termination path.

See [docs/compatibility.md](docs/compatibility.md),
[docs/architecture.md](docs/architecture.md), and
[docs/upstream-evidence.md](docs/upstream-evidence.md).

## Development

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
ruff check .
ruff format --check .
python3 scripts/validate-release.py
python3 -m build
```

Read [SECURITY.md](SECURITY.md) before reporting an attribution bypass or local data
exposure. Codex is a product of OpenAI. This independent project is not affiliated
with or endorsed by OpenAI.
