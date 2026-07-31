from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from biz.agent.backends import ClaudeCliBackend, CodexCliBackend, OpenCodeServeBackend
from biz.agent.config import AgentReviewConfig, load_agent_review_config
from biz.agent.job_store import AgentJobStore
from biz.agent.review_request import AgentReviewRequest, from_webhook
from biz.agent.workspace import RepositoryResolver, WorkspaceManager, normalize_remote_url


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _local_repo(path: Path, remote: str) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("repo\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "initial")
    _git(path, "remote", "add", "origin", remote)
    return path


def test_normalize_remote_url_equates_https_and_ssh():
    assert normalize_remote_url("https://gitlab.example.com/team/payment.git") == "gitlab.example.com/team/payment"
    assert normalize_remote_url("git@gitlab.example.com:team/payment.git") == "gitlab.example.com/team/payment"
    assert normalize_remote_url("https://gitlab.example.com:8443/team/payment.git") != normalize_remote_url("https://gitlab.example.com:9443/team/payment.git")


def test_resolver_direct_path_and_bounded_recursive_search(tmp_path):
    root = tmp_path / "repos"
    direct = _local_repo(root / "team" / "payment", "git@gitlab.example.com:team/payment.git")
    config = AgentReviewConfig(repo_roots={"https://gitlab.example.com/team/": root / "team"}, discovery_max_depth=2)
    resolver = RepositoryResolver(config)
    assert resolver.resolve_local_repo("https://gitlab.example.com/team/payment.git") == direct.resolve()

    nested = _local_repo(root / "nested" / "one" / "two" / "payment", "https://gitlab.example.com/team/payment.git")
    shallow_config = AgentReviewConfig(repo_roots={"https://gitlab.example.com/team/": root}, discovery_max_depth=2)
    assert RepositoryResolver(shallow_config).resolve_local_repo("https://gitlab.example.com/team/payment.git") == direct.resolve()
    # The deeper repository is deliberately beyond the configured search limit.
    assert nested.exists()

    parent = tmp_path / "parent-search"
    fallback = _local_repo(parent / "team" / "payment", "https://gitlab.example.com/team/payment.git")
    missing_prefix = AgentReviewConfig(
        repo_roots={"https://gitlab.example.com/team/": parent / "not-yet-mounted"},
        discovery_max_depth=3,
    )
    assert RepositoryResolver(missing_prefix).resolve_local_repo("https://gitlab.example.com/team/payment.git") == fallback.resolve()


def test_review_request_prefers_github_head_repo_for_fork():
    data = {
        "action": "synchronize",
        "repository": {"full_name": "upstream/payment", "clone_url": "https://github.com/upstream/payment.git"},
        "pull_request": {
            "html_url": "https://github.com/upstream/payment/pull/4",
            "head": {"ref": "feature", "sha": "abc123", "repo": {"full_name": "fork/payment", "clone_url": "https://github.com/fork/payment.git"}},
            "base": {"ref": "main"},
        },
    }
    request = from_webhook("github", data)
    assert request is not None
    assert request.remote_url.endswith("fork/payment.git")
    assert request.source_branch == "feature"
    assert request.target_branch == "main"
    assert request.event_key
    assert request.target_project_path == "upstream/payment"


def test_gitlab_fork_request_uses_source_project_remote():
    data = {
        "object_kind": "merge_request",
        "project": {
            "path_with_namespace": "upstream/payment",
            "git_http_url": "https://gitlab.example.com/upstream/payment.git",
        },
        "source": {
            "path_with_namespace": "fork/payment",
            "git_http_url": "https://gitlab.example.com/fork/payment.git",
        },
        "object_attributes": {
            "action": "open",
            "url": "https://gitlab.example.com/upstream/payment/-/merge_requests/2",
            "source_branch": "feature",
            "target_branch": "main",
            "last_commit": {"id": "b" * 40},
        },
    }
    request = from_webhook("gitlab", data, gitlab_url="https://gitlab.example.com")
    assert request is not None
    assert request.project_path == "fork/payment"
    assert request.remote_url.endswith("fork/payment.git")
    assert request.target_project_path == "upstream/payment"


def test_job_store_skips_completed_and_allows_failed_retry(tmp_path):
    store = AgentJobStore(tmp_path / "jobs.db")
    kwargs = dict(key="key", provider="gitlab", review_url="https://gitlab.example.com/p/-/merge_requests/1", backend="codex", source_branch="main")
    assert store.claim(**kwargs)
    assert not store.claim(**kwargs)
    store.finish("key", status="failed", error="temporary")
    assert store.claim(**kwargs)
    store.finish("key", status="completed")
    assert not store.claim(**kwargs)


def test_job_store_reclaims_stale_running_job(tmp_path):
    store = AgentJobStore(tmp_path / "jobs.db")
    kwargs = dict(key="stale", provider="gitlab", review_url="https://gitlab.example.com/p/-/merge_requests/1", backend="codex", source_branch="main")
    assert store.claim(**kwargs, lease_seconds=3600)
    with sqlite3.connect(tmp_path / "jobs.db") as conn:
        conn.execute("UPDATE agent_review_jobs SET updated_at = ? WHERE idempotency_key = ?", ("2000-01-01T00:00:00+00:00", "stale"))
    assert store.claim(**kwargs, lease_seconds=3600)


def test_cleanup_removes_worktrees_but_not_external_local_repo(tmp_path):
    source = _local_repo(tmp_path / "source", "https://gitlab.example.com/team/payment.git")
    job_root = tmp_path / "jobs" / "job-1"
    job_root.mkdir(parents=True)
    child = job_root / "agent-choice"
    _git(source, "worktree", "add", "--detach", str(child), "main")
    config = AgentReviewConfig(worktree_parent=tmp_path / "jobs")
    manager = WorkspaceManager(config)
    from biz.agent.workspace import WorkspaceContext

    context = WorkspaceContext(source, job_root, None, "revision", "main")
    manager.cleanup(context, success=True)
    assert not child.exists()
    assert not job_root.exists()
    assert source.exists()


def test_prepare_clones_latest_branch_and_cleanup_removes_owned_clone(tmp_path):
    seed = _local_repo(tmp_path / "seed", "placeholder")
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True, text=True)
    _git(seed, "remote", "set-url", "origin", bare.as_uri())
    _git(seed, "push", "origin", "main")
    request = AgentReviewRequest(
        provider="gitlab", remote_url=bare.as_uri(), review_url="https://gitlab.example.com/team/payment/-/merge_requests/1",
        project_path="team/payment", source_branch="main", target_branch="main", revision_hint="", action="open", event_key="clone-job",
    )
    config = AgentReviewConfig(
        repo_roots={}, clone_parent=tmp_path / "clones", worktree_parent=tmp_path / "worktrees", clone_cleanup="always",
    )
    manager = WorkspaceManager(config)
    context = manager.prepare(request)
    assert context.clone_path is not None and context.clone_path.exists()
    assert len(context.latest_revision) == 40
    assert context.job_root.parent == config.worktree_parent
    clone_path = context.clone_path
    manager.cleanup(context, success=True)
    assert not clone_path.exists()
    assert not context.job_root.exists()


