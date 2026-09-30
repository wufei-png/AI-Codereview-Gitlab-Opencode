import json
import signal
import sqlite3
import subprocess
import sys
import threading
from dataclasses import asdict
from unittest.mock import Mock, patch

import pytest
import requests

from biz.agent import worker
from biz.agent.backends import BackendExecutionError, OpenCodeServeBackend, _run_cli
from biz.agent.config import AgentReviewConfig
from biz.agent.job_store import AgentJobStore
from biz.agent.review_request import AgentReviewRequest
from biz.agent.service import execute_claimed_job
from biz.agent.workspace import WorkspaceContext


def queued_job(tmp_path, key='one'):
    config = AgentReviewConfig(job_db=tmp_path / 'jobs.db', clone_parent=tmp_path / 'clones', worktree_parent=tmp_path / 'worktrees', worker_concurrency=1, job_lease_seconds=1)
    request = AgentReviewRequest(provider='github', remote_url='https://github.com/o/r.git', review_url='https://github.com/o/r/pull/1', project_path='o/r', source_branch='feature', target_branch='main', revision_hint='S1', action='open', event_key=key)
    store = AgentJobStore(config.job_db)
    store.enqueue(key=key, provider=request.provider, review_url=request.review_url, backend='codex', source_branch='feature', target_branch='main', request_json=json.dumps(asdict(request)))
    return config, store


def saved_status(config):
    with sqlite3.connect(config.job_db) as conn:
        return conn.execute('SELECT status, delivery_status FROM agent_review_jobs').fetchone()


def test_worker_once_claims_and_finishes_isolated_job(tmp_path, monkeypatch):
    config, _ = queued_job(tmp_path)
    before = signal.getsignal(signal.SIGTERM)
    def execute(store, row, cfg):
        assert cfg == config
        assert store.is_owner(row['idempotency_key'])
        store.finish(row['idempotency_key'], status='completed')
    monkeypatch.setattr(worker, 'load_agent_review_config', lambda: config)
    monkeypatch.setattr(worker, 'execute_claimed_job', execute)
    monkeypatch.setattr(worker, 'reap_agent_review_workspaces', lambda: None)
    worker.run_worker(once=True)
    assert saved_status(config) == ('completed', 'not_attempted')
    assert signal.getsignal(signal.SIGTERM) == before


def test_sigterm_stops_claiming_and_terminates_active_backend(tmp_path, monkeypatch):
    config, _ = queued_job(tmp_path)
    from dataclasses import replace
    config = replace(config, worker_shutdown_grace=0)
    released = threading.Event()
    def execute(store, row, cfg):
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        assert released.wait(2)
        store.finish(row['idempotency_key'], status='failed')
    monkeypatch.setattr(worker, 'load_agent_review_config', lambda: config)
    monkeypatch.setattr(worker, 'execute_claimed_job', execute)
    monkeypatch.setattr(worker, 'reap_agent_review_workspaces', lambda: None)
    monkeypatch.setattr(worker, 'terminate_active_backends', released.set)
    worker.run_worker()
    assert released.is_set()
    assert saved_status(config)[0] == 'failed'


def test_worker_failure_is_reported_and_restores_signal_handlers(tmp_path, monkeypatch):
    config, _ = queued_job(tmp_path)
    before = signal.getsignal(signal.SIGTERM)
    def fail(*args):
        raise RuntimeError('storage unavailable')
    monkeypatch.setattr(worker, 'load_agent_review_config', lambda: config)
    monkeypatch.setattr(worker, 'execute_claimed_job', fail)
    monkeypatch.setattr(worker, 'reap_agent_review_workspaces', lambda: None)
    with pytest.raises(RuntimeError, match='worker execution failed'):
        worker.run_worker(once=True)
    assert signal.getsignal(signal.SIGTERM) == before


@pytest.mark.parametrize('heartbeat', [False, sqlite3.OperationalError('busy')])
def test_heartbeat_loss_cancels_current_job(tmp_path, heartbeat):
    config, store = queued_job(tmp_path)
    row = store.claim_next()
    job = config.worktree_parent / 'job'
    job.mkdir(parents=True)
    context = WorkspaceContext(job, job, None, 'S1', 'feature', 'T1')
    observed = threading.Event()
    def run(**kwargs):
        assert kwargs['cancel'].wait(2)
        observed.set()
        raise BackendExecutionError('job cancelled after heartbeat loss')
    with patch('biz.agent.service._preflight'), patch('biz.agent.service.WorkspaceManager.prepare', return_value=context), patch('biz.agent.service.WorkspaceManager.cleanup') as cleanup, patch('biz.agent.service.create_backend', return_value=Mock(run=run)), patch.object(store, 'heartbeat', side_effect=[heartbeat]):
        execute_claimed_job(store, row, config)
    assert observed.is_set()
    assert saved_status(config) == ('failed', 'unconfirmed')
    cleanup.assert_called_once()


