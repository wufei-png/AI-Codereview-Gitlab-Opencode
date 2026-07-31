"""Shared environment flag parsing."""
from __future__ import annotations


TRUE_VALUES = {"1", "true", "yes", "on"}


def env_flag(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in TRUE_VALUES
