"""Explicit external agent backends; no automatic backend fallback."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import requests
from requests.auth import HTTPBasicAuth

from biz.agent.config import AgentReviewConfig


@dataclass(frozen=True)
class BackendResult:
    backend: str
    output: str
    session_id: str | None = None


class AgentBackend(Protocol):
    name: str

    def run(self, *, prompt: str, job_root: Path, source_repo: Path, config: AgentReviewConfig) -> BackendResult:
        ...


def _required_binary(value: str) -> str:
    resolved = shutil.which(value) or (value if Path(value).exists() else None)
    if not resolved:
        raise RuntimeError(f"agent CLI not found: {value}")
    return resolved


_COMMON_AGENT_ENV = {
    "HOME", "PATH", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE",
    "TMPDIR", "TERM", "NO_COLOR", "SSH_AUTH_SOCK", "GIT_SSH_COMMAND", "GIT_ASKPASS",
    "GIT_TERMINAL_PROMPT", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "XDG_CONFIG_HOME",
    "XDG_DATA_HOME", "XDG_STATE_HOME", "GH_CONFIG_DIR", "GLAB_CONFIG_DIR", "TEA_CONFIG_DIR",
    "GH_TOKEN", "GITHUB_TOKEN", "GITLAB_TOKEN", "GITEA_TOKEN", "TEA_TOKEN",
}


def _agent_env(backend: str) -> dict[str, str]:
    """Pass only CLI/runtime variables, never the service's provider secrets."""
    names = set(_COMMON_AGENT_ENV)
    if backend == "codex":
        names.update({"CODEX_HOME", "CODEX_API_KEY"})
    elif backend == "claude":
        names.update({"CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_API_KEY"})
    return {name: value for name, value in os.environ.items() if name in names}


class OpenCodeServeBackend:
    name = "opencode"

    def run(self, *, prompt: str, job_root: Path, source_repo: Path, config: AgentReviewConfig) -> BackendResult:
        del source_repo
        self._materialize_project_config(job_root, config)
        auth = None
        if config.opencode_server_password:
            auth = HTTPBasicAuth(config.opencode_server_username, config.opencode_server_password)
        base = config.opencode_api_url.rstrip("/")
        params = {"directory": str(job_root)}
        response = requests.post(
            f"{base}/session", params=params, json={"title": "Agent Review"}, auth=auth,
            timeout=min(config.backend_timeout, 60),
        )
        response.raise_for_status()
        session_id = response.json().get("id")
        if not session_id:
            raise RuntimeError("OpenCode session response did not contain id")
        payload = {
            "agent": config.opencode_agent_name,
            "parts": [{"type": "text", "text": prompt}],
        }
        message = requests.post(
            f"{base}/session/{session_id}/message", params=params, json=payload, auth=auth,
            timeout=config.backend_timeout,
        )
        message.raise_for_status()
        return BackendResult(self.name, message.text, str(session_id))

    @staticmethod
    def _materialize_project_config(job_root: Path, config: AgentReviewConfig) -> None:
        """Make the repository's named agent available to a directory-scoped server.

        OpenCode resolves project configuration relative to the directory passed
        to the API. The job directory is disposable, so a small generated config
        keeps the active agent and canonical skill available without changing the
        target repository or relying on a user's global config.
        """
        source = Path(__file__).resolve().parents[2] / "opencode" / "opencode.json"
        if source.exists():
            payload = json.loads(source.read_text(encoding="utf-8"))
        else:
            payload = {"$schema": "https://opencode.ai/config.json", "agent": {}}
        agents = payload.setdefault("agent", {})
        template = agents.get("code-reviewer", {})
        reviewer = agents.setdefault(config.opencode_agent_name, deepcopy(template))
        skill_path = job_root / ".agent-skill" / "SKILL.md"
        if not skill_path.is_file():
            if not config.shared_review_skill.is_file():
                raise RuntimeError(f"materialized review skill not found: {skill_path}")
            skill_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(config.shared_review_skill, skill_path)
        reviewer["prompt"] = "{file:" + str(skill_path) + "}"
        reviewer.setdefault("permission", {"read": "allow", "edit": "allow", "bash": "allow", "external_directory": "allow"})
        prompt_source = source.parent / "prompts" / "docs-searcher.md"
        if prompt_source.exists():
            prompt_target = job_root / "prompts" / "docs-searcher.md"
            prompt_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(prompt_source, prompt_target)
        (job_root / "opencode.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class CodexCliBackend:
    name = "codex"

    def run(self, *, prompt: str, job_root: Path, source_repo: Path, config: AgentReviewConfig) -> BackendResult:
        binary = _required_binary(config.codex_bin)
        args = [
            binary, "exec", "--sandbox", "workspace-write", "--cd", str(job_root),
            "--ephemeral", "--color", "never", "-",
        ]
        return self._run(args, prompt, job_root, config.backend_timeout, env=_agent_env(self.name))

    @staticmethod
    def _run(args: list[str], prompt: str, cwd: Path, timeout: int, *, env: dict[str, str]) -> BackendResult:
        try:
            completed = subprocess.run(args, input=prompt, text=True, capture_output=True, cwd=cwd, timeout=timeout, env=env)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"codex timed out after {timeout}s") from exc
        if completed.returncode != 0:
            raise RuntimeError(f"codex failed ({completed.returncode}): {(completed.stderr or '').strip()[-2000:]}")
        return BackendResult("codex", completed.stdout)


class ClaudeCliBackend:
    name = "claude"

    def run(self, *, prompt: str, job_root: Path, source_repo: Path, config: AgentReviewConfig) -> BackendResult:
        binary = _required_binary(config.claude_bin)
        args = [
            binary, "-p", "--permission-mode", "acceptEdits", "--no-session-persistence",
            "--add-dir", str(job_root),
            "--output-format", "text",
        ]
        try:
            completed = subprocess.run(args, input=prompt, text=True, capture_output=True, cwd=job_root, timeout=config.backend_timeout, env=_agent_env(self.name))
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"claude timed out after {config.backend_timeout}s") from exc
        if completed.returncode != 0:
            raise RuntimeError(f"claude failed ({completed.returncode}): {(completed.stderr or '').strip()[-2000:]}")
        return BackendResult(self.name, completed.stdout)


def create_backend(config: AgentReviewConfig) -> AgentBackend:
    if config.backend == "opencode":
        return OpenCodeServeBackend()
    if config.backend == "codex":
        return CodexCliBackend()
    if config.backend == "claude":
        return ClaudeCliBackend()
    raise ValueError(f"unsupported agent backend: {config.backend}")
