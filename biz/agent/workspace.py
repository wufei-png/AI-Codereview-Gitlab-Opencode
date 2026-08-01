"""Repository resolution, latest-branch fetching, and disposable workspace cleanup."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import urlparse, urlunparse

from biz.agent.config import AgentReviewConfig
from biz.agent.review_request import AgentReviewRequest


@dataclass
class WorkspaceContext:
    source_repo: Path
    job_root: Path
    clone_path: Path | None
    source_revision: str
    source_branch: str
    target_revision: str = ""
    cleaned: bool = False
    skill_path: Path | None = None
    source_repo_owned: bool = False

    @property
    def latest_revision(self) -> str:
        """Compatibility alias for callers migrating to Source Revision."""
        return self.source_revision


def redact_credentials(value: str) -> str:
    """Remove basic-auth material before errors reach logs or the job store."""
    return re.sub(r"(https?://)[^/@\s]+@", r"\1***@", value)


def normalize_remote_url(value: str) -> str:
    """Normalize HTTPS/SSH clone URLs to a comparable host/path identity."""
    value = value.strip()
    if value.startswith("git@") and ":" in value:
        host, path = value[4:].split(":", 1)
        parsed = (host, "/" + path)
    else:
        parsed_url = urlparse(value if "://" in value else f"ssh://{value}")
        host = parsed_url.hostname or ""
        if parsed_url.port:
            host = f"{host}:{parsed_url.port}"
        parsed = (host, parsed_url.path)
    host, path = parsed
    path = path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return f"{host.lower()}/{path.lstrip('/')}".rstrip("/")


def _remote_parts(value: str) -> tuple[str, ...]:
    normalized = normalize_remote_url(value)
    host, _, path = normalized.partition("/")
    del host
    return tuple(part for part in path.split("/") if part)


def _git(
    cwd: Path,
    *args: str,
    timeout: int = 60,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args], cwd=cwd, check=check, capture_output=True, text=True, timeout=timeout, env=env
        )
    except FileNotFoundError as exc:
        raise RuntimeError("git is required for agent review") from exc


def _is_git_repo(path: Path) -> bool:
    if not path.is_dir():
        return False
    result = _git(path, "rev-parse", "--is-inside-work-tree", check=False)
    return result.returncode == 0 and result.stdout.strip() == "true"


def _origin(path: Path) -> str | None:
    result = _git(path, "remote", "get-url", "origin", check=False)
    return result.stdout.strip() if result.returncode == 0 else None


class RepositoryResolver:
    def __init__(self, config: AgentReviewConfig):
        self.config = config

    def resolve_local_repo(self, remote_url: str) -> Path | None:
        wanted = normalize_remote_url(remote_url)
        wanted_parts = _remote_parts(remote_url)
        matched: list[tuple[str, Path, tuple[str, ...]]] = []
        for configured_remote, root in self.config.repo_roots.items():
            key = normalize_remote_url(configured_remote)
            key_parts = _remote_parts(configured_remote)
            if wanted != key and not (len(wanted_parts) > len(key_parts) and wanted_parts[: len(key_parts)] == key_parts):
                continue
            matched.append((configured_remote, root, key_parts))
        if not matched:
            return None
        longest = max(len(item[2]) for item in matched)
        candidates: list[Path] = []
        for configured_remote, root, key_parts in matched:
            if len(key_parts) != longest:
                continue
            suffix = wanted_parts[len(key_parts) :]
            direct = root if not suffix else root.joinpath(*suffix)
            if _is_git_repo(direct) and normalize_remote_url(_origin(direct) or "") == wanted:
                return direct.resolve()
            search_root = root
            while not search_root.exists() and search_root != search_root.parent:
                search_root = search_root.parent
            candidates.extend(self._find_repositories(search_root, wanted))
        unique = sorted({candidate.resolve() for candidate in candidates}, key=lambda item: (len(item.parts), str(item)))
        return unique[0] if unique else None

    def _find_repositories(self, root: Path, wanted: str) -> list[Path]:
        if not root.is_dir():
            return []
        matches: list[Path] = []
        root = root.resolve()
        for current, dirs, _files in os.walk(root, followlinks=False):
            current_path = Path(current)
            depth = len(current_path.relative_to(root).parts)
            dirs[:] = sorted(d for d in dirs if d != ".git")
            if depth > self.config.discovery_max_depth:
                dirs[:] = []
                continue
            if _is_git_repo(current_path) and normalize_remote_url(_origin(current_path) or "") == wanted:
                matches.append(current_path)
                dirs[:] = []
        return matches


class WorkspaceManager:
    def __init__(self, config: AgentReviewConfig):
        self.config = config
        self.resolver = RepositoryResolver(config)

    def prepare(
        self,
        request: AgentReviewRequest,
        *,
        on_workspace_allocated: Callable[[Path, Path | None, Path | None], None] | None = None,
    ) -> WorkspaceContext:
        self.config.ensure_runtime_directories()
        source_repo = self.resolver.resolve_local_repo(request.remote_url)
        clone_path: Path | None = None
        job_root: Path | None = Path(tempfile.mkdtemp(prefix="job-", dir=self.config.worktree_parent))
        clone_complete = False
        self._notify_allocation(on_workspace_allocated, job_root, None, None)
        try:
            if source_repo is None:
                safe = re.sub(r"[^A-Za-z0-9._-]+", "-", request.project_path).strip("-") or "repo"
                clone_path = Path(tempfile.mkdtemp(prefix=f"{safe}-", dir=self.config.clone_parent))
                # mkdtemp gives us an empty owned directory; clone into its parent,
                # then remove the temporary leaf so git can create the repository.
                clone_path.rmdir()
                # Register the destination before Git starts so a process crash
                # during clone still leaves a path the reaper can remove.
                self._notify_allocation(on_workspace_allocated, job_root, clone_path, None)
                self._run_git_clone(request.remote_url, request.provider, clone_path)
                clone_complete = True
                source_repo = clone_path
            else:
                safe = re.sub(r"[^A-Za-z0-9._-]+", "-", request.project_path).strip("-") or "repo"
                clone_path = Path(tempfile.mkdtemp(prefix=f"{safe}-", dir=self.config.clone_parent))
                clone_path.rmdir()
                self._notify_allocation(on_workspace_allocated, job_root, clone_path, None)
                self._run_local_clone(source_repo, clone_path, request.remote_url)
                clone_complete = True
                source_repo = clone_path
            self._fetch_source_branch(source_repo, request.source_branch, request.provider)
            self._fetch_target_branch(
                source_repo, request.target_remote_url or request.remote_url,
                request.target_branch, request.provider,
            )
            if clone_path is not None:
                self._scrub_clone_origin(clone_path, request.remote_url)
            source_revision = _git(source_repo, "rev-parse", f"refs/remotes/origin/{request.source_branch}^{{commit}}", timeout=self.config.clone_timeout).stdout.strip()
            target_revision = _git(source_repo, "rev-parse", f"refs/remotes/agent-target/{request.target_branch}^{{commit}}", timeout=self.config.clone_timeout).stdout.strip()
            agent_repo = job_root / ".agent-source"
            self._notify_allocation(on_workspace_allocated, job_root, clone_path, agent_repo)
            self._create_agent_source(
                source_repo, agent_repo, request.remote_url, source_revision, target_revision,
            )
            skill_path = job_root / ".agent-skill" / "SKILL.md"
            if not self.config.shared_review_skill.is_file():
                raise RuntimeError(f"shared review skill not found: {self.config.shared_review_skill}")
            skill_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.config.shared_review_skill, skill_path)
            return WorkspaceContext(
                source_repo=agent_repo, job_root=job_root, clone_path=clone_path,
                source_revision=source_revision, target_revision=target_revision,
                source_branch=request.source_branch, skill_path=skill_path, source_repo_owned=True,
            )
        except Exception:
            if job_root is not None:
                shutil.rmtree(job_root, ignore_errors=True)
            if clone_path is not None and (not clone_complete or self.config.clone_cleanup == "always"):
                if clone_complete:
                    try:
                        self._scrub_clone_origin(clone_path, request.remote_url)
                    except Exception:
                        pass
                shutil.rmtree(clone_path, ignore_errors=True)
            raise

    def _run_git_clone(self, remote_url: str, provider: str, target: Path) -> None:
        try:
            with self._git_auth_env(provider) as env:
                subprocess.run(
                    ["git", "clone", self._without_credentials(remote_url), str(target)],
                    check=True, capture_output=True, text=True, timeout=self.config.clone_timeout, env=env,
                )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"git clone timed out after {self.config.clone_timeout}s") from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"git clone failed: {redact_credentials((exc.stderr or '').strip())}") from exc

    def _run_local_clone(self, source: Path, target: Path, remote_url: str) -> None:
        """Seed a disposable sync clone without modifying the operator checkout."""
        try:
            subprocess.run(
                ["git", "clone", "--no-hardlinks", "--no-checkout", str(source), str(target)],
                check=True, capture_output=True, text=True, timeout=self.config.clone_timeout,
            )
            self._scrub_clone_origin(target, remote_url)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"local repository clone timed out after {self.config.clone_timeout}s") from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"local repository clone failed: {redact_credentials((exc.stderr or '').strip())}") from exc

    @staticmethod
    def _scrub_clone_origin(repo: Path, clean_url: str) -> None:
        _git(repo, "remote", "set-url", "origin", WorkspaceManager._without_credentials(clean_url))

    @staticmethod
    def _without_credentials(value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme in {"http", "https"} and "@" in parsed.netloc:
            return urlunparse(parsed._replace(netloc=parsed.netloc.rsplit("@", 1)[-1]))
        return value

    @staticmethod
    def _notify_allocation(
        callback: Callable[[Path, Path | None, Path | None], None] | None,
        job_root: Path,
        clone_path: Path | None,
        source_repo: Path | None,
    ) -> None:
        if callback is not None:
            callback(job_root, clone_path, source_repo)

    @contextmanager
    def _git_auth_env(self, provider: str) -> Iterator[dict[str, str]]:
        """Give Git a short-lived askpass helper without putting tokens in argv/config."""
        env_names = {
            "github": "GITHUB_ACCESS_TOKEN",
            "gitlab": "GITLAB_ACCESS_TOKEN",
            "gitea": "GITEA_ACCESS_TOKEN",
        }
        base_names = {
            "HOME", "PATH", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE",
            "TMPDIR", "TERM", "SSH_AUTH_SOCK", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM",
            "GIT_SSH_COMMAND", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE",
            "SSL_CERT_DIR",
        }
        env = {name: value for name, value in os.environ.items() if name in base_names}
        env["GIT_TERMINAL_PROMPT"] = "0"
        token = os.environ.get(env_names.get(provider, ""), "")
        askpass_path: Path | None = None
        if token:
            askpass_fd, askpass_name = tempfile.mkstemp(prefix="git-askpass-", dir=self.config.clone_parent)
            os.close(askpass_fd)
            askpass_path = Path(askpass_name)
            askpass_path.write_text(
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  *[Uu]sername*) printf '%s' \"${GIT_ASKPASS_USERNAME:-oauth2}\" ;;\n"
                "  *) printf '%s' \"${GIT_ASKPASS_TOKEN:-}\" ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            askpass_path.chmod(0o700)
            env.update({
                "GIT_ASKPASS": str(askpass_path),
                "GIT_ASKPASS_TOKEN": token,
                "GIT_ASKPASS_USERNAME": "x-access-token" if provider == "github" else "oauth2",
            })
        try:
            yield env
        finally:
            if askpass_path is not None:
                askpass_path.unlink(missing_ok=True)

    def _create_agent_source(
        self, source_repo: Path, target: Path, remote_url: str,
        source_revision: str, target_revision: str,
    ) -> None:
        try:
            subprocess.run(
                ["git", "clone", "--shared", "--no-checkout", str(source_repo), str(target)],
                check=True, capture_output=True, text=True, timeout=self.config.clone_timeout,
            )
            for revision, ref in (
                (source_revision, "refs/agent/source"),
                (target_revision, "refs/agent/target"),
            ):
                _git(
                    target, "fetch", "--no-tags", str(source_repo),
                    f"{revision}:{ref}", timeout=self.config.clone_timeout,
                )
            self._scrub_clone_origin(target, remote_url)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"agent source clone failed: {redact_credentials((exc.stderr or '').strip())}") from exc

    def _fetch_source_branch(self, repo: Path, branch: str, provider: str = "") -> None:
        if not branch or branch.startswith("/"):
            raise ValueError(f"invalid source branch: {branch!r}")
        valid = _git(repo, "check-ref-format", "--branch", branch, check=False)
        if valid.returncode != 0:
            raise ValueError(f"invalid source branch: {branch!r}")
        refspec = f"+refs/heads/{branch}:refs/remotes/origin/{branch}"
        try:
            with self._git_auth_env(provider) as env:
                _git(repo, "fetch", "origin", "--prune", refspec, timeout=self.config.clone_timeout, env=env)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"git fetch source branch failed: {redact_credentials((exc.stderr or '').strip())}") from exc

    def _fetch_target_branch(self, repo: Path, remote_url: str, branch: str, provider: str) -> None:
        if not branch or branch.startswith("/"):
            raise ValueError(f"invalid target branch: {branch!r}")
        if _git(repo, "check-ref-format", "--branch", branch, check=False).returncode != 0:
            raise ValueError(f"invalid target branch: {branch!r}")
        refspec = f"+refs/heads/{branch}:refs/remotes/agent-target/{branch}"
        try:
            with self._git_auth_env(provider) as env:
                _git(
                    repo, "fetch", "--no-tags", self._without_credentials(remote_url), refspec,
                    timeout=self.config.clone_timeout, env=env,
                )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"git fetch target branch failed: {redact_credentials((exc.stderr or '').strip())}") from exc

    def cleanup(self, context: WorkspaceContext, *, success: bool) -> None:
        if context.cleaned:
            return
        try:
            owned_job = self._owned_path(str(context.job_root), self.config.worktree_parent)
            if owned_job is None:
                # Never follow a job-root symlink that an Agent may have
                # replaced. A symlink itself is safe to unlink; its target is
                # deliberately left untouched.
                if context.job_root.is_symlink():
                    context.job_root.unlink(missing_ok=True)
                return
            source_repo = context.source_repo
            if context.source_repo_owned:
                source_repo = self._owned_path(str(context.source_repo), owned_job)
            if source_repo is not None:
                self._remove_worktrees(source_repo, owned_job)
            shutil.rmtree(owned_job, ignore_errors=True)
            owned_clone = self._owned_path(str(context.clone_path), self.config.clone_parent) if context.clone_path else None
            if owned_clone and (
                self.config.clone_cleanup == "always"
                or (self.config.clone_cleanup == "on_success" and success)
            ):
                shutil.rmtree(owned_clone, ignore_errors=True)
        finally:
            context.cleaned = True

    def reclaim_orphan(self, *, source_repo: str | None, job_root: str | None, clone_path: str | None) -> None:
        """Reclaim workspace paths from a lease that expired after a crash."""
        job = self._owned_path(job_root, self.config.worktree_parent)
        clone = self._owned_path(clone_path, self.config.clone_parent)
        owned_source = self._owned_path(source_repo, job) if source_repo and job else None
        if owned_source and owned_source.is_dir():
            self._remove_worktrees(owned_source, job)
        if job:
            shutil.rmtree(job, ignore_errors=True)
        elif job_root:
            candidate = Path(job_root).expanduser()
            try:
                candidate.relative_to(self.config.worktree_parent.resolve())
            except ValueError:
                candidate = None
            if candidate is not None and candidate.is_symlink():
                candidate.unlink(missing_ok=True)
        if clone and self.config.clone_cleanup == "always":
            shutil.rmtree(clone, ignore_errors=True)

    @staticmethod
    def _owned_path(value: str | None, parent: Path) -> Path | None:
        if not value:
            return None
        candidate = Path(value).expanduser().resolve()
        try:
            candidate.relative_to(parent.resolve())
        except ValueError:
            return None
        return candidate

    def _remove_worktrees(self, source_repo: Path, job_root: Path) -> None:
        result = _git(
            source_repo, "worktree", "list", "--porcelain",
            check=False, timeout=self.config.cleanup_timeout,
        )
        if result.returncode != 0:
            return
        for path in self._worktree_paths(result.stdout):
            if path.resolve() == source_repo.resolve():
                continue
            try:
                path.resolve().relative_to(job_root.resolve())
            except ValueError:
                continue
            _git(
                source_repo, "worktree", "remove", "--force", str(path),
                check=False, timeout=self.config.cleanup_timeout,
            )
        _git(source_repo, "worktree", "prune", check=False, timeout=self.config.cleanup_timeout)

    @staticmethod
    def _worktree_paths(output: str) -> list[Path]:
        paths: list[Path] = []
        for line in output.splitlines():
            if line.startswith("worktree "):
                paths.append(Path(line.removeprefix("worktree ").strip()).expanduser().resolve())
        return paths
