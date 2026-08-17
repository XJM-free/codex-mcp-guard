# Upstream evidence and scope

The project is motivated by
[openai/codex#17574](https://github.com/openai/codex/issues/17574), an open issue
about MCP processes associated with subagents remaining after the subagent turn.

The accurate scope is narrower than “Codex leaks forever.” Upstream has merged
several relevant improvements:

- [PR #19753](https://github.com/openai/codex/pull/19753) drains MCP clients and
  terminates stdio process trees during thread/session shutdown.
- [PR #26632](https://github.com/openai/codex/pull/26632) bounds resident
  subagents through idle LRU eviction.
- [PR #37068](https://github.com/openai/codex/pull/37068) and
  [PR #37366](https://github.com/openai/codex/pull/37366) harden process-tree
  cleanup behavior on macOS and Windows.

Those changes reduce or eventually clean retained runtimes, but they do not by
themselves establish that every completed subagent turn immediately releases its
MCP runtime. Codex MCP Guard observes the interval between turn completion and a
later shutdown or eviction boundary.

The current [Codex Hooks documentation](https://learn.chatgpt.com/docs/hooks)
documents `SubagentStart`, `SubagentStop`, user hook configuration, plugin hook
layout, `PLUGIN_ROOT`, and trust review. Current source schemas include:

- [SubagentStart command input](https://github.com/openai/codex/blob/main/codex-rs/hooks/schema/generated/subagent-start.command.input.schema.json)
- [SubagentStop command input](https://github.com/openai/codex/blob/main/codex-rs/hooks/schema/generated/subagent-stop.command.input.schema.json)

These payloads identify lifecycle events and transcript paths. They do not expose
an authoritative helper PID/handle or launch generation. This repository
therefore treats all process association as diagnostic correlation.

An upstream fix can safely close the remaining window by shutting down the MCP
runtime at the appropriate idle boundary and marking the client for lazy
reconnection. An external plugin cannot perform both operations atomically, so
this alpha does not attempt containment.
