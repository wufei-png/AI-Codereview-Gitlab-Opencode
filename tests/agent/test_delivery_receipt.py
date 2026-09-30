import json
import sqlite3
from dataclasses import asdict
from unittest.mock import Mock

import pytest

from biz.agent.backends import BackendResult
from biz.agent.config import AgentReviewConfig
from biz.agent.delivery_receipt import read_delivery_receipt
from biz.agent.job_store import AgentJobStore
from biz.agent.review_request import AgentReviewRequest
from biz.agent.service import execute_claimed_job
from biz.agent.workspace import WorkspaceContext


def request_for(provider):
    suffix = {'github': 'pull/1', 'gitea': 'pulls/1', 'gitlab': '-/merge_requests/1'}[provider]
    return AgentReviewRequest(provider, f'https://{provider}.example/fork/r.git',
                              f'https://{provider}.example/o/r/{suffix}', 'fork/r', 'f', 'main',
                              '', 'opened', 'job', target_project_path='o/r')


def native_note(request):
    body = f'<!-- {request.review_marker} -->\nSource Revision: {"a" * 40}\nTarget Revision: {"b" * 40}'
    if request.provider == 'gitlab':
        return {'id': 9, 'body': body, 'noteable_id': 99999, 'noteable_iid': 1,
                'noteable_type': 'MergeRequest', 'provider_extra': {'keep': True}}
    return {'id': 9, 'body': body, 'html_url': request.review_url + '#issuecomment-9',
            'issue_url': f'https://{request.provider}.example/api/v1/repos/o/r/issues/1',
            'url': f'https://{request.provider}.example/api/v1/repos/o/r/issues/comments/9'}


@pytest.mark.parametrize('provider', ['gitlab', 'github', 'gitea'])
def test_valid_native_note_is_preserved_without_provider_readback(tmp_path, provider):
    request = request_for(provider)
    raw = json.dumps(native_note(request), indent=2) + '\n'
    path = tmp_path / 'receipt.json'
    path.write_text(raw)
    receipt = read_delivery_receipt(path, request, 'a' * 40, 'b' * 40)
    assert receipt.error is None
    assert receipt.raw == raw and receipt.note_id == '9'


@pytest.mark.parametrize('change', [
    {'id': None}, {'id': ''}, {'id': 0}, {'id': -1}, {'id': True}, {'id': 1.5},
    {'html_url': 'https://[broken/path'},
    {'body': None}, {'body': 'published'}, {'body': 'wrong revisions'},
    {'html_url': 'https://github.example/o/r/pull/2#issuecomment-9'},
    {'html_url': 'https://github.example/other/r/pull/1#issuecomment-9'},
    {'html_url': 'https://github.example/o/r/pull/1#issuecomment-10'},
    {'issue_url': 'https://github.example/api/v3/repos/o/r/issues/2'},
    {'issue_url': 'https://github.example/api/v3/repos/fork/r/issues/1'},
    {'url': 'https://github.example/api/v3/repos/o/r/issues/comments/10'},
])
def test_invalid_receipt_retains_raw_but_cannot_confirm(tmp_path, change):
    request = request_for('github')
    raw = json.dumps({**native_note(request), **change})
    path = tmp_path / 'receipt.json'
    path.write_text(raw)
    receipt = read_delivery_receipt(path, request, 'a' * 40, 'b' * 40)
    assert receipt.error and receipt.note_id is None and receipt.raw == raw


@pytest.mark.parametrize('mutation', ['duplicate', 'other_marker', 'other_source', 'other_target', 'sha_prefix'])
def test_body_requires_unique_exact_marker_and_full_revisions(tmp_path, mutation):
    request = request_for('gitea')
    note = native_note(request)
    if mutation == 'duplicate':
        note['body'] += f' <!-- {request.review_marker} -->'
    elif mutation == 'other_marker':
        note['body'] = note['body'].replace(request.review_marker, 'ai-codereview:another')
    elif mutation == 'other_source':
        note['body'] = note['body'].replace('a' * 40, 'c' * 40)
    elif mutation == 'other_target':
        note['body'] = note['body'].replace('b' * 40, 'c' * 40)
    else:
        note['body'] = note['body'].replace('a' * 40, 'a' * 41)
    path = tmp_path / 'receipt.json'
    path.write_text(json.dumps(note))
    assert read_delivery_receipt(path, request, 'a' * 40, 'b' * 40).error


