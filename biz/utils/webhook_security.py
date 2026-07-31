"""Webhook authentication for the external Agent dispatch path."""
from __future__ import annotations

import hashlib
import hmac
import os
import base64
import binascii
import time


def _secret(provider: str) -> str:
    names = {
        "gitlab": ("GITLAB_WEBHOOK_SECRET", "GITLAB_ACCESS_TOKEN"),
        "github": ("GITHUB_WEBHOOK_SECRET", "GITHUB_ACCESS_TOKEN"),
        "gitea": ("GITEA_WEBHOOK_SECRET", "GITEA_ACCESS_TOKEN"),
    }
    for name in names.get(provider, ())[:1]:
        value = os.environ.get(name)
        if value:
            return value
    if os.environ.get("AGENT_ALLOW_ACCESS_TOKEN_WEBHOOK_FALLBACK", "0").lower() in {"1", "true", "yes", "on"}:
        for name in names.get(provider, ())[1:]:
            value = os.environ.get(name)
            if value:
                return value
    return ""


def _hmac_matches(secret: str, body: bytes, header_value: str, *, prefix: str = "sha256=") -> bool:
    if not header_value:
        return False
    expected = prefix + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header_value)


def _gitlab_signed_matches(signing_token: str, headers, body: bytes) -> bool:
    if not signing_token:
        return False
    message_id = headers.get("webhook-id", "")
    timestamp = headers.get("webhook-timestamp", "")
    received = headers.get("webhook-signature", "")
    if not (message_id and timestamp and received):
        return False
    try:
        if abs(time.time() - int(timestamp)) > int(os.environ.get("GITLAB_WEBHOOK_TOLERANCE_SECONDS", "300")):
            return False
        raw_key = base64.b64decode(signing_token.removeprefix("whsec_"), validate=True)
    except (ValueError, TypeError, binascii.Error):
        return False
    message = f"{message_id}.{timestamp}.".encode("utf-8") + body
    digest = base64.b64encode(hmac.new(raw_key, message, hashlib.sha256).digest()).decode("ascii")
    expected = f"v1,{digest}"
    return any(hmac.compare_digest(expected, item) for item in received.split())


def verify_webhook(provider: str, headers, body: bytes) -> bool:
    """Verify a configured webhook secret or HMAC signature.

    Legacy token headers remain accepted when a matching access-token env var
    is configured. External Agent dispatch never accepts an absent secret.
    """
    provider = provider.lower()
    secret = _secret(provider)
    if not secret:
        if provider == "gitlab" and headers.get("webhook-signature"):
            return _gitlab_signed_matches(os.environ.get("GITLAB_WEBHOOK_SIGNING_TOKEN", ""), headers, body)
        return False
    if provider == "gitlab":
        signing_token = os.environ.get("GITLAB_WEBHOOK_SIGNING_TOKEN")
        if headers.get("webhook-signature") and signing_token:
            return _gitlab_signed_matches(signing_token, headers, body)
        return hmac.compare_digest(headers.get("X-Gitlab-Token", ""), secret)
    if provider == "github":
        signature = headers.get("X-Hub-Signature-256", "")
        if signature:
            return _hmac_matches(secret, body, signature)
        return hmac.compare_digest(headers.get("X-GitHub-Token", ""), secret)
    if provider == "gitea":
        signature = headers.get("X-Gitea-Signature", "")
        if signature:
            return _hmac_matches(secret, body, signature, prefix="")
        signature = headers.get("X-Hub-Signature-256", "")
        if signature:
            return _hmac_matches(secret, body, signature)
        return hmac.compare_digest(headers.get("X-Gitea-Token", ""), secret)
    return False
