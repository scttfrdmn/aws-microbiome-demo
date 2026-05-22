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
  - nf-core/taxprofiler (pre-pulled as a Singularity image)
  - Kraken2 k2_pluspf_16_GB database  (11.9 GB, pre-staged on EBS)
  - MetaPhlAn 4 + its marker gene database
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

# --- System packages --------------------------------------------------------
dnf update -y
dnf install -y \\
    java-21-amazon-corretto \\
    singularity \\
    docker \\
    git \\
    wget \\
    pigz \\
    parallel \\
    htop

# --- SRA Toolkit  -----------------------------------------------------------
# Needed to convert .sra files to FASTQ inside the pipeline.
cd /tmp
wget -q https://ftp-trace.ncbi.nlm.nih.gov/sra/sdk/current/sratoolkit.current-centos_linux64.tar.gz
tar -xzf sratoolkit.current-centos_linux64.tar.gz
cp sratoolkit.*/bin/fasterq-dump /usr/local/bin/
cp sratoolkit.*/bin/prefetch      /usr/local/bin/
cp sratoolkit.*/bin/vdb-config    /usr/local/bin/

# --- Nextflow ---------------------------------------------------------------
mkdir -p /usr/local/bin
cd /usr/local/bin
wget -q https://get.nextflow.io -O nextflow
chmod +x nextflow
./nextflow self-update  # pull latest stable version

# --- nf-core/taxprofiler Singularity image ----------------------------------
# Pre-pull so demo instances don't download it at runtime.
mkdir -p /opt/singularity/images
cd /opt/singularity/images
singularity pull --force \\
    nf-core-taxprofiler-latest.sif \\
    docker://nfcore/taxprofiler:latest

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

# --- MetaPhlAn 4 + marker gene database ------------------------------------
pip3 install metaphlan
# Pre-download the MetaPhlAn marker gene database (~2 GB)
mkdir -p /opt/databases/metaphlan
metaphlan --install --bowtie2db /opt/databases/metaphlan \\
    --nproc 4 2>&1

# --- spawn CLI --------------------------------------------------------------
# The head node uses spawn to launch task instances via nf-spawn.
# Install via the spore-host homebrew tap (ARM64 binary).
curl -fsSL https://raw.githubusercontent.com/spore-host/homebrew-tap/main/install.sh \
    | bash 2>&1 || true
# Fallback: direct binary install if brew isn't available on AL2023
if ! command -v spawn &>/dev/null; then
    SPAWN_VER=$(curl -sf https://api.github.com/repos/spore-host/spore-host/releases/latest \
        | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'])" 2>/dev/null \
        || echo "v0.34.14")
    curl -fsSL \
        "https://github.com/spore-host/spore-host/releases/download/${SPAWN_VER}/spawn_linux_arm64.tar.gz" \
        | tar -xz -C /usr/local/bin spawn
fi
spawn --version

# --- nf-spawn plugin (Nextflow executor for spawn) --------------------------
# Builds the JAR from source and installs it into the shared Nextflow plugin
# directory so all users on this AMI have the executor available.
# Requires Java 21 (installed above) and Gradle (bundled in the repo as gradlew).
mkdir -p /opt/nf-spawn
cd /opt/nf-spawn
git clone --depth 1 https://github.com/spore-host/nf-spawn.git .
# Build the plugin JAR
./gradlew jar 2>&1
# Nextflow looks for plugins in NXF_HOME/plugins/{name}-{version}/
# We install into the shared NXF_HOME so all runs on this AMI pick it up.
NF_PLUGIN_DIR=/opt/nextflow_cache/plugins
mkdir -p "${NF_PLUGIN_DIR}"
# The JAR name and plugin ID come from build.gradle; copy with a stable name
# so nextflow.config can reference it as 'nf-spawn' without a version pin.
SPAWN_JAR=$(ls build/libs/nf-spawn-*.jar 2>/dev/null | head -1)
if [ -z "${SPAWN_JAR}" ]; then
    echo "ERROR: nf-spawn JAR not found after build"
    exit 1
fi
PLUGIN_VERSION=$(basename "${SPAWN_JAR}" | sed 's/nf-spawn-//;s/\.jar//')
PLUGIN_DEST="${NF_PLUGIN_DIR}/nf-spawn-${PLUGIN_VERSION}"
mkdir -p "${PLUGIN_DEST}/classes"
cp "${SPAWN_JAR}" "${PLUGIN_DEST}/nf-spawn-${PLUGIN_VERSION}.jar"
echo "nf-spawn plugin installed: ${PLUGIN_DEST}"

# --- Nextflow pipeline cache ------------------------------------------------
# Pre-download the nf-core/taxprofiler pipeline so demo runs don't need
# GitHub access or internet during the live demo.
mkdir -p /opt/nextflow_cache
NXF_HOME=/opt/nextflow_cache \\
    /usr/local/bin/nextflow pull nf-core/taxprofiler

# --- Permissions ------------------------------------------------------------
# Make everything readable by all users (pipeline runs as ec2-user)
chmod -R 755 /opt/databases
chmod -R 755 /opt/nextflow_cache
chmod -R 755 /opt/singularity
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

    print("Launching bake instance via spawn...")
    print(f"  Instance type: {cfg.INSTANCE_TYPE}")
    print(f"  Region: {cfg.REGION}")

    # Write the bake script to a temp file
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
        f.write(_BAKE_SCRIPT)
        bake_script_path = f.name

    # Launch via spawn: ARM64 AL2023, with enough EBS for the databases
    # (~50 GB: 16 GB Kraken2 + 2 GB MetaPhlAn + OS + images)
    result = subprocess.run(
        [
            "spawn",
            "launch",
            "microbiome-bake",
            "--instance-type",
            cfg.INSTANCE_TYPE,
            "--region",
            cfg.REGION,
            "--ami",
            "auto",  # latest AL2023 ARM64
            "--volume-size",
            "80",  # GB EBS
            "--user-data-file",
            bake_script_path,
            "--ttl",
            "2h",  # safety net
            "--active-processes",
            "nextflow,metaphlan,singularity",
            "--wait-for-ssh",
            "-o",
            "json",
            "-y",  # skip cost confirmation
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        print(f"spawn launch failed: {result.stderr[:500]}")
        sys.exit(1)

    launch_data = json.loads(result.stdout)
    instance_id = launch_data.get("instance_id") or launch_data.get("InstanceId")
    print(f"\n  Instance launched: {instance_id}")
    print("  Installing Nextflow, databases, Singularity images...")
    print("  This takes ~30-45 minutes.  Grab a coffee.\n")

    # Poll until the bake script signals completion via /tmp/SPAWN_COMPLETE
    print("  Waiting for bake to complete (polling every 60s)...")
    ec2 = boto3.client("ec2", region_name=cfg.REGION)

    for attempt in range(60):  # up to 60 minutes
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
        print("  Bake timed out after 60 minutes — check instance logs")
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
    print("\nNext step: python corpus_prep.py")
