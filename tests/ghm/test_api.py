from fastapi.testclient import TestClient

from ghm.api import create_app
from ghm.config import Settings
from ghm.executors.fake import FakeExecutor
from ghm.security import SecretStore


def test_host_crud_and_key_pinning_over_http(tmp_path) -> None:
    app = create_app(
        Settings(database_url=f"sqlite:///{tmp_path / 'api.db'}"),
        SecretStore("test-key"),
        lambda host, secret: FakeExecutor("SHA256:verified"),
    )
    with TestClient(app) as client:
        created = client.post(
            "/api/hosts",
            json={
                "label": "Remote 5090",
                "address": "gpu.example.test",
                "port": 22,
                "username": "ubuntu",
                "auth_kind": "password",
                "secret": "not-exposed",
            },
        )
        assert created.status_code == 201
        body = created.json()
        assert "secret" not in body
        host_id = body["id"]

        inspected = client.post(f"/api/hosts/{host_id}/inspect-key")
        assert inspected.json()["fingerprint"] == "SHA256:verified"
        confirmed = client.post(
            f"/api/hosts/{host_id}/confirm-key", json={"fingerprint": "SHA256:verified"}
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["pinned_fingerprint"] == "SHA256:verified"
        preflight = client.post(f"/api/hosts/{host_id}/preflight")
        assert preflight.status_code == 200
        assert preflight.json()["status"] == "fail"
        assert client.get(f"/api/hosts/{host_id}/preflight").status_code == 200
        updated = client.patch(f"/api/hosts/{host_id}", json={"label": "Updated 5090"})
        assert updated.status_code == 200
        assert client.get(f"/api/hosts/{host_id}").json()["label"] == "Updated 5090"
        assert client.delete(f"/api/hosts/{host_id}").status_code == 204
        assert client.get(f"/api/hosts/{host_id}").status_code == 404
