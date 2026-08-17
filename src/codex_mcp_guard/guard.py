from __future__ import annotations

import json
import os
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import GuardConfig, load_config
from .models import ProcessCohort, ProcessInfo
from .processes import (
    ProcessBackend,
    SystemProcessBackend,
    ancestry,
    candidate_roots,
    classify_mcp_root,
    cluster_processes,
    find_codex_host,
    verify_identity,
)
from .state import StateStore

SUPPORTED_EVENTS = {"SubagentStart", "SubagentStop"}
MAX_IDENTIFIER_LENGTH = 256


@dataclass(frozen=True)
class GuardResult:
    event: str
    outcome: str
    agent_id: str | None = None
    process_count: int = 0
    detail: str = ""


class Guard:
    """Correlate lifecycle events with process snapshots without sending signals."""

    def __init__(
        self,
        *,
        backend: ProcessBackend | None = None,
        store: StateStore | None = None,
        config: GuardConfig | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.backend = backend or SystemProcessBackend()
        self.store = store or StateStore()
        self.config = config or load_config(self.store.root)
        self.config.validate()
        self.clock = clock

    def handle_event(
        self, event: Mapping[str, Any], hook_pid: int | None = None
    ) -> GuardResult:
        if not isinstance(event, Mapping):
            raise TypeError("hook event must be a JSON object")
        event_name = event.get("hook_event_name")
        if event_name not in SUPPORTED_EVENTS:
            return GuardResult(
                str(event_name), "ignored", detail="unsupported hook event"
            )

        agent_id = _required_string(event, "agent_id")
        session_id = _required_string(event, "session_id")
        snapshot = self.backend.snapshot()
        current_pid = os.getpid() if hook_pid is None else hook_pid
        codex_host = find_codex_host(snapshot, current_pid)
        if codex_host is None:
            return GuardResult(
                str(event_name),
                "report-only",
                agent_id,
                detail="Codex parent not found",
            )

        if event_name == "SubagentStart":
            return self._handle_start(event, snapshot, codex_host, current_pid)
        return self._handle_stop(snapshot, codex_host, session_id, agent_id)

    def _handle_start(
        self,
        event: Mapping[str, Any],
        snapshot: Mapping[int, ProcessInfo],
        codex_host: ProcessInfo,
        hook_pid: int,
    ) -> GuardResult:
        now = self.clock()
        session_id = _required_string(event, "session_id")
        agent_id = _required_string(event, "agent_id")
        key = _agent_key(session_id, agent_id)
        reference_at, reference_source = _event_reference_time(event, now)
        excluded_pids = {process.pid for process in ancestry(snapshot, hook_pid)}

        with self.store.locked() as state:
            existing = state["agents"].get(key)
            if existing and existing.get("status") == "candidate":
                return GuardResult(
                    "SubagentStart",
                    "idempotent",
                    agent_id,
                    len(existing.get("processes", [])),
                    "agent already has an active candidate record",
                )

            claimed = _claimed_process_identities(state)
            classified = candidate_roots(snapshot, codex_host.pid, excluded_pids)
            all_cohorts = cluster_processes(
                [process for process, _ in classified],
                self.config.cohort_window_seconds,
            )
            if reference_source == "transcript-birthtime":
                earliest = reference_at - self.config.max_pre_reference_seconds
                latest = min(
                    now + self.config.future_clock_skew_seconds,
                    reference_at + self.config.max_post_reference_seconds,
                )
            else:
                earliest = now - self.config.lookback_seconds
                latest = now + self.config.future_clock_skew_seconds
            eligible = [
                process
                for process, _ in classified
                if (process.pid, round(process.started_at, 3)) not in claimed
                and earliest <= process.started_at <= latest
            ]
            cohorts = cluster_processes(eligible, self.config.cohort_window_seconds)
            selection, confidence, detail = self._select_cohort(
                cohorts, reference_at, reference_source
            )
            if (
                selection is not None
                and confidence == "correlated"
                and not _has_matching_older_cohort(selection, all_cohorts)
            ):
                confidence = "ambiguous"
                detail = "candidate cohort does not match an older helper cohort"

            generation = int(existing.get("generation", 0)) + 1 if existing else 1
            record = {
                "session_id": session_id,
                "agent_id": agent_id,
                "agent_type": _optional_string(event, "agent_type", 128),
                "generation": generation,
                "status": "candidate" if confidence == "correlated" else "ambiguous",
                "confidence": confidence,
                "started_event_at": now,
                "reference_at": reference_at,
                "reference_source": reference_source,
                "codex_host": _host_identity(codex_host),
                "processes": [],
                "detail": detail,
            }
            if selection is not None:
                record["processes"] = [
                    process.identity(classify_mcp_root(process) or "unknown")
                    for process in selection.processes
                ]
            state["agents"][key] = record
            _record_history(state, "start", record)

        outcome = "candidate-recorded" if confidence == "correlated" else "report-only"
        return GuardResult(
            "SubagentStart",
            outcome,
            agent_id,
            len(record["processes"]),
            detail,
        )

    def _select_cohort(
        self,
        cohorts: list[ProcessCohort],
        reference_at: float,
        reference_source: str,
    ) -> tuple[ProcessCohort | None, str, str]:
        if not cohorts:
            return None, "none", "no unclaimed MCP helper cohort found"
        ranked = sorted(
            ((abs(cohort.started_at - reference_at), cohort) for cohort in cohorts),
            key=lambda item: item[0],
        )
        score, selected = ranked[0]
        fingerprints = [process.fingerprint for process in selected.processes]
        if len(fingerprints) != len(set(fingerprints)):
            return (
                selected,
                "ambiguous",
                "candidate cohort contains duplicate helper signatures",
            )
        if reference_source != "transcript-birthtime":
            return selected, "ambiguous", "trusted transcript birth time unavailable"
        if selected.started_at < reference_at - self.config.max_pre_reference_seconds:
            return selected, "ambiguous", "candidate cohort predates the start window"
        if (
            selected.finished_starting_at
            > reference_at + self.config.max_post_reference_seconds
        ):
            return selected, "ambiguous", "candidate cohort exceeds the start window"
        if (
            len(ranked) > 1
            and ranked[1][0] - score < self.config.ambiguity_margin_seconds
        ):
            return (
                selected,
                "ambiguous",
                "two helper cohorts are equally close to subagent start",
            )
        return (
            selected,
            "correlated",
            "candidate cohort correlates with the start event; ownership is unproven",
        )

    def _handle_stop(
        self,
        snapshot: Mapping[int, ProcessInfo],
        codex_host: ProcessInfo,
        session_id: str,
        agent_id: str,
    ) -> GuardResult:
        now = self.clock()
        key = _agent_key(session_id, agent_id)
        with self.store.locked() as state:
            record = state["agents"].get(key)
            if not record:
                result = GuardResult(
                    "SubagentStop",
                    "report-only",
                    agent_id,
                    detail="no start candidate record",
                )
                _record_history(state, "stop-without-record", asdict(result))
                return result
            if (
                record.get("session_id") != session_id
                or record.get("agent_id") != agent_id
            ):
                record["status"] = "verification-failed"
                record["detail"] = "stored lifecycle identifiers do not match"
                _record_history(state, "stop-verification-failed", record)
                return GuardResult(
                    "SubagentStop", "skipped", agent_id, detail=record["detail"]
                )
            if (
                record.get("status") != "candidate"
                or record.get("confidence") != "correlated"
            ):
                record["status"] = "completed-report-only"
                record["stopped_event_at"] = now
                _record_history(state, "stop-report-only", record)
                return GuardResult(
                    "SubagentStop",
                    "report-only",
                    agent_id,
                    len(record.get("processes", [])),
                    record.get("detail", "candidate correlation was ambiguous"),
                )

            host_error = _verify_host(codex_host, record.get("codex_host"))
            if host_error:
                record["status"] = "verification-failed"
                record["detail"] = host_error
                _record_history(state, "stop-verification-failed", record)
                return GuardResult(
                    "SubagentStop", "skipped", agent_id, detail=host_error
                )

            shared = _shared_identities(state, key)
            verification_errors: list[str] = []
            live_processes: list[int] = []
            exited_processes: list[int] = []
            for identity in record.get("processes", []):
                if not isinstance(identity, Mapping):
                    verification_errors.append("stored process identity is invalid")
                    continue
                try:
                    pid = int(identity["pid"])
                    identity_key = (pid, round(float(identity["started_at"]), 3))
                except (KeyError, TypeError, ValueError):
                    verification_errors.append("stored process identity is invalid")
                    continue
                if identity_key in shared:
                    verification_errors.append(
                        f"PID {pid} is correlated with another active agent"
                    )
                    continue
                error = verify_identity(snapshot.get(pid), identity)
                if error == "process already exited":
                    exited_processes.append(pid)
                elif error:
                    verification_errors.append(f"PID {pid}: {error}")
                else:
                    live_processes.append(pid)

            record["stopped_event_at"] = now
            record["live_process_count"] = len(live_processes)
            record["exited_process_count"] = len(exited_processes)
            if verification_errors:
                detail = "; ".join(verification_errors)
                record["status"] = "verification-failed"
                record["detail"] = detail
                _record_history(state, "stop-verification-failed", record)
                return GuardResult(
                    "SubagentStop",
                    "skipped",
                    agent_id,
                    len(record.get("processes", [])),
                    detail,
                )

            if live_processes:
                record["status"] = "retained-candidate"
                record["detail"] = (
                    "candidate helpers remain live; audit-only, no signal sent"
                )
            else:
                record["status"] = "candidate-exited"
                record["detail"] = "all correlated candidate helpers already exited"
            _record_history(state, "stop-audit", record)
            return GuardResult(
                "SubagentStop",
                record["status"],
                agent_id,
                len(live_processes),
                record["detail"],
            )


def _required_string(event: Mapping[str, Any], key: str) -> str:
    value = event.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"hook event is missing {key}")
    if len(value) > MAX_IDENTIFIER_LENGTH:
        raise ValueError(f"hook event {key} exceeds {MAX_IDENTIFIER_LENGTH} characters")
    if any(ord(character) < 32 for character in value):
        raise ValueError(f"hook event {key} contains control characters")
    return value


