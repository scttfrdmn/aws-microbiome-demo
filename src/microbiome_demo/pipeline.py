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

# On-demand prices in us-east-1 (ARM64, verified 2026-05)
_INSTANCE_PRICES: dict[str, float] = {
    "t4g.small": 0.0168,
    "t4g.medium": 0.0336,
    "t4g.large": 0.0672,
    "c7g.2xlarge": 0.3264,
    "c7g.4xlarge": 0.6528,
    "c7g.8xlarge": 1.3056,
}


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

    # Cost estimate: head (t4g.small) + average task instance mix.
    # ~40% queue utilisation on average across the run is a reasonable
    # approximation before we have per-task billing data.
    head_price = _INSTANCE_PRICES["t4g.small"]
    task_price = _INSTANCE_PRICES.get(getattr(cfg, "INSTANCE_TYPE", "c7g.4xlarge"), 0.6528)
    ec2_cost = (elapsed / 3600) * (head_price + queue_size * task_price * 0.4)

    p = PipelineProgress(
        queue_size=queue_size,
        elapsed_seconds=elapsed,
        ec2_cost_usd=ec2_cost,
    )

    if raw is None:
        return p

    p.status = raw.get("status", "running")
    p.tasks_total = raw.get("tasks_total", 0)
    p.tasks_running = raw.get("tasks_running", 0)
    p.tasks_done = raw.get("tasks_done", 0)
    p.tasks_failed = raw.get("tasks_failed", 0)

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
    """Return True if the head node has written a complete status to S3.

    Workaround for spore-host/spawn#26 where --check-complete returns 0
    before SPAWN_COMPLETE exists.  We instead check progress.json which
    the head node monitor writes with status="complete" when Nextflow exits.
    """
    s3 = boto3.client("s3", region_name=cfg.REGION)
    prog = _safe_get(s3, cfg.BUCKET, f"results/{cfg.JOB_NAME}/progress.json")
    if prog is None:
        return False
    return prog.get("status") == "complete"


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
