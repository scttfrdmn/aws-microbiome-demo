"""
worker_script.py  --  generate the cloud-init bash script for each spawn worker.

Each worker instance:
  1. Downloads its assigned samplesheet slice from S3.
  2. Runs nf-core/taxprofiler via Nextflow for that slice.
  3. Parses Kraken2 output files and writes per-sample JSON results to S3.
  4. Touches /tmp/SPAWN_COMPLETE to signal spawn that it is done.

The script uses token replacement (not str.format) so the embedded Python
code can contain its own { } without escaping conflicts.
"""

from __future__ import annotations

import tempfile

# Tokens in the template that render() will substitute.
# Using ALLCAPS_PLACEHOLDER style avoids any confusion with bash ${VAR} or
# Python {f-string} syntax inside the heredoc sections.
_TOK_BUCKET = "@@BUCKET@@"
_TOK_REGION = "@@REGION@@"
_TOK_JOB_NAME = "@@JOB_NAME@@"
_TOK_SAMPLESHEET = "@@SAMPLESHEET_KEY@@"

_WORKER_TEMPLATE = r"""#!/bin/bash
set -euxo pipefail
exec > /var/log/nextflow-worker.log 2>&1

echo "=== Microbiome Demo Worker ==="
echo "Started: $(date)"

BUCKET="@@BUCKET@@"
REGION="@@REGION@@"
JOB_NAME="@@JOB_NAME@@"
SAMPLESHEET_KEY="@@SAMPLESHEET_KEY@@"

# The AMI pre-stages all databases; verify before starting.
if [ ! -f /opt/databases/kraken2/hash.k2d ]; then
    echo "ERROR: Kraken2 database not found — was the AMI baked correctly?"
    exit 1
fi

# Download this instance's samplesheet slice from S3.
aws s3 cp "s3://${BUCKET}/${SAMPLESHEET_KEY}" /tmp/samplesheet.csv --region "${REGION}"
echo "Samplesheet: $(wc -l < /tmp/samplesheet.csv) lines"

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

# Run the pipeline.
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

# Parse Kraken2 outputs and upload per-sample JSON to S3.
# Written as an inline Python script so we can use boto3 directly.
python3 /tmp/parse_results.py

echo "Results uploaded."

# Signal spawn that this instance is done.
touch /tmp/SPAWN_COMPLETE
echo "=== Worker complete: $(date) ==="
"""

# The parse_results.py script is written separately so it can use normal
# Python syntax without fighting bash heredoc escaping.
_PARSE_SCRIPT_TEMPLATE = """
import json, glob, re, boto3

s3 = boto3.client("s3", region_name="@@REGION@@")
bucket = "@@BUCKET@@"
job_name = "@@JOB_NAME@@"
outdir = "/tmp/nextflow_output"


def parse_kraken2_report(path, srr, body_site):
    species = []
    with open(path) as f:
        for line in f:
            fields = line.strip().split("\\t")
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


# Read sample-to-body-site mapping from samplesheet
samplesheet = {}
with open("/tmp/samplesheet.csv") as f:
    header = None
    for line in f:
        if header is None:
            header = line.strip().split(",")
            continue
        vals = line.strip().split(",")
        if len(vals) >= 2:
            row = dict(zip(header, vals))
            srr = row.get("run_accession", "")
            sample = row.get("sample", "")
            body_site = sample.split("_", 1)[1] if "_" in sample else "unknown"
            samplesheet[srr] = body_site

# Find Kraken2 report files
reports = glob.glob(f"{outdir}/**/kraken2/*.report.txt", recursive=True)
if not reports:
    reports = glob.glob(f"{outdir}/**/taxonomy/*.report.txt", recursive=True)

uploaded = 0
for report_path in reports:
    m = re.search(r"(SRR\\d+)", report_path)
    if not m:
        continue
    srr = m.group(1)
    body_site = samplesheet.get(srr, "unknown")
    data = parse_kraken2_report(report_path, srr, body_site)
    key = f"results/{job_name}/kraken2/{srr}.json"
    s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(data))
    uploaded += 1

print(f"Uploaded {uploaded} Kraken2 result files to S3")
"""


def render(cfg, samplesheet_key: str) -> str:
    """Return the worker bash script with config values substituted.

    Uses token replacement (not str.format) to avoid escaping conflicts
    with bash ${VAR} and Python f-string syntax in the embedded scripts.

    Args:
        cfg:             config module (BUCKET, REGION, JOB_NAME).
        samplesheet_key: S3 key for this instance's samplesheet slice.

    Returns:
        The filled-in bash startup script.
    """

    def _sub(s: str) -> str:
        return (
            s.replace(_TOK_BUCKET, cfg.BUCKET)
            .replace(_TOK_REGION, cfg.REGION)
            .replace(_TOK_JOB_NAME, cfg.JOB_NAME)
            .replace(_TOK_SAMPLESHEET, samplesheet_key)
        )

    # The parse script is written to /tmp/parse_results.py by embedding it
    # in the bash script via a heredoc that is NOT inside a python3 -<< block.
    parse_script = _sub(_PARSE_SCRIPT_TEMPLATE)

    # Embed the parse script into the bash script before the exec line.
    bash_script = _sub(_WORKER_TEMPLATE)

    # Inject the parse_results.py write step right after the Nextflow run.
    inject = "\n# Write the result parser to disk (avoids heredoc escaping issues).\n"
    inject += "cat > /tmp/parse_results.py << 'PYEOF'\n"
    inject += parse_script.strip()
    inject += "\nPYEOF\n"

    bash_script = bash_script.replace(
        "# Parse Kraken2 outputs and upload per-sample JSON to S3.",
        inject + "# Parse Kraken2 outputs and upload per-sample JSON to S3.",
        1,
    )

    return bash_script


def write_temp(script: str) -> str:
    """Write the script to a NamedTemporaryFile and return its path."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
        f.write(script)
        return f.name


def write_samplesheet_slices(cfg, accessions: list[tuple[str, str]]) -> list[str]:
    """Split accessions into per-instance slices, upload to S3, return S3 keys.

    Args:
        cfg:        config module (BUCKET, REGION, JOB_NAME, INSTANCE_COUNT).
        accessions: list of (srr, body_site) tuples.

    Returns:
        List of S3 keys, one per worker instance (may be fewer than INSTANCE_COUNT
        if there are fewer samples than instances).
    """
    import csv
    import io

    import boto3

    s3 = boto3.client("s3", region_name=cfg.REGION)
    n = cfg.INSTANCE_COUNT
    slice_size = max(1, (len(accessions) + n - 1) // n)

    keys: list[str] = []
    for i in range(n):
        chunk = accessions[i * slice_size : (i + 1) * slice_size]
        if not chunk:
            break

        buf = io.StringIO()
        writer = csv.DictWriter(
            buf,
            fieldnames=[
                "sample",
                "run_accession",
                "instrument_platform",
                "fastq_1",
                "fastq_2",
                "fasta",
            ],
        )
        writer.writeheader()
        for srr, body_site in chunk:
            writer.writerow(
                {
                    "sample": f"{srr}_{body_site}",
                    "run_accession": srr,
                    "instrument_platform": "ILLUMINA",
                    "fastq_1": f"s3://{cfg.BUCKET}/corpus/{srr}/{srr}.sra",
                    "fastq_2": "",
                    "fasta": "",
                }
            )

        key = f"slices/{cfg.JOB_NAME}/samplesheet_{i:03d}.csv"
        s3.put_object(Bucket=cfg.BUCKET, Key=key, Body=buf.getvalue().encode())
        keys.append(key)

    return keys
