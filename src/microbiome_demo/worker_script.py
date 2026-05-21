"""
worker_script.py  --  generate the cloud-init bash script for each spawn worker.

Each worker instance:
  1. Downloads its assigned SRR accession list from S3 (a tiny JSON file).
  2. For each accession, pulls the SRA file DIRECTLY from RODA
     (s3://sra-pub-run-odp/) using --no-sign-request — no egress cost,
     no data copied to your bucket, S3-internal speeds within us-east-1.
  3. Converts SRA → FASTQ with fasterq-dump (pre-installed on the AMI).
  4. Runs nf-core/taxprofiler (Kraken2 + MetaPhlAn) against the local FASTQs.
  5. Parses Kraken2 output and writes per-sample JSON results to your S3 bucket.
  6. Touches /tmp/SPAWN_COMPLETE to signal spawn that it is done.

The S3 bucket is used ONLY for:
  - Input:  per-worker SRR lists     (written by app.py before launch, ~1 KB each)
  - Output: per-sample result JSON   (written by each worker, ~10 KB each)
  - Output: summary.json             (written by the last worker to finish)

The HMP data itself NEVER enters your bucket.
"""

from __future__ import annotations

import tempfile

# Token substitution style (not str.format) avoids conflicts with bash ${VAR}
# and Python f-string syntax inside the embedded script sections.
_TOK_BUCKET = "@@BUCKET@@"
_TOK_REGION = "@@REGION@@"
_TOK_JOB_NAME = "@@JOB_NAME@@"
_TOK_SLICE_KEY = "@@SLICE_KEY@@"

_WORKER_TEMPLATE = r"""#!/bin/bash
set -euxo pipefail
exec > /var/log/nextflow-worker.log 2>&1

echo "=== Microbiome Demo Worker ==="
echo "Started: $(date)"
echo "Instance: $(curl -sf http://169.254.169.254/latest/meta-data/instance-id || echo unknown)"

BUCKET="@@BUCKET@@"
REGION="@@REGION@@"
JOB_NAME="@@JOB_NAME@@"
SLICE_KEY="@@SLICE_KEY@@"

# Verify databases are present (should be pre-staged on the AMI).
if [ ! -f /opt/databases/kraken2/hash.k2d ]; then
    echo "ERROR: Kraken2 database not found — was the AMI baked correctly?"
    exit 1
fi

# Download this instance's SRR accession list from S3.
# This is a tiny JSON file (~1 KB) — the only thing we read from our bucket
# before the analysis starts.
aws s3 cp "s3://${BUCKET}/${SLICE_KEY}" /tmp/srr_list.json --region "${REGION}"
python3 -c 'import json; n=len(json.load(open("/tmp/srr_list.json"))); print(f"{n} accessions")'

mkdir -p /tmp/sra /tmp/fastq /tmp/nextflow_output

# For each SRR: pull directly from RODA, convert to FASTQ, run analysis.
# RODA (sra-pub-run-odp) is in us-east-1 — same region as these instances.
# --no-sign-request: the bucket is public, no AWS credentials needed to read it.
python3 - << 'PYEOF'
import json, os, subprocess, sys

with open("/tmp/srr_list.json") as f:
    samples = json.load(f)   # list of {"srr": "SRRxxxxxx", "body_site": "stool"}

samplesheet_rows = []

for item in samples:
    srr = item["srr"]
    body_site = item["body_site"]
    sra_path = f"/tmp/sra/{srr}.sra"
    fastq_dir = f"/tmp/fastq/{srr}"
    os.makedirs(fastq_dir, exist_ok=True)

    # Pull directly from RODA — no copy to our bucket, no egress charge.
    print(f"Fetching {srr} from RODA...", flush=True)
    subprocess.run([
        "aws", "s3", "cp",
        f"s3://sra-pub-run-odp/sra/{srr}/{srr}.sra",
        sra_path,
        "--no-sign-request",
        "--region", "us-east-1",
        "--no-progress",
    ], check=True)

    # Convert SRA → FASTQ (in-place, no extra download needed).
    print(f"Converting {srr} to FASTQ...", flush=True)
    result = subprocess.run([
        "fasterq-dump", sra_path,
        "--outdir", fastq_dir,
        "--threads", "8",
        "--skip-technical",
        "--split-3",        # paired-end if available, single-end otherwise
    ], capture_output=True, text=True)

    if result.returncode != 0:
        print(f"  fasterq-dump failed for {srr}: {result.stderr[:200]}", flush=True)
        continue

    # Find the output FASTQ file(s)
    fastq_files = sorted([
        f for f in os.listdir(fastq_dir) if f.endswith(".fastq")
    ])
    if not fastq_files:
        print(f"  No FASTQ output for {srr} — skipping", flush=True)
        continue

    fq1 = os.path.join(fastq_dir, fastq_files[0])
    fq2 = os.path.join(fastq_dir, fastq_files[1]) if len(fastq_files) > 1 else ""

    samplesheet_rows.append({
        "sample": f"{srr}_{body_site}",
        "run_accession": srr,
        "body_site": body_site,
        "instrument_platform": "ILLUMINA",
        "fastq_1": fq1,
        "fastq_2": fq2,
        "fasta": "",
    })

    # Remove the SRA file immediately to free disk space.
    os.remove(sra_path)
    print(f"  {srr} ready ({len(fastq_files)} FASTQ file(s))", flush=True)

# Write the samplesheet for Nextflow.
import csv
with open("/tmp/samplesheet.csv", "w", newline="") as f:
    writer = csv.DictWriter(
        f,
        fieldnames=["sample", "run_accession", "instrument_platform",
                    "fastq_1", "fastq_2", "fasta"],
    )
    writer.writeheader()
    for row in samplesheet_rows:
        writer.writerow({k: row[k] for k in ["sample", "run_accession",
                                              "instrument_platform",
                                              "fastq_1", "fastq_2", "fasta"]})

# Save body_site mapping for the result parser.
body_site_map = {r["run_accession"]: r["body_site"] for r in samplesheet_rows}
with open("/tmp/body_site_map.json", "w") as f:
    json.dump(body_site_map, f)

print(f"Samplesheet written: {len(samplesheet_rows)} samples", flush=True)
PYEOF

echo "FASTQ preparation complete."

# Write a Nextflow config pointing at the pre-staged databases.
cat > /tmp/nextflow.config << 'NFCONFIG'
process {
    executor = 'local'
    maxForks = 16
}
params {
    kraken2_db = '/opt/databases/kraken2'
    metaphlan_db = '/opt/databases/metaphlan'
    run_kraken2 = true
    run_metaphlan = true
    run_bracken = false
    run_humann = false
    perform_shortread_hostremoval = false
    shortread_qc_tool = 'fastp'
    save_preprocessed_reads = false
}
NFCONFIG

# Run the pipeline against the local FASTQs.
OUTDIR="/tmp/nextflow_output"
NXF_HOME=/opt/nextflow_cache \
    /usr/local/bin/nextflow run nf-core/taxprofiler \
    --input /tmp/samplesheet.csv \
    --outdir "${OUTDIR}" \
    -c /tmp/nextflow.config \
    -profile singularity \
    -resume \
    2>&1 | tee /tmp/nextflow.stdout

echo "Nextflow run complete."

# Parse Kraken2 outputs and upload per-sample JSON to S3 (results only — no raw data).
cat > /tmp/parse_results.py << 'PYEOF'
import glob, json, re, boto3

s3 = boto3.client("s3", region_name="@@REGION@@")
bucket = "@@BUCKET@@"
job_name = "@@JOB_NAME@@"
outdir = "/tmp/nextflow_output"

with open("/tmp/body_site_map.json") as f:
    body_site_map = json.load(f)


def parse_kraken2_report(path, srr, body_site):
    species = []
    with open(path) as f:
        for line in f:
            fields = line.strip().split("\t")
            if len(fields) < 6:
                continue
            pct, reads, rank, name = fields[0], fields[1], fields[3], fields[5]
            if rank == "S" and float(pct) > 0.1:
                species.append({"name": name.strip(), "pct": float(pct), "reads": int(reads)})
    species.sort(key=lambda x: x["pct"], reverse=True)
    return {
        "srr": srr,
        "body_site": body_site,
        "top_species": [s["name"] for s in species[:10]],
        "species_detail": species[:20],
    }


reports = glob.glob(f"{outdir}/**/kraken2/*.report.txt", recursive=True)
if not reports:
    reports = glob.glob(f"{outdir}/**/taxonomy/*.report.txt", recursive=True)

uploaded = 0
for report_path in reports:
    m = re.search(r"(SRR\d+)", report_path)
    if not m:
        continue
    srr = m.group(1)
    body_site = body_site_map.get(srr, "unknown")
    data = parse_kraken2_report(report_path, srr, body_site)
    key = f"results/{job_name}/kraken2/{srr}.json"
    s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(data))
    uploaded += 1

print(f"Uploaded {uploaded} result files to s3://{bucket}/results/{job_name}/kraken2/")
PYEOF

python3 /tmp/parse_results.py

echo "Results uploaded."

# Signal spawn that this instance is done.
touch /tmp/SPAWN_COMPLETE
echo "=== Worker complete: $(date) ==="
"""

