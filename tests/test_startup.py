import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

from biz.utils.environment import load_project_environment


def test_project_env_is_independent_of_cwd_and_preserves_exports(tmp_path, monkeypatch):
    root = tmp_path / 'project'
    (root / 'conf').mkdir(parents=True)
    (root / 'conf/.env').write_text('AGENT_BACKEND=pi\nOPENCODE_AGENT_NAME=from-file\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('AGENT_BACKEND', 'codex')
    monkeypatch.delenv('OPENCODE_AGENT_NAME', raising=False)
    monkeypatch.delenv('LOG_FILE', raising=False)
    load_project_environment(root)
    assert os.environ['AGENT_BACKEND'] == 'codex'
    assert os.environ['OPENCODE_AGENT_NAME'] == 'from-file'
    assert Path(os.environ['LOG_FILE']).parent == root / 'log'
    assert (root / 'log').is_dir()


def test_agent_only_startup_never_constructs_llm_client(monkeypatch):
    from biz.utils import config_checker
    monkeypatch.setenv('LLM_REVIEW_ENABLED', '0')
    monkeypatch.delenv('LLM_PROVIDER', raising=False)
    factory = Mock(side_effect=AssertionError('unexpected LLM client'))
    monkeypatch.setattr(config_checker, 'Factory', factory)
    config_checker.check_config()
    factory.assert_not_called()


def test_api_import_and_config_check_from_another_directory(tmp_path):
    root = Path(__file__).resolve().parents[1]
    environment = {**os.environ, 'PYTHONPATH': str(root), 'LLM_REVIEW_ENABLED': '0',
                   'LOG_FILE': str(tmp_path / 'startup.log')}
    result = subprocess.run([sys.executable, '-c',
                             'import api; api.check_config(); assert api.api_app.test_client().get("/").status_code == 200'],
                            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
