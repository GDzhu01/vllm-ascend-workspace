from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _number(name: str, default: int, minimum: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    project_root: Path
    state_dir: Path
    bind: str
    port: int
    idle_interval: int
    history_interval: int
    infrastructure_interval: int
    retention_days: int
    max_workers: int
    ssh_timeout: int
    hbm_busy_threshold_mb: int

    @classmethod
    def load(cls) -> "Settings":
        project_root = Path(__file__).resolve().parents[2]
        state_dir = Path(os.environ.get("NFM_STATE_DIR", project_root / "data")).expanduser().resolve()
        return cls(
            project_root=project_root,
            state_dir=state_dir,
            bind=os.environ.get("NFM_BIND", "127.0.0.1"),
            port=_number("NFM_PORT", 8789, 1),
            idle_interval=_number("NFM_IDLE_INTERVAL_SECONDS", 120, 10),
            history_interval=_number("NFM_HISTORY_INTERVAL_SECONDS", 30, 5),
            infrastructure_interval=_number("NFM_INFRA_INTERVAL_SECONDS", 60, 15),
            retention_days=_number("NFM_RETENTION_DAYS", 90, 1),
            max_workers=_number("NFM_MAX_WORKERS", 8, 1),
            ssh_timeout=_number("NFM_SSH_TIMEOUT_SECONDS", 12, 2),
            hbm_busy_threshold_mb=_number("NFM_HBM_BUSY_THRESHOLD_MB", 8192, 1),
        )

    def prepare(self) -> None:
        for directory in (
            self.state_dir,
            self.state_dir / "keys",
            self.state_dir / "ssh-control",
        ):
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(0o700)
