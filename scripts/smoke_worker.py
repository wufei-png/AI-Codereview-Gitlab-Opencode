"""Offline container smoke: stdin script, temporary DB, fake execution, no provider calls."""
import os
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ['LLM_REVIEW_ENABLED'] = '0'

from biz.agent import worker
from biz.agent.config import AgentReviewConfig
from biz.agent.job_store import AgentJobStore

with TemporaryDirectory(prefix='worker-smoke-') as temp:
    root = Path(temp)
    config = replace(AgentReviewConfig(), job_db=root / 'jobs.db',
                     clone_parent=root / 'clones', worktree_parent=root / 'worktrees', worker_concurrency=1)
    assert worker.check_worker_configuration(config)['local_checks'] == 'passed'
    store = AgentJobStore(config.job_db)
    store.enqueue(key='offline-smoke', provider='github', review_url='https://example.test/o/r/pull/1',
                  backend='opencode', source_branch='f', target_branch='main', request_json='{}')
    worker.load_agent_review_config = lambda: config
    worker.reap_agent_review_workspaces = lambda: None

    def execute(claimed_store, row, cfg):
        assert cfg.job_db == config.job_db and claimed_store.is_owner(row['idempotency_key'])
        claimed_store.finish(row['idempotency_key'], status='completed')

    worker.execute_claimed_job = execute
    worker.run_worker(once=True)
    assert store.claim_next() is None
    with store._connect() as connection:
        assert connection.execute('SELECT status FROM agent_review_jobs').fetchone()[0] == 'completed'
    assert not Path('/app/conf/.env').exists()
    import api
    api.check_config()
    assert api.api_app.test_client().get('/').status_code == 200
print('offline worker and Agent-only API smoke passed')