_SLICE_TEMPLATE = """[
@@ENTRIES@@
]
"""


def render(cfg, slice_key: str) -> str:
    """Return the worker bash script with config values substituted."""

    def _sub(s: str) -> str:
        return (
            s.replace(_TOK_BUCKET, cfg.BUCKET)
            .replace(_TOK_REGION, cfg.REGION)
            .replace(_TOK_JOB_NAME, cfg.JOB_NAME)
            .replace(_TOK_SLICE_KEY, slice_key)
        )

    return _sub(_WORKER_TEMPLATE)


def write_temp(script: str) -> str:
    """Write the script to a NamedTemporaryFile and return its path."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
        f.write(script)
        return f.name


def write_srr_slices(cfg, accessions: list[tuple[str, str]]) -> list[str]:
    """Split accessions into per-instance slices and upload as JSON to S3.

    Each slice file is a tiny JSON list of {"srr": ..., "body_site": ...} dicts.
    The actual HMP data stays on RODA — only accession numbers are written here.

    Args:
        cfg:        config module (BUCKET, REGION, JOB_NAME, INSTANCE_COUNT).
        accessions: list of (srr, body_site) tuples.

    Returns:
        List of S3 keys for the slice files.
    """
    import json

    import boto3

    s3 = boto3.client("s3", region_name=cfg.REGION)
    n = cfg.INSTANCE_COUNT
    slice_size = max(1, (len(accessions) + n - 1) // n)

    keys: list[str] = []
    for i in range(n):
        chunk = accessions[i * slice_size : (i + 1) * slice_size]
        if not chunk:
            break

        entries = [{"srr": srr, "body_site": bs} for srr, bs in chunk]
        key = f"slices/{cfg.JOB_NAME}/srr_list_{i:03d}.json"
        s3.put_object(
            Bucket=cfg.BUCKET,
            Key=key,
            Body=json.dumps(entries, indent=2).encode(),
        )
        keys.append(key)

    return keys
