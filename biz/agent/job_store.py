"""Small SQLite idempotency store for asynchronous agent review jobs."""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
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
            additions = {
                "request_json": "TEXT",
                "target_branch": "TEXT",
                "source_revision": "TEXT",
                "target_revision": "TEXT",
                "previous_reviewed_source_revision": "TEXT",
                "previous_review_note_id": "TEXT",
                "previous_review_note_url": "TEXT",
                "delivery_status": "TEXT NOT NULL DEFAULT 'not_attempted'",
                "delivery_receipt": "TEXT",
                "agent_result": "TEXT",
                "result_truncated": "INTEGER NOT NULL DEFAULT 0",
                "cleanup_error": "TEXT",
                "agent_started": "INTEGER NOT NULL DEFAULT 0",
                "attempt": "INTEGER NOT NULL DEFAULT 0",
                "available_at": "TEXT",
                "started_at": "TEXT",
                "completed_at": "TEXT",
            }
            for name, declaration in additions.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE agent_review_jobs ADD COLUMN {name} {declaration}")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_jobs_queue ON agent_review_jobs(status, available_at, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_jobs_review ON agent_review_jobs(review_url, status)")

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

    def enqueue(
        self, *, key: str, provider: str, review_url: str, backend: str,
        source_branch: str, target_branch: str, request_json: str,
    ) -> bool:
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM agent_review_jobs WHERE idempotency_key=?", (key,)
            ).fetchone():
                return False
            conn.execute(
                "UPDATE agent_review_jobs SET status='failed', error='superseded by newer queued revision', completed_at=?, updated_at=? "
                "WHERE review_url=? AND status='queued'",
                (now, now, review_url),
            )
            conn.execute(
                """INSERT INTO agent_review_jobs
                (idempotency_key, provider, review_url, backend, status, source_branch,
                 target_branch, request_json, delivery_status, available_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, 'not_attempted', ?, ?, ?)""",
                (key, provider, review_url, backend, source_branch, target_branch, request_json, now, now, now),
            )
            return True

    def claim_next(self) -> dict[str, object] | None:
        now = _now()
        token = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT * FROM agent_review_jobs AS candidate
                WHERE candidate.status='queued'
                  AND COALESCE(candidate.available_at, candidate.created_at) <= ?
                  AND NOT EXISTS (
                    SELECT 1 FROM agent_review_jobs AS active
                    WHERE active.review_url=candidate.review_url AND active.status='running'
                  )
                ORDER BY candidate.created_at, candidate.idempotency_key LIMIT 1""",
                (now,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE agent_review_jobs SET status='running', lease_token=?, attempt=attempt+1, "
                "started_at=COALESCE(started_at, ?), updated_at=? WHERE idempotency_key=? AND status='queued'",
                (token, now, now, row["idempotency_key"]),
            )
            self.claim_token = token
            return dict(row)

    def mark_agent_started(self, key: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE agent_review_jobs SET agent_started=1, delivery_status='unconfirmed', updated_at=? "
                "WHERE idempotency_key=? AND status='running' AND lease_token=?",
                (_now(), key, self.claim_token),
            )
            return cursor.rowcount == 1

    def retry_before_agent(self, key: str, *, error: str, max_retries: int = 3) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT attempt, agent_started FROM agent_review_jobs WHERE idempotency_key=? AND lease_token=?",
                (key, self.claim_token),
            ).fetchone()
            if row is None or row["agent_started"] or row["attempt"] > max_retries:
                return False
            delay = min(300, 5 * (2 ** max(0, row["attempt"] - 1)))
            available = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
            conn.execute(
                "UPDATE agent_review_jobs SET status='queued', lease_token=NULL, error=?, available_at=?, updated_at=? "
                "WHERE idempotency_key=? AND lease_token=?",
                (error, available, _now(), key, self.claim_token),
            )
            return True

    def set_revisions(
        self, key: str, *, source_revision: str, target_revision: str,
        previous_source_revision: str = "", previous_note_id: str = "", previous_note_url: str = "",
    ) -> bool:
        with self._connect() as conn:
            duplicate = conn.execute(
                "SELECT 1 FROM agent_review_jobs WHERE idempotency_key=("
                "SELECT candidate.idempotency_key FROM agent_review_jobs AS candidate "
                "WHERE candidate.review_url=(SELECT review_url FROM agent_review_jobs WHERE idempotency_key=?) "
                "AND candidate.delivery_status='confirmed' AND candidate.idempotency_key<>? "
                "ORDER BY candidate.completed_at DESC, candidate.updated_at DESC LIMIT 1"
                ") AND source_revision=? AND target_revision=?",
                (key, key, source_revision, target_revision),
            ).fetchone()
            cursor = conn.execute(
                "UPDATE agent_review_jobs SET source_revision=?, target_revision=?, previous_reviewed_source_revision=?, "
                "previous_review_note_id=?, previous_review_note_url=?, updated_at=? "
                "WHERE idempotency_key=? AND status='running' AND lease_token=?",
                (source_revision, target_revision, previous_source_revision or None, previous_note_id or None,
                 previous_note_url or None, _now(), key, self.claim_token),
            )
            return cursor.rowcount == 1 and not bool(duplicate)

    def previous_delivery(self, review_url: str, *, exclude_key: str = "") -> dict[str, str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT source_revision, previous_review_note_id, previous_review_note_url FROM agent_review_jobs "
                "WHERE review_url=? AND delivery_status='confirmed' AND idempotency_key<>? "
                "ORDER BY completed_at DESC, updated_at DESC LIMIT 1",
                (review_url, exclude_key),
            ).fetchone()
            if row is None:
                return {}
            return {
                "source_revision": row["source_revision"] or "",
                "note_id": row["previous_review_note_id"] or "",
                "note_url": row["previous_review_note_url"] or "",
            }

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
                "SELECT idempotency_key, job_root, clone_path, source_repo, updated_at, agent_started, attempt "
                "FROM agent_review_jobs WHERE status='running'"
            ).fetchall()
            for row in rows:
                if not _is_stale(row["updated_at"], lease_seconds):
                    continue
                reclaimed.append({
                    "source_repo": row["source_repo"],
                    "job_root": row["job_root"],
                    "clone_path": row["clone_path"],
                })
                if not row["agent_started"] and row["attempt"] <= 3:
                    delay = min(300, 5 * (2 ** max(0, row["attempt"] - 1)))
                    available = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
                    conn.execute(
                        "UPDATE agent_review_jobs SET status='queued', lease_token=NULL, error=?, available_at=?, updated_at=? "
                        "WHERE idempotency_key=? AND status='running'",
                        ("job lease expired before Agent start; retrying", available, _now(), row["idempotency_key"]),
                    )
                else:
                    conn.execute(
                        "UPDATE agent_review_jobs SET status='failed', lease_token=NULL, delivery_status='unconfirmed', "
                        "error=?, completed_at=?, updated_at=? WHERE idempotency_key=? AND status='running'",
                        ("job lease expired after Agent start; not safe to retry", _now(), _now(), row["idempotency_key"]),
                    )
        return reclaimed

    def finish(
        self, key: str, *, status: str, error: str | None = None,
        agent_result: str | None = None, result_truncated: bool = False,
        cleanup_error: str | None = None, delivery_status: str | None = None,
        delivery_receipt: str | None = None, note_id: str | None = None, note_url: str | None = None,
    ) -> None:
        if status not in {"completed", "failed", "timed_out"}:
            raise ValueError("job status must be completed, failed, or timed_out")
        if delivery_status not in {None, "not_attempted", "confirmed", "unconfirmed"}:
            raise ValueError("invalid delivery status")
        with self._connect() as conn:
            conn.execute(
                "UPDATE agent_review_jobs SET status=?, error=?, agent_result=?, result_truncated=?, cleanup_error=?, "
                "delivery_status=COALESCE(?, delivery_status), delivery_receipt=?, "
                "previous_review_note_id=COALESCE(?, previous_review_note_id), previous_review_note_url=COALESCE(?, previous_review_note_url), "
                "completed_at=?, updated_at=?, lease_token=NULL WHERE idempotency_key=? AND status='running' AND lease_token=?",
                (status, error, agent_result, int(result_truncated), cleanup_error, delivery_status, delivery_receipt,
                 note_id, note_url, _now(), _now(), key, self.claim_token),
            )

    def delete_expired(self, *, retention_days: int, batch_size: int = 100) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT idempotency_key FROM agent_review_jobs WHERE status IN ('completed','failed','timed_out') "
                "AND COALESCE(completed_at, updated_at) < ? ORDER BY COALESCE(completed_at, updated_at) LIMIT ?",
                (cutoff, batch_size),
            ).fetchall()
            if not rows:
                return 0
            conn.executemany(
                "DELETE FROM agent_review_jobs WHERE idempotency_key=?",
                [(row["idempotency_key"],) for row in rows],
            )
            return len(rows)
