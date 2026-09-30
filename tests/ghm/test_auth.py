from fastapi.testclient import TestClient

from ghm import auth
from ghm.api import create_app
from ghm.config import Settings
from ghm.security import SecretStore


def make(tmp_path):
    return create_app(Settings(database_url=f"sqlite:///{tmp_path / 'db'}", studio_root=tmp_path / "data"),
                      SecretStore("test"), lambda h, s: None, require_password=True)


def test_everything_locked_until_password_is_created(tmp_path):
    with TestClient(make(tmp_path)) as client:
        assert client.get("/api/auth/status").json() == {"enabled": True, "configured": False, "authenticated": False, "min_length": 8}
        blocked = client.get("/api/hosts")
        assert blocked.status_code == 401 and blocked.json()["code"] == "setup_required"
        assert client.post("/api/auth/setup", json={"password": "short"}).status_code == 422
        ok = client.post("/api/auth/setup", json={"password": "correct horse"})
        assert ok.status_code == 200 and "httponly" in ok.headers["set-cookie"].lower() and "samesite=strict" in ok.headers["set-cookie"].lower()
        assert client.get("/api/hosts").status_code == 200
        assert client.post("/api/auth/setup", json={"password": "another one 1"}).status_code == 409


def test_login_logout_and_wrong_password(tmp_path):
    app = make(tmp_path)
    TestClient(app).post("/api/auth/setup", json={"password": "correct horse"})
    with TestClient(app) as client:  # fresh browser without a cookie
        assert client.get("/api/hosts").json()["code"] == "login_required"
        assert client.post("/api/auth/login", json={"password": "wrong password"}).status_code == 401
        assert client.get("/api/hosts").status_code == 401
        assert client.post("/api/auth/login", json={"password": "correct horse"}).status_code == 200
        assert client.get("/api/hosts").status_code == 200
        client.post("/api/auth/logout")
        assert client.get("/api/hosts").status_code == 401


def test_password_is_hashed_not_stored_plain(tmp_path):
    app = make(tmp_path)
    with TestClient(app) as client:
        client.post("/api/auth/setup", json={"password": "correct horse"})
        stored = app.state.host_service.setting(auth.HASH_SETTING)
        assert stored.startswith("scrypt$") and "correct horse" not in stored
        assert "correct horse" not in open(tmp_path / "db", "rb").read().decode("latin-1")


def test_brute_force_lockout(tmp_path):
    app = make(tmp_path)
    with TestClient(app) as client:
        client.post("/api/auth/setup", json={"password": "correct horse"})
        client.post("/api/auth/logout")
        for _ in range(auth.FREE_ATTEMPTS):
            assert client.post("/api/auth/login", json={"password": "nope nope nope"}).status_code == 401
        locked = client.post("/api/auth/login", json={"password": "correct horse"})
        assert locked.status_code == 429  # even the right password waits


def test_change_password_invalidates_other_sessions(tmp_path):
    app = make(tmp_path)
    a, b = TestClient(app), TestClient(app)
    if True:
        a.post("/api/auth/setup", json={"password": "correct horse"})
        b.post("/api/auth/login", json={"password": "correct horse"})
        assert a.post("/api/auth/change-password", json={"current": "bad bad bad", "new": "brand new pass"}).status_code == 401
        assert a.post("/api/auth/change-password", json={"current": "correct horse", "new": "brand new pass"}).status_code == 200
        assert a.get("/api/hosts").status_code == 200  # the changing browser stays in
        assert b.get("/api/hosts").status_code == 401  # everyone else is logged out
        assert b.post("/api/auth/login", json={"password": "brand new pass"}).status_code == 200


def test_forged_and_expired_sessions_rejected(tmp_path, monkeypatch):
    app = make(tmp_path)
    with TestClient(app) as client:
        client.post("/api/auth/setup", json={"password": "correct horse"})
        token = client.cookies.get(auth.COOKIE)
        client.cookies.set(auth.COOKIE, token[:-2] + "00")
        assert client.get("/api/hosts").status_code == 401
        client.cookies.set(auth.COOKIE, token)
        assert client.get("/api/hosts").status_code == 200
        monkeypatch.setattr(auth.time, "time", lambda: 4102444800)  # year 2100
        assert client.get("/api/hosts").status_code == 401


def test_setup_refused_through_a_tunnel(tmp_path):
    app = make(tmp_path)
    with TestClient(app) as client:
        client.post("/api/auth/logout")
        remote = client.post("/api/auth/setup", json={"password": "correct horse"}, headers={"x-forwarded-for": "1.2.3.4"})
        assert remote.status_code in (401, 403)  # blocked by the tunnel cookie gate or local-only check
        assert not app.state.auth.configured()
