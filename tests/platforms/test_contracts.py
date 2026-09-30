from unittest.mock import Mock, patch

import pytest

from biz.platforms.gitea.webhook_handler import PullRequestHandler as GiteaPR, PushHandler as GiteaPush
from biz.platforms.github.webhook_handler import PullRequestHandler as GitHubPR
from biz.platforms.gitlab.webhook_handler import MergeRequestHandler as GitLabMR
from biz.queue.worker import _resolve_repo_for_event


def test_github_enterprise_endpoint(monkeypatch):
    monkeypatch.setenv('GITHUB_API_URL', 'https://git.example/api/v3/')
    handler = GitHubPR({'repository': {'full_name': 'o/r'}, 'pull_request': {'number': 3}}, 'token', 'https://git.example')
    with patch('biz.platforms.http.requests.request', return_value=Mock(status_code=200, json=lambda: [])) as send:
        assert handler.get_pull_request_commits() == []
    assert send.call_args.args == ('GET', 'https://git.example/api/v3/repos/o/r/pulls/3/commits')
    assert send.call_args.kwargs['verify'] is True
    assert send.call_args.kwargs['timeout'] == (5, 30)


@pytest.mark.parametrize('factory,payload,args,method', [
    (GitHubPR, {'repository': {'full_name': 'o/r'}, 'pull_request': {'number': 3}}, ('token', 'https://github.com'), 'add_pull_request_notes'),
    (GiteaPR, {'repository': {'full_name': 'o/r'}, 'pull_request': {'number': 3}}, ('token', 'https://gitea.example'), 'add_pull_request_notes'),
    (GitLabMR, {'object_kind': 'merge_request', 'object_attributes': {'iid': 3, 'target_project_id': 4}}, ('token', 'https://gitlab.example'), 'add_merge_request_notes'),
])
def test_comment_facades_use_verified_bounded_transport(factory, payload, args, method, caplog):
    with patch('biz.platforms.http.requests.request', return_value=Mock(status_code=401, text='DO_NOT_LOG')) as send:
        getattr(factory(payload, *args), method)('body')
    assert send.call_count == 1
    assert send.call_args.kwargs['verify'] is True
    assert send.call_args.kwargs['timeout'] == (5, 30)
    assert 'DO_NOT_LOG' not in caplog.text


def test_gitea_push_delivery_is_explicitly_unsupported(caplog):
    with patch('biz.platforms.http.requests.request') as send:
        assert GiteaPush({'repository': {}}, 'token', 'https://gitea.example').add_push_notes('review') is False
    send.assert_not_called()
    assert 'unsupported' in caplog.text


@pytest.mark.parametrize('host', ['github.com', 'gitea.example'])
def test_shared_pr_resolver_uses_fork_source(host):
    payload = {'repository': {'full_name': 'upstream/r', 'clone_url': f'https://{host}/upstream/r.git'}, 'pull_request': {'head': {'sha': 'a' * 40, 'repo': {'full_name': 'fork/r', 'clone_url': f'https://{host}/fork/r.git'}}}}
    assert _resolve_repo_for_event(payload) == (f'https://{host}/fork/r.git', 'fork/r', 'a' * 40)


def test_gitlab_resolver_uses_fork_source():
    payload = {'object_kind': 'merge_request', 'project': {'path_with_namespace': 'upstream/r', 'git_http_url': 'https://gitlab.example/upstream/r.git'}, 'source': {'path_with_namespace': 'fork/r', 'git_http_url': 'https://gitlab.example/fork/r.git'}, 'object_attributes': {'source_branch': 'feature', 'last_commit': {'id': 'a' * 40}}}
    assert _resolve_repo_for_event(payload) == ('https://gitlab.example/fork/r.git', 'fork/r', 'a' * 40)


def test_gitea_compatible_push_resolves():
    payload = {'repository': {'full_name': 'o/r', 'clone_url': 'https://gitea.example/o/r.git'}, 'ref': 'refs/heads/main', 'after': 'b' * 40}
    assert _resolve_repo_for_event(payload) == ('https://gitea.example/o/r.git', 'o/r', 'b' * 40)