@pytest.mark.parametrize('field,value', [('noteable_iid', 2), ('noteable_type', 'Issue')])
def test_gitlab_uses_iid_not_global_id(tmp_path, field, value):
    request = request_for('gitlab')
    path = tmp_path / 'receipt.json'
    path.write_text(json.dumps({**native_note(request), field: value}))
    assert read_delivery_receipt(path, request, 'a' * 40, 'b' * 40).error


def test_public_github_native_api_and_issue_browser_alias(tmp_path):
    request = AgentReviewRequest('github', 'https://github.com/o/r.git', 'https://github.com/o/r/pull/1', 'o/r', 'f', 'main', '', 'opened', 'job')
    note = native_note(request)
    note.update(issue_url='https://api.github.com/repos/o/r/issues/1',
                url='https://api.github.com/repos/o/r/issues/comments/9',
                html_url='https://github.com/o/r/issues/1#issuecomment-9')
    path = tmp_path / 'receipt.json'
    path.write_text(json.dumps(note))
    assert read_delivery_receipt(path, request, 'a' * 40, 'b' * 40).error is None


@pytest.mark.parametrize('configured_api', [None, 'https://api-gateway.example/github'])
def test_enterprise_receipt_does_not_implicitly_accept_public_github_api(tmp_path, monkeypatch, configured_api):
    request = request_for('github')
    if configured_api:
        monkeypatch.setenv('GITHUB_API_URL', configured_api)
    else:
        monkeypatch.delenv('GITHUB_API_URL', raising=False)
    note = native_note(request)
    note['issue_url'] = 'https://api.github.com/repos/o/r/issues/1'
    path = tmp_path / 'receipt.json'
    path.write_text(json.dumps(note))
    assert read_delivery_receipt(path, request, 'a' * 40, 'b' * 40).error
    if configured_api:
        note['issue_url'] = configured_api + '/repos/o/r/issues/1'
        path.write_text(json.dumps(note))
        assert read_delivery_receipt(path, request, 'a' * 40, 'b' * 40).error is None


@pytest.mark.parametrize('raw', ['not JSON', '[]', '{"id":9}'])
def test_malformed_or_partial_receipt_is_not_delivery(tmp_path, raw):
    path = tmp_path / 'receipt.json'
    path.write_text(raw)
    result = read_delivery_receipt(path, request_for('gitlab'), 'a' * 40, 'b' * 40)
    assert result.raw == raw and result.error


def test_unconfirmed_receipt_does_not_advance_history_or_rerun_agent(tmp_path, monkeypatch):
    request = request_for('github')
    config = AgentReviewConfig(backend='codex', job_db=tmp_path / 'jobs.db',
                               worktree_parent=tmp_path / 'jobs', clone_parent=tmp_path / 'clones')
    store = AgentJobStore(config.job_db)
    store.enqueue(key='job', provider=request.provider, review_url=request.review_url, backend='codex',
                  source_branch='f', target_branch='main', request_json=json.dumps(asdict(request)))
    row = store.claim_next()
    root = tmp_path / 'jobs/job'
    root.mkdir(parents=True)
    raw = '{"id": 9}'
    (root / '.agent-delivery-receipt.json').write_text(raw)
    context = WorkspaceContext(root, root, None, source_revision='a' * 40, source_branch='f', target_revision='b' * 40)
    backend = Mock()
    backend.run.return_value = BackendResult('codex', 'native result')
    monkeypatch.setattr('biz.agent.service._preflight', lambda *_: None)
    monkeypatch.setattr('biz.agent.service.WorkspaceManager.prepare', lambda *_args, **_kwargs: context)
    monkeypatch.setattr('biz.agent.service.WorkspaceManager.cleanup', lambda *_args, **_kwargs: None)
    monkeypatch.setattr('biz.agent.service.create_backend', lambda *_: backend)
    execute_claimed_job(store, row, config)
    with sqlite3.connect(config.job_db) as conn:
        saved = conn.execute('SELECT status, delivery_status, delivery_receipt, delivery_error, error FROM agent_review_jobs').fetchone()
    assert saved == ('completed', 'unconfirmed', raw, 'delivery receipt has no note body', None)
    assert store.previous_delivery(request.review_url) == {}
    assert store.claim_next() is None
    backend.run.assert_called_once()
