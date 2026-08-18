from __future__ import annotations

import json
import math
import os
import secrets
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from .config import GuardConfig, load_config
from .models import ProcessInfo
from .processes import (
    ProcessBackend,
    SystemProcessBackend,
    ancestry,
    candidate_roots,
    classify_mcp_root,
    cluster_processes,
    find_codex_host,
    process_group_rss_bytes,
)
from .state import StateStore

SUPPORTED_EVENTS = {"SubagentStart", "SubagentStop"}
MAX_IDENTIFIER_LENGTH = 256
MAX_BASELINE_PROCESSES = 256
EVIDENCE_MODEL = "snapshot-window-v2"


@dataclass(frozen=True)
class GuardResult:
    event: str
    outcome: str
    agent_id: str | None = None
    process_count: int = 0
    detail: str = ""


class Guard:
    """Compare bounded lifecycle snapshots without claiming process ownership."""

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
        turn_id = _optional_string(event, "turn_id", MAX_IDENTIFIER_LENGTH)
        receipt_at = self.clock()
        pending_key: str | None = None
        pending_started_at: float | None = None
        pending_token: str | None = None
        if event_name == "SubagentStart":
            registration = self._register_start_pending(
                event, session_id, turn_id, agent_id
            )
            if isinstance(registration, GuardResult):
                return registration
            pending_key, pending_started_at, pending_token = registration
        try:
            snapshot = self.backend.snapshot()
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            detail = f"process snapshot unavailable ({type(error).__name__})"
            if pending_key is not None:
                return self._finish_pending_start(
                    pending_key,
                    pending_token,
                    pending_started_at,
                    agent_id,
                    detail,
                    event_name="snapshot-failed",
                )
            result = GuardResult(
                str(event_name), "report-only", agent_id, detail=detail
            )
            with self.store.locked() as state:
                _record_history(
                    state,
                    "snapshot-failed",
                    {**asdict(result), "turn_id": turn_id},
                    at=receipt_at,
                )
            return result
        current_pid = os.getpid() if hook_pid is None else hook_pid
        codex_host = find_codex_host(snapshot, current_pid)
        if codex_host is None:
            if pending_key is not None:
                return self._finish_pending_start(
                    pending_key,
                    pending_token,
                    pending_started_at,
                    agent_id,
                    "Codex parent not found",
                    event_name="host-not-found",
                )
            result = GuardResult(
                str(event_name),
                "report-only",
                agent_id,
                detail="Codex parent not found",
            )
            with self.store.locked() as state:
                _record_history(
                    state,
                    "host-not-found",
                    {**asdict(result), "turn_id": turn_id},
                    at=receipt_at,
                )
            return result

        if event_name == "SubagentStart":
            assert pending_key is not None
            assert pending_started_at is not None
            assert pending_token is not None
            return self._finalize_start(
                snapshot,
                codex_host,
                current_pid,
                pending_key,
                pending_started_at,
                pending_token,
                agent_id,
            )
        return self._handle_stop(
            snapshot,
            codex_host,
            current_pid,
            session_id,
            turn_id,
            agent_id,
        )

    def _register_start_pending(
        self,
        event: Mapping[str, Any],
        session_id: str,
        turn_id: str | None,
        agent_id: str,
    ) -> tuple[str, float, str] | GuardResult:
        key = _agent_key(session_id, agent_id, turn_id)
        with self.store.locked() as state:
            registered_at = self.clock()
            _expire_stale_observations(
                state,
                registered_at,
                self.config.max_observation_seconds,
                self.config.max_pending_seconds,
            )
            existing = state["agents"].get(key)
            if isinstance(existing, Mapping) and existing.get("status") in {
                "starting",
                "observing",
            }:
                return GuardResult(
                    "SubagentStart",
                    "idempotent",
                    agent_id,
                    _safe_nonnegative_int(existing.get("baseline_process_count")),
                    "observation start already registered",
                )
            generation = (
                _safe_nonnegative_int(existing.get("generation")) + 1
                if isinstance(existing, Mapping)
                else 1
            )
            pending_token = secrets.token_hex(16)
            record = {
                "session_id": session_id,
                "turn_id": turn_id,
                "agent_id": agent_id,
                "agent_type": _optional_string(event, "agent_type", 128),
                "generation": generation,
                "status": "starting",
                "evidence_grade": "none",
                "evidence_model": EVIDENCE_MODEL,
                "started_event_at": registered_at,
                "pending_token": pending_token,
                "baseline_process_count": 0,
                "baseline_processes": [],
                "processes": [],
                "detail": "start snapshot pending",
            }
            state["agents"][key] = record
            _record_history(state, "start-pending", record, at=registered_at)
        return key, registered_at, pending_token

    def _finalize_start(
        self,
        snapshot: Mapping[int, ProcessInfo],
        codex_host: ProcessInfo,
        hook_pid: int,
        key: str,
        receipt_at: float,
        pending_token: str,
        agent_id: str,
    ) -> GuardResult:
        excluded_pids = {process.pid for process in ancestry(snapshot, hook_pid)}
        classified = candidate_roots(snapshot, codex_host.pid, excluded_pids)
        baseline = [
            process.identity(kind)
            for process, kind in classified[: MAX_BASELINE_PROCESSES + 1]
        ]

        with self.store.locked() as state:
            record = state["agents"].get(key)
            if not isinstance(record, dict):
                return GuardResult(
                    "SubagentStart",
                    "report-only",
                    agent_id,
                    detail="pending start record disappeared before baseline commit",
                )
            if not _pending_matches(record, pending_token, receipt_at):
                return GuardResult(
                    "SubagentStart",
                    "report-only",
                    agent_id,
                    detail=str(
                        record.get("detail")
                        or "pending start ended before baseline commit"
                    ),
                )
            overflow = len(baseline) > MAX_BASELINE_PROCESSES
            record.update(
                {
                    "status": "completed-report-only" if overflow else "observing",
                    "evidence_grade": "none" if overflow else "baseline-only",
                    "codex_host": _host_identity(codex_host),
                    "baseline_process_count": len(classified),
                    "baseline_processes": [] if overflow else baseline,
                    "detail": (
                        f"baseline exceeds {MAX_BASELINE_PROCESSES} helper roots"
                        if overflow
                        else "baseline snapshot recorded; attribution deferred until stop"
                    ),
                }
            )
            record.pop("pending_token", None)
            if overflow:
                record["stopped_event_at"] = receipt_at
            _record_history(state, "start-baseline", record, at=receipt_at)

        return GuardResult(
            "SubagentStart",
            "report-only" if overflow else "baseline-recorded",
            agent_id,
            len(classified),
            str(record["detail"]),
        )

    def _finish_pending_start(
        self,
        key: str,
        pending_token: str | None,
        pending_started_at: float | None,
        agent_id: str,
        detail: str,
        *,
        event_name: str,
    ) -> GuardResult:
        now = self.clock()
        with self.store.locked() as state:
            record = state["agents"].get(key)
            if (
                isinstance(record, dict)
                and pending_token is not None
                and pending_started_at is not None
                and _pending_matches(
                    record,
                    pending_token,
                    pending_started_at,
                )
            ):
                record["status"] = "completed-report-only"
                record["evidence_grade"] = "none"
                record["stopped_event_at"] = now
                record["detail"] = detail
                record.pop("pending_token", None)
                _record_history(state, event_name, record, at=now)
            elif isinstance(record, Mapping):
                detail = "stale start completion ignored"
                _record_history(
                    state,
                    "stale-start-completion",
                    {"agent_id": agent_id, "status": "report-only"},
                    at=now,
                )
            else:
                _record_history(
                    state,
                    event_name,
                    {"agent_id": agent_id, "status": "report-only"},
                    at=now,
                )
        return GuardResult("SubagentStart", "report-only", agent_id, detail=detail)

    def _handle_stop(
        self,
        snapshot: Mapping[int, ProcessInfo],
        codex_host: ProcessInfo,
        hook_pid: int,
        session_id: str,
        turn_id: str | None,
        agent_id: str,
    ) -> GuardResult:
        now = self.clock()
        key = _agent_key(session_id, agent_id, turn_id)
        excluded_pids = {process.pid for process in ancestry(snapshot, hook_pid)}
        with self.store.locked() as state:
            _expire_stale_observations(
                state,
                now,
                self.config.max_observation_seconds,
                self.config.max_pending_seconds,
            )
            record = state["agents"].get(key)
            if not isinstance(record, Mapping):
                result = GuardResult(
                    "SubagentStop",
                    "report-only",
                    agent_id,
                    detail="no matching start baseline",
                )
                _record_history(
                    state,
                    "stop-without-record",
                    {**asdict(result), "turn_id": turn_id},
                    at=now,
                )
                return result

            if record.get("status") == "starting":
                return _finish_report_only(
                    state,
                    record,
                    agent_id,
                    now,
                    "stop arrived before the start baseline was committed",
                )
            if record.get("status") != "observing":
                return _terminal_result(record, agent_id)

            if (
                record.get("session_id") != session_id
                or record.get("agent_id") != agent_id
                or record.get("turn_id") != turn_id
            ):
                return _finish_verification_failure(
                    state,
                    record,
                    agent_id,
                    now,
                    "stored lifecycle identifiers do not match",
                )

            host_error = _verify_host(codex_host, record.get("codex_host"))
            if host_error:
                return _finish_verification_failure(
                    state, record, agent_id, now, host_error
                )

            baseline = _identity_keys(record.get("baseline_processes", []))
            classified = candidate_roots(snapshot, codex_host.pid, excluded_pids)
            started_at = _finite_float(record.get("started_event_at"), now)
            candidates = [
                process
                for process, _ in classified
                if _process_identity_key(process) not in baseline
                and started_at - self.config.future_clock_skew_seconds
                <= process.started_at
                <= now + self.config.future_clock_skew_seconds
            ]
            if len(candidates) > MAX_BASELINE_PROCESSES:
                record["processes"] = []
                record["live_process_count"] = len(candidates)
                record["live_group_rss_bytes"] = None
                return _finish_report_only(
                    state,
                    record,
                    agent_id,
                    now,
                    f"window delta exceeds {MAX_BASELINE_PROCESSES} helper roots",
                )
            cohorts = cluster_processes(candidates, self.config.cohort_window_seconds)
            identities = [
                process.identity(classify_mcp_root(process) or "unknown")
                for process in candidates
            ]
            record["stopped_event_at"] = now
            record["processes"] = identities
            record["live_process_count"] = len(candidates)
            record["baseline_processes"] = []

            rss_values = [
                process_group_rss_bytes(snapshot, process) for process in candidates
            ]
            record["live_group_rss_bytes"] = (
                sum(int(value) for value in rss_values if value is not None)
                if rss_values and all(value is not None for value in rss_values)
                else None
            )

            if not cohorts:
                return _finish_report_only(
                    state,
                    record,
                    agent_id,
                    now,
                    "no new MCP helper roots appeared between lifecycle snapshots",
                )
            if len(cohorts) != 1:
                return _finish_report_only(
                    state,
                    record,
                    agent_id,
                    now,
                    f"{len(cohorts)} helper cohorts appeared during the observation window",
                )

            cohort = cohorts[0]
            fingerprints = [process.fingerprint for process in cohort.processes]
            if len(fingerprints) != len(set(fingerprints)):
                return _finish_report_only(
                    state,
                    record,
                    agent_id,
                    now,
                    "window cohort contains duplicate helper signatures",
                )

            overlap_count = _overlapping_observation_count(
                state,
                key,
                record,
                cohort.processes,
                now,
                self.config.ambiguity_margin_seconds,
            )
            if overlap_count:
                return _finish_report_only(
                    state,
                    record,
                    agent_id,
                    now,
                    f"candidate timing overlaps {overlap_count} other subagent observation(s)",
                )

            record["status"] = "retained-candidate"
            record["evidence_grade"] = "window-delta"
            record["detail"] = (
                "helper cohort appeared between lifecycle snapshots and remained live "
                "at stop; ownership is unproven and no signal was sent"
            )
            _record_history(state, "stop-audit", record, at=now)
            return GuardResult(
                "SubagentStop",
                "retained-candidate",
                agent_id,
                len(candidates),
                str(record["detail"]),
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


def _agent_key(session_id: str, agent_id: str, turn_id: str | None = None) -> str:
    parts = (
        [session_id, agent_id] if turn_id is None else [session_id, turn_id, agent_id]
    )
    return json.dumps(parts, ensure_ascii=True, separators=(",", ":"))


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


def _same_host(left: object, right: object) -> bool:
    if not isinstance(left, Mapping) or not isinstance(right, Mapping):
        return False
    try:
        return (
            int(left["pid"]) == int(right["pid"])
            and abs(float(left["started_at"]) - float(right["started_at"])) <= 0.01
            and left["command_sha256"] == right["command_sha256"]
        )
    except (KeyError, TypeError, ValueError):
        return False


def _pending_matches(
    record: Mapping[str, Any], pending_token: str, started_at: float
) -> bool:
    token = record.get("pending_token")
    return (
        record.get("status") == "starting"
        and isinstance(token, str)
        and secrets.compare_digest(token, pending_token)
        and abs(
            _finite_float(record.get("started_event_at"), float("inf")) - started_at
        )
        <= 0.000001
    )


def _process_identity_key(process: ProcessInfo) -> tuple[int, float, str]:
    return process.pid, round(process.started_at, 3), process.fingerprint


def _identity_keys(raw: object) -> set[tuple[int, float, str]]:
    result: set[tuple[int, float, str]] = set()
    if not isinstance(raw, list):
        return result
    for identity in raw:
        if not isinstance(identity, Mapping):
            continue
        try:
            result.add(
                (
                    int(identity["pid"]),
                    round(float(identity["started_at"]), 3),
                    str(identity["command_sha256"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return result


def _overlapping_observation_count(
    state: Mapping[str, Any],
    current_key: str,
    current_record: Mapping[str, Any],
    candidates: tuple[ProcessInfo, ...],
    now: float,
    margin: float,
) -> int:
    agents = state.get("agents", {})
    if not isinstance(agents, Mapping):
        return 0
    overlaps = 0
    for key, other in agents.items():
        if (
            key == current_key
            or not isinstance(other, Mapping)
            or other.get("evidence_model") != EVIDENCE_MODEL
        ):
            continue
        other_host = other.get("codex_host")
        if other_host is not None and not _same_host(
            current_record.get("codex_host"), other_host
        ):
            continue
        if other_host is None and other.get("status") != "starting":
            continue
        other_start = _finite_float(other.get("started_event_at"), float("inf"))
        other_end = _finite_float(other.get("stopped_event_at"), now)
        if any(
            other_start - margin <= process.started_at <= other_end + margin
            for process in candidates
        ):
            overlaps += 1
    return overlaps


def _expire_stale_observations(
    state: dict[str, Any],
    now: float,
    maximum_age: float,
    pending_maximum_age: float,
    *,
    except_key: str | None = None,
) -> None:
    agents = state.get("agents", {})
    if not isinstance(agents, dict):
        return
    for key, record in agents.items():
        if (
            key == except_key
            or not isinstance(record, dict)
            or record.get("status") not in {"starting", "observing"}
        ):
            continue
        started_at = _finite_float(record.get("started_event_at"), now)
        allowed_age = (
            pending_maximum_age if record.get("status") == "starting" else maximum_age
        )
        if now - started_at <= allowed_age:
            continue
        record["status"] = "abandoned"
        record["evidence_grade"] = "none"
        record["stopped_event_at"] = now
        record["baseline_processes"] = []
        record["detail"] = "observation expired without a matching stop event"
        _record_history(state, "observation-expired", record, at=now)


def _finish_report_only(
    state: dict[str, Any],
    record: Mapping[str, Any],
    agent_id: str,
    now: float,
    detail: str,
) -> GuardResult:
    assert isinstance(record, dict)
    record["status"] = "completed-report-only"
    record["evidence_grade"] = "ambiguous"
    record["detail"] = detail
    record["stopped_event_at"] = now
    record["baseline_processes"] = []
    _record_history(state, "stop-report-only", record, at=now)
    return GuardResult(
        "SubagentStop",
        "report-only",
        agent_id,
        _safe_nonnegative_int(
            record.get("live_process_count", len(record.get("processes", [])))
        ),
        detail,
    )


def _finish_verification_failure(
    state: dict[str, Any],
    record: Mapping[str, Any],
    agent_id: str,
    now: float,
    detail: str,
) -> GuardResult:
    assert isinstance(record, dict)
    record["status"] = "verification-failed"
    record["evidence_grade"] = "invalid"
    record["detail"] = detail
    record["stopped_event_at"] = now
    record["baseline_processes"] = []
    _record_history(state, "stop-verification-failed", record, at=now)
    return GuardResult("SubagentStop", "skipped", agent_id, detail=detail)


def _terminal_result(record: Mapping[str, Any], agent_id: str) -> GuardResult:
    status = str(record.get("status") or "completed-report-only")
    if status == "retained-candidate":
        outcome = status
    elif status == "verification-failed":
        outcome = "skipped"
    else:
        outcome = "report-only"
    count = _safe_nonnegative_int(
        record.get("live_process_count", len(record.get("processes", [])))
    )
    return GuardResult(
        "SubagentStop",
        outcome,
        agent_id,
        count,
        str(record.get("detail") or "terminal lifecycle result already recorded"),
    )


def _finite_float(value: object, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _safe_nonnegative_int(value: object) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def _record_history(
    state: dict[str, Any],
    event: str,
    payload: Mapping[str, Any],
    *,
    at: float | None = None,
) -> None:
    process_count = payload.get("process_count")
    if not isinstance(process_count, int):
        processes = payload.get("processes", [])
        process_count = len(processes) if isinstance(processes, list) else 0
    state.setdefault("history", []).append(
        {
            "event": event,
            "at": time.time() if at is None else at,
            "agent_id": payload.get("agent_id"),
            "turn_id": payload.get("turn_id"),
            "generation": payload.get("generation"),
            "status": payload.get("status") or payload.get("outcome"),
            "process_count": process_count,
        }
    )
