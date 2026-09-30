import hashlib
import hmac
import json
from unittest.mock import patch

import pytest
from flask import Flask

from biz.api.routes.webhook import webhook_bp


@pytest.fixture
def client(monkeypatch):
    for provider in ('GITHUB', 'GITLAB', 'GITEA'):
        for suffix in ('WEBHOOK_SECRET', 'ACCESS_TOKEN'):
            monkeypatch.delenv(f'{provider}_{suffix}', raising=False)
    monkeypatch.delenv('GITLAB_WEBHOOK_SIGNING_TOKEN', raising=False)
    monkeypatch.setenv('AGENT_ALLOW_ACCESS_TOKEN_WEBHOOK_FALLBACK', '0')
    monkeypatch.setenv('GITLAB_URL', 'https://gitlab.example.com')
    app = Flask(__name__)
    app.register_blueprint(webhook_bp)
    return app.test_client()


def signed_headers(provider, body):
    digest = hmac.new(b'secret', body, hashlib.sha256).hexdigest()
    return {
        'github': {'X-GitHub-Event': 'pull_request', 'X-Hub-Signature-256': 'sha256=' + digest},
        'gitea': {'X-Gitea-Event': 'pull_request', 'X-Gitea-Signature': digest},
        'gitlab': {'X-Gitlab-Event': 'Merge Request Hook', 'X-Gitlab-Token': 'secret'},
    }[provider]


@pytest.mark.parametrize('provider', ['github', 'gitlab', 'gitea'])
@pytest.mark.parametrize('agent,llm', [(0, 0), (0, 1), (1, 0), (1, 1)])
@pytest.mark.parametrize('authenticated', [False, True])
def test_all_modes_authenticate_before_dispatch(client, monkeypatch, provider, agent, llm, authenticated):
    monkeypatch.setenv('AGENT_REVIEW_ENABLED', str(agent))
    monkeypatch.setenv('LLM_REVIEW_ENABLED', str(llm))
    monkeypatch.setenv(provider.upper() + '_WEBHOOK_SECRET', 'secret')
    monkeypatch.setenv(provider.upper() + '_ACCESS_TOKEN', 'api-token')
    body = json.dumps({'object_kind': 'merge_request', 'action': 'opened'}).encode()
    headers = signed_headers(provider, body)
    if not authenticated:
        for key in list(headers):
            if 'Signature' in key or 'Token' in key:
                headers[key] = 'bad'
    with patch('biz.api.routes.webhook.handle_agent_queue') as aq, patch('biz.api.routes.webhook.handle_queue') as bq:
        response = client.post('/review/webhook', data=body, content_type='application/json', headers=headers)
    assert response.status_code == (200 if authenticated else 401)
    assert aq.call_count == (agent if authenticated else 0)
    assert bq.call_count == (llm if authenticated else 0)


@pytest.mark.parametrize('provider', ['github', 'gitlab', 'gitea'])
def test_missing_secret_never_dispatches(client, monkeypatch, provider):
    monkeypatch.setenv('AGENT_REVIEW_ENABLED', '0')
    body = b'{"object_kind":"merge_request"}'
    with patch('biz.api.routes.webhook.handle_queue') as queue:
        response = client.post('/review/webhook', data=body, content_type='application/json', headers=signed_headers(provider, body))
    assert response.status_code == 401
    queue.assert_not_called()


@pytest.mark.parametrize('body', [b'{', b'[]', b'null', b'{}'])
def test_authenticated_malformed_body_is_rejected(client, monkeypatch, body):
    monkeypatch.setenv('GITHUB_WEBHOOK_SECRET', 'secret')
    response = client.post('/review/webhook', data=body, content_type='application/json', headers=signed_headers('github', body))
    assert response.status_code == 400


def test_body_tampering_fails_before_json_parsing(client, monkeypatch):
    monkeypatch.setenv('GITHUB_WEBHOOK_SECRET', 'secret')
    response = client.post('/review/webhook', data=b'{', content_type='application/json', headers=signed_headers('github', b'{}'))
    assert response.status_code == 401


def test_webhook_secret_is_not_a_platform_api_token(client, monkeypatch):
    monkeypatch.setenv('GITLAB_WEBHOOK_SECRET', 'secret')
    monkeypatch.setenv('LLM_REVIEW_ENABLED', '1')
    response = client.post('/review/webhook', json={'object_kind': 'merge_request'}, headers={'X-Gitlab-Token': 'secret'})
    assert response.status_code == 400
    assert 'access token' in response.json['message']


def test_payload_is_not_logged(client, monkeypatch, caplog):
    monkeypatch.setenv('GITHUB_WEBHOOK_SECRET', 'secret')
    monkeypatch.setenv('LLM_REVIEW_ENABLED', '0')
    monkeypatch.setenv('AGENT_REVIEW_ENABLED', '0')
    body = b'{"private_content":"DO_NOT_LOG_THIS"}'
    response = client.post('/review/webhook', data=body, content_type='application/json', headers=signed_headers('github', body))
    assert response.status_code == 200
    assert 'DO_NOT_LOG_THIS' not in caplog.text
