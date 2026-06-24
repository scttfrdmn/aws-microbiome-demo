"""
pipeline.py  --  poll S3 for Nextflow pipeline progress.

The head node writes progress.json every 15 seconds.  This module reads
it and returns a structured snapshot the dashboard can render.

Data volume tracking:
  - roda_bytes_read:     bytes pulled from s3://sra-pub-run-odp/ (RODA)
  - fastq_bytes:         bytes written as FASTQ after SRA conversion
  - analysis_bytes_read: bytes read by Kraken2/MetaPhlAn

  fastq_bytes / roda_bytes_read  ≈  SRA expansion ratio (typically 3-4×)
  The difference between fastq_bytes and analysis_bytes_read shows how
  much data was read multiple times (once by each classifier).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import boto3

from . import truffle as _truffle


@dataclass
class DataVolume:
    """Bytes moved at each stage of the pipeline."""

    roda_bytes_read: int = 0  # pulled from RODA (SRA format)
    fastq_bytes: int = 0  # after fasterq-dump conversion
    analysis_bytes_read: int = 0  # read by classifiers (Kraken2/MetaPhlAn)

    @property
    def expansion_ratio(self) -> float:
        """SRA → FASTQ expansion ratio (typically 3-4×)."""
        if self.roda_bytes_read == 0:
            return 0.0
        return self.fastq_bytes / self.roda_bytes_read

    @property
    def roda_gb(self) -> float:
        return self.roda_bytes_read / 1e9

    @property
    def fastq_gb(self) -> float:
        return self.fastq_bytes / 1e9


@dataclass
class PipelineProgress:
    """Live snapshot of pipeline execution."""

    status: str = "idle"  # idle | running | complete | error
    elapsed_seconds: float = 0.0
    tasks_total: int = 0
    tasks_running: int = 0
    tasks_done: int = 0
    tasks_failed: int = 0
    queue_size: int = 0  # Nextflow queueSize (from truffle quota)
    ec2_cost_usd: float = 0.0
    data: DataVolume = field(default_factory=DataVolume)
    species_counts: dict[str, list[str]] = field(default_factory=dict)

    @property
    def concurrency_pct(self) -> float:
        """tasks_running / queue_size as a 0-1 fraction."""
        if self.queue_size == 0:
            return 0.0
        return min(1.0, self.tasks_running / self.queue_size)

    @property
    def completion_pct(self) -> float:
        if self.tasks_total == 0:
            return 0.0
        return self.tasks_done / self.tasks_total


def poll_progress(cfg, start_time: float, queue_size: int) -> PipelineProgress:
    """Read progress.json from S3 and return a PipelineProgress snapshot.

    Args:
        cfg:        config module (REGION, BUCKET, JOB_NAME).
        start_time: time.time() when the head instance was launched.
        queue_size: queueSize derived from truffle quota query.
    """
    s3 = boto3.client("s3", region_name=cfg.REGION)
    raw = _safe_get(s3, cfg.BUCKET, f"results/{cfg.JOB_NAME}/progress.json")

    elapsed = time.time() - start_time

    head_type = getattr(cfg, "HEAD_INSTANCE_TYPE", "c7g.medium")
    head_spec = _truffle.get_instance_spec(head_type, cfg.REGION)
    head_price = head_spec.on_demand_price_usd if head_spec else 0.0363

    p = PipelineProgress(
        queue_size=queue_size,
        elapsed_seconds=elapsed,
    )

    if raw is None:
        p.ec2_cost_usd = (elapsed / 3600) * head_price
        return p

    p.status = raw.get("status", "running")
    p.tasks_total = raw.get("tasks_total", 0)
    p.tasks_running = raw.get("tasks_running", 0)
    p.tasks_done = raw.get("tasks_done", 0)
    p.tasks_failed = raw.get("tasks_failed", 0)

    # Cost: head node + actually-running task instances.
    # Use blended average of task instance prices from truffle.
    from . import nextflow_config as _nfc
    task_specs = _truffle.get_instance_specs(_nfc.all_instance_types(cfg), cfg.REGION)
    avg_task_price = (
        sum(s.on_demand_price_usd for s in task_specs.values()) / len(task_specs)
        if task_specs else 0.0
    )
    p.ec2_cost_usd = (elapsed / 3600) * (head_price + p.tasks_running * avg_task_price)

    # data_volumes key used in summary.json; flat in progress.json
    dv = raw.get("data_volumes") or raw
    p.data = DataVolume(
        roda_bytes_read=dv.get("roda_bytes_read", 0),
        fastq_bytes=dv.get("fastq_bytes", 0),
        analysis_bytes_read=dv.get("analysis_bytes_read", 0),
    )

    p.species_counts = _sample_species(s3, cfg.BUCKET, cfg.JOB_NAME)

    return p


def read_summary(cfg) -> dict[str, Any] | None:
    """Return summary.json if it exists, else None."""
    s3 = boto3.client("s3", region_name=cfg.REGION)
    return _safe_get(s3, cfg.BUCKET, f"results/{cfg.JOB_NAME}/summary.json")


def is_pipeline_complete(cfg) -> bool:
    """Return True if the head node has written a genuine complete status to S3.

    Requires status=="complete" AND tasks_done > 0 to avoid false positives
    from stale progress.json files left by previous failed runs.
    """
    s3 = boto3.client("s3", region_name=cfg.REGION)
    prog = _safe_get(s3, cfg.BUCKET, f"results/{cfg.JOB_NAME}/progress.json")
    if prog is None:
        return False
    return prog.get("status") == "complete" and prog.get("tasks_done", 0) > 0


def clear_results(cfg) -> None:
    """Delete the results prefix for this job so stale data doesn't mislead polling."""
    s3 = boto3.client("s3", region_name=cfg.REGION)
    prefix = f"results/{cfg.JOB_NAME}/"
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=cfg.BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            s3.delete_object(Bucket=cfg.BUCKET, Key=obj["Key"])


