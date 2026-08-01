from __future__ import annotations

import json
import sqlite3
import subprocess
from dataclasses import asdict
from pathlib import Path
from unittest.mock import MagicMock, patch

from biz.agent.backends import BackendExecutionError
from biz.agent.config import AgentReviewConfig, load_agent_review_config
from biz.agent.job_store import AgentJobStore
from biz.agent.review_request import AgentReviewRequest, from_webhook
from biz.agent.service import _parse_delivery_receipt, _preflight, _truncate_result, execute_claimed_job
from biz.agent.workspace import WorkspaceContext, WorkspaceManager


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


def test_config_defaults_to_unlimited_backend_and_optional_result_limit(tmp_path, monkeypatch):
    config_file = tmp_path / "agent.yml"
    config_file.write_text("backend: pi\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_REVIEW_CONFIG", str(config_file))
    config = load_agent_review_config()
    assert config.backend_timeout == -1
    assert config.agent_result_max_bytes is None
    assert config.pi_bin == "pi"

    monkeypatch.setenv("AGENT_RESULT_MAX_BYTES", "1024")
    assert load_agent_review_config().agent_result_max_bytes == 1024


def test_event_key_ignores_action_but_includes_target_revision():
    base = {
        "repository": {"full_name": "o/r", "clone_url": "https://github.com/o/r.git"},
        "pull_request": {
            "html_url": "https://github.com/o/r/pull/1",
            "head": {"ref": "feature", "sha": "source", "repo": {"full_name": "o/r", "clone_url": "https://github.com/o/r.git"}},
            "base": {"ref": "main", "sha": "target-a"},
        },
    }
    opened = from_webhook("github", {**base, "action": "opened"})
    updated = from_webhook("github", {**base, "action": "synchronize"})
    assert opened and updated and opened.event_key == updated.event_key
    changed = json.loads(json.dumps(base))
    changed["action"] = "synchronize"
    changed["pull_request"]["base"]["sha"] = "target-b"
    assert from_webhook("github", changed).event_key != opened.event_key


def test_queue_supersedes_queued_revision_and_serializes_review_url(tmp_path):
    store = AgentJobStore(tmp_path / "jobs.db")
    common = dict(provider="github", review_url="https://github.com/o/r/pull/1", backend="codex", source_branch="f", target_branch="main")
    assert store.enqueue(key="old", request_json='{"old": true}', **common)
    assert store.enqueue(key="new", request_json='{"new": true}', **common)
    row = store.claim_next()
    assert row and row["idempotency_key"] == "new"
    assert store.enqueue(key="latest", request_json='{"latest": true}', **common)
    assert store.claim_next() is None
    store.finish("new", status="failed")
    assert store.claim_next()["idempotency_key"] == "latest"
    with sqlite3.connect(tmp_path / "jobs.db") as conn:
        assert conn.execute("SELECT status FROM agent_review_jobs WHERE idempotency_key='old'").fetchone()[0] == "failed"


def test_confirmed_receipt_gates_previous_delivery_state(tmp_path):
    store = AgentJobStore(tmp_path / "jobs.db")
    kwargs = dict(provider="gitlab", review_url="https://gitlab.example/p/-/merge_requests/1", backend="pi", source_branch="f", target_branch="main")
    store.enqueue(key="one", request_json="{}", **kwargs)
    store.claim_next()
    assert store.set_revisions("one", source_revision="S1", target_revision="T1")
    store.mark_agent_started("one")
    store.finish("one", status="completed", delivery_status="unconfirmed")
    assert store.previous_delivery(kwargs["review_url"]) == {}

    store.enqueue(key="two", request_json="{}", **kwargs)
    store.claim_next()
    assert store.set_revisions("two", source_revision="S2", target_revision="T1")
    store.mark_agent_started("two")
    store.finish("two", status="completed", delivery_status="confirmed", note_id="42", note_url="https://note")
    assert store.previous_delivery(kwargs["review_url"]) == {
        "source_revision": "S2", "note_id": "42", "note_url": "https://note",
    }


