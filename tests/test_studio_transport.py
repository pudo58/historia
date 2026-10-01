from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from studio.backend import ReconcileRequired, RemoteBackend


@pytest.mark.asyncio
async def test_ambiguous_prompt_is_not_resubmitted(tmp_path):
    requests = []

    def response(request):
        requests.append((request.method, request.url.path))
        return httpx.Response(200, json={} if request.url.path.startswith("/history/") else {"queue_running": [], "queue_pending": []})

    backend = RemoteBackend(None, SimpleNamespace(job_directory=lambda id: tmp_path))
    async with httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(response)) as client:
        @asynccontextmanager
        async def connection(host_id):
            yield None, client
        backend.connection = connection
        job = SimpleNamespace(id=str(uuid4()), host_id="test", result={"submissions": {"keyframe-0": {"state": "submitting"}}})
        with pytest.raises(ReconcileRequired):
            await backend.generate(job, "qwen_image", "test", [], 42, "draft", lambda msg: None,
                                   lambda stage, value: None, "keyframe-0")
    assert all(method == "GET" for method, _ in requests)


@pytest.mark.asyncio
async def test_cancel_does_not_interrupt_someone_elses_job():
    requests = []

    def response(request):
        requests.append((request.method, request.url.path))
        return httpx.Response(200, json={"queue_running": [[0, "another-users-prompt"]], "queue_pending": []})

    backend = RemoteBackend(None, None)
    async with httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(response)) as client:
        @asynccontextmanager
        async def connection(host_id):
            yield None, client
        backend.connection = connection
        job = SimpleNamespace(host_id="test", result={"submissions": {"clip-0": {"prompt_id": "our-prompt"}}})
        await backend.cancel(job)
    assert ("POST", "/interrupt") not in requests
    assert ("POST", "/queue") in requests


def test_failed_or_interrupted_prompt_is_replaced_not_reread():
    from studio.backend import prompt_is_dead
    failed = {'status': {'status_str': 'error', 'completed': False, 'messages': [['execution_interrupted', {}]]}}
    assert prompt_is_dead({'state': 'submitted'}, failed, False)
    assert prompt_is_dead({'state': 'submitting'}, failed, False)
    # Never replace anything that may still be alive or that already succeeded.
    assert not prompt_is_dead({'state': 'submitted'}, failed, True)
    assert not prompt_is_dead({'state': 'submitted'}, None, False)
    assert not prompt_is_dead(None, failed, False)
    assert not prompt_is_dead({'state': 'submitted'}, {'status': {'status_str': 'success', 'completed': True}}, False)
    assert not prompt_is_dead({'state': 'downloaded'}, failed, False)
