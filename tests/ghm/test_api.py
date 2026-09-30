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


def test_hf_token_persists_in_database_and_is_never_returned(tmp_path) -> None:
    token = "hf_" + "A1b2C3d4E5f6G7h8"
    def app():
        return create_app(Settings(database_url=f"sqlite:///{tmp_path / 'api.db'}", studio_root=tmp_path / "data"),
                          SecretStore("test-key"), lambda host, secret: FakeExecutor())
    with TestClient(app()) as client:
        assert client.get("/api/settings").json()["hf_token_configured"] is False
        assert client.post("/api/settings/hf-token", json={"token": token}).status_code == 200
    import sqlite3
    rows = sqlite3.connect(tmp_path / "api.db").execute("select name, encrypted_value from local_settings").fetchall()
    assert sorted(r[0] for r in rows) == ["hf_token", "remote_access_token"] and token not in str(rows)  # stored encrypted
    # A restarted app reads the same encrypted row: no need to enter the token again.
    with TestClient(app()) as client:
        body = client.get("/api/settings").json()
        assert body["hf_token_configured"] is True
        assert body["hf_token_hint"] == "hf_…G7h8"
        assert token not in str(body)
        assert client.post("/api/settings/hf-token", json={"token": ""}).status_code == 200
        body = client.get("/api/settings").json()
        assert body["hf_token_configured"] is False and body["hf_token_hint"] is None