def test_resolved_revision_deduplication_only_uses_latest_confirmed_snapshot(tmp_path):
    store = AgentJobStore(tmp_path / "jobs.db")
    common = dict(
        provider="gitlab", review_url="https://gitlab.example/p/-/merge_requests/1",
        backend="pi", source_branch="f", target_branch="main", request_json="{}",
    )

    def confirm(key, source, target, completed_at):
        assert store.enqueue(key=key, **common)
        assert store.claim_next()["idempotency_key"] == key
        assert store.set_revisions(key, source_revision=source, target_revision=target)
        assert store.mark_agent_started(key)
        store.finish(key, status="completed", delivery_status="confirmed")
        with sqlite3.connect(tmp_path / "jobs.db") as conn:
            conn.execute(
                "UPDATE agent_review_jobs SET completed_at=? WHERE idempotency_key=?",
                (completed_at, key),
            )

    confirm("s1", "S1", "T1", "2026-01-01T00:00:00+00:00")
    confirm("s2", "S2", "T1", "2026-01-02T00:00:00+00:00")

    assert store.enqueue(key="reverted", **common)
    assert store.claim_next()["idempotency_key"] == "reverted"
    assert store.set_revisions("reverted", source_revision="S1", target_revision="T1")
    store.finish("reverted", status="failed")

    assert store.enqueue(key="same-latest", **common)
    assert store.claim_next()["idempotency_key"] == "same-latest"
    assert not store.set_revisions("same-latest", source_revision="S2", target_revision="T1")


def test_failed_backend_still_confirms_receipt_and_records_cleanup_error(tmp_path):
    request = AgentReviewRequest(
        provider="github", remote_url="https://github.com/o/r.git",
        target_remote_url="https://github.com/o/r.git",
        review_url="https://github.com/o/r/pull/1", project_path="o/r",
        target_project_path="o/r", source_branch="feature", target_branch="main",
        revision_hint="S1", target_revision_hint="T1", action="update", event_key="job-1",
    )
    config = AgentReviewConfig(
        backend="codex", job_db=tmp_path / "jobs.db",
        clone_parent=tmp_path / "clones", worktree_parent=tmp_path / "jobs",
    )
    store = AgentJobStore(config.job_db)
    assert store.enqueue(
        key=request.event_key, provider=request.provider, review_url=request.review_url,
        backend=config.backend, source_branch=request.source_branch,
        target_branch=request.target_branch,
        request_json=json.dumps(asdict(request)),
    )
    row = store.claim_next()
    assert row is not None
    job_root = tmp_path / "jobs" / "job-1"
    source_repo = job_root / ".agent-source"
    source_repo.mkdir(parents=True)
    context = WorkspaceContext(
        source_repo=source_repo, job_root=job_root, clone_path=None,
        source_revision="S1", target_revision="T1", source_branch="feature",
    )
    backend = MagicMock()

    def fail_after_delivery(**_kwargs):
        (job_root / ".agent-delivery-receipt.json").write_text(
            '{"id": 9, "html_url": "https://github.com/o/r/pull/1#issuecomment-9"}',
            encoding="utf-8",
        )
        raise BackendExecutionError("backend failed", output="partial", stderr="boom")

    backend.run.side_effect = fail_after_delivery
    with patch("biz.agent.service._preflight"), patch(
        "biz.agent.service.WorkspaceManager.prepare", return_value=context
    ), patch(
        "biz.agent.service.WorkspaceManager.cleanup", side_effect=OSError("cleanup denied")
    ), patch("biz.agent.service.create_backend", return_value=backend):
        execute_claimed_job(store, row, config)

    with sqlite3.connect(config.job_db) as conn:
        saved = conn.execute(
            "SELECT status, delivery_status, previous_review_note_id, agent_result, cleanup_error "
            "FROM agent_review_jobs WHERE idempotency_key=?",
            (request.event_key,),
        ).fetchone()
    assert saved == ("failed", "confirmed", "9", "partial", "cleanup denied")


