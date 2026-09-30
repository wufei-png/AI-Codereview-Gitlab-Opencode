"""Local validation of an unchanged provider-native Rolling Review Note receipt."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from biz.agent.review_request import AgentReviewRequest


@dataclass(frozen=True)
class DeliveryReceipt:
    raw: str | None = None
    note_id: str | None = None
    note_url: str | None = None
    error: str | None = None


def _positive_id(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value)
    return text if re.fullmatch(r"[1-9][0-9]*", text) else None


def _target_error(payload: dict, request: AgentReviewRequest, note_id: str) -> str | None:
    review = urlparse(request.review_url)
    match = re.search(r"/(?:pull|pulls|issues|merge_requests)/(\d+)/?$", review.path)
    if match is None:
        return "review URL has no recognized review number"
    number = match.group(1)
    project = request.target_project_path or request.project_path
    if request.provider == "gitlab":
        # noteable_id is the global database ID, not the MR IID.
        if "noteable_iid" in payload and str(payload["noteable_iid"]) != number:
            return "receipt belongs to another merge request"
        if "noteable_type" in payload and payload["noteable_type"] != "MergeRequest":
            return "receipt is not a merge request note"

    browser_paths = {review.path.rstrip("/")}
    if request.provider in {"github", "gitea"}:
        browser_paths.update({f"/{project}/issues/{number}", f"/{project}/pull/{number}", f"/{project}/pulls/{number}"})
    for field in ("html_url", "web_url", "noteable_url"):
        if not payload.get(field):
            continue
        candidate = urlparse(str(payload[field]))
        if (candidate.scheme, candidate.netloc, unquote(candidate.path).rstrip("/")) not in {
            (review.scheme, review.netloc, path) for path in browser_paths
        }:
            return f"{field} belongs to another review"
        fragment_id = re.fullmatch(r"(?:issuecomment-|note_|comment-)(\d+)", candidate.fragment)
        if fragment_id and fragment_id.group(1) != note_id:
            return f"{field} identifies another note"

    for field in ("issue_url", "pull_request_url", "url"):
        if not payload.get(field):
            continue
        candidate = urlparse(str(payload[field]))
        path = unquote(candidate.path).rstrip("/")
        native = re.search(r"/repos/(.+?)/(issues|pulls)/(.*)$", path)
        if native is None:
            continue  # GitLab does not expose this GitHub-shaped API field.
        allowed_hosts = {review.netloc}
        if request.provider == "github":
            configured_api = os.getenv("GITHUB_API_URL")
            if configured_api:
                allowed_hosts.add(urlparse(configured_api).netloc)
            elif review.hostname == "github.com":
                allowed_hosts.add("api.github.com")
        if candidate.netloc not in allowed_hosts or native.group(1) != project:
            return f"{field} belongs to another repository"
        resource = native.group(3)
        if resource not in {number, f"comments/{note_id}", f"{number}/comments/{note_id}"}:
            return f"{field} identifies another review or note"
    return None


def read_delivery_receipt(
    path: Path, request: AgentReviewRequest, source_revision: str, target_revision: str,
) -> DeliveryReceipt:
    """Confirm snapshot evidence locally; this does not perform provider readback."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return DeliveryReceipt(error="delivery receipt missing")
    except (OSError, UnicodeError) as exc:
        return DeliveryReceipt(error=f"delivery receipt unreadable: {type(exc).__name__}")
    try:
        payload = json.loads(raw)
    except ValueError:
        return DeliveryReceipt(raw=raw, error="delivery receipt is not JSON")
    if not isinstance(payload, dict):
        return DeliveryReceipt(raw=raw, error="delivery receipt is not a note object")
    note_id = _positive_id(payload.get("id"))
    if note_id is None:
        return DeliveryReceipt(raw=raw, error="delivery receipt has no valid note ID")
    body = payload.get("body") if "body" in payload else payload.get("note")
    if not isinstance(body, str):
        return DeliveryReceipt(raw=raw, error="delivery receipt has no note body")
    marker = f"<!-- {request.review_marker} -->"
    if body.count(marker) != 1:
        return DeliveryReceipt(raw=raw, error="delivery receipt must contain exactly one review marker")
    for label, revision in (("source", source_revision), ("target", target_revision)):
        if not revision or not re.search(rf"(?<![a-zA-Z0-9]){re.escape(revision)}(?![a-zA-Z0-9])", body):
            return DeliveryReceipt(raw=raw, error=f"delivery receipt does not match {label} revision")
    try:
        error = _target_error(payload, request, note_id)
    except ValueError:
        error = "delivery receipt has a malformed URL"
    if error:
        return DeliveryReceipt(raw=raw, error=error)
    note_url = payload.get("html_url") or payload.get("web_url") or payload.get("noteable_url") or ""
    return DeliveryReceipt(raw=raw, note_id=note_id, note_url=str(note_url))
