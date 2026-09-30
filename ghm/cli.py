"""CLI client for the local API; never trusts a stale database tunnel URL."""
import json
import os
import typer
import httpx
import yaml

app = typer.Typer(no_args_is_help=True)
models = typer.Typer(no_args_is_help=True)
app.add_typer(models, name="models")
BASE = "http://127.0.0.1:8000"


@app.command()
def serve(port: int = 8000):
    """Serve the API and built web UI on loopback only."""
    import uvicorn
    uvicorn.run("ghm.api:create_app", factory=True, host="127.0.0.1", port=port)


def _client() -> httpx.Client:
    """HTTP client logged in with the Historia password (HISTORIA_PASSWORD env var, or prompted)."""
    client = httpx.Client(base_url=BASE, timeout=60, trust_env=False)
    try:
        status = client.get("/api/auth/status").json()
    except httpx.HTTPError as exc:
        raise typer.BadParameter("Historia chưa chạy. Hãy chạy start-historia.bat trước.") from exc
    if status.get("enabled") and not status.get("authenticated"):
        if not status.get("configured"):
            raise typer.BadParameter("Chưa có mật khẩu. Mở giao diện Historia để tạo mật khẩu trước.")
        password = os.environ.get("HISTORIA_PASSWORD") or typer.prompt("Mật khẩu Historia", hide_input=True)
        reply = client.post("/api/auth/login", json={"password": password})
        if reply.is_error:
            raise typer.BadParameter(reply.json().get("detail", "Đăng nhập thất bại."))
    return client


@app.command("reset-password")
def reset_password():
    """Forgot the password? Remove it (needs access to this computer); a new one is asked on next open."""
    from ghm.api import Settings, HostService, SecretStore, make_session_factory, auth_module
    service = HostService(make_session_factory(Settings().database_url), SecretStore.from_environment(), lambda h, s: None)
    if not typer.confirm("Xóa mật khẩu Historia hiện tại và đăng xuất mọi phiên?"):
        raise typer.Exit(1)
    auth_module.Auth(service).clear()
    typer.echo("Đã xóa. Mở Historia để tạo mật khẩu mới.")


@app.command("export-backend")
def export_backend(host_id: str):
    """Export only a currently reachable and smoke-verified local backend."""
    try:
        response = _client().get(f"/api/hosts/{host_id}/backend", timeout=15)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise typer.BadParameter("Run Verify backend and start a healthy tunnel in the local UI first.") from exc
    typer.echo(yaml.safe_dump(response.json(), sort_keys=False).strip())


@models.command("fill")
def fill(name: str):
    """Resolve one reviewed candidate against official HF metadata without downloading weights."""
    response = _client().post(f"/api/models/{name}/resolve")
    if response.is_error:
        raise typer.BadParameter(response.json().get("detail", "Metadata resolution failed."))
    typer.echo(json.dumps(response.json(), indent=2))


if __name__ == "__main__":
    app()
