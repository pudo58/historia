"""CLI client for the local API; never trusts a stale database tunnel URL."""
import json
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


@app.command("export-backend")
def export_backend(host_id: str):
    """Export only a currently reachable and smoke-verified local backend."""
    try:
        response = httpx.get(f"{BASE}/api/hosts/{host_id}/backend", timeout=15, trust_env=False)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise typer.BadParameter("Run Verify backend and start a healthy tunnel in the local UI first.") from exc
    typer.echo(yaml.safe_dump(response.json(), sort_keys=False).strip())


@models.command("fill")
def fill(name: str):
    """Resolve one reviewed candidate against official HF metadata without downloading weights."""
    response = httpx.post(f"{BASE}/api/models/{name}/resolve", timeout=60, trust_env=False)
    if response.is_error:
        raise typer.BadParameter(response.json().get("detail", "Metadata resolution failed."))
    typer.echo(json.dumps(response.json(), indent=2))


if __name__ == "__main__":
    app()
