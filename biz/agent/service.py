"""Durable webhook enqueueing and execution for external Agent reviews."""
from __future__ import annotations

import json
import shutil
import threading
from dataclasses import asdict, replace
from pathlib import Path

from biz.agent.backends import BackendExecutionError, create_backend
from biz.agent.config import AgentReviewConfig, is_agent_review_enabled, load_agent_review_config, remote_allowed
from biz.agent.job_store import AgentJobStore
from biz.agent.review_request import AgentReviewRequest, build_prompt, from_webhook, is_reviewable_action
from biz.agent.workspace import WorkspaceManager, redact_credentials
from biz.utils.log import logger


class NonRetryableConfigurationError(RuntimeError):
    pass


def _truncate_result(value: str, limit: int | None) -> tuple[str, bool]:
    if limit is None:
        return value, False
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value, False
    marker = b"\n...[truncated]...\n"
    if limit <= len(marker) + 1:
        head = (limit + 1) // 2
        tail = limit - head
        clipped = encoded[:head] + (encoded[-tail:] if tail else b"")
        return clipped.decode("utf-8", errors="replace"), True
    budget = max(0, limit - len(marker))
    head = budget // 2
    tail = budget - head
    clipped = encoded[:head] + marker + (encoded[-tail:] if tail else b"")
    return clipped.decode("utf-8", errors="replace"), True


def _parse_delivery_receipt(provider: str, path: Path) -> tuple[str, str, str] | None:
    if not path.is_file():
        return None
    raw = path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get("id") is None:
        return None
    note_id = str(payload["id"])
    note_url = str(payload.get("html_url") or payload.get("web_url") or "")
    if provider == "gitlab" and not note_url:
        note_url = str(payload.get("noteable_url") or "")
    return raw, note_id, note_url


def _preflight(config: AgentReviewConfig, request: AgentReviewRequest) -> None:
    agent_bin = {
        "codex": config.codex_bin,
        "claude": config.claude_bin,
        "pi": config.pi_bin,
    }.get(config.backend)
    if agent_bin and not (shutil.which(agent_bin) or Path(agent_bin).exists()):
        raise NonRetryableConfigurationError(f"agent CLI not found: {agent_bin}")
    platform_cli = config.platform_clis.get(request.provider, request.platform_cli)
    if not (shutil.which(platform_cli) or Path(platform_cli).exists()):
        raise NonRetryableConfigurationError(f"platform CLI not found: {platform_cli}")


def enqueue_agent_review(provider: str, data: dict, *, gitlab_url: str = "") -> bool:
    if not is_agent_review_enabled():
        return False
    request = from_webhook(provider, data, gitlab_url=gitlab_url)
    if request is None or not is_reviewable_action(request):
        return False
    config = load_agent_review_config()
    urls = (request.remote_url, request.target_remote_url, request.review_url)
    if any(url and not remote_allowed(config, url) for url in urls):
        logger.error("[Agent Review] request host is not allowlisted: %s", request.review_url)
        return False
    return AgentJobStore(config.job_db).enqueue(
        key=request.event_key, provider=request.provider, review_url=request.review_url,
        backend=config.backend, source_branch=request.source_branch, target_branch=request.target_branch,
        request_json=json.dumps(asdict(request), ensure_ascii=False, sort_keys=True),
    )


def dispatch_agent_review(provider: str, data: dict, *, gitlab_url: str = "") -> None:
    """Backward-compatible webhook entrypoint; now only enqueues durable work."""
    enqueue_agent_review(provider, data, gitlab_url=gitlab_url)


def reap_agent_review_workspaces() -> None:
    if not is_agent_review_enabled():
        return
    config = load_agent_review_config()
    store = AgentJobStore(config.job_db)
    manager = WorkspaceManager(config)
    for orphan in store.reap_stale(lease_seconds=config.job_lease_seconds):
        manager.reclaim_orphan(**orphan)


