"""
spawn.py  --  thin programmatic wrapper around the spawn CLI.

Provides two async-friendly helpers:
  - launch_workers()   spawn a job array of N worker instances
  - poll_workers()     check whether all workers have finished
  - stop_workers()     terminate all instances in a job group

All calls shell out to the `spawn` binary.  The caller is responsible
for passing a valid config object that exposes INSTANCE_TYPE, REGION,
AMI_ID, INSTANCE_COUNT, INSTANCE_TTL, and JOB_NAME.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class WorkerGroup:
    """Tracks a launched group of spawn workers."""

    job_name: str
    instance_ids: list[str]
    count: int


def _run_spawn(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run a spawn subcommand, return the CompletedProcess."""
    cmd = ["spawn"] + args
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _spawn_json(args: list[str]) -> dict | list:
    """Run a spawn subcommand with JSON output and parse the result."""
    result = _run_spawn(args + ["-o", "json"], check=False)
    if result.returncode not in (0, 2):  # 2 = still running (expected for status)
        raise RuntimeError(f"spawn error ({result.returncode}): {result.stderr[:500]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"spawn returned non-JSON: {result.stdout[:200]}") from exc


def launch_workers(
    cfg,
    user_data_path: str,
    emit: Callable[[dict], None] | None = None,
) -> WorkerGroup:
    """Launch INSTANCE_COUNT worker instances via spawn.

    Each instance receives user_data_path as its cloud-init script and runs
    the Nextflow pipeline fragment assigned to it via env vars.

    Args:
        cfg:             config module with spawn/AWS settings.
        user_data_path:  path to the bash startup script for the workers.
        emit:            optional event callback for progress updates.

    Returns:
        WorkerGroup describing the running job.
    """
    if emit:
        emit({"type": "phase", "label": f"Launching {cfg.INSTANCE_COUNT} worker instances…"})

    # spawn job array: --count N creates N instances with the same job name.
    # Each instance gets a unique index injected as SPAWN_INSTANCE_INDEX env var
    # so the pipeline script can figure out which sample slice to process.
    result = subprocess.run(
        [
            "spawn",
            "launch",
            cfg.JOB_NAME,
            "--instance-type",
            cfg.INSTANCE_TYPE,
            "--region",
            cfg.REGION,
            "--ami",
            cfg.AMI_ID,
            "--count",
            str(cfg.INSTANCE_COUNT),
            "--user-data-file",
            user_data_path,
            "--ttl",
            cfg.INSTANCE_TTL,
            "--wait-for-ssh",
            "-o",
            "json",
            "-y",  # skip interactive cost confirmation
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        raise RuntimeError(f"spawn launch failed: {result.stderr[:500]}")

    data = json.loads(result.stdout)

    # spawn returns either a list (job array) or a single dict (single instance).
    if isinstance(data, list):
        instance_ids = [d.get("instance_id") or d.get("InstanceId") for d in data]
    else:
        instance_ids = [data.get("instance_id") or data.get("InstanceId")]

    instance_ids = [i for i in instance_ids if i]  # drop any None

    if emit:
        emit(
            {
                "type": "workers_launched",
                "count": len(instance_ids),
                "instance_ids": instance_ids,
            }
        )

    return WorkerGroup(
        job_name=cfg.JOB_NAME,
        instance_ids=instance_ids,
        count=len(instance_ids),
    )


def poll_workers(instance_ids: list[str]) -> dict[str, str]:
    """Return status for each instance_id.

    Returns:
        dict mapping instance_id → one of "running", "complete", "failed", "unknown".
    """
    statuses: dict[str, str] = {}

    for iid in instance_ids:
        result = _run_spawn(["status", iid, "--check-complete"], check=False)
        # spawn --check-complete exit codes:
        #   0 = complete (SPAWN_COMPLETE marker found)
        #   1 = failed
        #   2 = still running
        #   3 = error (instance not found, etc.)
        if result.returncode == 0:
            statuses[iid] = "complete"
        elif result.returncode == 1:
            statuses[iid] = "failed"
        elif result.returncode == 2:
            statuses[iid] = "running"
        else:
            statuses[iid] = "unknown"

    return statuses


def list_workers(job_name: str) -> list[dict]:
    """Return spawn's view of all instances matching job_name."""
    try:
        data = _spawn_json(["list"])
    except RuntimeError:
        return []
    if isinstance(data, list):
        return [d for d in data if d.get("name") == job_name or job_name in d.get("name", "")]
    return []


def stop_workers(instance_ids: list[str]) -> None:
    """Terminate all instances in the list (best-effort, no raise on failure)."""
    import contextlib

    for iid in instance_ids:
        with contextlib.suppress(Exception):
            _run_spawn(["stop", iid, "-y"], check=False)
