---
name: codex-mcp-guard
description: Inspect and troubleshoot retained MCP helper candidates around Codex subagent lifecycle events. Use when memory or swap grows after subagents finish, MCP-like child processes accumulate, or a user asks to install, verify, interpret, or remove Codex MCP Guard's read-only hooks.
---

# Codex MCP Guard

Treat every process association as diagnostic correlation, never proven
ownership. This release is audit-only and contains no process-termination path.

## Diagnose

1. Resolve this file with `realpath`; derive the plugin root with
   `Path(skill_file).resolve().parents[2]`.
2. Run `<plugin-root>/scripts/codex-mcp-guard doctor` first. Report that its PIDs
   are classifier matches, not known leaks or owned processes.
3. Check `codex plugin list` and `~/.codex/hooks.json` for this plugin or the
   `CODEX_MCP_GUARD_HOOK` marker. Treat an existing private state ledger as
   evidence that the Hook ran, not proof that it is still installed.
4. Run `<plugin-root>/scripts/codex-mcp-guard status` only when lifecycle hooks
   have run. If no Hook or ledger exists and installation is outside the request,
   stop with a snapshot-only conclusion and name the missing start/stop evidence.
5. Distinguish `retained-candidate`, `candidate-exited`, `report-only`, and
   `skipped`. Preserve the detail string when explaining ambiguity.
6. Do not infer unbounded growth from one inventory snapshot. Compare fresh
   start/stop evidence before making a lifecycle claim.

## Manage hooks

Only modify user hooks when the user asks for installation or removal.

- Preview: `python3 <plugin-root>/scripts/install-user-hooks.py --dry-run`
- Install: `python3 <plugin-root>/scripts/install-user-hooks.py`
- Remove: `python3 <plugin-root>/scripts/install-user-hooks.py --uninstall`

Explain that installation preserves unrelated handlers, backs up an existing
hooks file, and may trigger Codex's normal Hook trust review. Restart Codex after
changing Hook configuration.

Read [safety-model.md](references/safety-model.md) before changing correlation,
identity, state, or process-discovery logic.
