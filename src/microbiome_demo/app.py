"""
app.py  --  FastAPI backend for the microbiome demo dashboard.

Architecture:
  - Queries vCPU quotas via truffle before launch.
  - Launches ONE small Nextflow head instance (t4g.small) via spawn.
  - The head instance runs nf-core/taxprofiler with the nf-spawn executor,
    dispatching each pipeline task to its own ephemeral EC2 instance.
  - Polls progress.json from S3 every 15 s and streams events over WebSocket.

Fake mode:
  Set DEMO_FAKE=1 to run without any AWS calls — simulates the full pipeline
  with scripted events and delays.  Useful for rehearsing the UI.

WebSocket event protocol:
  { "type": "phase",    "label": str }
  { "type": "quota",    "queue_size": int, "summary": str }
  { "type": "head_launched", "instance_id": str }
  { "type": "progress", "tasks_done": int, "tasks_total": int,
                         "tasks_running": int, "queue_size": int,
                         "concurrency_pct": float, "completion_pct": float,
                         "ec2_cost_usd": float, "elapsed_seconds": float,
                         "roda_gb": float, "fastq_gb": float,
                         "expansion_ratio": float,
                         "species_counts": dict }
  { "type": "model",    "tier": str, "label": str, "state": "start"|"done",
                         "usage"?: dict, "cost"?: float }
  { "type": "insight",  "text": str }
  { "type": "cost",     "total": float }
  { "type": "done" }
  { "type": "error",    "message": str }
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# DEMO_FAKE=1 → run without AWS calls (for rehearsal / UI testing)
_FAKE = os.environ.get("DEMO_FAKE") == "1"

if _FAKE:
    # In fake mode, create a minimal config stub so the rest of the module
    # can reference cfg.* without importing the real config.py.
    import types as _types

    cfg = _types.SimpleNamespace(  # type: ignore[assignment]
        REGION="us-east-1",
        BUCKET="demo-fake-bucket",
        SAMPLE_COUNT=100,
        JOB_NAME="microbiome-demo",
        AMI_ID="ami-fake",
        INSTANCE_TTL="3h",
        HEAD_INSTANCE_TYPE="t4g.small",
        BEDROCK_REGION="us-west-2",
        BEDROCK_MODEL="us.anthropic.claude-sonnet-4-6",
        HOST="127.0.0.1",
        PORT=8000,
    )
else:
    if importlib.util.find_spec("config") is None:
        sys.exit("config.py not found — copy config.example.py to config.py and fill it in.")
    import config as cfg  # type: ignore[import]  # noqa: E402

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_STATE: dict[str, Any] = {
    "status": "idle",  # idle | running | complete | error
    "run_id": None,
    "start_time": None,
    "head_instance_id": None,
    "queue_size": 0,
    "progress": None,
    "summary": None,
    "error": None,
}

_SUBSCRIBERS: list[asyncio.Queue] = []
_STATE_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Microbiome Demo")

_STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

_launch_config: dict[str, Any] = {"url": None, "opened": False}


@app.on_event("startup")
async def _open_browser() -> None:
    url = _launch_config.get("url")
    if url and not _launch_config["opened"]:
        _launch_config["opened"] = True
        webbrowser.open(url)


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------


@app.get("/", include_in_schema=False)
async def serve_index():
    return FileResponse(str(_STATIC_DIR / "index.html"))


@app.post("/api/start")
async def start_run():
    """Launch the pipeline (or fake pipeline).  Idempotent if already running."""
    with _STATE_LOCK:
        if _STATE["status"] == "running":
            return {"status": "already_running", "run_id": _STATE["run_id"]}

        _STATE["status"] = "running"
        _STATE["run_id"] = f"run-{int(time.time())}"
        _STATE["start_time"] = time.time()
        _STATE["head_instance_id"] = None
        _STATE["queue_size"] = 0
        _STATE["progress"] = None
        _STATE["summary"] = None
        _STATE["error"] = None

    target = _run_fake_pipeline if _FAKE else _run_pipeline
    threading.Thread(target=target, daemon=True).start()
    return {"status": "started", "run_id": _STATE["run_id"], "fake": _FAKE}


@app.get("/api/status")
async def get_status():
    with _STATE_LOCK:
        return {
            "status": _STATE["status"],
            "run_id": _STATE["run_id"],
            "queue_size": _STATE["queue_size"],
            "progress": _STATE["progress"],
            "fake": _FAKE,
        }


@app.get("/api/results")
async def get_results():
    with _STATE_LOCK:
        summary = _STATE.get("summary")
    if summary is None:
        return JSONResponse({"error": "results not ready"}, status_code=404)
    return summary


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    queue: asyncio.Queue = asyncio.Queue()
    _SUBSCRIBERS.append(queue)

    with _STATE_LOCK:
        status = _STATE["status"]
        progress = _STATE["progress"]

    if status == "running" and progress:
        await ws.send_text(json.dumps({"type": "status_snapshot", **progress}))
    elif status == "complete":
        summary = _STATE.get("summary")
        if summary:
            await ws.send_text(json.dumps({"type": "summary", **summary}))

    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30)
                await ws.send_text(json.dumps(event))
            except TimeoutError:
                await ws.send_text(json.dumps({"type": "heartbeat"}))
    except WebSocketDisconnect:
        pass
    finally:
        _SUBSCRIBERS.remove(queue)


def _broadcast(event: dict) -> None:
    for q in list(_SUBSCRIBERS):
        with contextlib.suppress(Exception):
            q.put_nowait(event)


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def _validate_config(config) -> str | None:
    """Return an error message if config is invalid, else None."""
    ami = getattr(config, "AMI_ID", "")
    if not ami:
        return "AMI_ID is not set in config.py — run `make ami` first."

    bucket = getattr(config, "BUCKET", "")
    if not bucket or bucket == "your-microbiome-demo-bucket":
        return "BUCKET is not configured in config.py — set it to your S3 bucket name."

    region = getattr(config, "REGION", "")
    valid_prefixes = ("us-", "eu-", "ap-", "ca-", "sa-", "me-", "af-")
    if not region or not any(region.startswith(p) for p in valid_prefixes):
        return f"REGION '{region}' looks invalid in config.py."

    return None


# ---------------------------------------------------------------------------
# Real pipeline runner (background thread)
# ---------------------------------------------------------------------------


def _run_pipeline() -> None:
    from . import agent, nextflow_config, pipeline, spawn, truffle, worker_script
    from .accessions import HMP_ACCESSIONS

    def emit(event: dict) -> None:
        _broadcast(event)
        if event.get("type") == "progress":
            with _STATE_LOCK:
                _STATE["progress"] = event

    try:
        # ── 0. Validate config ────────────────────────────────────────────
        err = _validate_config(cfg)
        if err:
            emit({"type": "error", "message": err})
            with _STATE_LOCK:
                _STATE["status"] = "error"
                _STATE["error"] = err
            return

        # ── 1. Query vCPU quotas via truffle ─────────────────────────────
        emit({"type": "phase", "label": "Querying vCPU quotas via truffle…"})

        families = list({t.split(".")[0] for t in nextflow_config.ALL_INSTANCE_TYPES})
        quotas = truffle.query_quotas(cfg.REGION, families)
        queue_size = truffle.derive_queue_size(quotas, nextflow_config.ALL_INSTANCE_TYPES)

        with _STATE_LOCK:
            _STATE["queue_size"] = queue_size

        if not quotas:
            emit(
                {
                    "type": "phase",
                    "label": (
                        f"truffle not available — using default queueSize={queue_size} "
                        "(install: brew install spore-host/tap/truffle)"
                    ),
                }
            )
        emit(
            {
                "type": "quota",
                "queue_size": queue_size,
                "summary": truffle.quota_summary(quotas, queue_size),
            }
        )

        # ── 2. Ensure S3 bucket exists ────────────────────────────────────
        emit({"type": "phase", "label": f"Ensuring S3 bucket s3://{cfg.BUCKET}…"})
        _ensure_bucket(cfg)

        # ── 3. Upload SRR list and nextflow.config to S3 ─────────────────
        emit({"type": "phase", "label": "Uploading SRR list and Nextflow config…"})

        sample_count = min(cfg.SAMPLE_COUNT, len(HMP_ACCESSIONS))
        accessions = HMP_ACCESSIONS[:sample_count]

        srr_key = worker_script.write_srr_slice(cfg, accessions)
        nf_cfg_str = nextflow_config.render(cfg, queue_size)
        nf_cfg_key = worker_script.upload_nextflow_config(cfg, nf_cfg_str)

        emit(
            {
                "type": "phase",
                "label": f"Config ready — queueSize={queue_size}, {sample_count} samples",
            }
        )  # noqa: E501

        # ── 4. Launch Nextflow head instance ──────────────────────────────
        emit({"type": "phase", "label": "Launching Nextflow head instance (t4g.small)…"})

        head_script = worker_script.render(cfg, nf_cfg_key, srr_key)
        head_script_path = worker_script.write_temp(head_script)

        head_cfg = _head_cfg(cfg)
        wg = spawn.launch_workers(head_cfg, head_script_path, emit=emit)
        head_id = wg.instance_ids[0] if wg.instance_ids else None
        start_time = time.time()

        with _STATE_LOCK:
            _STATE["head_instance_id"] = head_id

        emit({"type": "head_launched", "instance_id": head_id})

        # ── 5. Poll progress until head completes ─────────────────────────
        _poll_until_done(cfg, head_id, start_time, queue_size, emit)

        # ── 6. Bedrock synthesis ──────────────────────────────────────────
        summary = pipeline.read_summary(cfg)
        if summary:
            with _STATE_LOCK:
                _STATE["summary"] = summary
            agent.synthesize(summary, emit)
        else:
            emit({"type": "error", "message": "Pipeline done but no summary.json found."})

        with _STATE_LOCK:
            _STATE["status"] = "complete"

    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        with _STATE_LOCK:
            _STATE["status"] = "error"
            _STATE["error"] = msg
        emit({"type": "error", "message": msg})


# ---------------------------------------------------------------------------
# Fake pipeline runner — no AWS calls, scripted events for rehearsal
# ---------------------------------------------------------------------------

_FAKE_SUMMARY = {
    "total_samples": 100,
    "completed": 100,
    "failed": 0,
    "elapsed_seconds": 1140.0,
    "body_sites": {
        "stool": {
            "top_species": [
                "Bacteroides vulgatus",
                "Prevotella copri",
                "Faecalibacterium prausnitzii",
                "Ruminococcus bromii",
                "Eubacterium hallii",
            ],
            "diversity": {"shannon": 3.8, "observed": 142},
        },
        "buccal_mucosa": {
            "top_species": [
                "Streptococcus salivarius",
                "Veillonella parvula",
                "Actinomyces odontolyticus",
            ],
            "diversity": {"shannon": 2.1, "observed": 67},
        },
        "anterior_nares": {
            "top_species": [
                "Staphylococcus epidermidis",
                "Corynebacterium accolens",
                "Dolosigranulum pigrum",
            ],
            "diversity": {"shannon": 1.6, "observed": 38},
        },
    },
    "cross_site_comparison": {
        "bray_curtis": 0.72,
        "site_specific_taxa": [
            "Bacteroides vulgatus",
            "Staphylococcus epidermidis",
            "Streptococcus salivarius",
        ],
    },
    "data_volumes": {
        "roda_bytes_read": 10_800_000_000,  # ~10.8 GB SRA files from RODA
        "fastq_bytes": 38_200_000_000,  # ~38.2 GB FASTQs (3.5× expansion)
        "analysis_bytes_read": 76_400_000_000,  # ~76.4 GB read by Kraken2+MetaPhlAn
    },
}


def _run_fake_pipeline() -> None:
    """Simulated pipeline — emits scripted events with realistic timing."""
    from . import agent

    def emit(event: dict) -> None:
        _broadcast(event)
        if event.get("type") == "progress":
            with _STATE_LOCK:
                _STATE["progress"] = event

    def step(label: str, delay: float = 0.8) -> None:
        emit({"type": "phase", "label": label})
        time.sleep(delay)

    try:
        step("Querying vCPU quotas via truffle…")
        queue_size = 12
        with _STATE_LOCK:
            _STATE["queue_size"] = queue_size
        emit(
            {
                "type": "quota",
                "queue_size": queue_size,
                "summary": "Queue size: 12  (c7g: 192 vCPUs free, t4g: 384 vCPUs free)",
            }
        )

        step(f"Ensuring S3 bucket s3://{getattr(cfg, 'BUCKET', 'demo-bucket')}…")
        step("Uploading SRR list and Nextflow config…")
        step(f"Config ready — queueSize={queue_size}, 100 samples")
        step("Launching Nextflow head instance (t4g.small)…", delay=1.5)

        emit({"type": "head_launched", "instance_id": "i-0fake1234567890ab"})
        step("Head node booting, pulling nf-core/taxprofiler…", delay=2.0)
        step("Nextflow started — dispatching tasks via nf-spawn…", delay=1.0)

        # Simulate rolling progress over ~20 seconds
        start_time = time.time()
        species_counts: dict = {}
        total = 100

        for tick in range(20):
            elapsed = time.time() - start_time
            done = min(total, int(tick * 5.5))
            running = min(queue_size, total - done)
            ec2_cost = elapsed / 3600 * (0.0168 + queue_size * 0.6528 * 0.4)

            # Reveal species progressively as samples complete
            if done > 10 and "stool" not in species_counts:
                species_counts["stool"] = [
                    "Bacteroides vulgatus",
                    "Prevotella copri",
                    "Faecalibacterium prausnitzii",
                ]
            if done > 40 and "buccal_mucosa" not in species_counts:
                species_counts["buccal_mucosa"] = [
                    "Streptococcus salivarius",
                    "Veillonella parvula",
                ]
            if done > 70 and "anterior_nares" not in species_counts:
                species_counts["anterior_nares"] = [
                    "Staphylococcus epidermidis",
                    "Corynebacterium accolens",
                ]

            roda_gb = done * 0.108  # ~108 MB SRA per sample
            fastq_gb = roda_gb * 3.5

            emit(
                {
                    "type": "progress",
                    "tasks_done": done * 5,  # ~5 tasks per sample
                    "tasks_total": total * 5,
                    "tasks_running": running,
                    "tasks_failed": 0,
                    "queue_size": queue_size,
                    "concurrency_pct": running / queue_size,
                    "completion_pct": done / total,
                    "ec2_cost_usd": ec2_cost,
                    "elapsed_seconds": elapsed,
                    "roda_gb": roda_gb,
                    "fastq_gb": fastq_gb,
                    "expansion_ratio": 3.5 if roda_gb > 0 else 0,
                    "analysis_gb": fastq_gb * 2,
                    "species_counts": species_counts,
                }
            )
            time.sleep(1.0)

        step("All tasks complete. Building summary…", delay=1.0)

        with _STATE_LOCK:
            _STATE["summary"] = _FAKE_SUMMARY

        agent.synthesize(_FAKE_SUMMARY, emit, backend=agent.FakeBackend())

        with _STATE_LOCK:
            _STATE["status"] = "complete"

    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        with _STATE_LOCK:
            _STATE["status"] = "error"
            _STATE["error"] = msg
        emit({"type": "error", "message": msg})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_bucket(config) -> None:
    """Create the S3 bucket if it doesn't exist (idempotent)."""
    import boto3
    from botocore.exceptions import ClientError

    s3 = boto3.client("s3", region_name=config.REGION)
    try:
        if config.REGION == "us-east-1":
            s3.create_bucket(Bucket=config.BUCKET)
        else:
            s3.create_bucket(
                Bucket=config.BUCKET,
                CreateBucketConfiguration={"LocationConstraint": config.REGION},
            )
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            raise


