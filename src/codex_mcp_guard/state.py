from __future__ import annotations

import errno
import json
import math
import os
import stat
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

STATE_VERSION = 2
MAX_STATE_BYTES = 2 * 1024 * 1024
MAX_AGENT_RECORDS = 500
MAX_HISTORY_RECORDS = 200
MAX_IDENTITIES_PER_RECORD = 256
LOCK_TIMEOUT_SECONDS = 1.5
LOCK_POLL_SECONDS = 0.01


def default_state_dir() -> Path:
    configured = os.environ.get("CODEX_MCP_GUARD_STATE")
    return (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".codex-mcp-guard"
    )


def empty_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "agents": {}, "history": []}


class StateStore:
    def __init__(self, root: Path | None = None) -> None:
        selected = (root or default_state_dir()).expanduser()
        if not selected.is_absolute():
            raise ValueError("state directory must be an absolute path")
        self.root = selected
        self.path = self.root / "state.json"
        self.lock_path = self.root / "state.lock"

    @contextmanager
    def locked(self) -> Iterator[dict[str, Any]]:
        self._ensure_root(create=True)
        descriptor = _open_nofollow(
            self.lock_path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600
        )
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "a+b") as lock_file:
            _validate_private_file(lock_file, self.lock_path)
            _lock(lock_file, LOCK_TIMEOUT_SECONDS)
            try:
                state = self.read()
                yield state
                self.write(state)
            finally:
                _unlock(lock_file)

    def read(self) -> dict[str, Any]:
        try:
            self.root.lstat()
        except FileNotFoundError:
            return empty_state()
        self._ensure_root(create=False)
        try:
            descriptor = _open_nofollow(self.path, os.O_RDONLY, 0o600)
        except FileNotFoundError:
            return empty_state()
        with os.fdopen(descriptor, "rb") as state_file:
            file_stat = _validate_private_file(state_file, self.path)
            if file_stat.st_size > MAX_STATE_BYTES:
                raise ValueError(f"state file exceeds {MAX_STATE_BYTES} bytes")
            try:
                state = json.load(state_file)
            except json.JSONDecodeError as error:
                raise ValueError(f"state file is not valid JSON: {error}") from error
        return _normalize_state(state)

    def write(self, state: dict[str, Any]) -> None:
        self._ensure_root(create=True)
        normalized = _normalize_state(state)
        _prune_state(normalized)
        encoded = _encode_state(normalized)
        if len(encoded) > MAX_STATE_BYTES:
            _prune_to_byte_budget(normalized)
            encoded = _encode_state(normalized)
        if len(encoded) > MAX_STATE_BYTES:
            raise ValueError(f"state file would exceed {MAX_STATE_BYTES} bytes")

        descriptor, temporary = tempfile.mkstemp(
            prefix="state.", suffix=".tmp", dir=self.root
        )
        try:
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            if os.name != "nt":
                os.chmod(self.path, 0o600)
                _fsync_directory(self.root)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _ensure_root(self, *, create: bool) -> None:
        try:
            root_stat = self.root.lstat()
        except FileNotFoundError:
            if not create:
                return
            self.root.mkdir(parents=True, mode=0o700)
            root_stat = self.root.lstat()
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            raise ValueError(
                f"state directory must be a non-symlink directory: {self.root}"
            )
        if hasattr(os, "getuid") and root_stat.st_uid != os.getuid():
            raise ValueError(
                f"state directory is not owned by the current user: {self.root}"
            )
        if os.name != "nt" and stat.S_IMODE(root_stat.st_mode) & 0o077:
            raise ValueError(f"state directory must have mode 0700: {self.root}")


