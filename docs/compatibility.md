# Compatibility

This document describes the validation snapshot for `v0.1.0-alpha.1`, not a
guarantee for every Codex release channel.

## Codex surface

- Developed against local `codex-cli 0.148.0-alpha.9` on 2026-08-17.
- `codex features list` reported both `hooks` and `plugins` as stable.
- The official Hooks documentation and current schemas include
  `SubagentStart` and `SubagentStop`.
- The explicit installer targets `~/.codex/hooks.json`, preserves unrelated
  handlers, and creates a timestamped backup before replacing an existing file.
- The plugin bundle also includes the conventional `hooks/hooks.json` layout.
- An isolated `CODEX_HOME` accepted the repository marketplace at `./`, installed
  `codex-mcp-guard@xjm-free`, and discovered both plugin Hooks with no parser
  warnings or errors. The expected trust status before review was `untrusted`.
- Plugin Hooks and skill shell commands share `~/.codex-mcp-guard`; the observer
  does not depend on plugin-only `PLUGIN_DATA` for its ledger.

Older Codex builds may not recognize these lifecycle events or plugin layout.
Verify one fresh start/stop cycle before relying on the ledger. The explicit
source-clone installer does not install the bundled skill.

## Platform surface

| Platform | Discovery | Transcript reference | Mutation |
| --- | --- | --- | --- |
| macOS | `ps` direct-child snapshots | filesystem birth time | none |
| Linux | `ps` direct-child snapshots | usually hook time, therefore ambiguous | none |
| Windows | CIM process snapshots | creation time when available | none |

CI runs the synthetic and state-security tests on macOS, Ubuntu, and Windows
with Python 3.10 and 3.14. The live process inventory has been exercised on
macOS. Windows and Linux discovery should be treated as best effort until more
isolated live-process fixtures are contributed.

## Known limits

- Hook timing cannot establish authoritative process ownership.
- Concurrent subagents with the same MCP server set can remain report-only.
- Resumed resident subagents may predate the observed lifecycle window.
- Unix process start timestamps are only second-resolution in this implementation.
- The classifier favors precision and can miss custom MCP commands whose
  executable, module, script, or package position does not identify them as MCP.
