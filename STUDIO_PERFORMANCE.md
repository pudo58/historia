# Wan performance and safe continuation

This change keeps the current render geometry, 81 frames, 16 fps and two Wan experts.
The default Lightning sampler remains four total steps (2 high-noise + 2 low-noise).
It does not enable Qwen Lightning, multiple GPUs, queue filling or download/render overlap.

## Scene settings and existing films

`keyframe_steps` and `clip_steps` are nullable scene API fields. Resolution order is
the non-null stage field, then legacy `steps`, then the historical workflow default
(Qwen Image 20, Qwen Edit 4, Wan 4). The editor shows the two separate controls.
Existing `steps=20` snapshots still render 20 Wan steps unless explicitly overridden.
New snapshots have `generation_version=2`; old snapshots are not rewritten.

Changing clip steps in the scene editor invalidates clips, not speech/keyframes.
For an unfinished film, use **pause at the shot boundary**, then **apply to unsent
shots** in the production panel or the manual clip job. These endpoints accept
`{"clip_steps":4}`:

- `POST /api/studio/jobs/{id}/pending-clip-config`
- `POST /api/studio/production-runs/{id}/pending-clip-config`

Overrides live in versioned checkpoints, separately from immutable snapshots.
They protect submitted prompt IDs and completed artifact names. A production override
also applies to future clip jobs in that run. Configuration changes are rejected while
the worker is running or a submitted prompt needs reconciliation. Existing shot indices,
seeds, frame count and duration partitioning do not change. Mixed output records retain
their provenance and are not reused as if every shot had the original steps.

The two workflow schema repairs are `resolution_steps=16` / `megapixels=0.88` for
legacy Qwen Edit and SaveVideo codec normalization using connected `/object_info`.
The active geometry-aware Edit branch still uses explicit ImageScale dimensions.
The backend checks required fields, choices/model names, ranges and link types before
persisting a submission intent. Comfy remains the authoritative execution validator.
Known pre-change workflow hashes remain eligible for safe media reuse; unrelated
workflow changes are not declared compatible.

## Transport and measurements

A clip job shares one connection and in-memory upload/schema caches until it ends,
pauses, cancels or disconnects. Upload keys include image SHA-256 and fitted dimensions.
Comfy PID/start-time and runtime changes invalidate caches. If the process cannot be
identified, caching is disabled conservatively. Remote files are not deleted.

Before each POST, the checkpoint contains the deterministic prompt ID, graph hash,
resolved generation configuration and observed runtime. Updates merge fields under a
SQLite writer transaction. A lost response never triggers a blind re-POST. Successful
history output descriptors are persisted before download, allowing a subsequent download
even if history was evicted. Missing both history and completion evidence requires
reconciliation. Missing/corrupt downloaded artifacts are not silently regenerated.

`GET /api/studio/projects/{id}/performance` supplies measured audio, per-scene shot
counts/configurations, compatible hot-shot median, remaining ETA/cost and shot records.
The Video screen displays this information. Audio which has not been generated is not
treated as zero-cost work: whole-film ETA stays unknown.

Shot checkpoint timing fields:

| Field | Meaning |
| --- | --- |
| `prepare_upload_seconds` | Runtime/schema checks, image fitting/upload and graph preparation |
| `remote_wait_seconds` | Local time waiting for Comfy completion, including queue/polling |
| `comfy_execution_seconds` | Matching prompt's execution_start → execution_success timestamps |
| `download_seconds` | Time transferring output, including failed attempts |
| `validation_seconds` | Local output readability validation |
| `storage_seconds` | Artifact registration/checksum work |
| `total_seconds` | Local active processing across attempts, plus artifact storage |

Unavailable Comfy timestamps remain null/absent; waiting time is never presented as GPU
execution time. Execution time is replaced with the same prompt's measurement on resume,
not added again. The first new prompt in a connection/process session is conservatively
excluded from hot medians. Retried shots are also excluded from ETA/benchmark samples.
Library versions are read from the detected Comfy Python when available; missing evidence
stays unknown. `compute_flags` records only an allowlist, not arbitrary command arguments.

ETA uses compatible generation configuration, GPU and runtime measurements. It is an
estimate of processing, not a RunPod bill; cold starts, disconnected time, user review and
idle rental are not included. No automatic Pod rental or shutdown is introduced.

## Runtime controls

The AI installation panel includes a runtime panel with:

- `attention_backend`: `default` or `sage`.
- `memory_policy`: `default` or `highvram`.
- Read current runtime; apply/restart; explicitly install SageAttention and apply;
  explicit recovery without reinstalling after interrupted maintenance.

`manifests/studio-runtime.json` pins SageAttention 2.2.0 and Python 3.11/3.12. Default
behavior is unchanged. Managed apply verifies ownership, local job boundaries, remote
queue, Python/Torch/CUDA and the requested Sage import before stopping Comfy. It checks
the queue again after dependency probing. Other launch arguments are preserved; only
the selected attention/memory option groups are replaced. Startup is checked afterward.
No automatic attention fallback, driver update, Torch upgrade or global Python change.

