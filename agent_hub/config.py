from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent
DEFAULT_DATA_DIR = PACKAGE_DIR / "data"


@dataclass(frozen=True)
class HubConfig:
    host: str = "127.0.0.1"
    port: int = 8766
    scan_interval: float = 5.0
    db_path: Path = DEFAULT_DATA_DIR / "agenthub.sqlite3"
    codex_home: Path = Path.home() / ".codex"
    tcodex_home: Path = Path.home() / ".tcodex"
    codex_history_limit: int = 80
    tmux_socket_name: str = "agenthub"
    enable_public_runtimes: bool = False

    def __post_init__(self) -> None:
        socket_name = self.tmux_socket_name.strip()
        if (
            not socket_name
            or socket_name == "default"
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,48}", socket_name)
        ):
            raise ValueError(
                "AGENTHUB_TMUX_SOCKET 必须是 1–48 位安全名称，且不能为 default"
            )
        object.__setattr__(self, "tmux_socket_name", socket_name)

    @classmethod
    def from_env(cls) -> "HubConfig":
        return cls(
            host=os.environ.get("AGENTHUB_HOST", "127.0.0.1"),
            port=int(os.environ.get("AGENTHUB_PORT", "8766")),
            scan_interval=float(os.environ.get("AGENTHUB_SCAN_INTERVAL", "5")),
            db_path=Path(
                os.environ.get(
                    "AGENTHUB_DB",
                    str(DEFAULT_DATA_DIR / "agenthub.sqlite3"),
                )
            ).expanduser(),
            codex_home=Path(
                os.environ.get("AGENTHUB_CODEX_HOME", str(Path.home() / ".codex"))
            ).expanduser(),
            tcodex_home=Path(
                os.environ.get("AGENTHUB_TCODEX_HOME", str(Path.home() / ".tcodex"))
            ).expanduser(),
            codex_history_limit=int(
                os.environ.get("AGENTHUB_CODEX_HISTORY_LIMIT", "80")
            ),
            tmux_socket_name=os.environ.get(
                "AGENTHUB_TMUX_SOCKET", "agenthub"
            ),
            enable_public_runtimes=os.environ.get(
                "AGENTHUB_ENABLE_PUBLIC_RUNTIMES", ""
            )
            .strip()
            .lower()
            in {"1", "true", "yes", "on"},
        )
