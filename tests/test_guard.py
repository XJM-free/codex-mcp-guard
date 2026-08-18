from __future__ import annotations

import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from codex_mcp_guard.config import GuardConfig
from codex_mcp_guard.guard import Guard, _agent_key
from codex_mcp_guard.models import ProcessInfo
from codex_mcp_guard.processes import ProcessBackend
from codex_mcp_guard.state import MAX_STATE_BYTES, StateStore


def process(
    pid: int,
    ppid: int,
    started_at: float,
    command: str,
    *,
    rss_bytes: int = 1024,
) -> ProcessInfo:
    return ProcessInfo(pid, ppid, pid, started_at, command, rss_bytes)


class FakeBackend(ProcessBackend):
    def __init__(self, snapshot: dict[int, ProcessInfo]) -> None:
        self.processes = snapshot

    def snapshot(self) -> dict[int, ProcessInfo]:
        return dict(self.processes)


class FailingBackend(ProcessBackend):
    def snapshot(self) -> dict[int, ProcessInfo]:
        raise subprocess.TimeoutExpired(["ps"], 5)


class BlockingStartBackend(FakeBackend):
    def __init__(self, snapshot: dict[int, ProcessInfo]) -> None:
        super().__init__(snapshot)
        self.entered = threading.Event()
        self.release = threading.Event()

    def snapshot(self) -> dict[int, ProcessInfo]:
        if threading.current_thread().name == "pending-start":
            self.entered.set()
            if not self.release.wait(timeout=2):
                raise TimeoutError("test barrier timed out")
        return super().snapshot()


def base_snapshot() -> dict[int, ProcessInfo]:
    return {
        100: process(100, 1, 900, "/usr/local/bin/codex resume root"),
        200: process(
            200, 100, 1004, "/bin/zsh -c python codex-mcp-guard/scripts/hook.py"
        ),
        201: process(201, 200, 1004, "python codex-mcp-guard/scripts/hook.py"),
        290: process(290, 100, 990, "/opt/xcodebuildmcp/node cli.js mcp"),
        291: process(
            291,
            100,
            990,
            "/Applications/ChatGPT.app/Contents/Resources/cua_node/bin/node_repl",
        ),
    }


def add_new_cohort(
    backend: FakeBackend, *, started_at: float = 1006.0, duplicate: bool = False
) -> None:
    second_command = (
        "/opt/xcodebuildmcp/node cli.js mcp"
        if duplicate
        else "/Applications/ChatGPT.app/Contents/Resources/cua_node/bin/node_repl"
    )
    backend.processes[300] = process(
        300, 100, started_at, "/opt/xcodebuildmcp/node cli.js mcp", rss_bytes=2048
    )
    backend.processes[301] = process(
        301, 100, started_at, second_command, rss_bytes=4096
    )


def event(
    name: str,
    *,
    session_id: str = "session-1",
    turn_id: str = "turn-1",
    agent_id: str = "agent-1",
) -> dict[str, object]:
    return {
        "hook_event_name": name,
        "session_id": session_id,
        "turn_id": turn_id,
        "agent_id": agent_id,
        "agent_type": "explorer",
        "transcript_path": "/old/parent/session.jsonl",
    }


class GuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temporary.name))
        self.now = 1005.0

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_guard(self, backend: FakeBackend) -> Guard:
        return Guard(
            backend=backend,
            store=self.store,
            config=GuardConfig(max_observation_seconds=60.0),
            clock=lambda: self.now,
        )

    def test_window_delta_is_retained_without_claiming_ownership(self) -> None:
        backend = FakeBackend(base_snapshot())
        guard = self.make_guard(backend)

        started = guard.handle_event(event("SubagentStart"), hook_pid=201)
        add_new_cohort(backend)
        self.now = 1010.0
        stopped = guard.handle_event(event("SubagentStop"), hook_pid=201)

        self.assertEqual(started.outcome, "baseline-recorded")
        self.assertEqual(stopped.outcome, "retained-candidate")
        self.assertEqual(stopped.process_count, 2)
        self.assertIn("ownership is unproven", stopped.detail)
        self.assertFalse(hasattr(backend, "terminate"))
        persisted = self.store.path.read_text(encoding="utf-8")
        self.assertNotIn("/opt/xcodebuildmcp/node", persisted)
        self.assertNotIn("node_repl", persisted)
        record = self.store.read()["agents"][
            _agent_key("session-1", "agent-1", "turn-1")
        ]
        self.assertEqual(record["evidence_grade"], "window-delta")
        self.assertEqual(record["live_group_rss_bytes"], 6144)

    def test_common_parent_transcript_is_never_a_start_clock(self) -> None:
        backend = FakeBackend(base_snapshot())
        guard = self.make_guard(backend)

        started = guard.handle_event(event("SubagentStart"), hook_pid=201)
        record = self.store.read()["agents"][
            _agent_key("session-1", "agent-1", "turn-1")
        ]

        self.assertEqual(started.outcome, "baseline-recorded")
        self.assertEqual(record["started_event_at"], 1005.0)
        self.assertNotIn("reference_at", record)
        self.assertNotIn("reference_source", record)
        self.assertEqual(record["baseline_process_count"], 2)

    def test_no_window_delta_remains_report_only(self) -> None:
        backend = FakeBackend(base_snapshot())
        guard = self.make_guard(backend)
        guard.handle_event(event("SubagentStart"), hook_pid=201)
        self.now = 1010.0

        stopped = guard.handle_event(event("SubagentStop"), hook_pid=201)

        self.assertEqual(stopped.outcome, "report-only")
        self.assertIn("no new MCP helper roots", stopped.detail)

    def test_multiple_new_cohorts_remain_report_only(self) -> None:
        backend = FakeBackend(base_snapshot())
        guard = self.make_guard(backend)
        guard.handle_event(event("SubagentStart"), hook_pid=201)
        backend.processes[300] = process(
            300, 100, 1006, "/opt/xcodebuildmcp/node cli.js mcp"
        )
        backend.processes[301] = process(
            301,
            100,
            1010,
            "/Applications/ChatGPT.app/Contents/Resources/cua_node/bin/node_repl",
        )
        self.now = 1012.0

        stopped = guard.handle_event(event("SubagentStop"), hook_pid=201)

        self.assertEqual(stopped.outcome, "report-only")
        self.assertIn("2 helper cohorts", stopped.detail)

    def test_duplicate_signatures_remain_report_only(self) -> None:
        backend = FakeBackend(base_snapshot())
        guard = self.make_guard(backend)
        guard.handle_event(event("SubagentStart"), hook_pid=201)
        add_new_cohort(backend, duplicate=True)
        self.now = 1010.0

        stopped = guard.handle_event(event("SubagentStop"), hook_pid=201)

        self.assertEqual(stopped.outcome, "report-only")
        self.assertIn("duplicate", stopped.detail)

    def test_overlapping_subagent_windows_remain_report_only_for_both(self) -> None:
        backend = FakeBackend(base_snapshot())
        guard = self.make_guard(backend)
        first_start = event("SubagentStart", agent_id="agent-1", turn_id="turn-1")
        second_start = event("SubagentStart", agent_id="agent-2", turn_id="turn-1")
        guard.handle_event(first_start, hook_pid=201)
        self.now = 1006.0
        guard.handle_event(second_start, hook_pid=201)
        add_new_cohort(backend, started_at=1007.0)

        self.now = 1010.0
        first = guard.handle_event(
            event("SubagentStop", agent_id="agent-1", turn_id="turn-1"),
            hook_pid=201,
        )
        self.now = 1011.0
        second = guard.handle_event(
            event("SubagentStop", agent_id="agent-2", turn_id="turn-1"),
            hook_pid=201,
        )

        self.assertEqual(first.outcome, "report-only")
        self.assertEqual(second.outcome, "report-only")
        self.assertIn("overlaps", first.detail)
        self.assertIn("overlaps", second.detail)

    def test_pending_start_is_visible_to_an_overlapping_stop(self) -> None:
        backend = BlockingStartBackend(base_snapshot())
        guard = self.make_guard(backend)
        guard.handle_event(
            event("SubagentStart", agent_id="agent-1", turn_id="turn-1"),
            hook_pid=201,
        )
        self.now = 1006.0
        result: list[object] = []

        def start_second_agent() -> None:
            result.append(
                guard.handle_event(
                    event("SubagentStart", agent_id="agent-2", turn_id="turn-1"),
                    hook_pid=201,
                )
            )

        thread = threading.Thread(target=start_second_agent, name="pending-start")
        thread.start()
        self.assertTrue(backend.entered.wait(timeout=1))
        try:
            add_new_cohort(backend, started_at=1007.0)
            self.now = 1010.0
            first_stop = guard.handle_event(
                event("SubagentStop", agent_id="agent-1", turn_id="turn-1"),
                hook_pid=201,
            )
        finally:
            backend.release.set()
            thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(result), 1)
        self.assertFalse(isinstance(result[0], BaseException))
        self.assertEqual(first_stop.outcome, "report-only")
        self.assertIn("overlaps", first_stop.detail)

        self.now = 1011.0
        second_stop = guard.handle_event(
            event("SubagentStop", agent_id="agent-2", turn_id="turn-1"),
            hook_pid=201,
        )
        self.assertEqual(second_stop.outcome, "report-only")

    def test_stale_start_finalize_cannot_overwrite_new_generation(self) -> None:
        backend = FakeBackend(base_snapshot())
        guard = self.make_guard(backend)
        first_registration = guard._register_start_pending(
            event("SubagentStart"), "session-1", "turn-1", "agent-1"
        )
        self.assertIsInstance(first_registration, tuple)
        key, first_started_at, first_token = first_registration

        stopped = guard.handle_event(event("SubagentStop"), hook_pid=201)
        self.assertEqual(stopped.outcome, "report-only")
        self.now = 1006.0
        second_registration = guard._register_start_pending(
            event("SubagentStart"), "session-1", "turn-1", "agent-1"
        )
        self.assertIsInstance(second_registration, tuple)
        second_key, _, second_token = second_registration
        self.assertEqual(second_key, key)
        old_snapshot = base_snapshot()
        old_snapshot[300] = process(300, 100, 1005.5, "node /srv/stale-window-mcp.js")

        stale = guard._finalize_start(
            old_snapshot,
            old_snapshot[100],
            201,
            key,
            first_started_at,
            first_token,
            "agent-1",
        )

        self.assertEqual(stale.outcome, "report-only")
        record = self.store.read()["agents"][key]
        self.assertEqual(record["generation"], 2)
        self.assertEqual(record["status"], "starting")
        self.assertEqual(record["pending_token"], second_token)
        self.assertEqual(record["baseline_processes"], [])

    def test_window_delta_identity_overflow_is_never_retained(self) -> None:
        backend = FakeBackend(base_snapshot())
        guard = self.make_guard(backend)
        guard.handle_event(event("SubagentStart"), hook_pid=201)
        for index in range(257):
            pid = 1000 + index
            backend.processes[pid] = process(
                pid, 100, 1006.0, f"node /srv/server-{index}-mcp.js"
            )
        self.now = 1010.0

        stopped = guard.handle_event(event("SubagentStop"), hook_pid=201)

        self.assertEqual(stopped.outcome, "report-only")
        self.assertEqual(stopped.process_count, 257)
        self.assertIn("exceeds 256", stopped.detail)
        record = self.store.read()["agents"][
            _agent_key("session-1", "agent-1", "turn-1")
        ]
        self.assertEqual(record["processes"], [])
        self.assertEqual(record["evidence_grade"], "ambiguous")

    def test_stop_prunes_old_terminal_records_near_byte_limit(self) -> None:
        backend = FakeBackend(base_snapshot())
        guard = self.make_guard(backend)
        guard.handle_event(event("SubagentStart"), hook_pid=201)
        state = self.store.read()
        for index in range(499):
            state["agents"][f"old-terminal-{index}"] = {
                "status": "completed-report-only",
                "stopped_event_at": float(index),
                "detail": "x" * 4100,
                "processes": [],
            }
        self.store.write(state)
        initial_size = self.store.path.stat().st_size
        self.assertGreater(initial_size, 1_500_000)
        for index in range(256):
            pid = 2000 + index
            backend.processes[pid] = process(
                pid, 100, 1006.0, f"node /srv/window-{index}-mcp.js"
            )
        self.now = 1010.0

        stopped = guard.handle_event(event("SubagentStop"), hook_pid=201)

        self.assertEqual(stopped.outcome, "retained-candidate")
        self.assertLess(self.store.path.stat().st_size, MAX_STATE_BYTES)
        restored = self.store.read()["agents"]
        current_key = _agent_key("session-1", "agent-1", "turn-1")
        self.assertEqual(restored[current_key]["status"], "retained-candidate")
        self.assertLess(len(restored), 500)

    def test_repeated_stop_preserves_terminal_result(self) -> None:
        backend = FakeBackend(base_snapshot())
        guard = self.make_guard(backend)
        guard.handle_event(event("SubagentStart"), hook_pid=201)
        add_new_cohort(backend)
        self.now = 1010.0

        first = guard.handle_event(event("SubagentStop"), hook_pid=201)
        second = guard.handle_event(event("SubagentStop"), hook_pid=201)

        self.assertEqual(first, second)
        record = self.store.read()["agents"][
            _agent_key("session-1", "agent-1", "turn-1")
        ]
        self.assertEqual(record["status"], "retained-candidate")

    def test_turn_id_prevents_cross_turn_idempotency(self) -> None:
        backend = FakeBackend(base_snapshot())
        guard = self.make_guard(backend)

        first = guard.handle_event(
            event("SubagentStart", turn_id="turn-1"), hook_pid=201
        )
        self.now = 1006.0
        second = guard.handle_event(
            event("SubagentStart", turn_id="turn-2"), hook_pid=201
        )

        self.assertEqual(first.outcome, "baseline-recorded")
        self.assertEqual(second.outcome, "baseline-recorded")
        self.assertEqual(len(self.store.read()["agents"]), 2)

    def test_stale_observation_is_replaced_instead_of_idempotent_forever(self) -> None:
        backend = FakeBackend(base_snapshot())
        guard = self.make_guard(backend)
        guard.handle_event(event("SubagentStart"), hook_pid=201)
        self.now = 1066.0

        restarted = guard.handle_event(event("SubagentStart"), hook_pid=201)

        self.assertEqual(restarted.outcome, "baseline-recorded")
        record = self.store.read()["agents"][
            _agent_key("session-1", "agent-1", "turn-1")
        ]
        self.assertEqual(record["generation"], 2)
        self.assertTrue(
            any(
                item["event"] == "observation-expired"
                for item in self.store.read()["history"]
            )
        )

    def test_stale_pending_start_uses_the_short_pending_deadline(self) -> None:
        stale_key = _agent_key("session-1", "stale-agent", "turn-1")
        state = {
            "version": 2,
            "agents": {
                stale_key: {
                    "session_id": "session-1",
                    "turn_id": "turn-1",
                    "agent_id": "stale-agent",
                    "status": "starting",
                    "evidence_model": "snapshot-window-v2",
                    "started_event_at": 900.0,
                    "baseline_processes": [],
                    "processes": [],
                }
            },
            "history": [],
        }
        self.store.write(state)
        guard = self.make_guard(FakeBackend(base_snapshot()))

        guard.handle_event(event("SubagentStart"), hook_pid=201)

        stale = self.store.read()["agents"][stale_key]
        self.assertEqual(stale["status"], "abandoned")
        self.assertIn("expired", stale["detail"])

    def test_stop_after_observation_deadline_is_report_only(self) -> None:
        backend = FakeBackend(base_snapshot())
        guard = self.make_guard(backend)
        guard.handle_event(event("SubagentStart"), hook_pid=201)
        add_new_cohort(backend)
        self.now = 1066.0

        stopped = guard.handle_event(event("SubagentStop"), hook_pid=201)

        self.assertEqual(stopped.outcome, "report-only")
        self.assertIn("expired", stopped.detail)

    def test_snapshot_failure_records_sanitized_health_event(self) -> None:
        guard = self.make_guard(FailingBackend())

        result = guard.handle_event(event("SubagentStart"), hook_pid=201)

        self.assertEqual(result.outcome, "report-only")
        self.assertIn("TimeoutExpired", result.detail)
        history = self.store.read()["history"]
        self.assertEqual(history[-1]["event"], "snapshot-failed")
        self.assertNotIn("detail", history[-1])

    def test_malformed_terminal_count_keeps_duplicate_stop_idempotent(self) -> None:
        state = {
            "version": 2,
            "agents": {
                _agent_key("session-1", "agent-1", "turn-1"): {
                    "status": "retained-candidate",
                    "live_process_count": "bad",
                    "detail": "historical terminal result",
                    "processes": [],
                }
            },
            "history": [],
        }
        self.store.write(state)
        guard = self.make_guard(FakeBackend(base_snapshot()))

        stopped = guard.handle_event(event("SubagentStop"), hook_pid=201)

        self.assertEqual(stopped.outcome, "retained-candidate")
        self.assertEqual(stopped.process_count, 0)

    def test_host_not_found_is_visible_in_history(self) -> None:
        snapshot = {201: process(201, 1, 1004, "python hook.py")}
        guard = self.make_guard(FakeBackend(snapshot))

        result = guard.handle_event(event("SubagentStart"), hook_pid=201)

        self.assertEqual(result.outcome, "report-only")
        self.assertEqual(self.store.read()["history"][-1]["event"], "host-not-found")

    def test_composite_key_is_not_vulnerable_to_colon_collisions(self) -> None:
        self.assertNotEqual(
            _agent_key("a:b", "c", "turn"), _agent_key("a", "b:c", "turn")
        )

    def test_identifier_length_is_bounded(self) -> None:
        guard = self.make_guard(FakeBackend(base_snapshot()))
        oversized = event("SubagentStart", agent_id="a" * 257)
        with self.assertRaisesRegex(ValueError, "exceeds"):
            guard.handle_event(oversized, hook_pid=201)


if __name__ == "__main__":
    unittest.main()
