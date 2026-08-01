"""Independent durable Agent Review worker command."""
from __future__ import annotations

import argparse
import signal
import threading
import time

from biz.agent.backends import reset_backend_shutdown, terminate_active_backends
from biz.agent.config import load_agent_review_config
from biz.agent.job_store import AgentJobStore
from biz.agent.service import execute_claimed_job, reap_agent_review_workspaces


def run_worker(*, once: bool = False, poll_interval: float = 1.0) -> None:
    reset_backend_shutdown()
    config = load_agent_review_config()
    config.ensure_runtime_directories()
    stop = threading.Event()

    def request_stop(_signum, _frame) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    reap_agent_review_workspaces()
    AgentJobStore(config.job_db).delete_expired(retention_days=config.job_retention_days)

    def maintain() -> None:
        while not stop.wait(60):
            reap_agent_review_workspaces()
            AgentJobStore(config.job_db).delete_expired(retention_days=config.job_retention_days)

    maintenance = threading.Thread(target=maintain, name="agent-review-maintenance", daemon=True)
    maintenance.start()

    def loop() -> None:
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Run durable Agent Review workers")
    parser.add_argument("--once", action="store_true", help="claim at most one job per worker thread")
    parser.add_argument("--poll-interval", type=float, default=1.0)
    args = parser.parse_args()
    run_worker(once=args.once, poll_interval=args.poll_interval)


if __name__ == "__main__":
    main()
