from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import Mock, patch

import pytest
import requests

from biz.platforms import http


def response(code, retry_after=None):
    return Mock(status_code=code, headers={} if retry_after is None else {'Retry-After': retry_after})


def test_tls_and_timeouts_are_explicit(monkeypatch):
    for name in ('PROVIDER_CA_BUNDLE', 'PROVIDER_CONNECT_TIMEOUT', 'PROVIDER_READ_TIMEOUT'):
        monkeypatch.delenv(name, raising=False)
    with patch.object(http.requests, 'request', return_value=response(200)) as send:
        http.get('https://example/api')
    assert send.call_args.kwargs == {'verify': True, 'timeout': (5, 30)}


def test_custom_ca_and_timeout(monkeypatch):
    monkeypatch.setenv('PROVIDER_CA_BUNDLE', '/corp/ca.pem')
    monkeypatch.setenv('PROVIDER_CONNECT_TIMEOUT', '2')
    monkeypatch.setenv('PROVIDER_READ_TIMEOUT', '7')
    with patch.object(http.requests, 'request', return_value=response(200)) as send:
        http.get('https://example/api')
    assert send.call_args.kwargs == {'verify': '/corp/ca.pem', 'timeout': (2, 7)}


@pytest.mark.parametrize('method', ['POST', 'PATCH', 'PUT', 'DELETE'])
@pytest.mark.parametrize('failure', [response(503), requests.Timeout('https://secret@example')])
def test_mutation_is_sent_once(method, failure):
    with patch.object(http.requests, 'request', side_effect=[failure]) as send, patch.object(http.time, 'sleep') as sleep:
        if isinstance(failure, Exception):
            with pytest.raises(requests.RequestException, match='Platform') as error:
                http.request(method, 'https://example/api')
            assert 'secret' not in str(error.value)
        else:
            assert http.request(method, 'https://example/api').status_code == 503
    assert send.call_count == 1
    sleep.assert_not_called()


@pytest.mark.parametrize('code', [429, 502, 503, 504])
def test_safe_retry_is_bounded(code):
    with patch.object(http.requests, 'request', side_effect=[response(code, '9999') for _ in range(3)]) as send, patch.object(http.time, 'sleep') as sleep:
        assert http.get('https://example/api').status_code == code
    assert send.call_count == 3
    assert [c.args[0] for c in sleep.call_args_list] == [60, 60]


@pytest.mark.parametrize('code', [401, 403, 404, 422])
def test_auth_and_client_errors_are_not_retried(code):
    with patch.object(http.requests, 'request', return_value=response(code)) as send:
        http.get('https://example/api')
    assert send.call_count == 1


def test_retry_after_http_date_and_recovery():
    date = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=20))
    with patch.object(http.requests, 'request', side_effect=[response(429, date), response(200)]), patch.object(http.time, 'sleep') as sleep:
        assert http.get('https://example/api').status_code == 200
    assert 0 < sleep.call_args.args[0] <= 20


def test_tls_failure_is_not_retried_and_cannot_be_disabled():
    with patch.object(http.requests, 'request', side_effect=requests.exceptions.SSLError('private detail')) as send:
        with pytest.raises(requests.RequestException, match='certificate'):
            http.get('https://example')
        assert send.call_count == 1
    with pytest.raises(ValueError, match='cannot be disabled'):
        http.get('https://example', verify=False)


@pytest.mark.parametrize('value', ['0', '-1', 'nan', 'inf'])
def test_timeout_configuration_must_be_finite(monkeypatch, value):
    monkeypatch.setenv('PROVIDER_CONNECT_TIMEOUT', value)
    with pytest.raises(ValueError):
        http.get('https://example')
