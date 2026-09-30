import pytest
import requests


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def unexpected_request(*args, **kwargs):
        raise AssertionError('Unexpected network request in provider contract test')
    monkeypatch.setattr(requests.Session, 'send', unexpected_request)