def _normalize_state(state: Any) -> dict[str, Any]:
    if not isinstance(state, dict):
        raise TypeError("state file must contain a JSON object")
    version = state.get("version")
    if version == 1:
        state = _migrate_v1(state)
    elif version != STATE_VERSION:
        raise ValueError(f"unsupported state version: {version!r}")

    agents = state.setdefault("agents", {})
    history = state.setdefault("history", [])
    if not isinstance(agents, dict) or not isinstance(history, list):
        raise TypeError("state agents/history fields have invalid types")

    cleaned_agents: dict[str, dict[str, Any]] = {}
    dropped = 0
    for key, raw_record in agents.items():
        if not isinstance(key, str) or not isinstance(raw_record, dict):
            dropped += 1
            continue
        record = dict(raw_record)
        repaired = False
        for field in ("baseline_processes", "processes"):
            if field not in record:
                continue
            value = record[field]
            if not isinstance(value, list):
                record[field] = []
                repaired = True
                continue
            identities = [item for item in value if isinstance(item, Mapping)]
            if (
                len(identities) != len(value)
                or len(identities) > MAX_IDENTITIES_PER_RECORD
            ):
                record[field] = identities[:MAX_IDENTITIES_PER_RECORD]
                repaired = True
        if not isinstance(record.get("status"), str):
            record["status"] = "completed-report-only"
            repaired = True
        if repaired:
            record["status"] = "completed-report-only"
            record["evidence_grade"] = "invalid"
            record["baseline_processes"] = []
            record["processes"] = []
            record["live_process_count"] = 0
            record["live_group_rss_bytes"] = None
            record["detail"] = "invalid observation record was downgraded"
        cleaned_agents[key] = record

    cleaned_history = [item for item in history if isinstance(item, dict)]
    dropped_history = len(history) - len(cleaned_history)
    if dropped or dropped_history:
        cleaned_history.append(
            {
                "event": "state-repair",
                "at": time.time(),
                "status": "dropped-invalid-records",
                "process_count": 0,
                "dropped_agent_records": dropped,
                "dropped_history_records": dropped_history,
            }
        )
    state["version"] = STATE_VERSION
    state["agents"] = cleaned_agents
    state["history"] = cleaned_history
    return state


def _migrate_v1(state: dict[str, Any]) -> dict[str, Any]:
    migrated = empty_state()
    raw_agents = state.get("agents", {})
    if isinstance(raw_agents, dict):
        for key, raw_record in raw_agents.items():
            if not isinstance(key, str) or not isinstance(raw_record, dict):
                continue
            processes = raw_record.get("processes", [])
            process_count = len(processes) if isinstance(processes, list) else 0
            migrated["agents"][key] = {
                "session_id": raw_record.get("session_id"),
                "turn_id": raw_record.get("turn_id"),
                "agent_id": raw_record.get("agent_id"),
                "agent_type": raw_record.get("agent_type"),
                "generation": raw_record.get("generation", 1),
                "status": "legacy-report-only",
                "evidence_grade": "retired",
                "evidence_model": "transcript-clock-v1-retired",
                "started_event_at": raw_record.get("started_event_at"),
                "stopped_event_at": raw_record.get("stopped_event_at"),
                "legacy_process_count": process_count,
                "baseline_processes": [],
                "processes": [],
                "detail": (
                    "v1 transcript-clock evidence was retired; record a fresh "
                    "start/stop observation"
                ),
            }
    raw_history = state.get("history", [])
    if isinstance(raw_history, list):
        migrated["history"] = [item for item in raw_history if isinstance(item, dict)]
    provenance_times: list[float] = []
    for item in migrated["history"]:
        try:
            value = float(item.get("at", 0.0))
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            provenance_times.append(value)
    migrated["history"].append(
        {
            "event": "state-migrated",
            "at": max(provenance_times, default=0.0),
            "migration_observed_at": time.time(),
            "status": "v1-evidence-retired",
            "process_count": 0,
        }
    )
    return migrated


