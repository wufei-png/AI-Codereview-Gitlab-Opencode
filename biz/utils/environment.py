"""Load project configuration before imports that construct clients or loggers."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


def load_project_environment(project_root: Path | None = None) -> None:
    root = project_root or Path(__file__).resolve().parents[2]
    load_dotenv(root / "conf" / ".env", override=False)
    os.environ.setdefault("LOG_FILE", str(root / "log" / "app.log"))
    Path(os.environ["LOG_FILE"]).parent.mkdir(parents=True, exist_ok=True)
