#!/usr/bin/env python3
"""
build_ami.py  --  bake an Amazon Machine Image (AMI) with everything the
                  demo pipeline needs pre-installed.

Run this ONCE before the talk.  It takes ~30-45 minutes.  The resulting AMI
eliminates all software download/install time on demo day -- the instances
boot ready to run Nextflow immediately.

What the AMI contains:
  - Amazon Linux 2023 (ARM64 / Graviton3)
  - Nextflow 24.x  (the workflow engine)
  - nf-core/taxprofiler (pulled at pipeline runtime via Docker)
  - Kraken2 k2_pluspf_16_GB database  (11.9 GB, pre-staged on EBS)
  - MetaPhlAn 4 (runs in nf-core/taxprofiler Docker container, no host install)
  - SRA Toolkit  (converts .sra → FASTQ on-the-fly)
  - spored agent  (Spawn's termination daemon, auto-installed by spawn launch)

Why a 16 GB Kraken2 database instead of the full standard (75 GB)?
  k2_pluspf includes bacteria, archaea, viruses, fungi, AND protozoa --
  everything relevant for human microbiome profiling.  The full standard
  database adds plant genomes and other irrelevant taxa.  16 GB fits in
  the 32 GB RAM of a c7g.4xlarge, allowing in-memory classification
  which is the bottleneck for Kraken2 throughput.

Re-running safely:
  If AMI_ID in config.py is already set, this script prints the existing
  AMI details and exits without creating a duplicate.

Requires:
  - spawn CLI installed (brew install spore-host/tap/spawn)
  - AWS credentials with EC2 + IAM permissions
  - ~$1-2 in EC2 costs for the bake instance (auto-terminates when done)
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import time

import boto3

# The bake script that runs INSIDE the instance to install everything.
# Passed to spawn launch via --user-data.
_BAKE_SCRIPT = """#!/bin/bash
set -euxo pipefail
# Log everything so we can see what happened if the AMI bake fails.
exec > /var/log/ami-bake.log 2>&1

echo "=== Microbiome Demo AMI Bake ==="
echo "Started: $(date)"

# Upload log to S3 on EXIT (success or failure) so we can diagnose failures
# even after the instance terminates.  Bucket must exist before bake starts.
BAKE_BUCKET="scttfrdmn-microbiome-demo"
BAKE_REGION="us-east-1"
upload_log() {
    aws s3 cp /var/log/ami-bake.log \\
        "s3://${BAKE_BUCKET}/bake-logs/ami-bake-$(date +%Y%m%d-%H%M%S).log" \\
        --region "${BAKE_REGION}" 2>/dev/null || true
}
trap upload_log EXIT

# --- System packages --------------------------------------------------------
dnf update -y
dnf install -y \\
    java-21-amazon-corretto \\
    docker \\
    git \\
    wget \\
    pigz \\
    parallel \\
    htop \\
    squashfs-tools \\
    fuse \\
    fuse-libs

# Start Docker (required for nf-core containers)
systemctl enable --now docker
usermod -aG docker ec2-user

# Note: Singularity/Apptainer has no pre-built ARM64/aarch64 RPM.
# We use Docker instead — nf-core/taxprofiler fully supports Docker,
# and Docker is the standard container runtime on AL2023.
docker --version

# Note: SRA Toolkit is NOT installed separately.
# nf-core/taxprofiler runs fasterq-dump inside its Docker container,
# so it's bundled in the nfcore/taxprofiler:latest image pulled below.

# --- Nextflow ---------------------------------------------------------------
mkdir -p /usr/local/bin
cd /usr/local/bin
wget -q --timeout=120 https://get.nextflow.io -O nextflow
chmod +x nextflow
./nextflow self-update  # pull latest stable version

# --- Python packages --------------------------------------------------------
# boto3 is needed by the head node script for S3 progress reporting.
dnf install -y python3-pip
python3 -m pip install --quiet boto3

# Note: nf-core/taxprofiler Docker image is NOT pre-pulled here.
# Nextflow pulls it automatically on first use from Docker Hub.
# Pre-pulling would require `docker login` which we avoid on bake instances.
echo "Docker ready — nfcore/taxprofiler will be pulled at pipeline runtime"

# --- Kraken2 k2_pluspf_16_GB database ---------------------------------------
# 11.9 GB compressed → ~16 GB uncompressed.  Stored in /opt/databases so
# the Nextflow pipeline can reference it without S3 download.
mkdir -p /opt/databases/kraken2
cd /opt/databases/kraken2
aws s3 cp \\
    s3://genome-idx/kraken/k2_pluspf_16_GB_20260226.tar.gz \\
    . \\
    --no-sign-request \\
    --no-progress
echo "Extracting Kraken2 database..."
tar -xzf k2_pluspf_16_GB_20260226.tar.gz
rm k2_pluspf_16_GB_20260226.tar.gz

