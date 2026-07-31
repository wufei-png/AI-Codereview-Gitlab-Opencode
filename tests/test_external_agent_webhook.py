from __future__ import annotations

from unittest.mock import patch

from flask import Flask

from biz.api.routes.webhook import handle_github_webhook, handle_gitlab_webhook


GITHUB_PR = {
    "action": "opened",
    "repository": {"full_name": "team/payment", "clone_url": "https://github.com/team/payment.git"},
    "pull_request": {
        "html_url": "https://github.com/team/payment/pull/1",
        "head": {"ref": "feature", "sha": "a" * 40, "repo": {"full_name": "team/payment", "clone_url": "https://github.com/team/payment.git"}},
        "base": {"ref": "main"},
    },
}


def test_cli_only_github_webhook_does_not_require_project_token(monkeypatch):
    monkeypatch.setenv("LLM_REVIEW_ENABLED", "0")
    monkeypatch.setenv("AGENT_REVIEW_ENABLED", "1")
    app = Flask(__name__)
    with app.test_request_context("/review/webhook", headers={"X-GitHub-Event": "pull_request"}):
        with patch("biz.api.routes.webhook.handle_agent_queue") as agent_queue, patch("biz.api.routes.webhook.handle_queue"):
            response, status = handle_github_webhook("pull_request", GITHUB_PR)
    assert status == 200
    agent_queue.assert_called_once()
    assert agent_queue.call_args.args[1:3] == ("github", GITHUB_PR)


def test_cli_only_gitlab_webhook_does_not_require_project_token(monkeypatch):
    monkeypatch.setenv("LLM_REVIEW_ENABLED", "0")
    monkeypatch.setenv("AGENT_REVIEW_ENABLED", "1")
    monkeypatch.setenv("GITLAB_URL", "https://gitlab.example.com")
    data = {
        "object_kind": "merge_request",
        "project": {
            "path_with_namespace": "team/payment",
            "git_http_url": "https://gitlab.example.com/team/payment.git",
        },
        "object_attributes": {
            "action": "open",
            "url": "https://gitlab.example.com/team/payment/-/merge_requests/1",
            "source_branch": "feature",
            "target_branch": "main",
            "last_commit": {"id": "a" * 40},
        },
    }
    app = Flask(__name__)
    with app.test_request_context("/review/webhook"):
        with patch("biz.api.routes.webhook.handle_agent_queue") as agent_queue, patch("biz.api.routes.webhook.handle_queue"):
            _response, status = handle_gitlab_webhook(data)
    assert status == 200
    agent_queue.assert_called_once()
    assert agent_queue.call_args.args[1:3] == ("gitlab", data)
    assert agent_queue.call_args.kwargs["gitlab_url"] == "https://gitlab.example.com"
