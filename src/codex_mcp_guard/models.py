from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from typing import Any


def normalize_command(command: str) -> str:
    return re.sub(r"\s+", " ", command.strip())


def command_fingerprint(command: str) -> str:
    normalized = normalize_command(command)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    ppid: int
    pgid: int | None
    started_at: float
    command: str

    @property
    def fingerprint(self) -> str:
        return command_fingerprint(self.command)

    def identity(self, kind: str) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "ppid": self.ppid,
            "pgid": self.pgid,
            "started_at": self.started_at,
            "command_sha256": self.fingerprint,
            "kind": kind,
        }


@dataclass(frozen=True)
class ProcessCohort:
    processes: tuple[ProcessInfo, ...]

    @property
    def started_at(self) -> float:
        return min(process.started_at for process in self.processes)

    @property
    def finished_starting_at(self) -> float:
        return max(process.started_at for process in self.processes)

    def as_dict(self) -> dict[str, Any]:
        return {"processes": [asdict(process) for process in self.processes]}
