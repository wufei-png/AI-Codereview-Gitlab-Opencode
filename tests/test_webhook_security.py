from __future__ import annotations

import hashlib
import hmac
import base64
import time

from biz.utils.webhook_security import verify_webhook


def test_github_hmac_signature_is_required_and_verified(monkeypatch):
    body = b'{"action":"opened"}'
    secret = "webhook-secret"
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", secret)
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_webhook("github", {"X-Hub-Signature-256": f"sha256={digest}"}, body)
    assert not verify_webhook("github", {"X-Hub-Signature-256": "sha256=bad"}, body)


def test_gitlab_token_header_requires_configured_secret(monkeypatch):
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "gitlab-secret")
    assert verify_webhook("gitlab", {"X-Gitlab-Token": "gitlab-secret"}, b"payload")
    assert not verify_webhook("gitlab", {"X-Gitlab-Token": "wrong"}, b"payload")


def test_gitlab_standard_webhook_signature(monkeypatch):
    raw_key = b"0123456789abcdef0123456789abcdef"
    token = "whsec_" + base64.b64encode(raw_key).decode()
    body = b'{"object_kind":"merge_request"}'
    message_id = "msg-1"
    timestamp = str(int(time.time()))
    message = f"{message_id}.{timestamp}.".encode() + body
    digest = base64.b64encode(hmac.new(raw_key, message, hashlib.sha256).digest()).decode()
    monkeypatch.setenv("GITLAB_WEBHOOK_SIGNING_TOKEN", token)
    headers = {"webhook-id": message_id, "webhook-timestamp": timestamp, "webhook-signature": f"v1,{digest}"}
    assert verify_webhook("gitlab", headers, body)


def test_gitlab_standard_signature_requires_signing_token(monkeypatch):
    body = b"payload"
    headers = {
        "webhook-id": "msg-1",
        "webhook-timestamp": str(int(time.time())),
        "webhook-signature": "v1,invalid",
    }
    monkeypatch.delenv("GITLAB_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("GITLAB_WEBHOOK_SIGNING_TOKEN", raising=False)
    assert not verify_webhook("gitlab", headers, body)


def test_gitea_signature_is_raw_hex_without_prefix(monkeypatch):
    secret = "gitea-secret"
    body = b'{"action":"synchronize"}'
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    monkeypatch.setenv("GITEA_WEBHOOK_SECRET", secret)
    assert verify_webhook("gitea", {"X-Gitea-Signature": digest}, body)
    assert not verify_webhook("gitea", {"X-Gitea-Signature": f"sha256={digest}"}, body)


def test_missing_external_webhook_secret_is_rejected(monkeypatch):
    monkeypatch.delenv("GITEA_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("GITEA_ACCESS_TOKEN", raising=False)
    assert not verify_webhook("gitea", {}, b"payload")