def test_prepare_uses_local_repo_as_read_only_seed(tmp_path):
    source = _local_repo(tmp_path / "repos" / "team" / "payment", "placeholder")
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True, text=True)
    _git(source, "remote", "set-url", "origin", bare.as_uri())
    _git(source, "push", "origin", "main")
    before = subprocess.run(
        ["git", "show-ref"], cwd=source, check=True, capture_output=True, text=True
    ).stdout
    request = AgentReviewRequest(
        provider="gitlab", remote_url=bare.as_uri(), review_url="https://gitlab.example.com/team/payment/-/merge_requests/2",
        project_path="team/payment", source_branch="main", target_branch="main", revision_hint="", action="open", event_key="local-seed",
    )
    config = AgentReviewConfig(
        repo_roots={bare.as_uri(): source}, clone_parent=tmp_path / "clones", worktree_parent=tmp_path / "worktrees",
    )
    context = WorkspaceManager(config).prepare(request)
    assert context.clone_path is not None
    assert context.source_repo.parent == context.job_root
    after = subprocess.run(
        ["git", "show-ref"], cwd=source, check=True, capture_output=True, text=True
    ).stdout
    assert after == before
    manager = WorkspaceManager(config)
    manager.cleanup(context, success=True)
    assert source.exists()
    assert not context.clone_path.exists()


def test_prepare_cleans_clone_when_post_clone_setup_fails(tmp_path):
    config = AgentReviewConfig(clone_parent=tmp_path / "clones", worktree_parent=tmp_path / "worktrees")
    manager = WorkspaceManager(config)
    request = AgentReviewRequest(
        provider="gitlab", remote_url="https://gitlab.example.com/team/payment.git", review_url="https://gitlab.example.com/team/payment/-/merge_requests/1",
        project_path="team/payment", source_branch="main", target_branch="main", revision_hint="", action="open", event_key="failed-prepare",
    )

    def fake_clone(_remote, _provider, target):
        target.mkdir()

    with patch.object(manager.resolver, "resolve_local_repo", return_value=None), patch.object(manager, "_run_git_clone", side_effect=fake_clone), patch.object(manager, "_fetch_source_branch", side_effect=RuntimeError("fetch failed")):
        try:
            manager.prepare(request)
        except RuntimeError as exc:
            assert str(exc) == "fetch failed"
        else:
            raise AssertionError("prepare should fail")
    assert list((tmp_path / "clones").iterdir()) == []


