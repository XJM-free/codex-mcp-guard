# Codex MCP Guard

[![Tests](https://github.com/XJM-free/codex-mcp-guard/actions/workflows/tests.yml/badge.svg)](https://github.com/XJM-free/codex-mcp-guard/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

> **Alpha safety boundary:** `v0.1.0-alpha.1` is audit-only. It does not send
> signals, terminate processes, or offer an enforcement mode.

Codex MCP Guard is an unofficial Codex plugin and command-line auditor for MCP
helper processes that may remain resident after a subagent turn completes.

It investigates the retained-runtime window described in
[openai/codex#17574](https://github.com/openai/codex/issues/17574): a completed
subagent can keep its stdio MCP runtime until a later lifecycle boundary such as
resident-agent eviction or thread shutdown. Upstream has already added shutdown,
LRU residency, and process-tree cleanup improvements, so this project does not
claim that every current build leaks indefinitely or that upstream has no fix.

## Why audit instead of kill

`SubagentStart` and `SubagentStop` identify the subagent, but they do not provide
an authoritative child-process handle, launch generation, or ownership token.
Start-time proximity and repeated command fingerprints are useful correlation
signals, not proof of ownership. Externally killing a helper can also leave a
resident subagent holding a dead MCP connection.

For those reasons, the first public release deliberately reports evidence and
never acts on a process. A safe lifecycle fix belongs upstream, where Codex can
shut down the runtime and mark it for lazy reconnection atomically.

## What it does

- inventories MCP-like direct children of each Codex process with `doctor`
- observes `SubagentStart` and `SubagentStop` through documented Codex Hooks
- correlates bounded start-time cohorts without calling them owned processes
- revalidates PID, parent, process group, start time, and command fingerprint
- reports candidates that remain live after the stop event
- persists no raw command line or transcript content

The status vocabulary is intentionally explicit:

| Status | Meaning |
| --- | --- |
| `candidate-recorded` | A cohort passed the correlation checks; ownership is still unproven. |
| `retained-candidate` | The same identities were still live at `SubagentStop`; no signal was sent. |
| `candidate-exited` | All correlated identities had already exited. |
| `report-only` | Evidence was absent or ambiguous. |
| `skipped` | A stored identity changed or could not be safely revalidated. |

## Quick start

Python 3.10 or newer is required. The runtime has no third-party dependencies.

Install the published alpha as a Codex plugin:

```bash
codex plugin marketplace add XJM-free/codex-mcp-guard --ref v0.1.0-alpha.1
codex plugin add codex-mcp-guard@xjm-free
```

Start a new Codex thread after installation. Codex will show both lifecycle
Hooks as untrusted until you review their commands through the normal trust UI.
The plugin provides the `$codex-mcp-guard` skill and its read-only Hooks together.

To evaluate a source checkout without installing the skill:

```bash
git clone https://github.com/XJM-free/codex-mcp-guard.git
cd codex-mcp-guard

# Read-only inventory. These are candidates, not proven subagent ownership.
scripts/codex-mcp-guard doctor

# Preview and then install user-level lifecycle hooks.
python3 scripts/install-user-hooks.py --dry-run
python3 scripts/install-user-hooks.py

# Restart Codex, complete a new subagent turn, then inspect the audit ledger.
scripts/codex-mcp-guard status
```

The source-clone installer preserves unrelated hooks and backs up an existing
`~/.codex/hooks.json` before writing. Codex may ask you to review and trust the
new command hook. It installs only the lifecycle Hooks; it does not install the
bundled skill.

Remove the plugin and marketplace with:

```bash
codex plugin remove codex-mcp-guard@xjm-free
codex plugin marketplace remove xjm-free
```

For a source-clone evaluation, remove only this project's user handlers with:

```bash
python3 scripts/install-user-hooks.py --uninstall
```

## Plugin layout

The repository contains:

- `.codex-plugin/plugin.json` for plugin metadata and skill discovery
- `.agents/plugins/marketplace.json` for repository installation
- `hooks/hooks.json` using the documented plugin hook convention
- `skills/codex-mcp-guard/SKILL.md` for the diagnostic workflow
- `scripts/install-user-hooks.py` for an explicit, reversible user-hook install

The dedicated-repository marketplace points at `./`, which was validated with an
isolated `CODEX_HOME` using `codex plugin marketplace add`, `codex plugin add`,
and `hooks/list`. Both Hooks were discovered as plugin-sourced, enabled, and
untrusted with no parser warnings or errors.

See the official [Codex Hooks documentation](https://learn.chatgpt.com/docs/hooks)
for Hook trust and configuration details.

## Local data and privacy

Both plugin Hooks and shell commands use the shared default ledger at
`~/.codex-mcp-guard/state.json`; plugin-private `PLUGIN_DATA` is intentionally
not used because ordinary skill shell commands do not receive that environment
variable. On POSIX systems, the
directory and files must be private (`0700` and `0600`), and symlinked state
roots/files are rejected. The ledger stores lifecycle identifiers, PIDs,
timestamps, process relationships, classifications, and SHA-256 command
fingerprints. It does not store raw command lines, prompts, or transcripts.

A command hash can still reveal equality and may be guessable for a known
command, so treat the ledger as private diagnostic data.

## Compatibility

The audit path was developed against Codex CLI `0.148.0-alpha.9` on macOS, where
Hooks and plugins report as stable features. CI exercises Python 3.10 and 3.14 on
macOS, Linux, and Windows. Linux and Windows process discovery remains best
effort; neither platform has a process-termination path in this release.

See [docs/compatibility.md](docs/compatibility.md) for the tested surface and
[docs/upstream-evidence.md](docs/upstream-evidence.md) for the upstream scope.

## Development

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
ruff check .
ruff format --check .
python3 scripts/validate-release.py
python3 -m build
```

Read [SECURITY.md](SECURITY.md) before reporting an attribution bypass or local
data exposure. Architecture and trust boundaries are documented in
[docs/architecture.md](docs/architecture.md).

Codex is a product of OpenAI. This independent project is not affiliated with or
endorsed by OpenAI.
