# Architecture

Codex MCP Guard is a local, dependency-free, read-only observer.

```text
SubagentStart Hook
  -> validate bounded JSON and lifecycle IDs
  -> locate the actual Codex ancestor executable
  -> snapshot direct MCP-like child roots
  -> correlate one bounded start-time cohort
  -> write a private candidate record

SubagentStop Hook
  -> load the exact session/agent record
  -> snapshot again
  -> revalidate host and candidate identities
  -> record retained-candidate, candidate-exited, or skipped
  -> send no signal
```

## Correlation model

A start cohort is labeled `correlated` only when all of these observations hold:

1. The process is an MCP-like direct child of the same Codex host.
2. On Unix, the candidate root is also its process-group leader.
3. The cohort begins no more than two seconds before, and finishes no more than
   thirty seconds after, a trusted transcript birth time.
4. No competing cohort is within the ambiguity margin.
5. No command fingerprint is duplicated inside the cohort.
6. The same fingerprint set exists in an older cohort, suggesting a repeated MCP
   server set rather than an unrelated one-off command.
7. No other active candidate record already contains the identity.

These conditions improve diagnostic precision but do not prove which subagent
launched a process. The Hook payload does not contain that ownership fact.

## Identity checks

The ledger stores PID, parent PID, process-group ID when available, observed
start time, kind, and a SHA-256 command fingerprint. At Stop, every field is
revalidated. A missing process is recorded as exited; a changed process is
recorded as skipped.

Unix `ps` start times have one-second resolution, so the observer cannot exclude
all same-second PID reuse. This is one reason the evidence cannot authorize
termination.

## Trust boundaries

- Hook JSON is capped at 1 MiB and requires bounded lifecycle identifiers.
- Transcript time is used only for an absolute, regular, current-user-owned,
  non-symlink `.jsonl` file.
- The composite state key is structured JSON, not delimiter concatenation.
- POSIX state storage requires a current-user-owned `0700` directory and `0600`
  regular files; symlinked roots and files are rejected.
- State retains at most 500 agent records and 200 history entries.
- No raw command line or transcript content is written to disk.

## Why there is no enforcement backend

Revalidating a PID is not an atomic ownership proof. PID/process-group reuse can
occur after verification, and killing an externally observed helper can strand a
resident Codex subagent with a dead MCP connection. Only the upstream runtime can
atomically shut down its client and arrange reconnection. The alpha therefore
contains no signal, taskkill, or process-tree termination API.
