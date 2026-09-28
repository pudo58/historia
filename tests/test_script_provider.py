"""OpenAI-compatible script provider never falls back or repeats ambiguous requests."""
import json

import httpx
import pytest

from studio.backend import ReconcileRequired
from studio.script_provider import generate, selected_provider


class Settings:
    def __init__(self):
        self.value = json.dumps({'url': 'https://example.org/v1/chat/completions',
                                 'model': 'script-model', 'api_key': 'secret'})

    def setting(self, name):
        assert name == 'studio_script_provider'
        return self.value


@pytest.mark.asyncio
async def test_script_api_keeps_malformed_response_for_editor(monkeypatch):
    hosts = Settings()
    monkeypatch.setattr('studio.script_provider.public_https', lambda url: None)
    client_type = httpx.AsyncClient
    calls = []

    def respond(request):
        calls.append(request)
        assert request.headers['Authorization'] == 'Bearer secret'
        return httpx.Response(200, json={'choices': [{'message': {'content': 'not JSON'}}]})

    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs:
        client_type(transport=httpx.MockTransport(respond), **kwargs))
    output = await generate(hosts, selected_provider(hosts), 'Write a cited scene')
    assert output == {'_raw': 'not JSON'}
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_script_api_timeout_is_ambiguous_and_never_falls_back(monkeypatch):
    hosts = Settings()
    monkeypatch.setattr('studio.script_provider.public_https', lambda url: None)
    client_type = httpx.AsyncClient
    calls = []

    def timeout(request):
        calls.append(request)
        raise httpx.ConnectTimeout('unknown', request=request)

    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs:
        client_type(transport=httpx.MockTransport(timeout), **kwargs))
    with pytest.raises(ReconcileRequired):
        await generate(hosts, selected_provider(hosts), 'Write a cited scene')
    assert len(calls) == 1
