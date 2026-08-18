---
name: codex-mcp-guard
description: Audit and troubleshoot Codex MCP helper candidates with read-only lifecycle snapshots. Use when memory or swap grows after subagents finish; MCP-like processes accumulate; a user asks what retained-candidate means; or a user asks to kill, terminate, clean up, install, verify, interpret, or remove Codex MCP Guard and its hooks.
---

# Codex MCP Guard

Treat every association as evidence, never process ownership. Never terminate a
candidate through this CLI, another shell command, an API, or UI automation—even
when the user asks for cleanup. Explain the uncertainty and offer the safe recovery
steps below.

Resolve this file with `realpath`; derive the plugin root with
`Path(skill_file).resolve().parents[2]`.

## Choose the workflow

### Inventory current processes

1. Run `<plugin-root>/scripts/codex-mcp-guard doctor --summary --json`.
2. Report the observation time, aggregate candidate roots/cohorts, ages, and process-
   group RSS. Call every PID a classifier match, not a known leak or owned process.
   State RSS coverage; `unknown` means at least one contributing process lacked RSS,
   and a complete sum is still not uniquely reclaimable physical memory.
3. Read raw `doctor --json` only if PID-level local debugging is explicitly needed;
   do not reproduce PIDs by default.
4. Do not infer lifecycle retention or unbounded growth from one or several short-
   interval inventory snapshots.

### Interpret lifecycle status

1. Check whether `~/.codex-mcp-guard/state.json` exists. A configured Hook is not
   proof it ran; a ledger is not proof of a complete Start/Stop cycle.
2. Run `<plugin-root>/scripts/codex-mcp-guard status --summary --json --revalidate`
   first. This output is redacted and separates historical Stop evidence from a
   fresh identity check.
3. Read raw `status` only when record-level detail is necessary. Do not reproduce
   session IDs, agent IDs, command hashes, or complete ledger contents in a public
   answer.
4. Interpret `retained-candidate` only as: one helper cohort was absent from the
   Start baseline, appeared inside a non-overlapping observation window, and was
   live at Stop. It still does not prove ownership, a leak, or current liveness.
5. Preserve `report-only`, `skipped`, and the detail/revalidation reason. State v1
   transcript-clock records migrate to `legacy-report-only` and must not be reused.

### Verify installation

1. Parse `codex plugin list --json` and inspect only the matching
   `codex-mcp-guard@xjm-free` entry.
2. Check `~/.codex/hooks.json` only for the `CODEX_MCP_GUARD_HOOK` marker. Plugin
   Hooks and user Hooks are alternative installation modes; both at once cause
   duplicate lifecycle events.
3. Distinguish four facts: configured, enabled/trusted, executed at least once, and
   completed a matching Start/Stop observation.
4. Never use `--dangerously-bypass-hook-trust`; ask the user to review the exact
   commands through Codex's normal `/hooks` trust UI.

### Handle cleanup requests

Do not call `kill`, `pkill`, `killall`, `taskkill`, a process API, or an app's force-
quit UI against candidates. The Hook has no authoritative runtime handle or launch
generation, and external termination can strand a resident subagent with a dead MCP
connection.

If resources are under pressure:

1. Preserve active work.
2. End or restart the relevant Codex session through Codex's normal lifecycle.
3. Run a fresh redacted summary and inventory after restart.
4. If repeated complete windows show growth, collect sanitized counts, timestamps,
   Codex version, and evidence grades for an upstream report.

## Manage user hooks

Only modify user hooks when explicitly asked and only when the plugin is not
installed.

- Preview: `python3 <plugin-root>/scripts/install-user-hooks.py --dry-run`
- Install: `python3 <plugin-root>/scripts/install-user-hooks.py`
- Remove: `python3 <plugin-root>/scripts/install-user-hooks.py --uninstall`

Explain that the installer preserves unrelated handlers, backs up an existing file,
and triggers normal Hook trust review. Restart Codex after changing Hook sources.

Read [safety-model.md](references/safety-model.md) before any cleanup response or
before changing correlation, identity, state, or process-discovery logic.
