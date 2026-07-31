"""Configuration for the external agent review workflow."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from biz.utils.flags import env_flag

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _path(value: str | Path, *, base: Path = PROJECT_ROOT) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _env_value(name: str, default: Any) -> Any:
    value = os.environ.get(name)
    return default if value is None or not value.strip() else value


@dataclass(frozen=True)
class AgentReviewConfig:
    repo_roots: dict[str, Path] = field(default_factory=dict)
    allowed_remote_hosts: tuple[str, ...] = ()
    discovery_max_depth: int = 3
    clone_parent: Path = PROJECT_ROOT / "data" / "agent-clones"
    clone_cleanup: str = "always"
    worktree_parent: Path = PROJECT_ROOT / "data" / "agent-worktrees"
    shared_review_skill: Path = PROJECT_ROOT / "skills" / "review-agent" / "SKILL.md"
    backend: str = "opencode"
    job_db: Path = PROJECT_ROOT / "data" / "agent_review_jobs.db"
    backend_timeout: int = 1800
    clone_timeout: int = 300
    job_lease_seconds: int = 3600
    opencode_api_url: str = "http://localhost:4096"
    opencode_agent_name: str = "code-reviewer"
    opencode_server_username: str = "opencode"
    opencode_server_password: str | None = None
    codex_bin: str = "codex"
    claude_bin: str = "claude"

    def ensure_runtime_directories(self) -> None:
        self.clone_parent.mkdir(parents=True, exist_ok=True)
        self.worktree_parent.mkdir(parents=True, exist_ok=True)
        self.job_db.parent.mkdir(parents=True, exist_ok=True)


def _raw_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        return {}
    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"agent config must be a mapping: {config_path}")
    return data


def load_agent_review_config() -> AgentReviewConfig:
    """Load YAML configuration, then apply explicit environment overrides."""
    config_path = _path(_env_value("AGENT_REVIEW_CONFIG", "conf/agent_repos.yml"))
    raw = _raw_config(config_path)

    roots: dict[str, Path] = {}
    for remote_root, local_root in (raw.get("repo_roots") or {}).items():
        if not isinstance(remote_root, str) or not isinstance(local_root, str):
            raise ValueError("repo_roots must map remote URL strings to local path strings")
        roots[remote_root] = _path(local_root, base=PROJECT_ROOT)

    configured_hosts = raw.get("allowed_remote_hosts") or []
    if isinstance(configured_hosts, str):
        configured_hosts = [item.strip() for item in configured_hosts.split(",") if item.strip()]
    if not isinstance(configured_hosts, list):
        raise ValueError("allowed_remote_hosts must be a list or comma-separated string")
    env_hosts = os.environ.get("AGENT_ALLOWED_REMOTE_HOSTS", "")
    configured_set = {str(item).lower().strip() for item in configured_hosts if str(item).strip()}
    env_set = {item.strip().lower() for item in env_hosts.split(",") if item.strip()}
    # An explicit allowlist is authoritative. Automatic defaults are only used
    # when neither YAML nor the environment supplied one.
    allowed_hosts = configured_set | env_set
    if not allowed_hosts:
        for remote_root in roots:
            parsed = urlparse(remote_root if "://" in remote_root else f"ssh://{remote_root}")
            if parsed.hostname:
                allowed_hosts.add(parsed.hostname.lower())
        for env_name, default_host in (("GITLAB_URL", ""), ("GITHUB_URL", "github.com"), ("GITEA_URL", "gitea.com")):
            value = os.environ.get(env_name, default_host)
            parsed = urlparse(value if "://" in value else f"https://{value}") if value else None
            if parsed and parsed.hostname:
                allowed_hosts.add(parsed.hostname.lower())

    discovery_max_depth = _int_env(
        "AGENT_DISCOVERY_MAX_DEPTH", int(raw.get("discovery_max_depth", 3))
    )
    clone_cleanup = str(_env_value("AGENT_CLONE_CLEANUP", raw.get("clone_cleanup", "always"))).lower()
    if clone_cleanup not in {"always", "never", "on_success"}:
        raise ValueError("AGENT_CLONE_CLEANUP must be always, never, or on_success")

    backend = str(_env_value("AGENT_BACKEND", raw.get("backend", "opencode"))).lower()
    if backend not in {"opencode", "codex", "claude"}:
        raise ValueError("AGENT_BACKEND must be opencode, codex, or claude")

    job_lease_seconds = _int_env("AGENT_JOB_LEASE_SECONDS", int(raw.get("job_lease_seconds", 3600)))
    if job_lease_seconds <= 0:
        raise ValueError("AGENT_JOB_LEASE_SECONDS must be greater than zero")

    return AgentReviewConfig(
        repo_roots=roots,
        allowed_remote_hosts=tuple(sorted(allowed_hosts)),
        discovery_max_depth=discovery_max_depth,
        clone_parent=_path(_env_value("AGENT_CLONE_PARENT", raw.get("clone_parent", "data/agent-clones"))),
        clone_cleanup=clone_cleanup,
        worktree_parent=_path(_env_value("AGENT_WORKTREE_PARENT", raw.get("worktree_parent", "data/agent-worktrees"))),
        shared_review_skill=_path(_env_value(
            "AGENT_SHARED_REVIEW_SKILL_PATH",
            raw.get("shared_review_skill", "skills/review-agent/SKILL.md"),
        )),
        backend=backend,
        job_db=_path(_env_value("AGENT_JOB_DB", raw.get("job_db", "data/agent_review_jobs.db"))),
        backend_timeout=_int_env("AGENT_BACKEND_TIMEOUT", int(raw.get("backend_timeout", 1800))),
        clone_timeout=_int_env("AGENT_CLONE_TIMEOUT", int(raw.get("clone_timeout", 300))),
        job_lease_seconds=job_lease_seconds,
        opencode_api_url=_env_value("OPENCODE_API_URL", "http://localhost:4096"),
        opencode_agent_name=_env_value("OPENCODE_AGENT_NAME", "code-reviewer"),
        opencode_server_username=_env_value("OPENCODE_SERVER_USERNAME", "opencode"),
        opencode_server_password=os.environ.get("OPENCODE_SERVER_PASSWORD"),
        codex_bin=os.environ.get("CODEX_BIN", "codex"),
        claude_bin=os.environ.get("CLAUDE_BIN", "claude"),
    )


def is_agent_review_enabled() -> bool:
    """AGENT_REVIEW_ENABLED is preferred; OPENCODE_ENABLED remains compatible."""
    value = os.environ.get("AGENT_REVIEW_ENABLED")
    if value is None:
        value = os.environ.get("OPENCODE_ENABLED", "0")
    return env_flag(value)


def remote_allowed(config: AgentReviewConfig, remote_url: str) -> bool:
    parsed = urlparse(remote_url if "://" in remote_url else f"ssh://{remote_url}")
    if parsed.scheme not in {"http", "https", "ssh"} or not parsed.hostname:
        return False
    return parsed.hostname.lower() in config.allowed_remote_hosts