def test_opencode_backend_passes_job_directory_to_both_requests(tmp_path):
    config = AgentReviewConfig(opencode_api_url="http://opencode:4096", opencode_agent_name="code-reviewer")
    created = MagicMock()
    created.raise_for_status.return_value = None
    created.json.return_value = {"id": "session-1"}
    message = MagicMock()
    message.raise_for_status.return_value = None
    message.text = "done"
    with patch("biz.agent.backends.requests.post", side_effect=[created, message]) as post:
        result = OpenCodeServeBackend().run(prompt="review", job_root=tmp_path, source_repo=tmp_path, config=config)
    assert result.session_id == "session-1"
    assert post.call_args_list[0].kwargs["params"] == {"directory": str(tmp_path)}
    assert post.call_args_list[1].kwargs["params"] == {"directory": str(tmp_path)}
    assert post.call_args_list[1].kwargs["json"]["agent"] == "code-reviewer"
    materialized = json.loads((tmp_path / "opencode.json").read_text(encoding="utf-8"))
    assert str(tmp_path / ".agent-skill" / "SKILL.md") in materialized["agent"]["code-reviewer"]["prompt"]
    assert (tmp_path / "prompts" / "docs-searcher.md").exists()


def test_opencode_backend_materializes_custom_agent_name(tmp_path):
    config = AgentReviewConfig(opencode_agent_name="custom-reviewer")
    OpenCodeServeBackend._materialize_project_config(tmp_path, config)
    materialized = json.loads((tmp_path / "opencode.json").read_text(encoding="utf-8"))
    assert "custom-reviewer" in materialized["agent"]
    assert str(tmp_path / ".agent-skill" / "SKILL.md") in materialized["agent"]["custom-reviewer"]["prompt"]


def test_codex_backend_uses_workspace_write_without_external_source_access(tmp_path):
    config = AgentReviewConfig(codex_bin="codex")
    completed = MagicMock(returncode=0, stdout="ok", stderr="")
    with patch("biz.agent.backends.shutil.which", return_value="/bin/codex"), patch("biz.agent.backends.subprocess.run", return_value=completed) as run:
        result = CodexCliBackend().run(prompt="review", job_root=tmp_path, source_repo=tmp_path / "source", config=config)
    args = run.call_args.args[0]
    assert args[:4] == ["/bin/codex", "exec", "--sandbox", "workspace-write"]
    assert "--add-dir" not in args
    assert str(tmp_path / "source") not in args
    assert str(config.shared_review_skill.parent) not in args
    assert "GITLAB_ACCESS_TOKEN" not in run.call_args.kwargs["env"]
    assert "OPENAI_API_KEY" not in run.call_args.kwargs["env"]
    assert result.output == "ok"


def test_claude_backend_uses_accept_edits_without_permission_bypass(tmp_path):
    config = AgentReviewConfig(claude_bin="claude")
    completed = MagicMock(returncode=0, stdout="ok", stderr="")
    with patch("biz.agent.backends.shutil.which", return_value="/bin/claude"), patch("biz.agent.backends.subprocess.run", return_value=completed) as run:
        ClaudeCliBackend().run(prompt="review", job_root=tmp_path, source_repo=tmp_path / "source", config=config)
    args = run.call_args.args[0]
    assert args[:3] == ["/bin/claude", "-p", "--permission-mode"]
    assert "acceptEdits" in args
    assert str(tmp_path) in args
    assert str(config.shared_review_skill.parent) not in args
    assert "--dangerously-skip-permissions" not in args


def test_config_accepts_full_project_url_mapping(tmp_path, monkeypatch):
    config_file = tmp_path / "agent.yml"
    config_file.write_text(
        "repo_roots:\n  'https://gitlab.example.com/team/payment.git': '/srv/payment'\n"
        "discovery_max_depth: 5\nbackend: codex\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_REVIEW_CONFIG", str(config_file))
    config = load_agent_review_config()
    assert config.backend == "codex"
    assert config.discovery_max_depth == 5
    assert config.repo_roots["https://gitlab.example.com/team/payment.git"] == Path("/srv/payment")
