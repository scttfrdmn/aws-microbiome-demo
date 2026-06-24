#!/usr/bin/env python3
"""
bake_x86_tools_ami.py  --  bake the x86_64 counterpart of the arm64 tools-AMI.

The benchmark needs a *fair* x86 leg: same tools-AMI recipe (nf-spawn 0.6.0,
Docker, Nextflow, NO baked DB — DBs ride the same EBS volumes), but x86_64 so
it runs native amd64 containers. The only x86 AMI in config.py is the legacy
nf-spawn-0.2.8 + baked-DB image, which can't run this pipeline and would mask
the EBS-volume DB. This driver reuses build_ami._BAKE_SCRIPT verbatim (it's
arch-aware via `uname -m`) but launches the bake on a c7i instance and names
the result for x86.

Standalone on purpose: build_ami.py's __main__ is hardcoded to arm64 and gates
on AMI_ID_ARM64. This does NOT touch that flow or config.py.

Output: prints `AMI_ID_X86 = "ami-..."` to paste into config.py.
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import time

if importlib.util.find_spec("config") is None:
    sys.exit("config.py not found")
import config as cfg  # type: ignore[import]
import build_ami  # reuse the arch-aware _BAKE_SCRIPT verbatim

BAKE_NAME = "microbiome-bake-x86"
BAKE_INSTANCE_TYPE = "c7i.xlarge"  # x86 counterpart of the arm64 c7g.xlarge bake
AMI_NAME = "nf-spawn-x86-tools-v0.6.0"

import tempfile

import boto3
from botocore.exceptions import ClientError

# Ensure the log bucket exists (bake script uploads its log there on exit).
s3 = boto3.client("s3", region_name=cfg.REGION)
try:
    s3.create_bucket(Bucket=cfg.BUCKET)
except ClientError as e:
    if e.response["Error"]["Code"] not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
        raise

with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
    f.write(build_ami._BAKE_SCRIPT)
    bake_script_path = f.name

print(f"=== x86 tools-AMI bake ===\n  instance: {BAKE_INSTANCE_TYPE}\n  region: {cfg.REGION}")
print("  spawn auto-detects latest AL2023 x86_64 AMI for an x86 instance type.\n")

subprocess.run(
    [
        "spawn", "launch", BAKE_NAME,
        "--instance-type", BAKE_INSTANCE_TYPE,
        "--region", cfg.REGION,
        "--volume-size", "20",
        "--user-data-file", bake_script_path,
        "--ttl", "5h",
        "--wait-for-ssh",
        "-y",
    ],
    check=False,
)

# Resolve instance ID.
instance_id = None
for _ in range(10):
    r = subprocess.run(["spawn", "list", "-o", "json"], capture_output=True, text=True, check=False)
    try:
        instances = json.loads(r.stdout)
        if not isinstance(instances, list):
            instances = [instances]
        for inst in instances:
            if inst.get("name") == BAKE_NAME:
                instance_id = inst.get("instance_id") or inst.get("InstanceId")
                break
    except (json.JSONDecodeError, KeyError):
        pass
    if instance_id:
        break
    time.sleep(5)

if not instance_id:
    sys.exit(f"Could not find {BAKE_NAME} instance via spawn list.")
print(f"\n  Instance launched: {instance_id}\n  Baking (polling every 60s)...")

for attempt in range(180):
    time.sleep(60)
    st = subprocess.run(
        ["spawn", "status", BAKE_NAME, "--check-complete"],
        capture_output=True, check=False,
    )
    if st.returncode == 0:
        print(f"  Bake complete after ~{attempt + 1} min")
        break
    if st.returncode == 1:
        sys.exit("  Bake FAILED — check s3://bucket/bake-logs/")
    if (attempt + 1) % 5 == 0:
        print(f"  Still running... ({attempt + 1} min)")
else:
    sys.exit("  Bake timed out after 180 min")

print(f"\n  Creating AMI '{AMI_NAME}'...")
r = subprocess.run(
    [
        "spawn", "ami", "create", BAKE_NAME,
        "--name", AMI_NAME,
        "--description", "Microbiome demo x86_64 tools AMI: Nextflow + nf-spawn 0.6.0 + Docker (DBs on EBS volumes, not baked)",
        "--wait", "-o", "json",
    ],
    capture_output=True, text=True, check=False,
)
try:
    data = json.loads(r.stdout)
    ami_id = data.get("image_id") or data.get("ImageId") or data.get("ami_id")
except (json.JSONDecodeError, AttributeError):
    m = re.search(r"ami-[0-9a-f]+", r.stdout + r.stderr)
    ami_id = m.group(0) if m else None

if not ami_id:
    sys.exit(f"Could not parse AMI ID:\n{r.stdout}\n{r.stderr}")

print(f"\n  x86 tools-AMI ready: {ami_id}")
subprocess.run(["spawn", "terminate", BAKE_NAME, "-y"], check=False)
print(f'\nPaste into config.py:\n  AMI_ID_X86 = "{ami_id}"')