def db_marker_prefix(cfg) -> str:
    """S3 key prefix holding the db_path markers for this job (no trailing slash)."""
    return f"dbs/{cfg.JOB_NAME}"


def db_marker_s3_uri(cfg, db_name: str) -> str:
    """The s3:// URI taxprofiler's databases.csv points db_path at for `db_name`.

    The basename (e.g. 'kraken2') MUST equal the ext.volumes mount basename so
    nf-spawn (#55) symlinks the staged input to the mounted EBS volume on the
    task instead of downloading — zero copy. The object at this URI is a tiny
    MARKER, never the DB: it exists only so taxprofiler's head-side `exists:true`
    schema check passes against the s3:// workDir filesystem (so Nextflow does
    NOT foreign-copy a head-local path up to S3 and deadlock). Real DB bytes are
    read in place off the volume via the symlink.
    """
    return f"s3://{cfg.BUCKET}/{db_marker_prefix(cfg)}/{db_name}"


def write_db_markers(cfg, db_names: list[str]) -> None:
    """Create the marker objects backing each db_path s3:// URI.

    taxprofiler types db_path as format:path + exists:true, so the URI must
    resolve to something. We write a zero-byte marker AT the db_path key itself
    (an object whose key is the db dir name); the task-side symlink replaces it
    with the real mounted volume, so the marker content is never read.
    """
    s3 = boto3.client("s3", region_name=cfg.REGION)
    for name in db_names:
        s3.put_object(
            Bucket=cfg.BUCKET,
            Key=f"{db_marker_prefix(cfg)}/{name}",
            Body=b"nf-spawn ext.volumes marker - real DB is on the mounted EBS volume\n",
        )


def clear_db_markers(cfg) -> None:
    """Delete this job's db_path marker objects (cleanup after a run)."""
    s3 = boto3.client("s3", region_name=cfg.REGION)
    prefix = f"{db_marker_prefix(cfg)}/"
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=cfg.BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            s3.delete_object(Bucket=cfg.BUCKET, Key=obj["Key"])


def _safe_get(s3, bucket: str, key: str) -> dict | None:
    try:
        resp = s3.get_object(Bucket=bucket, Key=key)
        return json.loads(resp["Body"].read())
    except Exception:  # noqa: BLE001
        return None


def _sample_species(s3, bucket: str, job_name: str) -> dict[str, list[str]]:
    """Return up to 3 top species per body site from landed Kraken2 JSONs."""
    counts: dict[str, list[str]] = {}
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket=bucket,
            Prefix=f"results/{job_name}/kraken2/",
            PaginationConfig={"MaxItems": 20},
        ):
            for obj in page.get("Contents", []):
                data = _safe_get(s3, bucket, obj["Key"])
                if not data:
                    continue
                site = data.get("body_site", "unknown")
                top = data.get("top_species", [])[:3]
                if top and site not in counts:
                    counts[site] = top
    except Exception:  # noqa: BLE001
        pass
    return counts


def _fmt_elapsed(seconds: float) -> str:
    """Format elapsed seconds as 'Xm Ys' or 'Xs'."""
    s = int(seconds)
    m, sec = divmod(s, 60)
    if m:
        return f"{m}m {sec}s"
    return f"{sec}s"


def _instance_price(instance_type: str) -> float:
    return _INSTANCE_PRICES.get(instance_type, 0.6528)
