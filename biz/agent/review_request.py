"""Normalize provider webhook payloads into one agent review request."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

from biz.agent.config import AgentReviewConfig


@dataclass(frozen=True)
class AgentReviewRequest:
    provider: str
    remote_url: str
    review_url: str
    project_path: str
    source_branch: str
    target_branch: str
    revision_hint: str
    action: str
    event_key: str
    target_project_path: str = ""
    target_remote_url: str = ""

    @property
    def platform_cli(self) -> str:
        return {"gitlab": "glab", "github": "gh", "gitea": "tea"}.get(self.provider, "the platform CLI")

    @property
    def title(self) -> str:
        parsed = urlparse(self.review_url)
        return (parsed.path.strip("/") or self.review_url)[-120:]


def _safe_remote_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and "@" in parsed.netloc:
        return urlunparse(parsed._replace(netloc=parsed.netloc.rsplit("@", 1)[-1]))
    # Keep the fallback defensive for malformed webhook data without changing
    # the URL shape that Git uses for normal SSH/scp remotes.
    return re.sub(r"(https?://)[^/@\s]+@", r"\1***@", value)


def _event_key(provider: str, review_url: str, action: str, revision: str) -> str:
    value = "|".join((provider, review_url, action, revision))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _gitlab_request(data: dict, gitlab_url: str) -> AgentReviewRequest | None:
    project = data.get("project") or {}
    attrs = data.get("object_attributes") or {}
    source_project = data.get("source") or data.get("source_project") or attrs.get("source") or attrs.get("source_project") or {}
    target_project_path = project.get("path_with_namespace") or project.get("name") or ""
    project_path = source_project.get("path_with_namespace") or source_project.get("name") or target_project_path
    target_remote_url = (
        project.get("git_http_url")
        or project.get("http_url_to_repo")
        or project.get("ssh_url_to_repo")
        or project.get("git_ssh_url")
        or (f"{gitlab_url.rstrip('/')}/{target_project_path}.git" if target_project_path and gitlab_url else "")
    )
    remote_url = (
        source_project.get("git_http_url")
        or source_project.get("http_url_to_repo")
        or source_project.get("ssh_url_to_repo")
        or source_project.get("git_ssh_url")
        or target_remote_url
    )
    review_url = attrs.get("url") or attrs.get("web_url")
    if not review_url and project.get("web_url") and attrs.get("iid"):
        review_url = f"{project['web_url'].rstrip('/')}/-/merge_requests/{attrs['iid']}"
    revision = (attrs.get("last_commit") or {}).get("id", "")
    if not (remote_url and review_url and project_path):
        return None
    action = str(attrs.get("action") or data.get("event_type") or "update").lower()
    return AgentReviewRequest(
        provider="gitlab",
        remote_url=remote_url,
        review_url=review_url,
        project_path=project_path,
        source_branch=attrs.get("source_branch") or "",
        target_branch=attrs.get("target_branch") or "",
        revision_hint=revision,
        action=action,
        event_key=_event_key("gitlab", review_url, action, revision),
        target_project_path=target_project_path,
        target_remote_url=target_remote_url,
    )


def _github_like_request(data: dict, provider: str) -> AgentReviewRequest | None:
    repo = data.get("repository") or {}
    pr = data.get("pull_request") or {}
    head = pr.get("head") or {}
    base = pr.get("base") or {}
    head_repo = head.get("repo") or {}
    remote_url = head_repo.get("clone_url") or head_repo.get("ssh_url") or repo.get("clone_url") or repo.get("ssh_url")
    project_path = head_repo.get("full_name") or repo.get("full_name") or repo.get("name")
    review_url = pr.get("html_url") or pr.get("url")
    action = str(data.get("action") or "update").lower()
    revision = head.get("sha") or ""
    if not (remote_url and project_path and review_url):
        return None
    return AgentReviewRequest(
        provider=provider,
        remote_url=remote_url,
        review_url=review_url,
        project_path=project_path,
        source_branch=head.get("ref") or "",
        target_branch=base.get("ref") or "",
        revision_hint=revision,
        action=action,
        event_key=_event_key(provider, review_url, action, revision),
        target_project_path=repo.get("full_name") or repo.get("name") or "",
        target_remote_url=repo.get("clone_url") or repo.get("ssh_url") or "",
    )


def from_webhook(provider: str, data: dict, *, gitlab_url: str = "") -> AgentReviewRequest | None:
    provider = provider.lower()
    if provider == "gitlab":
        return _gitlab_request(data, gitlab_url)
    if provider in {"github", "gitea"}:
        return _github_like_request(data, provider)
    return None


def is_reviewable_action(request: AgentReviewRequest) -> bool:
    if request.provider == "gitlab":
        return request.action in {"open", "opened", "update", "updated", "reopen", "reopened"}
    return request.action in {"open", "opened", "synchronize", "synchronized", "reopen", "reopened", "update", "updated"}


def build_prompt(request: AgentReviewRequest, source_repo: str, job_root: str, latest_revision: str, config: AgentReviewConfig, *, skill_path: str | None = None) -> str:
    skill = skill_path or str(config.shared_review_skill)
    return f"""You are the review execution agent for one merge/pull request.

Read and follow this single canonical skill before doing any work:
{skill}

The service has already resolved the repository and fetched the latest remote source branch. Treat these values as authoritative:
- PLATFORM: {request.provider}
- PLATFORM_CLI: {request.platform_cli} (the CLI is expected to be installed and authenticated; do not configure credentials)
- REVIEW_URL: {request.review_url}
- REMOTE_URL: {_safe_remote_url(request.remote_url)}
- PROJECT_PATH: {request.project_path}
- TARGET_PROJECT_PATH: {request.target_project_path or request.project_path}
- TARGET_REMOTE_URL: {_safe_remote_url(request.target_remote_url or request.remote_url)}
- SOURCE_REPOSITORY: {source_repo}
- SOURCE_BRANCH: {request.source_branch}
- TARGET_BRANCH: {request.target_branch}
- LATEST_REVISION: {latest_revision}
- WORKTREE_PARENT: {job_root}
- CLONE_PARENT: {config.clone_parent}
- CLONE_CLEANUP: configured policy is {config.clone_cleanup}; Review Worktrees are always removed
- DISCOVERY_MAX_DEPTH: {config.discovery_max_depth}

Create your own disposable git worktree under WORKTREE_PARENT, choose its child directory and branch details yourself, and do all inspection, review, edits, tests, and platform delivery from that worktree. Review the latest fetched source branch at LATEST_REVISION, not the webhook revision if they differ. Do not modify the source repository's checked-out files. The service removes the worktree and temporary clone after this run.
"""
