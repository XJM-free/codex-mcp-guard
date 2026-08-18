# Architecture

Codex MCP Guard is a local, dependency-free, read-only observer.

```text
SubagentStart Hook
  -> validate bounded JSON and session/turn/agent IDs
  -> register a short `starting` transaction before process discovery
  -> locate the actual Codex ancestor executable
  -> snapshot MCP-like direct child roots under an internal deadline
  -> commit a private baseline with status=observing

SubagentStop Hook
  -> load the exact session/turn/agent observation
  -> snapshot the same Codex host again
  -> subtract baseline identities
  -> reject multiple cohorts or overlapping subagent windows
  -> record retained-candidate, report-only, or skipped
  -> send no signal to a candidate
```

## Snapshot-window model

The common Hook `transcript_path` is the parent session transcript, not a subagent
creation clock. Alpha 2 never reads it for attribution.

A Stop result can become `retained-candidate` only when all of these observations
hold:

1. Start and Stop run under the same revalidated Codex host identity.
2. The process is an MCP-like direct child and, on Unix, its process-group leader.
3. Its PID/start-time/command identity was absent from the Start baseline.
4. It started no earlier than the bounded Start receipt time, allowing only configured
   clock skew.
5. Exactly one bounded cohort is live at Stop.
6. The cohort has no duplicate command fingerprints.
7. Its process start times do not overlap another recorded subagent observation on
   the same Codex host.

These conditions establish a window delta, not which subagent launched the process.
The model intentionally misses helpers already present at the Start snapshot and
helpers that exit before Stop.

## Lifecycle state machine

`SubagentStart` first writes `starting`, takes the expensive snapshot outside the
lock, then commits `observing/baseline-only`. This makes pending concurrent Starts
visible to Stop ambiguity checks without holding the ledger lock during `ps` or CIM.
A matching Stop transitions once to a terminal state. Duplicate Hook sources
therefore return the same terminal result instead of overwriting it. The key includes
parent session, parent turn, and agent.

Observations without Stop expire to `abandoned` after the configured maximum age.
Pending `starting` records use a separate 60-second default because a snapshot should
finish well inside the outer Hook deadline.
Active observations are never count-evicted; only the newest terminal records are
retained when the 500-record or serialized-byte bound is reached. Byte pruning keeps
at least the newest terminal record; if that record alone is oversized, the write is
rejected without replacing the last readable state.

State version 1 used an invalid transcript-clock assumption. Version 2 migrates those
records to `legacy-report-only` and clears their candidate identities.

## Identity and memory observations

The ledger stores PID, parent PID, process-group ID when available, observed start
time, kind, and a SHA-256 command fingerprint. At Stop, current delta identities are
revalidated and process-group RSS is recorded. `doctor` computes age and group RSS
from the current snapshot. An aggregate RSS is `unknown` if any contributing process
is unknown; reports include RSS coverage and never present a partial sum as complete.

Unix `ps` start times have one-second resolution, so same-second PID reuse cannot be
excluded. RSS is an observation, not unique physical memory; summing groups can still
overstate system-wide impact when memory pages are shared.

## Trust and resource boundaries

- Hook JSON is capped at 1 MiB and lifecycle IDs are bounded.
- Process snapshots have platform-specific internal deadlines and an 8 MiB output
  bound, both below the outer synchronous Hook timeout.
- State lock acquisition has a 1.5-second internal deadline.
- POSIX state storage requires a current-user-owned `0700` directory and `0600`
  regular files; symlinked roots and files are rejected.
- Identity arrays, agent records, history, and serialized state bytes are bounded.
- An oversized write is rejected before replacing the last readable state.
- No raw command line, Hook payload, or transcript content is written to disk.

## Why there is no observed-process enforcement backend

Revalidating a PID is not atomic ownership. PID/process-group reuse can occur after a
check, and killing an externally observed helper can strand a resident Codex subagent
with a dead MCP connection. Only the upstream runtime can atomically shut down its
client and arrange reconnection. The package contains no signal, taskkill, or process-
tree termination path for observed Codex/MCP candidates. Its capped snapshot runner
can stop only the `ps` or PowerShell inventory child that it created itself when that
child times out or exceeds the output cap.
