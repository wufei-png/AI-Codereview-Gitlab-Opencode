"""Explicit external agent backends; no automatic backend fallback."""
from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import requests
from requests.auth import HTTPBasicAuth

from biz.agent.config import AgentReviewConfig


_ACTIVE_PROCESSES: set[subprocess.Popen[str]] = set()
_ACTIVE_OPENCODE_EXECUTIONS: set[_OpenCodeExecution] = set()
_ACTIVE_LOCK = threading.Lock()
_BACKEND_SHUTDOWN = threading.Event()


@dataclass(eq=False)
class _OpenCodeExecution:
    base_url: str
    session_id: str
    directory: str
    auth: object | None
    request_timeout: int
    stop: threading.Event


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


def _terminate_process_groups(
    processes: list[subprocess.Popen[str]], *, grace_seconds: float,
) -> None:
    for process in processes:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + max(0.0, grace_seconds)
    while any(process.poll() is None for process in processes) and time.monotonic() < deadline:
        time.sleep(0.05)
    for process in processes:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _run_cli(
    backend: str, args: list[str], prompt: str, cwd: Path, timeout: int, *, env: dict[str, str]
) -> BackendResult:
    if _BACKEND_SHUTDOWN.is_set():
        raise BackendExecutionError(f"{backend} interrupted by worker shutdown before start")
    process = subprocess.Popen(
        args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=cwd, env=env, start_new_session=True,
    )
    with _ACTIVE_LOCK:
        _ACTIVE_PROCESSES.add(process)
    if _BACKEND_SHUTDOWN.is_set():
        _terminate_process_groups([process], grace_seconds=5)
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


def reset_backend_shutdown() -> None:
    _BACKEND_SHUTDOWN.clear()


def terminate_active_backends(*, grace_seconds: float = 5.0) -> None:
    _BACKEND_SHUTDOWN.set()
    with _ACTIVE_LOCK:
        processes = list(_ACTIVE_PROCESSES)
        opencode_executions = list(_ACTIVE_OPENCODE_EXECUTIONS)
    for execution in opencode_executions:
        execution.stop.set()
    _terminate_process_groups(processes, grace_seconds=grace_seconds)


class OpenCodeServeBackend:
    name = "opencode"

    def run(self, *, prompt: str, job_root: Path, source_repo: Path, config: AgentReviewConfig) -> BackendResult:
        del source_repo
        if _BACKEND_SHUTDOWN.is_set():
            raise BackendExecutionError("opencode interrupted by worker shutdown before start")
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
        execution = _OpenCodeExecution(
            base_url=base, session_id=str(session_id), directory=str(job_root), auth=auth,
            request_timeout=min(config.opencode_session_timeout, 5), stop=threading.Event(),
        )
        with _ACTIVE_LOCK:
            _ACTIVE_OPENCODE_EXECUTIONS.add(execution)
        try:
            if _BACKEND_SHUTDOWN.is_set():
                execution.stop.set()
                self._abort(execution)
                raise BackendExecutionError("opencode interrupted by worker shutdown before prompt")
            message = requests.post(
                f"{base}/session/{session_id}/prompt_async", params=params, json=payload, auth=auth,
                timeout=execution.request_timeout,
            )
            message.raise_for_status()
            return self._wait_for_result(execution, config.backend_timeout)
        finally:
            with _ACTIVE_LOCK:
                _ACTIVE_OPENCODE_EXECUTIONS.discard(execution)

    def _wait_for_result(self, execution: _OpenCodeExecution, timeout: int) -> BackendResult:
        deadline = None if timeout == -1 else time.monotonic() + timeout
        params = {"directory": execution.directory}
        latest_output = ""
        while True:
            if execution.stop.is_set():
                self._abort(execution)
                raise BackendExecutionError(
                    "opencode interrupted by worker shutdown", output=latest_output,
                )
            if deadline is not None and time.monotonic() >= deadline:
                self._abort(execution)
                raise BackendExecutionError(
                    f"opencode timed out after {timeout}s", output=latest_output, timed_out=True,
                )
            try:
                statuses_response = requests.get(
                    f"{execution.base_url}/session/status", params=params, auth=execution.auth,
                    timeout=execution.request_timeout,
                )
                statuses_response.raise_for_status()
                statuses = statuses_response.json()
                status = statuses.get(execution.session_id, {}) if isinstance(statuses, dict) else {}
                status_type = status.get("type") if isinstance(status, dict) else None

                messages_response = requests.get(
                    f"{execution.base_url}/session/{execution.session_id}/message",
                    params=params, auth=execution.auth, timeout=execution.request_timeout,
                )
                messages_response.raise_for_status()
                messages = messages_response.json()
                latest = self._latest_assistant(messages)
                if latest is not None:
                    latest_output = json.dumps(latest, ensure_ascii=False)
                    info = latest.get("info")
                    if isinstance(info, dict) and info.get("error") is not None:
                        if status_type not in {"busy", "retry"}:
                            detail = self._assistant_error_detail(info.get("error"))
                            raise BackendExecutionError(
                                f"opencode agent failed: {detail}",
                                output=latest_output,
                                stderr=detail,
                            )
                    elif self._is_completed_assistant(latest) and status_type not in {"busy", "retry"}:
                        return BackendResult(self.name, latest_output, execution.session_id)
            except requests.RequestException:
                # Control requests are deliberately short and retryable so an
                # unlimited Agent execution can still observe worker shutdown.
                pass
            execution.stop.wait(0.5)

    @staticmethod
    def _latest_assistant(messages: object) -> dict[str, object] | None:
        if not isinstance(messages, list):
            return None
        for item in reversed(messages):
            if not isinstance(item, dict):
                continue
            info = item.get("info")
            if not isinstance(info, dict) or info.get("role") != "assistant":
                continue
            return item
        return None

    @classmethod
    def _latest_completed_assistant(cls, messages: object) -> dict[str, object] | None:
        latest = cls._latest_assistant(messages)
        if latest is not None and cls._is_completed_assistant(latest):
            return latest
        return None

    @staticmethod
    def _is_completed_assistant(message: dict[str, object]) -> bool:
        info = message.get("info")
        if not isinstance(info, dict) or info.get("error") is not None:
            return False
        timing = info.get("time")
        return isinstance(timing, dict) and timing.get("completed") is not None

    @staticmethod
    def _assistant_error_detail(error: object) -> str:
        if isinstance(error, dict):
            data = error.get("data")
            if isinstance(data, dict) and data.get("message"):
                return str(data["message"])
            for key in ("message", "name"):
                if error.get(key):
                    return str(error[key])
        return str(error or "unknown OpenCode assistant error")

    @staticmethod
    def _abort(execution: _OpenCodeExecution) -> None:
        try:
            response = requests.post(
                f"{execution.base_url}/session/{execution.session_id}/abort",
                params={"directory": execution.directory}, auth=execution.auth,
                timeout=execution.request_timeout,
            )
            response.raise_for_status()
        except requests.RequestException:
            pass

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
