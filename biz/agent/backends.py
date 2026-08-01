"""Explicit external agent backends; no automatic backend fallback."""
from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import threading
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import requests
from requests.auth import HTTPBasicAuth

from biz.agent.config import AgentReviewConfig


_ACTIVE_PROCESSES: set[subprocess.Popen[str]] = set()
_ACTIVE_LOCK = threading.Lock()


@dataclass(frozen=True)
class BackendResult:
    backend: str
    output: str
    session_id: str | None = None
    stderr: str = ""


class BackendExecutionError(RuntimeError):
    def __init__(self, message: str, *, output: str = "", stderr: str = "", timed_out: bool = False):
        super().__init__(message)
        self.output = output
        self.stderr = stderr
        self.timed_out = timed_out


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
    "GITHUB_ACCESS_TOKEN", "GITLAB_ACCESS_TOKEN", "GITEA_ACCESS_TOKEN",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
}


def _agent_env(backend: str) -> dict[str, str]:
    """Pass only CLI/runtime variables, never the service's provider secrets."""
    names = set(_COMMON_AGENT_ENV)
    if backend == "codex":
        names.update({"CODEX_HOME", "CODEX_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL"})
    elif backend == "claude":
        names.update({
            "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_API_KEY",
            "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_OAUTH_TOKEN",
            "ANTHROPIC_BASE_URL", "AWS_PROFILE", "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_REGION",
            "AWS_DEFAULT_REGION", "AWS_BEARER_TOKEN_BEDROCK",
            "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
            "GOOGLE_APPLICATION_CREDENTIALS", "CLOUD_ML_REGION", "ANTHROPIC_VERTEX_PROJECT_ID",
        })
    elif backend == "pi":
        names.update({
            "PI_CODING_AGENT_DIR", "PI_PACKAGE_DIR", "PI_TELEMETRY",
            "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_OAUTH_TOKEN",
            "OPENAI_API_KEY", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_BASE_URL",
            "AZURE_OPENAI_RESOURCE_NAME", "AZURE_OPENAI_API_VERSION",
            "AZURE_OPENAI_DEPLOYMENT_NAME_MAP", "GEMINI_API_KEY", "DEEPSEEK_API_KEY",
            "OPENROUTER_API_KEY", "AI_GATEWAY_API_KEY", "OPENCODE_API_KEY",
            "AWS_PROFILE",
            "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
            "AWS_REGION", "AWS_DEFAULT_REGION",
        })
    return {name: value for name, value in os.environ.items() if name in names}


def _timeout(value: int) -> int | None:
    return None if value == -1 else value


def _run_cli(
    backend: str, args: list[str], prompt: str, cwd: Path, timeout: int, *, env: dict[str, str]
) -> BackendResult:
    process = subprocess.Popen(
        args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=cwd, env=env, start_new_session=True,
    )
    with _ACTIVE_LOCK:
        _ACTIVE_PROCESSES.add(process)
    try:
        try:
            stdout, stderr = process.communicate(prompt, timeout=_timeout(timeout))
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate()
            raise BackendExecutionError(
                f"{backend} timed out after {timeout}s", output=stdout or "", stderr=stderr or "", timed_out=True,
            ) from exc
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE_PROCESSES.discard(process)
    if process.returncode != 0:
        tail = (stderr or "").strip()[-2000:]
        raise BackendExecutionError(
            f"{backend} failed ({process.returncode}): {tail}", output=stdout or "", stderr=stderr or "",
        )
    return BackendResult(backend, stdout or "", stderr=stderr or "")


def terminate_active_backends() -> None:
    with _ACTIVE_LOCK:
        processes = list(_ACTIVE_PROCESSES)
    for process in processes:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


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
            timeout=config.opencode_session_timeout,
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
            timeout=_timeout(config.backend_timeout),
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
        permissions = reviewer.setdefault("permission", {})
        permissions.update({
            "read": "allow", "edit": "allow", "bash": "allow",
            "webfetch": "allow", "external_directory": "allow",
        })
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
            "--config", "sandbox_workspace_write.network_access=true",
            "--skip-git-repo-check", "--ephemeral", "--color", "never", "-",
        ]
        return _run_cli(self.name, args, prompt, job_root, config.backend_timeout, env=_agent_env(self.name))


class ClaudeCliBackend:
    name = "claude"

    def run(self, *, prompt: str, job_root: Path, source_repo: Path, config: AgentReviewConfig) -> BackendResult:
        binary = _required_binary(config.claude_bin)
        args = [
            binary, "-p", "--permission-mode", "bypassPermissions", "--tools", "default",
            "--no-session-persistence",
            "--add-dir", str(job_root),
            "--output-format", "text",
        ]
        return _run_cli(self.name, args, prompt, job_root, config.backend_timeout, env=_agent_env(self.name))


class PiCliBackend:
    name = "pi"

    def run(self, *, prompt: str, job_root: Path, source_repo: Path, config: AgentReviewConfig) -> BackendResult:
        del source_repo
        binary = _required_binary(config.pi_bin)
        skill_path = job_root / ".agent-skill" / "SKILL.md"
        args = [
            binary, "--print", "--no-session", "--no-approve", "--no-extensions",
            "--no-prompt-templates", "--no-context-files",
            "--tools", "read,bash,edit,write,grep,find,ls", "--skill", str(skill_path),
        ]
        return _run_cli(self.name, args, prompt, job_root, config.backend_timeout, env=_agent_env(self.name))


def create_backend(config: AgentReviewConfig) -> AgentBackend:
    if config.backend == "opencode":
        return OpenCodeServeBackend()
    if config.backend == "codex":
        return CodexCliBackend()
    if config.backend == "claude":
        return ClaudeCliBackend()
    if config.backend == "pi":
        return PiCliBackend()
    raise ValueError(f"unsupported agent backend: {config.backend}")
