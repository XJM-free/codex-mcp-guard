# Compatibility

This is the validation target for `v0.1.0-alpha.2`, not a guarantee for every Codex
release channel.

## Codex surface

- Validated with local `codex-cli 0.148.0-alpha.9` on 2026-08-18.
- `codex features list` reports `hooks` and `plugins` as stable and enabled.
- Current official documentation includes `SubagentStart`, `SubagentStop`, `turn_id`,
  plugin `PLUGIN_ROOT`/`PLUGIN_DATA`, Hook trust, synchronous execution, and the
  default `hooks/hooks.json` plugin location.
- The common `transcript_path` is a parent session transcript. It is ignored for
  subagent timing. `agent_transcript_path` exists only at Stop and is not an ownership
  signal.
- An isolated `CODEX_HOME` accepts the repository marketplace, installs
  `codex-mcp-guard@xjm-free`, and discovers both plugin Hooks with no parser warnings
  or errors. Their expected pre-review trust state is `untrusted`.
- Current official docs allow a manifest `hooks` field, but the validator bundled
  with the tested release rejects it. The plugin uses compatible default discovery.
- Installed skills are namespaced as `codex-mcp-guard:codex-mcp-guard` in this Codex
  version.
- Plugin Hooks and skill shell commands share `~/.codex-mcp-guard`; ordinary shell
  commands do not receive plugin-only `PLUGIN_DATA`.

Matching plugin and user Hooks both run. Treat those installation modes as mutually
exclusive. A pinned marketplace must be removed and re-added to select a new tag;
start a new thread after installation or upgrade.

## Platform surface

| Platform | Discovery | RSS observation | Mutation |
| --- | --- | --- | --- |
| macOS | `ps` direct-child/process-group snapshots | `ps` RSS, group sum | none |
| Linux | `ps` direct-child/process-group snapshots | `ps` RSS, group sum | none |
| Windows | CIM direct-child snapshots | working set, descendant sum | none |

CI runs synthetic lifecycle, state-security, classifier, installer, release, and
summary tests on macOS, Ubuntu, and Windows with Python 3.10 and 3.14. Live inventory
has been exercised on macOS. Windows and Linux discovery remain best effort until
isolated live process-tree fixtures are contributed.

## Known limits

- A snapshot-window delta cannot establish process ownership.
- Helpers already present at Start or gone before Stop are invisible to the delta.
- Concurrent windows that could explain the same cohort remain `report-only`.
- Resumed resident subagents may predate the observation window.
- Unix process start timestamps are second-resolution.
- The classifier favors precision and can miss shell-wrapped or custom MCP launches.
- RSS is not unique physical memory and can include shared pages.
- Enterprise policy can disable non-managed/plugin Hooks entirely.