def test_opencode_preflight_does_not_require_worker_local_platform_cli(tmp_path):
    request = AgentReviewRequest(
        provider="gitea", remote_url="https://gitea.example/o/r.git",
        target_remote_url="https://gitea.example/o/r.git",
        review_url="https://gitea.example/o/r/pulls/1", project_path="o/r",
        target_project_path="o/r", source_branch="feature", target_branch="main",
        revision_hint="S1", action="update", event_key="job-1",
    )
    with patch("biz.agent.service.shutil.which", return_value=None):
        _preflight(AgentReviewConfig(backend="opencode"), request)


def test_cleanup_raises_aggregated_job_and_clone_errors(tmp_path):
    job_root = tmp_path / "jobs" / "job-1"
    source_repo = job_root / ".agent-source"
    clone_path = tmp_path / "clones" / "clone-1"
    source_repo.mkdir(parents=True)
    clone_path.mkdir(parents=True)
    config = AgentReviewConfig(
        worktree_parent=tmp_path / "jobs", clone_parent=tmp_path / "clones",
        clone_cleanup="always",
    )
    context = WorkspaceContext(
        source_repo=source_repo, job_root=job_root, clone_path=clone_path,
        source_revision="S1", target_revision="T1", source_branch="feature",
        source_repo_owned=True,
    )
    manager = WorkspaceManager(config)
    with patch.object(manager, "_remove_worktrees"), patch(
        "biz.agent.workspace.shutil.rmtree",
        side_effect=[OSError("job denied"), OSError("clone denied")],
    ):
        try:
            manager.cleanup(context, success=True)
        except RuntimeError as exc:
            assert "job workspace cleanup: job denied" in str(exc)
            assert "clone cleanup: clone denied" in str(exc)
        else:
            raise AssertionError("cleanup should report removal failures")
    assert context.cleaned


def test_result_truncation_keeps_head_and_tail_and_receipt_stays_native(tmp_path):
    clipped, truncated = _truncate_result("HEAD" + "x" * 100 + "TAIL", 64)
    assert truncated and clipped.startswith("HEAD") and clipped.endswith("TAIL")
    assert "truncated" in clipped
    receipt = tmp_path / "receipt.json"
    receipt.write_text('{"id": 7, "html_url": "https://example/note/7", "provider_extra": true}', encoding="utf-8")
    raw, note_id, note_url = _parse_delivery_receipt("github", receipt)
    assert json.loads(raw)["provider_extra"] is True
    assert (note_id, note_url) == ("7", "https://example/note/7")


def test_fork_fetches_latest_target_from_upstream(tmp_path):
    upstream = tmp_path / "upstream.git"
    fork = tmp_path / "fork.git"
    subprocess.run(["git", "init", "--bare", str(upstream)], check=True, capture_output=True)
    subprocess.run(["git", "init", "--bare", str(fork)], check=True, capture_output=True)
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "Test")
    (work / "base.txt").write_text("base\n", encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "base")
    base = _git(work, "rev-parse", "HEAD")
    _git(work, "remote", "add", "upstream", upstream.as_uri())
    _git(work, "remote", "add", "fork", fork.as_uri())
    _git(work, "push", "upstream", "main")
    _git(work, "push", "fork", "main")
    _git(work, "checkout", "-b", "feature")
    (work / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "feature")
    source = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "fork", "feature")
    _git(work, "checkout", "main")
    (work / "target.txt").write_text("upstream advanced\n", encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "target advanced")
    target = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "upstream", "main")

    config = AgentReviewConfig(
        clone_parent=tmp_path / "clones", worktree_parent=tmp_path / "jobs", clone_cleanup="always",
    )
    request = AgentReviewRequest(
        provider="gitlab", remote_url=fork.as_uri(), target_remote_url=upstream.as_uri(),
        review_url="https://gitlab.example/upstream/r/-/merge_requests/1",
        project_path="fork/r", target_project_path="upstream/r", source_branch="feature",
        target_branch="main", revision_hint=source, action="update", event_key="fork-job",
    )
    manager = WorkspaceManager(config)
    context = manager.prepare(request)
    try:
        assert context.source_revision == source
        assert context.target_revision == target
        assert _git(context.source_repo, "merge-base", context.target_revision, context.source_revision) == base
    finally:
        manager.cleanup(context, success=True)
