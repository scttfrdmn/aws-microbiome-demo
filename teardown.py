#!/usr/bin/env python3
"""
teardown.py  --  delete everything the microbiome demo created.

Run this after the talk to clean up billable resources.

What this script deletes:
  - Any running spawn instances for this job (head + any stray task instances)
  - S3 results bucket (SRR slice lists, result JSONs, summary, trace)
  - S3 work directory prefix (Nextflow intermediate files)

Note: there is NO corpus data to delete.  HMP data lives on RODA and
was never copied to your account.

What this script does NOT delete:
  - The AMI (no ongoing hourly charge; EBS snapshots ~$0.05/GB-month)
    Deregister manually if desired — see output below.

Re-running safely:
  Idempotent — resources already gone just print "skip" and continue.
"""

from __future__ import annotations

import json
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


def _delete_prefix(bucket: str, prefix: str) -> int:
    """Delete all objects under prefix. Returns count deleted."""
    paginator = s3.get_paginator("list_objects_v2")
    deleted = 0
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        objects = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if objects:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": objects})
            deleted += len(objects)
    return deleted


def _delete_bucket(bucket: str) -> None:
    """Empty then delete an S3 bucket (handles both prefixes in one pass)."""
    n = _delete_prefix(bucket, "")
    s3.delete_bucket(Bucket=bucket)
    print(f"    ({n} objects deleted)")


def _stop_spawn_instances() -> None:
    """Stop all running spawn instances for this job."""
    result = subprocess.run(
        ["spawn", "list", "-o", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"  skip (spawn list): returncode {result.returncode}")
        return

    try:
        instances = json.loads(result.stdout)
    except json.JSONDecodeError:
        print("  skip (spawn list): could not parse JSON")
        return

    if not isinstance(instances, list):
        instances = [instances]

    job_name = cfg.JOB_NAME
    # Match head instance (job_name) and any nf-spawn task instances (nf-{hash})
    matching = [
        i for i in instances if job_name in i.get("name", "") or i.get("name", "").startswith("nf-")
    ]

    if not matching:
        print(f"  skip (spawn instances for {job_name!r}): none running")
        return

    for inst in matching:
        iid = inst.get("instance_id") or inst.get("InstanceId") or inst.get("id", "")
        name = inst.get("name", iid)
        if iid:
            _try(
                f"spawn instance {name} ({iid})",
                lambda i=iid: subprocess.run(["spawn", "stop", i, "-y"], check=False),
            )


if __name__ == "__main__":
    print("=== Microbiome Demo — Teardown ===\n")

    # 1. Stop running instances (head + any nf-spawn task instances)
    print("1/3  Stopping spawn instances…")
    _stop_spawn_instances()

    # 2. Delete S3 results + work directory
    print(f"\n2/3  Deleting S3 bucket s3://{cfg.BUCKET}…")
    _try(f"S3 bucket {cfg.BUCKET}", lambda: _delete_bucket(cfg.BUCKET))

    # 3. AMI note
    print("\n3/3  AMI:")
    ami_id = getattr(cfg, "AMI_ID", "")
    if ami_id:
        print(f"  {ami_id} was NOT deleted (no ongoing hourly charge).")
        print("  EBS snapshots: ~$0.05/GB-month.  To deregister:")
        print(f"    aws ec2 deregister-image --image-id {ami_id} --region {cfg.REGION}")
        print("  Then delete the associated snapshot:")
        print(f"    aws ec2 describe-images --image-ids {ami_id} --region {cfg.REGION}")
        print("    aws ec2 delete-snapshot --snapshot-id <snap-id> --region {cfg.REGION}")
    else:
        print("  AMI_ID not set in config.py — nothing to note.")

    print("\nDone.")
