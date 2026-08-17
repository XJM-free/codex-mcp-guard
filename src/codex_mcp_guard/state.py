from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

STATE_VERSION = 1
MAX_STATE_BYTES = 2 * 1024 * 1024
MAX_AGENT_RECORDS = 500
MAX_HISTORY_RECORDS = 200


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
            _lock(lock_file)
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
        state["version"] = STATE_VERSION
        state["history"] = list(state.get("history", []))[-MAX_HISTORY_RECORDS:]
        agents = state.get("agents", {})
        if isinstance(agents, dict) and len(agents) > MAX_AGENT_RECORDS:
            state["agents"] = dict(list(agents.items())[-MAX_AGENT_RECORDS:])

        descriptor, temporary = tempfile.mkstemp(
            prefix="state.", suffix=".tmp", dir=self.root
        )
        try:
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(state, output, indent=2, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            if os.name != "nt":
                os.chmod(self.path, 0o600)
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
    if state.get("version") != STATE_VERSION:
        raise ValueError(f"unsupported state version: {state.get('version')!r}")
    agents = state.setdefault("agents", {})
    history = state.setdefault("history", [])
    if not isinstance(agents, dict) or not isinstance(history, list):
        raise TypeError("state agents/history fields have invalid types")
    return state


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


def _lock(file_object: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        file_object.seek(0, os.SEEK_END)
        if file_object.tell() == 0:
            file_object.write(b"0")
            file_object.flush()
        file_object.seek(0)
        msvcrt.locking(file_object.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(file_object.fileno(), fcntl.LOCK_EX)


def _unlock(file_object: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        file_object.seek(0)
        msvcrt.locking(file_object.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(file_object.fileno(), fcntl.LOCK_UN)