def _prune_state(state: dict[str, Any]) -> None:
    history = state.get("history", [])
    state["history"] = (
        list(history)[-MAX_HISTORY_RECORDS:] if isinstance(history, list) else []
    )
    agents = state.get("agents", {})
    if not isinstance(agents, dict) or len(agents) <= MAX_AGENT_RECORDS:
        return
    active = [
        (key, record)
        for key, record in agents.items()
        if isinstance(record, dict)
        and record.get("status") in {"starting", "observing"}
    ]
    if len(active) > MAX_AGENT_RECORDS:
        raise ValueError(f"active observations exceed {MAX_AGENT_RECORDS} records")
    terminal = [
        (key, record)
        for key, record in agents.items()
        if not (
            isinstance(record, dict)
            and record.get("status") in {"starting", "observing"}
        )
    ]
    terminal.sort(key=lambda item: _record_timestamp(item[1]), reverse=True)
    selected = active + terminal[: MAX_AGENT_RECORDS - len(active)]
    state["agents"] = dict(selected)


def _prune_to_byte_budget(state: dict[str, Any]) -> None:
    agents = state.get("agents", {})
    if not isinstance(agents, dict):
        return
    active_statuses = {"starting", "observing"}
    active = [
        (key, record)
        for key, record in agents.items()
        if isinstance(record, Mapping) and record.get("status") in active_statuses
    ]
    terminal = [
        (key, record)
        for key, record in agents.items()
        if not (isinstance(record, Mapping) and record.get("status") in active_statuses)
    ]
    terminal.sort(key=lambda item: _record_timestamp(item[1]), reverse=True)
    minimum_terminal = 1 if terminal and not active else 0

    selected_count = _maximum_terminal_count(state, active, terminal, minimum_terminal)
    if selected_count is None:
        state["history"] = []
        selected_count = _maximum_terminal_count(
            state, active, terminal, minimum_terminal
        )
    keep = minimum_terminal if selected_count is None else selected_count
    state["agents"] = dict(active + terminal[:keep])


def _maximum_terminal_count(
    state: dict[str, Any],
    active: list[tuple[str, object]],
    terminal: list[tuple[str, object]],
    minimum: int,
) -> int | None:
    low = minimum
    high = len(terminal)
    best: int | None = None
    while low <= high:
        middle = (low + high) // 2
        state["agents"] = dict(active + terminal[:middle])
        if len(_encode_state(state)) <= MAX_STATE_BYTES:
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    return best


def _encode_state(state: Mapping[str, Any]) -> bytes:
    rendered = (
        json.dumps(state, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    )
    return rendered.encode("utf-8")


def _record_timestamp(record: object) -> float:
    if not isinstance(record, Mapping):
        return 0.0
    for field in ("stopped_event_at", "started_event_at"):
        try:
            value = float(record.get(field, 0.0))
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            return value
    return 0.0


def _open_nofollow(path: Path, flags: int, mode: int) -> int:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        path_stat = None
    if path_stat is not None and stat.S_ISLNK(path_stat.st_mode):
        raise ValueError(f"refusing symlinked state file: {path}")
    return os.open(
        path,
        flags | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )


def _validate_private_file(file_object: BinaryIO, path: Path) -> os.stat_result:
    file_stat = os.fstat(file_object.fileno())
    if not stat.S_ISREG(file_stat.st_mode):
        raise ValueError(f"state file must be regular: {path}")
    if hasattr(os, "getuid") and file_stat.st_uid != os.getuid():
        raise ValueError(f"state file is not owned by the current user: {path}")
    if os.name != "nt" and stat.S_IMODE(file_stat.st_mode) & 0o077:
        raise ValueError(f"state file must have mode 0600: {path}")
    return file_stat


def _lock(file_object: BinaryIO, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    if os.name == "nt":
        import msvcrt

        file_object.seek(0, os.SEEK_END)
        if file_object.tell() == 0:
            file_object.write(b"0")
            file_object.flush()
        file_object.seek(0)
        while True:
            try:
                msvcrt.locking(file_object.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError as error:
                if time.monotonic() >= deadline:
                    raise TimeoutError("state lock acquisition timed out") from error
                time.sleep(LOCK_POLL_SECONDS)
    else:
        import fcntl

        while True:
            try:
                fcntl.flock(file_object.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError("state lock acquisition timed out") from error
                time.sleep(LOCK_POLL_SECONDS)


def _unlock(file_object: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        file_object.seek(0)
        msvcrt.locking(file_object.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(file_object.fileno(), fcntl.LOCK_UN)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
