from pathlib import Path
from uuid import uuid4

import typer

from vv.config import load_config
from vv.manifest import Manifest, sha256_file
from vv.stages import encode, extract, probe

app = typer.Typer(no_args_is_help=True)


@app.command()
def init(path: Path = typer.Option(Path("configs/example.yaml"), "--path")) -> None:
    """Write an example configuration if it does not already exist."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        typer.echo(f"Already exists: {path}")
        return
    path.write_text("runs_dir: runs\nprocess_fps: native\nencode:\n  crf: 18\n", encoding="utf-8")
    typer.echo(f"Created {path}")


@app.command()
def probe_video(video: Path) -> None:
    """Print ffprobe metadata for a video."""
    typer.echo(probe.run(video))


@app.command("run")
def run_video(video: Path, config: Path = typer.Option(..., "--config")) -> None:
    """Run Phase 1: probe, extract PNGs, then re-encode unchanged frames."""
    cfg = load_config(config)
    run_id = uuid4().hex[:12]
    run_dir = cfg.runs_dir / run_id
    manifest = Manifest(run_dir / "manifest.json")
    source_hash = sha256_file(video)
    probe_record = manifest.data["stages"].get("probe", {})
    if manifest.is_complete("probe", source_hash):
        metadata = probe_record["metadata"]
    else:
        metadata = probe.run(video)
        manifest.mark("probe", "complete", source_hash, metadata=metadata)
    frames_dir = run_dir / "frames"
    extract_record = manifest.data["stages"].get("extract_frames", {})
    if manifest.is_complete("extract_frames", source_hash) and list(frames_dir.glob("*.png")):
        count = int(extract_record["frame_count"])
    else:
        count = extract.run(video, frames_dir)
        manifest.mark("extract_frames", "complete", source_hash, frame_count=count)
    output = run_dir / "output.mp4"
    encode_input_hash = f"{source_hash}:{count}:{cfg.encode.crf}"
    if not (manifest.is_complete("encode", encode_input_hash) and output.exists()):
        encode.run(frames_dir, video, output, metadata, cfg.encode.crf)
        manifest.mark("encode", "complete", encode_input_hash, output=str(output), frame_count=count)
    typer.echo(f"Completed run {run_id}: {output}")


@app.command()
def resume(run_id: str, config: Path = typer.Option(..., "--config")) -> None:
    """Show the manifest for a resumable run."""
    cfg = load_config(config)
    path = cfg.runs_dir / run_id / "manifest.json"
    if not path.exists():
        raise typer.BadParameter(f"Unknown run: {run_id}")
    typer.echo(path.read_text(encoding="utf-8"))


@app.command()
def web(host: str = "127.0.0.1", port: int = 8765) -> None:
    """Launch the local browser UI."""
    from vv.web import serve

    serve(host, port)


if __name__ == "__main__":
    app()
