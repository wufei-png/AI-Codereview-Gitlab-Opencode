"""Independent durable Agent Review worker command."""
from __future__ import annotations

import argparse
import json
import shutil
import signal
import tempfile
import threading
import time
from urllib.parse import urlparse

from biz.utils.environment import load_project_environment

load_project_environment()

from biz.agent.backends import reset_backend_shutdown, terminate_active_backends
from biz.agent.config import AgentReviewConfig, load_agent_review_config
from biz.agent.job_store import AgentJobStore
from biz.agent.service import execute_claimed_job, reap_agent_review_workspaces


def check_worker_configuration(config: AgentReviewConfig | None = None) -> dict[str, object]:
    """Check local readiness without claiming work or calling an external backend."""
    config = config or load_agent_review_config()
    config.ensure_runtime_directories()
    for directory in {config.clone_parent, config.worktree_parent, config.job_db.parent}:
        with tempfile.TemporaryFile(dir=directory):
            pass
    AgentJobStore(config.job_db)  # Schema/writability check; no claim or maintenance.
    if not config.shared_review_skill.is_file():
        raise ValueError("shared review skill is missing")
    config.shared_review_skill.read_bytes()
    if not shutil.which("git"):
        raise ValueError("git is not installed")
    if config.backend == "opencode":
        endpoint = urlparse(config.opencode_api_url)
        if endpoint.scheme not in {"http", "https"} or not endpoint.hostname:
            raise ValueError("OPENCODE_API_URL must be an HTTP(S) endpoint")
    else:
        binary = {"codex": config.codex_bin, "claude": config.claude_bin, "pi": config.pi_bin}[config.backend]
        if not shutil.which(binary):
            raise ValueError(f"selected {config.backend} CLI is not executable")
    return {"backend": config.backend, "job_db": str(config.job_db), "local_checks": "passed",
            "external_auth_and_connectivity": "not checked",
            "worker_platform_clis": {provider: bool(shutil.which(binary)) for provider, binary in config.platform_clis.items()}}


def run_worker(*, once: bool = False, poll_interval: float = 1.0) -> None:
    reset_backend_shutdown()
    config = load_agent_review_config()
    config.ensure_runtime_directories()
    stop = threading.Event()

    def request_stop(_signum, _frame) -> None:
        stop.set()

    previous_signals = {sig: signal.signal(sig, request_stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    failures: list[Exception] = []
    maintenance = None
    try:
        reap_agent_review_workspaces()
        AgentJobStore(config.job_db).delete_expired(retention_days=config.job_retention_days)

        def maintain() -> None:
            try:
                while not stop.wait(60):
                    reap_agent_review_workspaces()
                    AgentJobStore(config.job_db).delete_expired(retention_days=config.job_retention_days)
            except Exception as exc:
                failures.append(exc)
                stop.set()

        maintenance = threading.Thread(target=maintain, name="agent-review-maintenance", daemon=True)
        maintenance.start()

        def loop() -> None:
            try:
                store = AgentJobStore(config.job_db)
                while not stop.is_set():
                    row = store.claim_next()
                    if row is None:
                        if once:
                            return
                        stop.wait(poll_interval)
                        continue
                    execute_claimed_job(store, row, config)
                    if once:
                        return
            except Exception as exc:
                failures.append(exc)
                stop.set()

        threads = [
            threading.Thread(target=loop, name=f"agent-review-worker-{index + 1}")
            for index in range(config.worker_concurrency)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            while thread.is_alive() and not stop.is_set():
                thread.join(timeout=0.5)
        if stop.is_set():
            deadline = time.monotonic() + config.worker_shutdown_grace
            for thread in threads:
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
            if any(thread.is_alive() for thread in threads):
                terminate_active_backends()
                for thread in threads:
                    thread.join(timeout=5)

    finally:
        stop.set()
        if maintenance is not None:
            maintenance.join(timeout=2)
        for sig, handler in previous_signals.items():
            signal.signal(sig, handler)
    if failures:
        raise RuntimeError("Agent worker execution failed") from failures[0]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run durable Agent Review workers")
    parser.add_argument("--check", action="store_true", help="check local configuration without claiming any job")
    parser.add_argument("--once", action="store_true", help="claim at most one job per worker thread")
    parser.add_argument("--poll-interval", type=float, default=1.0)
    args = parser.parse_args()
    if args.check:
        print(json.dumps(check_worker_configuration(), ensure_ascii=False))
        return
    run_worker(once=args.once, poll_interval=args.poll_interval)


if __name__ == "__main__":
    main()
