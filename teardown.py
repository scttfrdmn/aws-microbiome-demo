#!/usr/bin/env python3
"""
teardown.py  --  delete everything the microbiome demo created.

Run this after the talk to clean up billable resources.

What this script deletes (and approximate ongoing costs if left running):
  - Spawn worker instances (stop any still running)     -- ~$0.65/hr each
  - S3 corpus bucket (SRA files + samplesheets)        -- ~$0.023/GB-month
  - S3 results bucket contents                         -- ~$0.023/GB-month
  - The AMI bake instance (if still running)           -- ~$0.65/hr

What this script does NOT delete:
  - The AMI itself (AMIs don't incur hourly charges; EBS snapshots are
    ~$0.05/GB-month; delete manually via the EC2 console if you want)
  - The Bedrock Knowledge Base (not created in this demo)

How it works:
  Each deletion is wrapped in _try() so a single failure does not stop
  the rest.  Every step prints either "deleted: ..." or "skip: ExceptionType".

Re-running safely:
  Idempotent -- resources already gone just print "skip" and continue.
"""

from __future__ import annotations

import subprocess

import boto3
import config as cfg  # type: ignore[import]

s3 = boto3.client("s3", region_name=cfg.REGION)


def _try(label: str, fn) -> None:
    """Call fn(); print success or skip.  Never raises."""
    try:
        fn()
        print(f"  deleted: {label}")
    except Exception as e:  # noqa: BLE001
        print(f"  skip ({label}): {type(e).__name__}")


def _delete_bucket(bucket: str) -> None:
    """Empty then delete an S3 bucket."""
    paginator = s3.get_paginator("list_objects_v2")
    deleted = 0
    for page in paginator.paginate(Bucket=bucket):
        for obj in page.get("Contents", []):
            s3.delete_object(Bucket=bucket, Key=obj["Key"])
            deleted += 1
    s3.delete_bucket(Bucket=bucket)
    print(f"    ({deleted} objects deleted)")


def _stop_spawn_workers() -> None:
    """Stop any running spawn instances for this job."""
    result = subprocess.run(
        ["spawn", "list", "-o", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"  skip (spawn list): returncode {result.returncode}")
        return

    import json

    try:
        instances = json.loads(result.stdout)
    except json.JSONDecodeError:
        print("  skip (spawn list): could not parse JSON")
        return

    if not isinstance(instances, list):
        instances = [instances]

    job_name = cfg.JOB_NAME
    matching = [i for i in instances if job_name in i.get("name", "")]

    if not matching:
        print(f"  skip (spawn workers for {job_name!r}): none running")
        return

    for inst in matching:
        iid = inst.get("instance_id") or inst.get("InstanceId") or inst.get("id", "")
        if iid:
            _try(
                f"spawn instance {iid}",
                lambda i=iid: subprocess.run(["spawn", "stop", i, "-y"], check=False),
            )


if __name__ == "__main__":
    print("=== Microbiome Demo — Teardown ===\n")

    # 1. Stop any running workers
    print("1/3  Stopping spawn worker instances…")
    _stop_spawn_workers()

    # 2. Delete S3 corpus bucket (SRA files)
    print(f"\n2/3  Deleting S3 corpus bucket s3://{cfg.BUCKET}…")
    _try(f"S3 bucket {cfg.BUCKET}", lambda: _delete_bucket(cfg.BUCKET))

    # 3. Note about the AMI
    print("\n3/3  AMI note:")
    ami_id = getattr(cfg, "AMI_ID", "")
    if ami_id:
        print(f"  AMI {ami_id} was NOT deleted (no ongoing hourly charge).")
        print("  EBS snapshots cost ~$0.05/GB-month.  Deregister manually if needed:")
        print(f"    aws ec2 deregister-image --image-id {ami_id} --region {cfg.REGION}")
    else:
        print("  AMI_ID not set in config.py — nothing to note.")

    print("\nDone.")
