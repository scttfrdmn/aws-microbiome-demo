"""
app.py  --  FastAPI backend for the microbiome demo dashboard.

Architecture:
  - Queries vCPU quotas via truffle before launch.
  - Launches ONE small Nextflow head instance (t4g.small) via spawn.
  - The head instance runs nf-core/taxprofiler with the nf-spawn executor,
    dispatching each pipeline task to its own ephemeral EC2 instance.
  - Polls progress.json from S3 every 15 s and streams events over WebSocket.

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
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

if importlib.util.find_spec("config") is None:
    sys.exit("config.py not found — copy config.example.py to config.py and fill it in.")

import config as cfg  # type: ignore[import]

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
    """Launch the Nextflow head instance.  Idempotent if already running."""
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

    threading.Thread(target=_run_pipeline, daemon=True).start()
    return {"status": "started", "run_id": _STATE["run_id"]}


@app.get("/api/status")
async def get_status():
    with _STATE_LOCK:
        return {
            "status": _STATE["status"],
            "run_id": _STATE["run_id"],
            "queue_size": _STATE["queue_size"],
            "progress": _STATE["progress"],
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
# Pipeline runner (background thread)
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
        # ── 1. Query vCPU quotas via truffle ─────────────────────────────
        emit({"type": "phase", "label": "Querying vCPU quotas via truffle…"})

        families = list({t.split(".")[0] for t in nextflow_config.ALL_INSTANCE_TYPES})
        quotas = truffle.query_quotas(cfg.REGION, families)
        queue_size = truffle.derive_queue_size(quotas, nextflow_config.ALL_INSTANCE_TYPES)

        with _STATE_LOCK:
            _STATE["queue_size"] = queue_size

        emit(
            {
                "type": "quota",
                "queue_size": queue_size,
                "summary": truffle.quota_summary(quotas, queue_size),
            }
        )

        # ── 2. Upload SRR list and nextflow.config to S3 ─────────────────
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

        # ── 3. Launch Nextflow head instance ──────────────────────────────
        emit({"type": "phase", "label": "Launching Nextflow head instance (t4g.small)…"})

        head_script = worker_script.render(cfg, nf_cfg_key, srr_key)
        head_script_path = worker_script.write_temp(head_script)

        # Head instance: small, just runs Nextflow + spawn CLI
        head_cfg = _head_cfg(cfg)
        wg = spawn.launch_workers(head_cfg, head_script_path, emit=emit)
        head_id = wg.instance_ids[0] if wg.instance_ids else None
        start_time = time.time()

        with _STATE_LOCK:
            _STATE["head_instance_id"] = head_id

        emit({"type": "head_launched", "instance_id": head_id})

        # ── 4. Poll progress until head completes ─────────────────────────
        _poll_until_done(cfg, head_id, start_time, queue_size, emit)

        # ── 5. Bedrock synthesis ──────────────────────────────────────────
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


def _head_cfg(base_cfg):
    """Return a config-like namespace for the head instance."""
    import types

    hc = types.SimpleNamespace(
        **{k: getattr(base_cfg, k) for k in dir(base_cfg) if not k.startswith("_")}
    )
    # HEAD_INSTANCE_TYPE from config.py; fall back to t4g.small
    hc.INSTANCE_TYPE = getattr(base_cfg, "HEAD_INSTANCE_TYPE", "t4g.small")
    hc.INSTANCE_COUNT = 1
    return hc


def _poll_until_done(
    cfg,
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

        # Check head instance status
        if head_id:
            statuses = spawn.poll_workers([head_id])
            head_status = statuses.get(head_id, "running")
            if head_status == "failed":
                emit({"type": "error", "message": "Head instance failed — check logs."})
                return
        else:
            head_status = "running"

        # Poll S3 progress.json
        prog = pipeline.poll_progress(cfg, start_time, queue_size)

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
                # Data volumes from RODA
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

    _launch_config["url"] = url
    print(f"Microbiome Demo → {url}")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