def _optional_string(
    event: Mapping[str, Any], key: str, maximum_length: int
) -> str | None:
    value = event.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > maximum_length:
        raise ValueError(
            f"hook event {key} must be a string of at most {maximum_length} characters"
        )
    if any(ord(character) < 32 for character in value):
        raise ValueError(f"hook event {key} contains control characters")
    return value


def _agent_key(session_id: str, agent_id: str) -> str:
    return json.dumps([session_id, agent_id], ensure_ascii=True, separators=(",", ":"))


def _event_reference_time(event: Mapping[str, Any], now: float) -> tuple[float, str]:
    transcript = event.get("agent_transcript_path") or event.get("transcript_path")
    if not isinstance(transcript, str) or not transcript:
        return now, "hook-time"
    path = Path(transcript)
    if not path.is_absolute() or path.suffix.lower() != ".jsonl":
        return now, "hook-time"
    try:
        path_stat = path.lstat()
        if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
            return now, "hook-time"
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            file_stat = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            return now, "hook-time"
        if hasattr(os, "getuid") and file_stat.st_uid != os.getuid():
            return now, "hook-time"
        birthtime = getattr(file_stat, "st_birthtime", None)
        if birthtime and 0 < birthtime <= now + 5.0:
            return float(birthtime), "transcript-birthtime"
        if os.name == "nt" and 0 < file_stat.st_ctime <= now + 5.0:
            return float(file_stat.st_ctime), "transcript-birthtime"
    except OSError:
        pass
    return now, "hook-time"