Optional install uses the existing managed Comfy venv, pinned package, `--no-deps` and
`--no-build-isolation`. A compatible compiler/Triton/build environment must already be
available; failure is explicit and may leave the managed service stopped. A remote flock
protects an install that outlives SSH. A maintenance-pending checkpoint blocks other work
until explicit recovery confirms the service. Recovery never reruns pip automatically.

Adopted Comfy instances expose read-only inspection and instructions. Historia does not
install packages into them or restart them. Runtime maintenance is distinct from changing
the render profile and does not invalidate completed media. `highvram` is a benchmark
candidate, not a universal recommendation for A100 40GB or 80GB.

## A100 benchmark procedure

Do not rent a Pod until local checks pass and the rental/time budget has been agreed.
Use the same physical Pod, model files, scene image, prompt, seed, steps, dimensions and
Comfy/Torch/CUDA versions throughout a comparison. Record the actual hourly rate in the
project. Choose an idle project/host with an approved keyframe; benchmark creates separate
artifacts and never replaces production media.

The Video screen's benchmark control submits one first-shot sample and five warm samples.
API equivalent:

```http
POST /api/studio/projects/{project_id}/benchmarks
Content-Type: application/json

{"scene_id":"approved-scene-id","warm_shots":5,"max_wall_seconds":1800}
```

The time limit is checked between shots, not a hard billing cap. An in-flight prompt may
finish beyond it; the GPU rental does not stop. Failed/uncertain requests retain IDs and
resume through the ordinary durable job path. No raw curl POST to Comfy is needed.

1. Baseline runtime, fresh Comfy start, unchanged Wan settings; save benchmark job ID.
2. Install/probe Sage explicitly, restart into Sage, repeat the same scene and sample count.
3. If memory permits, restart into Sage + highvram and repeat. Do not change quality to win.
4. Download `benchmark.json` and six MP4s per run from the job results. Review matching
   seeds for identity, motion, flicker and historical details before choosing a runtime.
5. Compare using `GET /api/studio/benchmarks/compare?baseline_id=...&candidate_id=...`.
   The comparison rejects different input/hardware/software evidence, mixed runtime within
   a run, or fewer than five valid warm samples. It never auto-enables a winner.

The report includes hot median wall/Comfy seconds, hot total, measured stage total,
per-shot runtime/graph evidence and GPU-hour/$ estimates for 300s and 326s audio using
the measured per-scene duration proportions and individual ceil(duration/5.0625).
If any scene lacks audio, film estimates stay unknown. These are extrapolations, not
measurements of a complete film. Benchmark multiple representative scenes if their
configurations differ. Claim 2× only after measured wall-time improvement and visual review.

## Parallel Pods for Wan clips

A production run may name extra Pods (`parallel_host_ids` in
`POST /api/studio/projects/{id}/production-runs`, or the checkboxes under the consent panel).
Only the clip stage fans out: each Pod renders a different Wan scene at the same time; speech,
keyframes, RIFE and export stay on the main Pod/local machine. Ken Burns/still scenes render
locally beside the Pods. Each extra Pod must have a pinned fingerprint and a proven Wan
installation; it is billed separately. Wall time for clips drops roughly by the number of Pods;
total GPU-seconds stay about the same.

The local worker runs one job per host at a time (plus one local lane), so jobs on different
Pods no longer wait for each other. A failed/uncertain clip stops new dispatch; clips already
rendering on other Pods finish first, then the run shows failed/reconciling. Pause waits for
every Pod to reach a shot boundary; resume re-queues every stopped clip. Deleting an extra Pod
drops it from the run and resume moves its unfinished scene to the main Pod. A clip finished on
any Pod of the run is reused. `GET /performance` adds `parallel_gpus` and `wall_eta_seconds`.

## Local verification

Use `.venv/Scripts/python.exe -m pytest -q` on Windows and run frontend build/lint.
`tests/test_wan_performance.py` covers graph settings, legacy identity, per-shot overrides,
cache invalidation, checkpoint merging, ambiguous POST, output recovery after history
eviction, runtime ownership/queue guards and benchmark accounting. Existing production,
cancel and media-export tests remain required.

GPU speed/quality and real A100 package compatibility have not yet been benchmarked.

Local verification on 2026-09-28: full suite 227 passed; the final updated performance
module passed all 35 cases separately. Changed Python files passed Ruff; frontend
TypeScript/build/ESLint passed. The full test process disabled AsyncSSH's default keypair
discovery in memory because this workstation's default SSH keypair caused two unrelated
test failures. No user SSH key/config file was changed. The only remaining test warning
is the existing Starlette/httpx TestClient deprecation.

## Generation 4 (smoothness and quality)

- New projects default to RIFE (`rife24`): Wan renders 16 fps; without interpolation the export duplicates frames to reach 24 fps, which judders. RIFE goes 16→48 and the export drops to 24.
- Wan and RIFE clips are saved at H.264 CRF 17 (ComfyUI default is about 23) because the export re-encodes them. Verified against ComfyUI `ee71d5c` (SaveVideo `format.codec.encoding`).
- With LightX2V / Lightning (`cfg 1`) ComfyUI ignores the negative prompt, so "no text/watermark" now lives in the positive prompt.
- ComfyUI already falls back to tiled VAE decoding on out-of-memory, so the Wan graph keeps `VAEDecode`.
- Resumed jobs keep the generation version they started with and render exactly as before.
