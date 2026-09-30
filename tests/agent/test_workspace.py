import shutil
import subprocess
from unittest.mock import Mock, patch

import pytest

from biz.agent.backends import ClaudeCliBackend, CodexCliBackend, PiCliBackend
from biz.agent.config import AgentReviewConfig
from biz.agent.review_request import AgentReviewRequest, build_prompt
from biz.agent.workspace import WorkspaceManager


def git(path, *args):
    return subprocess.run(['git', *args], cwd=path, check=True, capture_output=True, text=True).stdout.strip()


@pytest.mark.parametrize('local_seed', [False, True])
def test_service_prepares_exact_detached_worktree_with_owned_objects(tmp_repo, tmp_path, local_seed):
    remote = tmp_path / 'remote.git'
    subprocess.run(['git', 'clone', '--bare', str(tmp_repo), str(remote)], check=True, capture_output=True)
    branch = git(tmp_repo, 'branch', '--show-current')
    git(tmp_repo, 'remote', 'add', 'origin', remote.as_uri())
    before = git(tmp_repo, 'status', '--porcelain')
    seed = tmp_path / 'borrowed-seed'
    if local_seed:
        subprocess.run(['git', 'clone', '--shared', str(tmp_repo), str(seed)], check=True, capture_output=True)
        git(seed, 'remote', 'set-url', 'origin', remote.as_uri())
    request = AgentReviewRequest('github', remote.as_uri(), 'https://github.com/o/r/pull/1', 'o/r', branch, branch, '', 'opened', 'job')
    config = AgentReviewConfig(repo_roots={remote.as_uri(): seed} if local_seed else {}, clone_parent=tmp_path / 'clones', worktree_parent=tmp_path / 'jobs')
    manager = WorkspaceManager(config)
    context = manager.prepare(request)
    assert git(context.worktree_path, 'rev-parse', 'HEAD') == context.source_revision
    assert git(context.worktree_path, 'rev-parse', context.target_revision) == context.target_revision
    assert git(context.worktree_path, 'branch', '--show-current') == ''
    assert git(context.worktree_path, 'status', '--porcelain') == ''
    assert not (context.source_repo / '.git/objects/info/alternates').exists()
    # The clone parent is not needed by a remote Serve sharing just the job root.
    shutil.rmtree(context.clone_path)
    assert git(context.worktree_path, 'show', f'{context.source_revision}:README.md') == '# test repo'
    git(context.worktree_path, 'switch', '-c', 'agent-chosen-fix')
    (context.worktree_path / 'fix.txt').write_text('a fix')
    manager.cleanup(context, success=True)
    assert not context.job_root.exists()
    assert git(tmp_repo, 'status', '--porcelain') == before
    assert git(tmp_repo, 'remote', 'get-url', 'origin') == remote.as_uri()


@pytest.mark.parametrize('backend', [CodexCliBackend(), ClaudeCliBackend(), PiCliBackend()])
def test_cli_executes_from_prepared_worktree(tmp_path, backend):
    worktree = tmp_path / 'worktree'
    worktree.mkdir()
    process = Mock(pid=123, returncode=0, communicate=lambda *a, **kw: ('native output', ''))
    with patch('biz.agent.backends.shutil.which', return_value='/bin/agent'), patch('biz.agent.backends.subprocess.Popen', return_value=process) as run:
        assert backend.run(prompt='review', job_root=tmp_path, source_repo=tmp_path / '.agent-source', worktree_path=worktree, config=AgentReviewConfig()).output == 'native output'
    assert run.call_args.kwargs['cwd'] == worktree
    if backend.name == 'codex':
        args = run.call_args.args[0]
        assert args[args.index('--cd') + 1] == str(worktree)
        assert args[args.index('--add-dir') + 1] == str(tmp_path)


def test_prompt_provides_existing_worktree_without_creation_protocol(tmp_path):
    request = AgentReviewRequest('github', 'https://github.com/o/r.git', 'https://github.com/o/r/pull/1', 'o/r', 'feature', 'main', '', 'opened', 'job')
    prompt = build_prompt(request, str(tmp_path / '.agent-source'), str(tmp_path), 'a' * 40, 'b' * 40, AgentReviewConfig(), worktree_path=str(tmp_path / 'worktree'))
    assert f'WORKTREE_PATH: {tmp_path}/worktree' in prompt
    assert 'WORKTREE_PARENT' not in prompt
    assert 'do not create another worktree' in prompt
    assert 'AUTOFIX_TARGET_BRANCH: feature' in prompt
