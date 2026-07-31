"""Small SQLite idempotency store for asynchronous agent review jobs."""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_stale(updated_at: str, lease_seconds: int) -> bool:
    try:
        updated = datetime.fromisoformat(updated_at)
    except ValueError:
        return True
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - updated).total_seconds() > lease_seconds


class AgentJobStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.claim_token: str | None = None
        self.last_reclaimed_workspace: dict[str, str | None] | None = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_review_jobs (
                    idempotency_key TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    review_url TEXT NOT NULL,
                    backend TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source_branch TEXT,
                    job_root TEXT,
                    clone_path TEXT,
                    source_repo TEXT,
                    lease_token TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(agent_review_jobs)")}
            if "source_repo" not in columns:
                conn.execute("ALTER TABLE agent_review_jobs ADD COLUMN source_repo TEXT")
            if "lease_token" not in columns:
                conn.execute("ALTER TABLE agent_review_jobs ADD COLUMN lease_token TEXT")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def claim(
        self, *, key: str, provider: str, review_url: str, backend: str,
        source_branch: str, lease_seconds: int = 3600,
    ) -> bool:
        now = _now()
        previous_token = self.claim_token
        self.claim_token = None
        self.last_reclaimed_workspace = None
        token = uuid.uuid4().hex
        with self._connect() as conn:
            # Serialize the read/update decision so concurrent webhook workers
            # cannot both launch the same review.
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status, updated_at, job_root, clone_path, source_repo FROM agent_review_jobs WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if row and row["status"] == "completed":
                self.claim_token = previous_token
                return False
            if row and row["status"] == "running" and not _is_stale(row["updated_at"], lease_seconds):
                self.claim_token = previous_token
                return False
            if row and row["status"] == "running":
                self.last_reclaimed_workspace = {
                    "source_repo": row["source_repo"],
                    "job_root": row["job_root"],
                    "clone_path": row["clone_path"],
                }
            if row:
                conn.execute(
                    "UPDATE agent_review_jobs SET provider=?, review_url=?, backend=?, status='running', source_branch=?, job_root=NULL, clone_path=NULL, source_repo=NULL, lease_token=?, error=NULL, updated_at=? WHERE idempotency_key=?",
                    (provider, review_url, backend, source_branch, token, now, key),
                )
            else:
                conn.execute(
                    "INSERT INTO agent_review_jobs (idempotency_key, provider, review_url, backend, status, source_branch, lease_token, created_at, updated_at) VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?)",
                    (key, provider, review_url, backend, source_branch, token, now, now),
                )
            self.claim_token = token
            return True

    def set_workspace(self, key: str, *, job_root: str, clone_path: str | None, source_repo: str | None) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE agent_review_jobs SET job_root=?, clone_path=?, source_repo=?, updated_at=? WHERE idempotency_key=? AND status='running' AND lease_token=?",
                (job_root, clone_path, source_repo, _now(), key, self.claim_token),
            )
            return cursor.rowcount == 1

    def heartbeat(self, key: str) -> bool:
        """Renew the current lease; a false result means this worker was fenced."""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE agent_review_jobs SET updated_at=? WHERE idempotency_key=? AND status='running' AND lease_token=?",
                (_now(), key, self.claim_token),
            )
            return cursor.rowcount == 1

    def is_owner(self, key: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM agent_review_jobs WHERE idempotency_key=? AND status='running' AND lease_token=?",
                (key, self.claim_token),
            ).fetchone()
            return row is not None

    def reap_stale(self, *, lease_seconds: int) -> list[dict[str, str | None]]:
        """Fence expired jobs and return their owned paths for filesystem cleanup."""
        reclaimed: list[dict[str, str | None]] = []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT idempotency_key, job_root, clone_path, source_repo, updated_at FROM agent_review_jobs WHERE status='running'"
            ).fetchall()
            for row in rows:
                if not _is_stale(row["updated_at"], lease_seconds):
                    continue
                reclaimed.append({
                    "source_repo": row["source_repo"],
                    "job_root": row["job_root"],
                    "clone_path": row["clone_path"],
                })
                conn.execute(
                    "UPDATE agent_review_jobs SET status='failed', lease_token=NULL, error=?, updated_at=? WHERE idempotency_key=? AND status='running'",
                    ("job lease expired; workspace reclaimed", _now(), row["idempotency_key"]),
                )
        return reclaimed

    def finish(self, key: str, *, status: str, error: str | None = None) -> None:
        if status not in {"completed", "failed"}:
            raise ValueError("job status must be completed or failed")
        with self._connect() as conn:
            conn.execute(
                "UPDATE agent_review_jobs SET status=?, error=?, updated_at=? WHERE idempotency_key=? AND status='running' AND lease_token=?",
                (status, error, _now(), key, self.claim_token),
            )
