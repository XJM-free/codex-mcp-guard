# Safety model

## Non-authoritative evidence

Subagent Hooks identify a lifecycle event but do not provide an authoritative
helper-process handle, launch generation, or ownership token. Process start time,
parentage, process group, and command fingerprint are correlations only.

Label a cohort `correlated` only when the bounded time, uniqueness, repeated
fingerprint, and unclaimed-identity checks all pass. Otherwise keep it
`report-only`.

## Read-only invariant

Never send a signal, invoke `taskkill`, or add a terminate method to the process
backend. External termination can race PID reuse and can leave a resident
subagent with a dead MCP connection. Escalate a requested cleanup feature to an
upstream lifecycle design instead of weakening this invariant.

## Persisted data

Store no raw command line, prompt, or transcript content. Keep the state root
absolute, current-user-owned, non-symlinked, and private. Bound hook input,
identifier length, state size, agent records, and history.

## Claims

An inventory proves only that matching child processes were resident at that
moment. A `retained-candidate` proves that the recorded identities still matched
at Stop; it does not prove which subagent launched them or that they would remain
indefinitely.
