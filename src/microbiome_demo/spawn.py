"""
spawn.py  --  thin programmatic wrapper around the spawn CLI.

Provides helpers for launching and monitoring EC2 instances via spawn:
  - launch_instance()  launch a single named instance
  - poll_workers()     check whether instances have finished
  - stop_workers()     terminate instances (best-effort)

All calls shell out to the `spawn` binary on PATH.  The caller is responsible
for passing a valid config object that exposes INSTANCE_TYPE, REGION,
AMI_ID, INSTANCE_TTL, and JOB_NAME.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class WorkerGroup:
    """Tracks a launched set of spawn instances."""

    job_name: str
    instance_ids: list[str]
    count: int


def _run_spawn(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run a spawn subcommand, return the CompletedProcess."""
    return subprocess.run(["spawn"] + args, capture_output=True, text=True, check=check)


def _spawn_json(args: list[str]) -> dict | list:
    """Run a spawn subcommand with JSON output and parse the result."""
    result = _run_spawn(args + ["-o", "json"], check=False)
    if result.returncode not in (0, 2):  # 2 = still running, expected for status
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
    """Launch one or more instances via spawn.

    When cfg.INSTANCE_COUNT == 1 (the head node case), omits --count so spawn
    returns a single instance dict rather than a list.

    Args:
        cfg:            config namespace (INSTANCE_TYPE, REGION, AMI_ID,
                        INSTANCE_COUNT, INSTANCE_TTL, JOB_NAME).
        user_data_path: path to the cloud-init bash script.
        emit:           optional event callback for phase/progress updates.

    Returns:
        WorkerGroup with the launched instance IDs.
    """
    count = getattr(cfg, "INSTANCE_COUNT", 1)
    label = "head instance" if count == 1 else f"{count} instances"

    if emit:
        emit({"type": "phase", "label": f"Launching {label} ({cfg.INSTANCE_TYPE})…"})

    volume_size = getattr(cfg, "VOLUME_SIZE", 0)  # 0 = use AMI default

    cmd = [
        "spawn",
        "launch",
        cfg.JOB_NAME,
        "--instance-type",
        cfg.INSTANCE_TYPE,
        "--region",
        cfg.REGION,
        "--ami",
        cfg.AMI_ID,
        "--user-data-file",
        user_data_path,
        "--ttl",
        cfg.INSTANCE_TTL,
        "--wait-for-ssh",
        "-o", "json",   # stdout = clean JSON array; stderr = audit logs
        "-y",
    ]
    if volume_size:
        cmd.extend(["--volume-size", str(volume_size)])
    # Attach read-only DB snapshots to the head node at their mount paths. This
    # is required by nf-spawn 0.5.0's volume-backed-DB recipe: nf-core/taxprofiler
    # validates that every databases.csv db_path EXISTS on the head (head-side
    # schema validation) before dispatching tasks. The tasks get the same volume
    # via ext.volumes; the head needs it only so that validation passes.
    # Each entry is a "snap-xxx:/mount:ro" spec (see _head_attach_volumes()).
    for spec in getattr(cfg, "HEAD_ATTACH_VOLUMES", []) or []:
        cmd.extend(["--attach-volume", spec])
    # Only pass --count for arrays; omit for single instances.
    if count > 1:
        cmd.extend(["--count", str(count)])

    # Capture stdout only — stderr contains audit log JSON and pricing lines
    # which are not parseable as the launch result (spore-host/spawn#21).
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)

    if result.returncode != 0:
        raise RuntimeError(
            f"spawn launch failed (exit {result.returncode}): {result.stderr[:300]}"
        )

    # stdout is a clean JSON array: [{"instance_id": "i-...", "name": "...", ...}]
    try:
        data = json.loads(result.stdout)
        if not isinstance(data, list):
            data = [data]
        instance_ids = [
            d.get("instance_id") or d.get("InstanceId")
            for d in data
            if d.get("instance_id") or d.get("InstanceId")
        ]
    except (json.JSONDecodeError, KeyError):
        # Fallback: look up by name if stdout parse fails
        instance_ids = _find_instances_by_name(cfg.JOB_NAME, expected=count)

    if not instance_ids:
        instance_ids = _find_instances_by_name(cfg.JOB_NAME, expected=count)

    if emit:
        emit(
            {
                "type": "workers_launched",
                "count": len(instance_ids),
                "instance_ids": instance_ids,
            }
        )

    return WorkerGroup(job_name=cfg.JOB_NAME, instance_ids=instance_ids, count=len(instance_ids))


def poll_workers(instance_ids: list[str]) -> dict[str, str]:
    """Return status for each instance_id.

    Returns:
        dict mapping instance_id → "running" | "complete" | "failed" | "unknown"

    spawn --check-complete exit codes:
      0 = complete   (SPAWN_COMPLETE marker found)
      1 = failed
      2 = running
      3 = error / not found
    """
    statuses: dict[str, str] = {}
    for iid in instance_ids:
        result = _run_spawn(["status", iid, "--check-complete"], check=False)
        if result.returncode == 0:
            statuses[iid] = "complete"
        elif result.returncode == 1:
            statuses[iid] = "failed"
        elif result.returncode == 2:
            statuses[iid] = "running"
        else:
            statuses[iid] = "unknown"
    return statuses


def _upload_script(cfg, local_path: str, s3_key: str) -> None:
    """Upload a local script to S3 so --command can fetch it."""
    import boto3

    s3 = boto3.client("s3", region_name=cfg.REGION)
    with open(local_path, "rb") as f:
        s3.put_object(Bucket=cfg.BUCKET, Key=s3_key, Body=f.read())


def _find_instances_by_name(job_name: str, expected: int = 1, retries: int = 12) -> list[str]:
    """Poll `spawn list -o json` until we find `expected` instances named job_name.

    Workaround for spore-host/spawn#21: `spawn launch -o json` emits log lines
    rather than a parseable instance JSON, so we look up instances by name after
    launch instead of parsing stdout.

    Args:
        job_name: the name passed to spawn launch.
        expected: number of instances to find (1 for head node, N for arrays).
        retries:  number of 5-second polling attempts before giving up.

    Returns:
        List of instance_id strings (may be shorter than expected on timeout).
    """
    import time

    for _ in range(retries):
        found = list_workers(job_name)
        ids = [
            d.get("instance_id") or d.get("InstanceId")
            for d in found
            if d.get("instance_id") or d.get("InstanceId")
        ]
        if len(ids) >= expected:
            return ids[:expected]
        time.sleep(5)
    return []


def list_workers(job_name: str) -> list[dict]:
    """Return spawn's view of all instances matching job_name."""
    try:
        data = _spawn_json(["list"])
    except RuntimeError:
        return []
    if isinstance(data, list):
        return [d for d in data if job_name in d.get("name", "")]
    return []


def stop_workers(instance_ids: list[str]) -> None:
    """Terminate all instances in the list (best-effort, no raise on failure)."""
    import contextlib

    for iid in instance_ids:
        with contextlib.suppress(Exception):
            _run_spawn(["stop", iid, "-y"], check=False)
