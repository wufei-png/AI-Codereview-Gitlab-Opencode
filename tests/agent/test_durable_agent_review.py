from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

from biz.agent.config import AgentReviewConfig, load_agent_review_config
from biz.agent.job_store import AgentJobStore
from biz.agent.review_request import AgentReviewRequest, from_webhook
from biz.agent.service import _parse_delivery_receipt, _truncate_result
from biz.agent.workspace import WorkspaceManager


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
