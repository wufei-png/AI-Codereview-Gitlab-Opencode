import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


def test_compose_profile_shares_state_and_uses_explicit_endpoint(tmp_path):
    if not shutil.which('docker'):
        pytest.skip('Docker CLI unavailable')
    root = Path(__file__).resolve().parents[1]
    fixture = tmp_path / 'compose.env'
    fixture.write_text('LLM_REVIEW_ENABLED=0\nAGENT_REVIEW_ENABLED=1\nAGENT_BACKEND=opencode\n')
    env = {**os.environ, 'CODE_REVIEW_ENV_FILE': str(fixture), 'CODE_REVIEW_IMAGE': 'review:test',
           'CODE_REVIEW_USER': '1000:1000', 'OPENCODE_API_URL': 'http://host.docker.internal:4096'}
    result = subprocess.run(['docker', 'compose', '--env-file', str(fixture), '--profile', 'agent',
                             'config', '--format', 'json'], cwd=root, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)['services']
    app, worker = services['app'], services['agent-worker']
    assert worker['profiles'] == ['agent']
    assert worker['command'] == ['python', '-m', 'biz.agent.worker']
    assert app['volumes'] == worker['volumes']
    assert app['user'] == worker['user'] == '1000:1000'
    assert app['image'] == worker['image'] == 'review:test'
    assert worker['environment']['OPENCODE_API_URL'] == 'http://host.docker.internal:4096'