def _host_identity(process: ProcessInfo) -> dict[str, Any]:
    return {
        "pid": process.pid,
        "ppid": process.ppid,
        "started_at": process.started_at,
        "command_sha256": process.fingerprint,
    }


def _verify_host(
    current: ProcessInfo, expected: Mapping[str, Any] | object
) -> str | None:
    if not isinstance(expected, Mapping):
        return "stored Codex host identity is invalid"
    try:
        expected_pid = int(expected["pid"])
        expected_started_at = float(expected["started_at"])
        expected_fingerprint = expected["command_sha256"]
    except (KeyError, TypeError, ValueError):
        return "stored Codex host identity is invalid"
    if current.pid != expected_pid:
        return "hook is running under a different Codex process"
    if abs(current.started_at - expected_started_at) > 0.01:
        return "Codex PID was reused"
    if current.fingerprint != expected_fingerprint:
        return "Codex command fingerprint changed"
    return None


def _claimed_process_identities(state: Mapping[str, Any]) -> set[tuple[int, float]]:
    claimed: set[tuple[int, float]] = set()
    agents = state.get("agents", {})
    if not isinstance(agents, Mapping):
        return claimed
    for record in agents.values():
        if not isinstance(record, Mapping) or record.get("status") != "candidate":
            continue
        for process in record.get("processes", []):
            if not isinstance(process, Mapping):
                continue
            try:
                claimed.add(
                    (int(process["pid"]), round(float(process["started_at"]), 3))
                )
            except (KeyError, TypeError, ValueError):
                continue
    return claimed


def _shared_identities(
    state: Mapping[str, Any], current_key: str
) -> set[tuple[int, float]]:
    shared: set[tuple[int, float]] = set()
    agents = state.get("agents", {})
    if not isinstance(agents, Mapping):
        return shared
    for key, record in agents.items():
        if (
            key == current_key
            or not isinstance(record, Mapping)
            or record.get("status") != "candidate"
        ):
            continue
        for process in record.get("processes", []):
            if not isinstance(process, Mapping):
                continue
            try:
                shared.add(
                    (int(process["pid"]), round(float(process["started_at"]), 3))
                )
            except (KeyError, TypeError, ValueError):
                continue
    return shared


def _has_matching_older_cohort(
    selected: ProcessCohort, cohorts: list[ProcessCohort]
) -> bool:
    selected_fingerprints = sorted(
        process.fingerprint for process in selected.processes
    )
    selected_pids = {process.pid for process in selected.processes}
    for cohort in cohorts:
        if {process.pid for process in cohort.processes} == selected_pids:
            continue
        if cohort.finished_starting_at >= selected.started_at:
            continue
        if (
            sorted(process.fingerprint for process in cohort.processes)
            == selected_fingerprints
        ):
            return True
    return False


def _record_history(
    state: dict[str, Any], event: str, payload: Mapping[str, Any]
) -> None:
    state.setdefault("history", []).append(
        {
            "event": event,
            "at": time.time(),
            "agent_id": payload.get("agent_id"),
            "generation": payload.get("generation"),
            "status": payload.get("status") or payload.get("outcome"),
            "process_count": len(payload.get("processes", [])),
        }
    )