def execute_claimed_job(store: AgentJobStore, row: dict[str, object], config: AgentReviewConfig) -> None:
    key = str(row["idempotency_key"])
    request = AgentReviewRequest(**json.loads(str(row["request_json"])))
    job_config = replace(config, backend=str(row["backend"]))
    manager = WorkspaceManager(job_config)
    context = None
    agent_started = False
    backend_succeeded = False
    status = "failed"
    error: str | None = None
    output = ""
    cleanup_error: str | None = None
    delivery_status = "not_attempted"
    receipt_raw = note_id = note_url = None
    heartbeat_stop = threading.Event()
    interval = max(0.1, min(job_config.job_lease_seconds / 3, 60.0))

    def heartbeat() -> None:
        while not heartbeat_stop.wait(interval):
            if not store.heartbeat(key):
                return

    heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
    heartbeat_thread.start()

    def record_workspace(job_root: Path, clone_path: Path | None, source_repo: Path | None) -> None:
        if not store.set_workspace(
            key, job_root=str(job_root), clone_path=str(clone_path) if clone_path else None,
            source_repo=str(source_repo) if source_repo else None,
        ):
            raise RuntimeError("agent review lease lost during workspace preparation")

    try:
        _preflight(job_config, request)
        context = manager.prepare(request, on_workspace_allocated=record_workspace)
        previous = store.previous_delivery(request.review_url, exclude_key=key)
        if not store.set_revisions(
            key, source_revision=context.source_revision, target_revision=context.target_revision,
            previous_source_revision=previous.get("source_revision", ""),
            previous_note_id=previous.get("note_id", ""), previous_note_url=previous.get("note_url", ""),
        ):
            status = "completed"
            delivery_status = "confirmed"
            note_id = previous.get("note_id") or None
            note_url = previous.get("note_url") or None
            return
        prompt = build_prompt(
            request, str(context.source_repo), str(context.job_root),
            context.source_revision, context.target_revision, job_config,
            skill_path=str(context.skill_path) if context.skill_path else None,
            previous_reviewed_source_revision=previous.get("source_revision", ""),
            previous_review_note_id=previous.get("note_id", ""),
        )
        if not store.mark_agent_started(key):
            raise RuntimeError("agent review lease lost before backend start")
        agent_started = True
        delivery_status = "unconfirmed"
        result = create_backend(job_config).run(
            prompt=prompt, job_root=context.job_root, source_repo=context.source_repo, config=job_config,
        )
        output = result.output
        backend_succeeded = True
        status = "completed"
        try:
            receipt = _parse_delivery_receipt(request.provider, context.job_root / ".agent-delivery-receipt.json")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("[Agent Review] invalid delivery receipt: %s", redact_credentials(str(exc)))
            receipt = None
        if receipt:
            receipt_raw, note_id, note_url = receipt
            delivery_status = "confirmed"
    except BackendExecutionError as exc:
        output = exc.output
        error = redact_credentials(exc.stderr or str(exc))[:2000]
        status = "timed_out" if exc.timed_out else "failed"
    except Exception as exc:
        error = redact_credentials(str(exc))[:2000]
        if not agent_started and not isinstance(exc, NonRetryableConfigurationError):
            if store.retry_before_agent(key, error=error):
                return
        status = "failed"
    finally:
        if context is not None:
            try:
                manager.cleanup(context, success=backend_succeeded)
            except Exception as exc:
                cleanup_error = redact_credentials(str(exc))[:2000]
        result_text, truncated = _truncate_result(output, job_config.agent_result_max_bytes)
        if store.is_owner(key):
            store.finish(
                key, status=status, error=error, agent_result=result_text,
                result_truncated=truncated, cleanup_error=cleanup_error,
                delivery_status=delivery_status, delivery_receipt=receipt_raw,
                note_id=note_id, note_url=note_url,
            )
        logger.info(
            "[Agent Review] finished status=%s delivery=%s backend=%s review=%s",
            status, delivery_status, job_config.backend, request.review_url,
        )
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=2)
