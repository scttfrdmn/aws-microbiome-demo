"""
app.py  --  FastAPI backend for the microbiome demo dashboard.

Endpoints:
  GET  /            serves index.html
  GET  /static/*    serves static assets
  POST /api/start   launches the spawn job array and returns a run_id
  WS   /ws          streams live progress events as JSON
  GET  /api/status  returns current PipelineProgress snapshot
  GET  /api/results returns the final summary.json (404 if not ready)

WebSocket event protocol (same style as the PCSK9 demo):

  { "type": "phase",      "label": str }
  { "type": "workers_launched", "count": int, "instance_ids": list[str] }
  { "type": "progress",   "completed": int, "total": int,
                          "running_instances": int, "complete_instances": int,
                          "ec2_cost_usd": float, "elapsed_seconds": float }
  { "type": "sample_done", "srr": str, "body_site": str, "top_species": list[str] }
  { "type": "model",      "tier": str, "label": str, "state": "start"|"done",
                          "usage"?: dict, "cost"?: float }
  { "type": "insight",    "text": str }
  { "type": "cost",       "total": float }
  { "type": "done" }
  { "type": "error",      "message": str }
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

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

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
    "instance_ids": [],
    "instance_statuses": {},
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

# Browser-open config: only open once on startup
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
    """Launch the spawn job array.  Idempotent: returns existing run_id if running."""
    with _STATE_LOCK:
        if _STATE["status"] == "running":
            return {"status": "already_running", "run_id": _STATE["run_id"]}

        _STATE["status"] = "running"
        _STATE["run_id"] = f"run-{int(time.time())}"
        _STATE["start_time"] = time.time()
        _STATE["instance_ids"] = []
        _STATE["instance_statuses"] = {}
        _STATE["progress"] = None
        _STATE["summary"] = None
        _STATE["error"] = None

    # Run the pipeline in a background thread so the HTTP response returns quickly.
    thread = threading.Thread(target=_run_pipeline, daemon=True)
    thread.start()

    return {"status": "started", "run_id": _STATE["run_id"]}


@app.get("/api/status")
async def get_status():
    with _STATE_LOCK:
        return {
            "status": _STATE["status"],
            "run_id": _STATE["run_id"],
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

    # Send current status immediately on connect
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
                # Send a heartbeat so the browser knows we're alive
                await ws.send_text(json.dumps({"type": "heartbeat"}))
    except WebSocketDisconnect:
        pass
    finally:
        _SUBSCRIBERS.remove(queue)


def _broadcast(event: dict) -> None:
    """Thread-safe broadcast to all connected WebSocket clients."""
    for q in list(_SUBSCRIBERS):
        with contextlib.suppress(Exception):
            q.put_nowait(event)


# ---------------------------------------------------------------------------
# Pipeline runner (background thread)
# ---------------------------------------------------------------------------


def _run_pipeline() -> None:
    """Launch workers, poll progress, synthesize results.  Runs in a daemon thread."""
    from . import agent, pipeline, spawn, worker_script

    def emit(event: dict) -> None:
        _broadcast(event)
        # Also update shared state for /api/status
        if event.get("type") == "progress":
            with _STATE_LOCK:
                _STATE["progress"] = event

    try:
        emit({"type": "phase", "label": "Preparing sample slices…"})

        # Split the samplesheet into per-instance slices and upload to S3
        from corpus_prep import HMP_ACCESSIONS  # type: ignore[import]

        sample_count = min(cfg.SAMPLE_COUNT, len(HMP_ACCESSIONS))
        accessions = HMP_ACCESSIONS[:sample_count]

        slice_keys = worker_script.write_samplesheet_slices(cfg, accessions)
        emit({"type": "phase", "label": f"Uploaded {len(slice_keys)} sample slice(s) to S3"})

        # Launch one worker per slice
        start_time = time.time()
        workers: list[spawn.WorkerGroup] = []
        for _i, skey in enumerate(slice_keys):
            script_str = worker_script.render(cfg, skey)
            script_path = worker_script.write_temp(script_str)
            wg = spawn.launch_workers(cfg, script_path, emit=emit)
            workers.append(wg)

        all_instance_ids = [iid for wg in workers for iid in wg.instance_ids]
        with _STATE_LOCK:
            _STATE["instance_ids"] = all_instance_ids

        emit(
            {
                "type": "workers_launched",
                "count": len(all_instance_ids),
                "instance_ids": all_instance_ids,
            }
        )

        # Poll until all workers are done (or failed)
        _poll_until_done(cfg, all_instance_ids, start_time, emit)

        # Synthesize insights via Bedrock
        summary = pipeline.read_summary(cfg)
        if summary:
            with _STATE_LOCK:
                _STATE["summary"] = summary
            agent.synthesize(summary, emit)
        else:
            emit(
                {
                    "type": "error",
                    "message": ("Pipeline completed but no summary.json found in S3."),
                }
            )

        with _STATE_LOCK:
            _STATE["status"] = "complete"

    except Exception as exc:  # noqa: BLE001
        error_msg = str(exc)
        with _STATE_LOCK:
            _STATE["status"] = "error"
            _STATE["error"] = error_msg
        emit({"type": "error", "message": error_msg})


def _poll_until_done(
    cfg,
    instance_ids: list[str],
    start_time: float,
    emit,
    poll_interval: int = 15,
    max_wait_minutes: int = 90,
) -> None:
    """Poll spawn status and S3 progress until all instances are complete."""
    from . import pipeline, spawn

    max_polls = (max_wait_minutes * 60) // poll_interval

    for poll_num in range(max_polls):
        time.sleep(poll_interval)

        instance_statuses = spawn.poll_workers(instance_ids)
        with _STATE_LOCK:
            _STATE["instance_statuses"] = instance_statuses

        progress = pipeline.poll_progress(cfg, instance_ids, start_time, instance_statuses)

        emit(
            {
                "type": "progress",
                "completed": progress.completed_samples,
                "total": progress.total_samples,
                "running_instances": progress.running_instances,
                "complete_instances": progress.complete_instances,
                "failed_instances": progress.failed_instances,
                "ec2_cost_usd": progress.ec2_cost_usd,
                "elapsed_seconds": progress.elapsed_seconds,
                "species_counts": progress.species_counts,
            }
        )

        all_done = all(s in ("complete", "failed") for s in instance_statuses.values())
        if all_done:
            emit({"type": "phase", "label": "All workers complete. Building summary…"})
            return

        if (poll_num + 1) % 4 == 0:  # every minute
            n_done = sum(1 for s in instance_statuses.values() if s == "complete")
            emit(
                {
                    "type": "phase",
                    "label": (
                        f"{progress.completed_samples}/{progress.total_samples} samples done — "
                        f"{n_done}/{len(instance_ids)} workers complete — "
                        f"{progress.elapsed_seconds / 60:.1f} min elapsed"
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
