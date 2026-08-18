from __future__ import annotations

import json
import math
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .state import default_state_dir

MAX_CONFIG_BYTES = 64 * 1024


@dataclass(frozen=True)
class GuardConfig:
    """Bounded snapshot-window settings for the audit-only guard."""

    cohort_window_seconds: float = 2.0
    ambiguity_margin_seconds: float = 2.0
    future_clock_skew_seconds: float = 5.0
    max_pending_seconds: float = 60.0
    max_observation_seconds: float = 86400.0

    def validate(self) -> None:
        bounds = {
            "cohort_window_seconds": (0.1, 10.0),
            "ambiguity_margin_seconds": (0.1, 30.0),
            "future_clock_skew_seconds": (0.0, 10.0),
            "max_pending_seconds": (5.0, 300.0),
            "max_observation_seconds": (60.0, 604800.0),
        }
        for field_name, (minimum, maximum) in bounds.items():
            value = getattr(self, field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError(f"{field_name} must be a finite number")
            if not minimum <= float(value) <= maximum:
                raise ValueError(
                    f"{field_name} must be between {minimum} and {maximum}"
                )


def config_path(root: Path | None = None) -> Path:
    return (root or default_state_dir()) / "config.json"


def load_config(root: Path | None = None) -> GuardConfig:
    path = config_path(root)
    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        return GuardConfig()
    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        raise ValueError(f"config must be a regular, non-symlink file: {path}")
    if hasattr(os, "getuid") and file_stat.st_uid != os.getuid():
        raise ValueError(f"config is not owned by the current user: {path}")
    if os.name != "nt" and stat.S_IMODE(file_stat.st_mode) & 0o077:
        raise ValueError(f"config must have mode 0600: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "r", encoding="utf-8") as config_file:
        opened_stat = os.fstat(config_file.fileno())
        if not stat.S_ISREG(opened_stat.st_mode):
            raise ValueError(f"config must be a regular file: {path}")
        if hasattr(os, "getuid") and opened_stat.st_uid != os.getuid():
            raise ValueError(f"config is not owned by the current user: {path}")
        if os.name != "nt" and stat.S_IMODE(opened_stat.st_mode) & 0o077:
            raise ValueError(f"config must have mode 0600: {path}")
        if opened_stat.st_size > MAX_CONFIG_BYTES:
            raise ValueError(f"config exceeds {MAX_CONFIG_BYTES} bytes")
        raw = json.load(config_file)
    if not isinstance(raw, dict):
        raise TypeError("config must contain a JSON object")

    allowed = set(GuardConfig.__dataclass_fields__)
    deprecated = {
        "lookback_seconds",
        "max_post_reference_seconds",
        "max_pre_reference_seconds",
        "mode",
        "terminate_timeout_seconds",
    }
    unknown = set(raw) - allowed - deprecated
    if unknown:
        raise ValueError(f"unknown config field(s): {', '.join(sorted(unknown))}")
    config = GuardConfig(**{key: value for key, value in raw.items() if key in allowed})
    config.validate()
    return config
