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
curl -fsSL --output /tmp/spawn.rpm \
    "https://github.com/spore-host/spawn/releases/download/v${SPAWN_VER}/spawn_${SPAWN_VER}_linux_arm64.rpm"
dnf install -y /tmp/spawn.rpm
spawn version

# --- nf-spawn plugin (Nextflow executor for spawn) --------------------------
# Clones the pinned v0.1.0 tag, builds the JAR, and installs it into the
# shared NXF_HOME/plugins/ directory so all Nextflow runs on this AMI find it.
# nextflow.config references it as: plugins { id 'nf-spawn@0.1.0' }
NF_SPAWN_VERSION="0.1.0"
NF_PLUGIN_DIR=/opt/nextflow_cache/plugins
mkdir -p /opt/nf-spawn "${NF_PLUGIN_DIR}"
cd /opt/nf-spawn
# Try the pinned tag; fall back to main if the tag hasn't been pushed yet.
git clone --depth 1 --branch "v${NF_SPAWN_VERSION}" \
    https://github.com/spore-host/nf-spawn.git . 2>/dev/null \
    || git clone --depth 1 https://github.com/spore-host/nf-spawn.git .
./gradlew jar 2>&1
# pf4j plugin discovery requires directory name == {id}-{version}.
# If building from main the JAR may have a different version suffix; normalise it.
BUILT_JAR=$(ls build/libs/nf-spawn-*.jar 2>/dev/null | head -1)
if [ -z "${BUILT_JAR}" ]; then
    echo "ERROR: nf-spawn JAR not found after build — check Gradle output above"
    exit 1
fi
PLUGIN_DEST="${NF_PLUGIN_DIR}/nf-spawn-${NF_SPAWN_VERSION}"
mkdir -p "${PLUGIN_DEST}"
cp "${BUILT_JAR}" "${PLUGIN_DEST}/nf-spawn-${NF_SPAWN_VERSION}.jar"
echo "nf-spawn installed: ${PLUGIN_DEST} (from $(basename ${BUILT_JAR}))"

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
chmod -R 755 /opt/nf-spawn

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

    # Use c7g.4xlarge for the bake — 16 vCPU speeds up gradle, pip installs,
    # and the Kraken2 database download.  Not configurable: the bake instance
    # type is independent of the demo's head/task instance types.
    bake_instance_type = "c7g.4xlarge"

    print("Launching bake instance via spawn...")
    print(f"  Instance type: {bake_instance_type}")
    print(f"  Region: {cfg.REGION}")

    # Write the bake script to a temp file
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
        f.write(_BAKE_SCRIPT)
        bake_script_path = f.name

    # Launch via spawn: ARM64 AL2023, 80 GB EBS (requires spawn >= 0.36.3).
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
            "80",
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
    print("  Installing Nextflow, databases, Singularity images...")
    print("  This takes ~30-45 minutes.  Grab a coffee.\n")

    # Poll until the bake script signals completion via /tmp/SPAWN_COMPLETE
    print("  Waiting for bake to complete (polling every 60s)...")
    ec2 = boto3.client("ec2", region_name=cfg.REGION)

    for attempt in range(180):  # up to 180 minutes
        time.sleep(60)
        status_result = subprocess.run(
            ["spawn", "status", instance_id, "--check-complete"],
            capture_output=True,
            check=False,
        )
        # exit 0 = complete, 2 = still running, 1 = failed, 3 = error
        if status_result.returncode == 0:
            print(f"  Bake complete after ~{attempt + 1} minutes")
            break
        if status_result.returncode == 1:
            print("  Bake FAILED — check /var/log/ami-bake.log on the instance")
            sys.exit(1)
        if (attempt + 1) % 5 == 0:
            print(f"  Still running... ({attempt + 1} min elapsed)")
    else:
        print("  Bake timed out after 180 minutes — check instance logs")
        sys.exit(1)

    # Create the AMI from the stopped instance
    print("\n  Creating AMI from bake instance...")
    ami_name = f"microbiome-demo-{int(time.time())}"
    resp = ec2.create_image(
        InstanceId=instance_id,
        Name=ami_name,
        Description="Microbiome demo AMI: Nextflow + Kraken2 + MetaPhlAn + databases",
        NoReboot=False,
    )
    ami_id = resp["ImageId"]
    print(f"  AMI creation started: {ami_id}  (name: {ami_name})")

    # Wait for the AMI to become available
    print("  Waiting for AMI to be ready...")
    waiter = ec2.get_waiter("image_available")
    waiter.wait(ImageIds=[ami_id], WaiterConfig={"Delay": 15, "MaxAttempts": 40})
    print(f"  AMI ready: {ami_id}")

    # Terminate the bake instance
    print("  Terminating bake instance...")
    subprocess.run(["spawn", "stop", instance_id, "-y"], check=False)

    return ami_id


if __name__ == "__main__":
    if importlib.util.find_spec("config") is None:
        sys.exit("config.py not found — copy config.example.py and fill it in.")

    import config as cfg  # type: ignore[import]

    # If AMI is already set in config, just report it
    if getattr(cfg, "AMI_ID", ""):
        print(f"AMI already configured: {cfg.AMI_ID}")
        print("To rebuild: set AMI_ID = '' in config.py and re-run.")
        sys.exit(0)

    print("=== Microbiome Demo — AMI Build ===\n")
    ami_id = bake_ami(cfg)

    print("\nDone.  Paste into config.py:")
    print(f'  AMI_ID = "{ami_id}"')
    print("\nNext step: make demo")
