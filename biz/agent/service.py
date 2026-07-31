"""Webhook-facing service for external agent reviews."""
from __future__ import annotations

import threading

from biz.agent.backends import create_backend
from biz.agent.config import is_agent_review_enabled, load_agent_review_config, remote_allowed
from biz.agent.job_store import AgentJobStore
from biz.agent.review_request import build_prompt, from_webhook, is_reviewable_action
from biz.agent.workspace import WorkspaceManager
from biz.agent.workspace import redact_credentials
from biz.utils.log import logger


def reap_agent_review_workspaces() -> None:
    """Reclaim crashed jobs even when no new webhook arrives."""
    if not is_agent_review_enabled():
        return
    config = load_agent_review_config()
    store = AgentJobStore(config.job_db)
    manager = WorkspaceManager(config)
    for orphan in store.reap_stale(lease_seconds=config.job_lease_seconds):
        manager.reclaim_orphan(**orphan)


def dispatch_agent_review(provider: str, data: dict, *, gitlab_url: str = "") -> None:
    """Run one webhook review in a worker process.

    The function deliberately accepts only JSON-like data so it is safe to pass
    as a multiprocessing target from Flask's webhook route.
    """
    if not is_agent_review_enabled():
        return
    request = from_webhook(provider, data, gitlab_url=gitlab_url)
    if request is None:
        logger.warning("[Agent Review] could not normalize %s webhook payload", provider)
        return
    if not is_reviewable_action(request):
        logger.info("[Agent Review] ignoring non-reviewable %s action=%s", provider, request.action)
        return

    config = load_agent_review_config()
    request_urls = (request.remote_url, request.target_remote_url, request.review_url)
    if any(url and not remote_allowed(config, url) for url in request_urls):
        logger.error("[Agent Review] request host is not allowlisted: %s", request.review_url)
        return
    store = AgentJobStore(config.job_db)
    workspace_manager = WorkspaceManager(config)
    for orphan in store.reap_stale(lease_seconds=config.job_lease_seconds):
        workspace_manager.reclaim_orphan(**orphan)
    if not store.claim(
        key=request.event_key, provider=request.provider, review_url=request.review_url,
        backend=config.backend, source_branch=request.source_branch,
        lease_seconds=config.job_lease_seconds,
    ):
        logger.info("[Agent Review] duplicate job skipped: %s", request.event_key[:12])
        return

    context = None
    succeeded = False
    heartbeat_stop = threading.Event()
    heartbeat_interval = max(1.0, min(config.job_lease_seconds / 3, 60.0))

    def renew_lease() -> None:
        while not heartbeat_stop.wait(heartbeat_interval):
            if not store.heartbeat(request.event_key):
                logger.error("[Agent Review] lease lost before backend completed: %s", request.event_key[:12])
                return

    heartbeat_thread = threading.Thread(target=renew_lease, name="agent-review-lease", daemon=True)
    heartbeat_thread.start()

    def record_workspace(job_root, clone_path, source_repo) -> None:
        if not store.set_workspace(
            request.event_key,
            job_root=str(job_root),
            clone_path=str(clone_path) if clone_path else None,
            source_repo=str(source_repo) if source_repo else None,
        ):
            raise RuntimeError("agent review lease was lost during workspace preparation")

    try:
        if store.last_reclaimed_workspace:
            workspace_manager.reclaim_orphan(**store.last_reclaimed_workspace)
        context = workspace_manager.prepare(request, on_workspace_allocated=record_workspace)
        if not store.set_workspace(
            request.event_key,
            job_root=str(context.job_root),
            clone_path=str(context.clone_path) if context.clone_path else None,
            source_repo=str(context.source_repo),
        ):
            raise RuntimeError("agent review lease was lost before backend start")
        prompt = build_prompt(
            request, str(context.source_repo), str(context.job_root), context.latest_revision, config,
            skill_path=str(context.skill_path) if context.skill_path else None,
        )
        result = create_backend(config).run(
            prompt=prompt, job_root=context.job_root, source_repo=context.source_repo, config=config
        )
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=2)
        if not store.is_owner(request.event_key):
            raise RuntimeError("agent review lease was lost before completion")
        succeeded = True
        store.finish(request.event_key, status="completed")
        logger.info("[Agent Review] completed provider=%s backend=%s review=%s", request.provider, config.backend, request.review_url)
        # Keep the output available to structured log handlers without dumping
        # a potentially huge model transcript into the normal log line.
        logger.debug("[Agent Review] backend output chars=%d session=%s", len(result.output), result.session_id or "-")
    except Exception as exc:
        safe_error = redact_credentials(str(exc))[:2000]
        store.finish(request.event_key, status="failed", error=safe_error)
        logger.exception("[Agent Review] failed provider=%s review=%s: %s", request.provider, request.review_url, safe_error)
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=2)
        if context is not None:
            workspace_manager.cleanup(context, success=succeeded)
