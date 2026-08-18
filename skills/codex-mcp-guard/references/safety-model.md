# Safety model

## Evidence grades

Codex Hooks do not provide an authoritative helper-process handle, launch
generation, or ownership token. The common `transcript_path` belongs to the parent
session and is never a subagent start clock.

- `baseline-only`: Start recorded a bounded snapshot; no lifecycle claim exists.
- `window-delta`: a cohort was absent at Start and live at Stop inside one
  non-overlapping observation window. Ownership remains unproven.
- `ambiguous`: competing cohorts, overlapping subagents, malformed state, or missing
  evidence require `report-only`.
- `retired`: v1 transcript-clock evidence is preserved only as
  `legacy-report-only`.

This model can miss helpers that start before the Start Hook snapshot or exit before
the Stop snapshot. Prefer that false-negative boundary over assigning old or
concurrent processes to a subagent.

## Read-only invariant

Never send a signal, invoke `kill`, `pkill`, `killall`, or `taskkill`, call a process-
termination API, or use UI automation to force-quit a candidate. This prohibition
applies even when the user requests cleanup. External termination can race PID reuse
and can leave a resident subagent with a dead MCP connection.

Escalate automatic cleanup to an upstream lifecycle design that can atomically close
the MCP client and arrange reconnection.

## Persisted data

Store no raw command line, prompt, or transcript content. Keep the state root
absolute, current-user-owned, non-symlinked, and private. Bound Hook input, identity
arrays, active records, history, lock waits, snapshot time, and serialized state
size.

`retained-candidate` is historical Stop evidence. Use redacted fresh revalidation
before discussing current liveness, and never publish session IDs, agent IDs, PIDs,
or command fingerprints by default.