# Note: MetaPhlAn 4 runs inside the nf-core/taxprofiler Docker container.
# No host install needed — Docker pulls the container at pipeline runtime.
# The MetaPhlAn marker gene database (~2 GB) is downloaded by nf-core on
# first use and cached in the Nextflow work directory.

# --- spawn CLI --------------------------------------------------------------
# Install from spore-host/spawn GitHub releases (ARM64 RPM for AL2023).
# Releases live at github.com/spore-host/spawn, not spore-host/spore-host.
SPAWN_VER=$(curl -sf https://api.github.com/repos/spore-host/spawn/releases/latest \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'].lstrip('v'))" 2>/dev/null \
    || echo "0.36.6")
# Arch-aware spawn rpm: an arm64 AMI needs the aarch64 binary (spawn is a Go
# binary, not arch-portable).  x86 bakes still get amd64.
case "$(uname -m)" in
    aarch64) SPAWN_RPM_ARCH="arm64" ;;
    x86_64)  SPAWN_RPM_ARCH="amd64" ;;
    *)       echo "Unknown arch $(uname -m)"; exit 1 ;;
esac
curl -fsSL --output /tmp/spawn.rpm \\
    "https://github.com/spore-host/spawn/releases/download/v${SPAWN_VER}/spawn_${SPAWN_VER}_linux_${SPAWN_RPM_ARCH}.rpm"
dnf install -y /tmp/spawn.rpm
spawn version

# --- nf-spawn plugin (Nextflow executor for spawn) --------------------------
# Download the pre-built release ZIP — no Gradle build needed.
NF_SPAWN_VERSION="0.3.0"
NF_PLUGIN_DIR=/opt/nextflow_cache/plugins
dnf install -y unzip
PLUGIN_DEST="${NF_PLUGIN_DIR}/nf-spawn-${NF_SPAWN_VERSION}"
mkdir -p "${PLUGIN_DEST}"
curl -fsSL "https://github.com/spore-host/nf-spawn/releases/download/v${NF_SPAWN_VERSION}/nf-spawn-${NF_SPAWN_VERSION}.zip" \
    -o /tmp/nf-spawn.zip
unzip -q /tmp/nf-spawn.zip -d "${PLUGIN_DEST}"
rm /tmp/nf-spawn.zip
echo "nf-spawn installed (exploded into classes/): ${PLUGIN_DEST}"
find "${PLUGIN_DEST}" -type f

# --- nf-amazon plugin (required for s3:// workDir) -------------------------
# Nextflow downloads plugins on first use; pre-cache it now so demo runs
# don't need internet access or suffer a cold-start delay.
# nf-amazon provides the S3 FileSystem implementation that lets Nextflow
# use s3://bucket/work/ as the work directory between task instances.
mkdir -p /opt/nextflow_cache
NXF_HOME=/opt/nextflow_cache \\
    /usr/local/bin/nextflow plugin install nf-amazon@2.8.0

# --- Nextflow pipeline cache ------------------------------------------------
# Pre-download the nf-core/taxprofiler pipeline so demo runs don't need
# GitHub access or internet during the live demo.
NXF_HOME=/opt/nextflow_cache \\
    /usr/local/bin/nextflow pull nf-core/taxprofiler

# --- Permissions ------------------------------------------------------------
# Make everything readable by all users (pipeline runs as ec2-user)
chmod -R 755 /opt/databases
chmod -R 755 /opt/nextflow_cache