def _head_cfg(base_cfg):
    """Return a config-like namespace for the head instance."""
    import types

    hc = types.SimpleNamespace(
        **{k: getattr(base_cfg, k) for k in dir(base_cfg) if not k.startswith("_")}
    )
    hc.INSTANCE_TYPE = getattr(base_cfg, "HEAD_INSTANCE_TYPE", "t4g.small")
    hc.INSTANCE_COUNT = 1
    return hc


def _poll_until_done(
    config,
    head_id: str | None,
    start_time: float,
    queue_size: int,
    emit,
    poll_interval: int = 15,
    max_wait_minutes: int = 90,
) -> None:
    from . import pipeline, spawn

    max_polls = (max_wait_minutes * 60) // poll_interval

    for poll_num in range(max_polls):
        time.sleep(poll_interval)

        if head_id:
            statuses = spawn.poll_workers([head_id])
            head_status = statuses.get(head_id, "running")
            if head_status == "failed":
                emit({"type": "error", "message": "Head instance failed — check logs."})
                return
        else:
            head_status = "running"

        prog = pipeline.poll_progress(config, start_time, queue_size)

        emit(
            {
                "type": "progress",
                "tasks_done": prog.tasks_done,
                "tasks_total": prog.tasks_total,
                "tasks_running": prog.tasks_running,
                "tasks_failed": prog.tasks_failed,
                "queue_size": prog.queue_size,
                "concurrency_pct": prog.concurrency_pct,
                "completion_pct": prog.completion_pct,
                "ec2_cost_usd": prog.ec2_cost_usd,
                "elapsed_seconds": prog.elapsed_seconds,
                "roda_gb": prog.data.roda_gb,
                "fastq_gb": prog.data.fastq_gb,
                "expansion_ratio": prog.data.expansion_ratio,
                "analysis_gb": prog.data.analysis_bytes_read / 1e9,
                "species_counts": prog.species_counts,
            }
        )

        if head_status == "complete":
            emit({"type": "phase", "label": "Nextflow complete. Building summary…"})
            return

        if (poll_num + 1) % 4 == 0:
            emit(
                {
                    "type": "phase",
                    "label": (
                        f"{prog.tasks_done}/{prog.tasks_total} tasks done · "
                        f"{prog.tasks_running} running · "
                        f"{prog.elapsed_seconds / 60:.1f} min elapsed"
                    ),
                }
            )

    emit({"type": "error", "message": f"Timed out after {max_wait_minutes} minutes."})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    import uvicorn

    host = getattr(cfg, "HOST", "127.0.0.1")
    port = getattr(cfg, "PORT", 8000)
    url = f"http://{host}:{port}"

    if _FAKE:
        print("Microbiome Demo → FAKE MODE (no AWS calls)")

    _launch_config["url"] = url
    print(f"Microbiome Demo → {url}")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