def test_lost_lease_is_not_a_confirmed_revision_duplicate(tmp_path):
    config, store = queued_job(tmp_path)
    row = store.claim_next()
    context = WorkspaceContext(tmp_path, tmp_path, None, 'S1', 'feature', 'T1')
    with patch('biz.agent.service._preflight'), patch('biz.agent.service.WorkspaceManager.prepare', return_value=context), patch('biz.agent.service.WorkspaceManager.cleanup'), patch.object(store, 'set_revisions', return_value=False), patch.object(store, 'is_owner', return_value=False), patch('biz.agent.service.create_backend') as backend:
        execute_claimed_job(store, row, config)
    backend.assert_not_called()
    assert saved_status(config)[1] == 'not_attempted'


def test_started_orphan_is_fenced_without_deleting_live_workspace(tmp_path):
    config, store = queued_job(tmp_path)
    row = store.claim_next()
    store.mark_agent_started(row['idempotency_key'])
    with sqlite3.connect(config.job_db) as conn:
        conn.execute("UPDATE agent_review_jobs SET updated_at='2000-01-01T00:00:00+00:00'")
    assert store.reap_stale(lease_seconds=1, exclude_keys=('one',)) == []
    assert saved_status(config)[0] == 'running'
    assert store.reap_stale(lease_seconds=1) == []
    assert saved_status(config) == ('failed', 'unconfirmed')
    assert store.claim_next() is None


def test_job_cancellation_does_not_stop_another_cli(tmp_path):
    cancels = [threading.Event(), threading.Event()]
    launched = threading.Event()
    popen = subprocess.Popen
    def launch(args, **kwargs):
        process = popen(args, **kwargs)
        if 'sleep(60)' in args[-1]:
            launched.set()
        return process
    results = {}
    def run(index, code):
        try:
            results[index] = _run_cli('test', [sys.executable, '-c', code], '', tmp_path, -1, env={}, cancel=cancels[index])
        except BackendExecutionError as exc:
            results[index] = exc
    threads = [threading.Thread(target=run, args=(0, 'import time; time.sleep(60)')), threading.Thread(target=run, args=(1, 'print("other job completed")'))]
    with patch('biz.agent.backends.subprocess.Popen', side_effect=launch):
        for thread in threads:
            thread.start()
        assert launched.wait(2)
        cancels[0].set()
        for thread in threads:
            thread.join(3)
    assert all(not thread.is_alive() for thread in threads)
    assert isinstance(results[0], BackendExecutionError)
    assert results[1].output == 'other job completed\n'


def test_cli_timeout_preserves_native_stdout(tmp_path):
    with pytest.raises(BackendExecutionError) as error:
        _run_cli('test', [sys.executable, '-c', 'import time; print("partial", flush=True); time.sleep(60)'], '', tmp_path, 1, env={})
    assert error.value.timed_out
    assert error.value.output == 'partial\n'


def test_opencode_abort_failure_is_explicit(tmp_path):
    cancel = threading.Event()
    def get(url, **kwargs):
        cancel.set()
        return Mock(raise_for_status=lambda: None, json=lambda: {})
    created = Mock(raise_for_status=lambda: None, json=lambda: {'id': 'session-A'})
    accepted = Mock(raise_for_status=lambda: None)
    with patch('biz.agent.backends.requests.post', side_effect=[created, accepted, requests.ConnectionError('offline')]), patch('biz.agent.backends.requests.get', side_effect=get):
        with pytest.raises(BackendExecutionError) as error:
            OpenCodeServeBackend().run(prompt='review', job_root=tmp_path, source_repo=tmp_path, config=AgentReviewConfig(), cancel=cancel)
    assert not error.value.cleanup_safe
    assert 'session-A' in str(error.value)


def test_uncertain_backend_retains_workspace(tmp_path):
    config, store = queued_job(tmp_path)
    row = store.claim_next()
    job = config.worktree_parent / 'retained'
    job.mkdir(parents=True)
    context = WorkspaceContext(job, job, None, 'S1', 'feature', 'T1')
    backend = Mock()
    backend.run.side_effect = BackendExecutionError('abort unconfirmed', cleanup_safe=False)
    with patch('biz.agent.service._preflight'), patch('biz.agent.service.WorkspaceManager.prepare', return_value=context), patch('biz.agent.service.WorkspaceManager.cleanup') as cleanup, patch('biz.agent.service.create_backend', return_value=backend):
        execute_claimed_job(store, row, config)
    cleanup.assert_not_called()
    assert job.exists()
    with sqlite3.connect(config.job_db) as conn:
        assert 'termination unconfirmed' in conn.execute('SELECT cleanup_error FROM agent_review_jobs').fetchone()[0]