# --- Completion signal ------------------------------------------------------
echo "=== AMI bake complete: $(date) ==="
touch /tmp/SPAWN_COMPLETE
"""


def _spawn_json(args: list[str]) -> dict:
    """Run a spawn command with -o json and return the parsed output."""
    import json

    result = subprocess.run(
        ["spawn"] + args + ["-o", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"spawn error: {result.stderr[:500]}", file=sys.stderr)
        sys.exit(1)
    return json.loads(result.stdout)


def bake_ami(cfg) -> str:
    """Launch a bake instance, install everything, create the AMI.

    Returns the new AMI ID (also printed for pasting into config.py).
    """
    import json

    from botocore.exceptions import ClientError

    # Ensure the S3 bucket exists — the bake script uploads its log there on exit.
    s3 = boto3.client("s3", region_name=cfg.REGION)
    try:
        s3.create_bucket(Bucket=cfg.BUCKET)
        print(f"  Created bucket: s3://{cfg.BUCKET}")
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            raise
        print(f"  Bucket exists: s3://{cfg.BUCKET}")

    # arm64 bake instance so the AMI (and its baked spawn CLI) target Graviton.
    # The Kraken2 DB is arch-neutral data; aarchbio supplies native arm64
    # containers for every taxprofiler step, so the whole pipeline runs native.
    # spawn auto-detects the latest AL2023 arm64 AMI for an arm64 instance type.
    bake_instance_type = "c7g.4xlarge"

    print("Launching bake instance via spawn...")
    print(f"  Instance type: {bake_instance_type}")
    print(f"  Region: {cfg.REGION}")

    # Write the bake script to a temp file
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
        f.write(_BAKE_SCRIPT)
        bake_script_path = f.name

    # Launch via spawn: ARM64 AL2023, 40 GB EBS (requires spawn >= 0.36.3).
    # 40 GB is sufficient: 20 GB baked content + 8 GB Docker cache + temp space.
    # Notes:
    #   - --ami omitted: spawn auto-detects latest AL2023 for the region/arch
    #   - -o json omitted: spawn's TUI overrides it and outputs ANSI progress,
    #     not JSON; we use `spawn list -o json` after launch to find the instance
    subprocess.run(
        [
            "spawn",
            "launch",
            "microbiome-bake",
            "--instance-type",
            bake_instance_type,
            "--region",
            cfg.REGION,
            "--volume-size",
            "40",
            "--user-data-file",
            bake_script_path,
            "--ttl",
            "5h",  # bake takes 2-3h with Docker pull + 11.9GB Kraken2 DB
            "--wait-for-ssh",
            "-y",
        ],
        check=False,
    )

    # Look up the instance by name via `spawn list -o json`.
    print("  Looking up instance ID via spawn list...")
    instance_id = None
    for _ in range(10):
        list_result = subprocess.run(
            ["spawn", "list", "-o", "json"],
            capture_output=True, text=True, check=False,
        )
        try:
            instances = json.loads(list_result.stdout)
            if not isinstance(instances, list):
                instances = [instances]
            for inst in instances:
                if inst.get("name") == "microbiome-bake":
                    instance_id = inst.get("instance_id") or inst.get("InstanceId")
                    break
        except (json.JSONDecodeError, KeyError):
            pass
        if instance_id:
            break
        time.sleep(5)

    if not instance_id:
        print("  Could not find microbiome-bake instance via spawn list.")
        sys.exit(1)
    print(f"\n  Instance launched: {instance_id}")
    print("  Installing Nextflow, nf-spawn, Kraken2 database...")
    print("  This takes ~2 hours.  Grab a coffee.\n")

    # Wait for the bake script to signal completion via SPAWN_COMPLETE,
    # then snapshot it into an AMI using `spawn ami create`.
    print("  Waiting for bake to complete (polling every 60s)...")
    for attempt in range(180):
        time.sleep(60)
        status_result = subprocess.run(
            ["spawn", "status", "microbiome-bake", "--check-complete"],
            capture_output=True, check=False,
        )
        if status_result.returncode == 0:
            print(f"  Bake complete after ~{attempt + 1} minutes")
            break
        if status_result.returncode == 1:
            print("  Bake FAILED — check s3://bucket/bake-logs/ for details")
            sys.exit(1)
        if (attempt + 1) % 5 == 0:
            print(f"  Still running... ({attempt + 1} min elapsed)")
    else:
        print("  Bake timed out after 180 minutes")
        sys.exit(1)

    ami_name = "nf-spawn-arm64-v0.2.8-spawn-latest-kraken2db"
    print(f"\n  Creating AMI '{ami_name}' via spawn ami create...")
    result = subprocess.run(
        [
            "spawn", "ami", "create", "microbiome-bake",
            "--name", ami_name,
            "--description", "Microbiome demo ARM64/Graviton: Nextflow + nf-spawn + Kraken2 k2_pluspf_16GB",
            "--wait",
            "-o", "json",
        ],
        capture_output=True, text=True, check=False,
    )
    try:
        data = json.loads(result.stdout)
        ami_id = data.get("image_id") or data.get("ImageId") or data.get("ami_id")
    except (json.JSONDecodeError, AttributeError):
        # Fall back to parsing stdout for the AMI ID
        import re
        m = re.search(r"ami-[0-9a-f]+", result.stdout + result.stderr)
        ami_id = m.group(0) if m else None

    if not ami_id:
        print(f"  Could not parse AMI ID from spawn output:\n{result.stdout}\n{result.stderr}")
        sys.exit(1)

    print(f"  AMI ready: {ami_id}")
    subprocess.run(["spawn", "terminate", "microbiome-bake", "-y"], check=False)
    return ami_id


if __name__ == "__main__":
    if importlib.util.find_spec("config") is None:
        sys.exit("config.py not found — copy config.example.py and fill it in.")

    import config as cfg  # type: ignore[import]

    # The bake now produces a native ARM64/Graviton AMI carrying the Kraken2 DB,
    # so the whole taxprofiler pipeline runs native (aarchbio containers).
    # If AMI_ID_ARM64 is already set, just report it.
    if getattr(cfg, "AMI_ID_ARM64", ""):
        print(f"ARM64 AMI already configured: {cfg.AMI_ID_ARM64}")
        print("To rebuild: set AMI_ID_ARM64 = '' in config.py and re-run.")
        sys.exit(0)

    print("=== Microbiome Demo — ARM64 AMI Build ===\n")
    ami_id = bake_ami(cfg)

    print("\nDone.  Paste into config.py:")
    print(f'  AMI_ID_ARM64 = "{ami_id}"')
    print("\nNext step: make demo")
