# Security policy

Codex MCP Guard `v0.1.0-alpha.2` is audit-only. Its public process backend exposes
only snapshot reads. An AST-based release check rejects observed-process control and
shell execution; the sole exception stops the `ps`/PowerShell inventory child that
the backend itself started when it exceeds a deadline or output cap.

Please treat any of the following as a security issue:

- a way to make the plugin execute or signal an observed Codex/MCP process
- a state/configuration symlink or permissions bypass
- exposure of raw process commands, prompts, transcripts, or credentials
- hook input that escapes the documented size and schema boundaries
- a false lifecycle correlation presented as proven ownership
- an overlapping lifecycle window presented as an unambiguous candidate
- a lock, process snapshot, or state write that escapes its documented bounds

Report vulnerabilities privately through the repository's GitHub Security
Advisories. Do not include prompts, transcripts, credentials, private repository
paths, or complete process command lines in a public issue. If private reporting
is unavailable, open a minimal issue requesting a private contact channel and
omit sensitive details.

The local ledger contains hashed command identities and lifecycle metadata. Keep
`~/.codex-mcp-guard` private and remove it before sharing a diagnostic archive.
