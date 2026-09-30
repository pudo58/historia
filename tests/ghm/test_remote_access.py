"""Tunnel/remote access: an unknown web page or tunnel visitor must not reach the API."""
import pytest
from fastapi.testclient import TestClient

from ghm import remote_access
from ghm.api import create_app
from ghm.config import Settings
from ghm.executors.fake import FakeExecutor
from ghm.security import SecretStore

TUNNEL = {"cf-connecting-ip": "203.0.113.9", "x-forwarded-for": "203.0.113.9"}


@pytest.fixture
def client(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>ui</html>")
    app = create_app(Settings(database_url=f"sqlite:///{tmp_path / 'db'}", studio_root=tmp_path / "data",
                              frontend_dist=dist), SecretStore("remote"), lambda h, s: FakeExecutor())
    return TestClient(app, follow_redirects=False)


def token(client):
    return client.get("/api/remote-access").json()["token"]


def test_foreign_origin_on_loopback_is_rejected(client):
    for origin in ("https://attacker.trycloudflare.com", "https://evil.example"):
        assert client.get("/api/hosts", headers={"origin": origin}).status_code == 403
        assert client.post("/api/runpod/pods/abc/action", headers={"origin": origin},
                           json={"action": "terminate", "confirm_name": "x", "force": True}).status_code == 403
    assert client.get("/api/hosts", headers={"origin": "http://127.0.0.1:8000"}).status_code == 200


def test_tunnel_visitor_needs_the_access_cookie(client):
    secret = token(client)
    assert client.get("/api/hosts", headers=TUNNEL).status_code == 401
    assert client.post("/api/runpod/pods/abc/action", headers=TUNNEL,
                       json={"action": "terminate", "confirm_name": "x", "force": True}).status_code == 401
    page = client.get("/", headers=TUNNEL)
    assert page.status_code == 401 and "mã truy cập" in page.text.lower()
    assert client.get("/?access=wrong", headers=TUNNEL).status_code == 401
    login = client.get(f"/projects?access={secret}&page=gpu", headers=TUNNEL)
    assert login.status_code == 303 and login.headers["location"] == "/projects?page=gpu"
    cookie = login.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
    authed = {**TUNNEL, "cookie": f"{remote_access.COOKIE}={secret}",
              "origin": "https://mine.trycloudflare.com"}
    assert client.get("/api/hosts", headers=authed).status_code == 200
    assert client.get("/", headers=authed).status_code == 200


def test_access_link_is_local_only_and_rotation_revokes_old_cookie(client):
    secret = token(client)
    authed = {**TUNNEL, "cookie": f"{remote_access.COOKIE}={secret}"}
    assert client.get("/api/remote-access", headers=authed).status_code == 403
    assert client.post("/api/remote-access/rotate", headers=authed).status_code == 403
    fresh = client.post("/api/remote-access/rotate").json()["token"]
    assert fresh != secret
    assert client.get("/api/hosts", headers=authed).status_code == 401
    assert client.get("/api/hosts", headers={**TUNNEL, "cookie": f"{remote_access.COOKIE}={fresh}"}).status_code == 200


def test_token_persists_across_restart(tmp_path):
    def make():
        return TestClient(create_app(Settings(database_url=f"sqlite:///{tmp_path / 'db'}", studio_root=tmp_path / "d"),
                                     SecretStore("remote"), lambda h, s: FakeExecutor()))
    first = make().get("/api/remote-access").json()["token"]
    assert make().get("/api/remote-access").json()["token"] == first and len(first) >= 40
