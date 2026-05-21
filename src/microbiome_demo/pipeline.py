"""
pipeline.py  --  monitor a running Nextflow metagenomics pipeline via S3.

The worker instances write progress to S3 as they run:
  s3://{BUCKET}/results/{JOB_NAME}/progress/{instance_id}.json
  s3://{BUCKET}/results/{JOB_NAME}/kraken2/{srr}.json
  s3://{BUCKET}/results/{JOB_NAME}/metaphlan/{srr}.json
  s3://{BUCKET}/results/{JOB_NAME}/summary.json  (written when all done)

This module provides:
  poll_progress()  --  read all progress files and return an aggregate view
  read_summary()   --  return the final summary.json or None if not ready
  estimate_cost()  --  compute running EC2 cost from elapsed time + config
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import boto3

# c7g.4xlarge on-demand price in us-east-1 (ARM64 Graviton3, 16 vCPU, 32 GB)
# Source: https://aws.amazon.com/ec2/pricing/on-demand/ (verified 2026-05)
_C7G_4XL_USD_PER_HOUR = 0.6528


@dataclass
class PipelineProgress:
    """Aggregate progress view across all worker instances."""

    total_samples: int = 0
    completed_samples: int = 0
    failed_samples: int = 0
    running_instances: int = 0
    complete_instances: int = 0
    failed_instances: int = 0
    elapsed_seconds: float = 0.0
    ec2_cost_usd: float = 0.0
    species_counts: dict[str, list[str]] = field(default_factory=dict)
    # Per-sample status: srr → "queued" | "running" | "done" | "failed"
    sample_statuses: dict[str, str] = field(default_factory=dict)


def _safe_get(s3, bucket: str, key: str) -> dict | None:
    """Fetch and parse a JSON object from S3; return None if missing or invalid."""
    try:
        resp = s3.get_object(Bucket=bucket, Key=key)
        return json.loads(resp["Body"].read())
    except s3.exceptions.NoSuchKey:
        return None
    except Exception:  # noqa: BLE001
        return None


def _list_keys(s3, bucket: str, prefix: str) -> list[str]:
    """Return all object keys under prefix (handles pagination)."""
    keys: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])
    return keys


def poll_progress(
    cfg,
    instance_ids: list[str],
    start_time: float,
    instance_statuses: dict[str, str],
) -> PipelineProgress:
    """Aggregate progress from all S3 progress files.

    Args:
        cfg:               config module (REGION, BUCKET, JOB_NAME, INSTANCE_COUNT,
                           INSTANCE_TYPE, SAMPLE_COUNT).
        instance_ids:      IDs returned by spawn.launch_workers().
        start_time:        time.time() when the job was launched.
        instance_statuses: dict from spawn.poll_workers() — iid → status string.

    Returns:
        PipelineProgress snapshot.
    """
    s3 = boto3.client("s3", region_name=cfg.REGION)
    prefix = f"results/{cfg.JOB_NAME}/"

    progress = PipelineProgress(total_samples=cfg.SAMPLE_COUNT)

    # Elapsed and cost
    elapsed = time.time() - start_time
    progress.elapsed_seconds = elapsed
    hours = elapsed / 3600.0
    n_instances = len(instance_ids) or cfg.INSTANCE_COUNT
    price_per_hour = _instance_price(cfg.INSTANCE_TYPE)
    progress.ec2_cost_usd = hours * n_instances * price_per_hour

    # Instance statuses
    for iid in instance_ids:
        st = instance_statuses.get(iid, "running")
        if st == "complete":
            progress.complete_instances += 1
        elif st == "failed":
            progress.failed_instances += 1
        else:
            progress.running_instances += 1

    # Per-sample results from kraken2 outputs
    kraken_keys = _list_keys(s3, cfg.BUCKET, f"{prefix}kraken2/")
    for key in kraken_keys:
        srr = key.split("/")[-1].replace(".json", "")
        data = _safe_get(s3, cfg.BUCKET, key)
        if data:
            progress.completed_samples += 1
            progress.sample_statuses[srr] = "done"
            # Collect top-3 species per body site for the live display
            body_site = data.get("body_site", "unknown")
            top_species = data.get("top_species", [])[:3]
            if top_species:
                progress.species_counts.setdefault(body_site, [])
                progress.species_counts[body_site] = top_species

    # Mark failed samples if their instance failed
    failed_keys = _list_keys(s3, cfg.BUCKET, f"{prefix}failed/")
    for key in failed_keys:
        srr = key.split("/")[-1].replace(".json", "")
        progress.failed_samples += 1
        progress.sample_statuses[srr] = "failed"

    return progress


def read_summary(cfg) -> dict[str, Any] | None:
    """Return the final summary.json if it exists, else None.

    The Nextflow pipeline writes this after all samples complete:
      {
        "total_samples": N,
        "completed": N,
        "body_sites": {
          "stool": {"top_species": [...], "diversity": {...}},
          "buccal_mucosa": {...},
          "anterior_nares": {...}
        },
        "cross_site_comparison": {...},
        "elapsed_seconds": ...,
        "ec2_cost_usd": ...
      }
    """
    s3 = boto3.client("s3", region_name=cfg.REGION)
    return _safe_get(s3, cfg.BUCKET, f"results/{cfg.JOB_NAME}/summary.json")


def _fmt_elapsed(seconds: float) -> str:
    """Format elapsed seconds as 'Xm Ys' or 'Xs'."""
    s = int(seconds)
    m, sec = divmod(s, 60)
    if m:
        return f"{m}m {sec}s"
    return f"{sec}s"


def _instance_price(instance_type: str) -> float:
    """Return the on-demand USD/hour for a known instance type.

    Hard-codes the prices we actually use so the demo doesn't need a
    live Pricing API call during the run.
    """
    prices = {
        "c7g.4xlarge": 0.6528,  # 16 vCPU, 32 GB, Graviton3
        "c7g.8xlarge": 1.3056,
        "c7g.2xlarge": 0.3264,
        "c6g.4xlarge": 0.5440,
    }
    return prices.get(instance_type, 0.6528)  # fallback to c7g.4xlarge
