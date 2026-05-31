"""
worker_script.py  --  generate the cloud-init bash script for the Nextflow head node.

Architecture:
  app.py launches ONE small head instance (t4g.small) via spawn.
  The head instance runs Nextflow with the nf-spawn executor plugin.
  Nextflow dispatches each pipeline task to its own ephemeral EC2 instance
  (sized per process label), which self-terminates when the task completes.

Data flow:
  RODA (s3://sra-pub-run-odp/) ──► fasterq-dump task instance
                                        │ FASTQs
                                        ▼ s3://bucket/work/
                                   fastp task instance
                                        │ trimmed FASTQs
                                        ▼ s3://bucket/work/
                         ┌─────────────┴────────────┐
                    Kraken2 instance         MetaPhlAn instance
                         └─────────────┬────────────┘
                                        ▼ s3://bucket/work/
                                  Taxpasta/MultiQC instance
                                        ▼
                               s3://bucket/results/

The head instance is responsible for:
  1. Writing nextflow.config (uploaded from app.py as an S3 object)
  2. Building the taxprofiler samplesheet from the SRR accession list
  3. Running `nextflow run nf-core/taxprofiler`
  4. Writing a progress.json to S3 periodically (dashboard polls this)
  5. Writing final summary.json on completion
  6. Touching /tmp/SPAWN_COMPLETE so spawn knows the head is done

Data volume tracking:
  fasterq-dump reports bytes read from RODA; we capture this and the
  output FASTQ sizes to show the SRA compression ratio in the dashboard.
"""

from __future__ import annotations

import tempfile

_TOK_BUCKET = "@@BUCKET@@"
_TOK_REGION = "@@REGION@@"
_TOK_JOB_NAME = "@@JOB_NAME@@"
_TOK_NF_CFG = "@@NF_CONFIG_KEY@@"  # S3 key for the rendered nextflow.config
_TOK_SRR_KEY = "@@SRR_LIST_KEY@@"  # S3 key for the SRR accession list JSON

_HEAD_SCRIPT = r"""#!/bin/bash
set -euxo pipefail
exec > /var/log/nextflow-head.log 2>&1

echo "=== Microbiome Demo — Head Node ==="
echo "Started: $(date)"
echo "Instance: $(curl -sf http://169.254.169.254/latest/meta-data/instance-id || echo unknown)"

# The spawn bootstrap installs spored concurrently with user-data execution.
# If this script starts while spored is mid-install, the spored install
# fails ("Text file busy") and cloud-init marks the whole user-data as
# failed.  We avoid this by sleeping briefly — by the time we wake up
# spored is installed and not being written to.
# A 30-second sleep is sufficient: the instance is already "SSH ready"
# (spawn's --wait-for-ssh already waited for that), and spored's install
# takes < 10 seconds from that point.
echo "Waiting 30s for spawn bootstrap to settle before starting pipeline..."
sleep 30
echo "Bootstrap wait done: $(date)"

BUCKET="@@BUCKET@@"
REGION="@@REGION@@"
JOB_NAME="@@JOB_NAME@@"
NF_CONFIG_KEY="@@NF_CONFIG_KEY@@"
SRR_LIST_KEY="@@SRR_LIST_KEY@@"
RESULTS_PREFIX="s3://${BUCKET}/results/${JOB_NAME}"
PROGRESS_KEY="results/${JOB_NAME}/progress.json"

# ── Prerequisites check ──────────────────────────────────────────────────────
command -v nextflow || { echo "ERROR: nextflow not found on PATH"; exit 1; }
command -v spawn    || { echo "ERROR: spawn not found on PATH"; exit 1; }
test -f /opt/databases/kraken2/hash.k2d \
    || { echo "ERROR: Kraken2 database missing — was the AMI baked?"; exit 1; }

# ── Download config and SRR list ─────────────────────────────────────────────
mkdir -p /tmp/nf-head
aws s3 cp "s3://${BUCKET}/${NF_CONFIG_KEY}" /tmp/nf-head/nextflow.config \
    --region "${REGION}"
aws s3 cp "s3://${BUCKET}/${SRR_LIST_KEY}" /tmp/nf-head/srr_list.json \
    --region "${REGION}"

# ── Build taxprofiler samplesheet ────────────────────────────────────────────
# Each sample reads its SRA file DIRECTLY from RODA at task runtime.
# The samplesheet contains SRR accessions — nf-core/fetchngs or the
# built-in SRA support converts them to FASTQ on the task instance.
python3 - << 'PYEOF'
import csv, json, sys

with open("/tmp/nf-head/srr_list.json") as f:
    samples = json.load(f)

rows = []
for item in samples:
    srr  = item["srr"]
    site = item["body_site"]
    rows.append({
        "sample":               f"{srr}_{site}",
        "run_accession":        srr,
        "instrument_platform":  "ILLUMINA",
        # SRA accession — nf-core/taxprofiler fetches via fasterq-dump
        # directly from RODA (s3://sra-pub-run-odp/).
        "fastq_1": "",
        "fastq_2": "",
        "fasta":   "",
    })

out = "/tmp/nf-head/samplesheet.csv"
with open(out, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["sample", "run_accession",
                                       "instrument_platform",
                                       "fastq_1", "fastq_2", "fasta"])
    w.writeheader()
    w.writerows(rows)

print(f"Samplesheet: {len(rows)} samples → {out}")
PYEOF

# ── Write initial progress ───────────────────────────────────────────────────
python3 - << 'PYEOF'
import json, boto3, time

s3 = boto3.client("s3", region_name="@@REGION@@")
progress = {
    "status":        "running",
    "started_at":    time.time(),
    "queue_size":    0,
    "tasks_total":   0,
    "tasks_running": 0,
    "tasks_done":    0,
    "tasks_failed":  0,
    # Data volume counters (updated as trace.tsv grows)
    "roda_bytes_read":  0,   # bytes read from s3://sra-pub-run-odp/
    "fastq_bytes":      0,   # total FASTQ bytes after SRA conversion
    "result_bytes":     0,   # Kraken2/MetaPhlAn output size
}
s3.put_object(
    Bucket="@@BUCKET@@",
    Key="results/@@JOB_NAME@@/progress.json",
    Body=json.dumps(progress),
)
PYEOF

# ── Run the pipeline ─────────────────────────────────────────────────────────
# NXF_HOME points to the pre-pulled pipeline cache on the AMI.
# The nf-spawn plugin JAR is in ~/.nextflow/plugins/ (also on the AMI).
NXF_HOME=/opt/nextflow_cache \
    /usr/local/bin/nextflow run nf-core/taxprofiler \
    --input /tmp/nf-head/samplesheet.csv \
    --outdir "${RESULTS_PREFIX}" \
    -c /tmp/nf-head/nextflow.config \
    -w "s3://${BUCKET}/work/${JOB_NAME}/" \
    -resume \
    2>&1 | tee /tmp/nf-head/nextflow.stdout &

NF_PID=$!

# ── Progress monitor (runs alongside Nextflow) ───────────────────────────────
# Polls the Nextflow trace file on S3 and the local .nextflow.log every 15s.
# Updates progress.json so the dashboard has live numbers.
cat > /tmp/nf-head/monitor.py << 'MONEOF'
import boto3, json, re, sys, time, os

s3        = boto3.client("s3", region_name="@@REGION@@")
bucket    = "@@BUCKET@@"
job_name  = "@@JOB_NAME@@"
trace_key = f"results/{job_name}/trace.tsv"
prog_key  = f"results/{job_name}/progress.json"
reports_prefix = f"results/{job_name}/kraken2_reports/"
kraken_json_prefix = f"results/{job_name}/kraken2/"
nf_pid    = int(sys.argv[1])

# Load body_site map from the SRR list so we can annotate Kraken2 results.
with open("/tmp/nf-head/srr_list.json") as f:
    srr_list = json.load(f)
body_site_map = {item["srr"]: item["body_site"] for item in srr_list}

def read_trace():
    # Parse the Nextflow trace TSV and return list of task dicts.
    try:
        resp = s3.get_object(Bucket=bucket, Key=trace_key)
        lines = resp["Body"].read().decode().splitlines()
    except Exception:
        return []
    if len(lines) < 2:
        return []
    header = lines[0].split("\t")
    return [dict(zip(header, line.split("\t"))) for line in lines[1:] if line.strip()]


def parse_rchar(s):
    # Bytes read field from trace (plain integer or 'N/A').
    if not s or s in ("-", "N/A", ""):
        return 0
    try:
        return int(s)
    except ValueError:
        return 0


def is_sra_task(name):
    return any(k in name.lower() for k in ("fasterq", "sratools", "fetchngs", "sra_to"))


def parse_kraken2_report(body):
    # Parse a Kraken2 report text and return sorted species list.
    species = []
    for line in body.decode().splitlines():
        fields = line.strip().split("\t")
        if len(fields) >= 6 and fields[3] == "S":
            try:
                pct = float(fields[0])
                if pct > 0.1:
                    species.append({"name": fields[5].strip(),
                                    "pct": pct, "reads": int(fields[1])})
            except (ValueError, IndexError):
                continue
    return sorted(species, key=lambda x: x["pct"], reverse=True)


def process_new_reports(already_done):
    # List published Kraken2 report files; parse and upload JSON for new ones.
    paginator = s3.get_paginator("list_objects_v2")
    new_done = set(already_done)
    for page in paginator.paginate(Bucket=bucket, Prefix=reports_prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key in already_done or not key.endswith(".report.txt"):
                continue
            m = re.search(r"(SRR\d+)", key)
            if not m:
                continue
            srr = m.group(1)
            try:
                body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
                sp = parse_kraken2_report(body)
                data = {
                    "srr": srr,
                    "body_site": body_site_map.get(srr, "unknown"),
                    "top_species": [s["name"] for s in sp[:10]],
                    "species_detail": sp[:20],
                }
                s3.put_object(
                    Bucket=bucket,
                    Key=f"{kraken_json_prefix}{srr}.json",
                    Body=json.dumps(data),
                )
                new_done.add(key)
            except Exception:
                pass
    return new_done


started_at  = time.time()
reports_done: set = set()

while True:
    time.sleep(15)

    nf_running = os.path.exists(f"/proc/{nf_pid}")
    tasks      = read_trace()

    running = sum(1 for t in tasks if t.get("status") == "RUNNING")
    done    = sum(1 for t in tasks if t.get("status") == "COMPLETED")
    failed  = sum(1 for t in tasks if t.get("status") in ("FAILED", "ABORTED"))
    total   = len(tasks)

    # Data volumes from trace rchar/wchar fields
    roda_bytes = sum(
        parse_rchar(t.get("rchar", "0"))
        for t in tasks if is_sra_task(t.get("name", ""))
    )
    fastq_bytes = sum(
        parse_rchar(t.get("wchar", "0"))
        for t in tasks if is_sra_task(t.get("name", ""))
    )
    analysis_bytes = sum(
        parse_rchar(t.get("rchar", "0"))
        for t in tasks
        if any(k in t.get("name", "").lower() for k in ("kraken2", "metaphlan"))
    )

    # Convert any newly published Kraken2 reports → per-sample JSON
    reports_done = process_new_reports(reports_done)

    progress = {
        "status":             "complete" if not nf_running else "running",
        "started_at":         started_at,
        "elapsed_seconds":    time.time() - started_at,
        "tasks_total":        total,
        "tasks_running":      running,
        "tasks_done":         done,
        "tasks_failed":       failed,
        "roda_bytes_read":    roda_bytes,
        "fastq_bytes":        fastq_bytes,
        "analysis_bytes_read": analysis_bytes,
    }

    s3.put_object(Bucket=bucket, Key=prog_key, Body=json.dumps(progress))

    if not nf_running:
        break

MONEOF

python3 /tmp/nf-head/monitor.py ${NF_PID} &
MONITOR_PID=$!

# Wait for Nextflow to finish
wait ${NF_PID}
NF_EXIT=$?

# Give monitor one final cycle then stop it
sleep 20
kill ${MONITOR_PID} 2>/dev/null || true

# ── Write final summary ──────────────────────────────────────────────────────
python3 - << 'PYEOF'
import boto3, json, time, glob, os

s3         = boto3.client("s3", region_name="@@REGION@@")
bucket     = "@@BUCKET@@"
job_name   = "@@JOB_NAME@@"
results_pf = f"results/{job_name}"

# Read final progress
try:
    prog = json.loads(
        s3.get_object(Bucket=bucket, Key=f"{results_pf}/progress.json")["Body"].read()
    )
except Exception:
    prog = {}

# Aggregate per-body-site species from Kraken2 result JSONs
body_sites: dict = {}
paginator = s3.get_paginator("list_objects_v2")
for page in paginator.paginate(Bucket=bucket, Prefix=f"{results_pf}/kraken2/"):
    for obj in page.get("Contents", []):
        try:
            data = json.loads(s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read())
            site = data.get("body_site", "unknown")
            if site not in body_sites:
                body_sites[site] = {"top_species": [], "diversity": {}}
            # Accumulate top species (dedup)
            for sp in data.get("top_species", []):
                if sp not in body_sites[site]["top_species"]:
                    body_sites[site]["top_species"].append(sp)
        except Exception:
            continue

# Trim to top 10 per site
for site in body_sites:
    body_sites[site]["top_species"] = body_sites[site]["top_species"][:10]

summary = {
    "total_samples":    prog.get("tasks_done", 0),
    "completed":        prog.get("tasks_done", 0),
    "failed":           prog.get("tasks_failed", 0),
    "elapsed_seconds":  prog.get("elapsed_seconds", 0),
    "body_sites":       body_sites,
    "cross_site_comparison": {},
    # Data volumes for the dashboard
    "data_volumes": {
        "roda_bytes_read":     prog.get("roda_bytes_read", 0),
        "fastq_bytes":         prog.get("fastq_bytes", 0),
        "analysis_bytes_read": prog.get("analysis_bytes_read", 0),
    },
}
s3.put_object(
    Bucket=bucket,
    Key=f"{results_pf}/summary.json",
    Body=json.dumps(summary),
)
print(f"Summary written: {summary['total_samples']} samples, "
      f"{summary['data_volumes']['roda_bytes_read']:,} bytes from RODA")
PYEOF

touch /tmp/SPAWN_COMPLETE
echo "=== Head node complete: $(date) (Nextflow exit: ${NF_EXIT}) ==="
"""


def render(cfg, nf_config_key: str, srr_list_key: str) -> str:
    """Return the head node bash script with config values substituted."""

    def _sub(s: str) -> str:
        return (
            s.replace(_TOK_BUCKET, cfg.BUCKET)
            .replace(_TOK_REGION, cfg.REGION)
            .replace(_TOK_JOB_NAME, cfg.JOB_NAME)
            .replace(_TOK_NF_CFG, nf_config_key)
            .replace(_TOK_SRR_KEY, srr_list_key)
        )

    return _sub(_HEAD_SCRIPT)


def write_temp(script: str) -> str:
    """Write script to a NamedTemporaryFile and return its path."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
        f.write(script)
        return f.name


def write_srr_slice(cfg, accessions: list[tuple[str, str]]) -> str:
    """Upload the full SRR accession list as a single JSON to S3.

    With nf-spawn, Nextflow manages parallelism — we give the head node
    all accessions and let queueSize control concurrency.

    Returns the S3 key.
    """
    import json

    import boto3

    s3 = boto3.client("s3", region_name=cfg.REGION)
    entries = [{"srr": srr, "body_site": bs} for srr, bs in accessions]
    key = f"slices/{cfg.JOB_NAME}/srr_list.json"
    s3.put_object(
        Bucket=cfg.BUCKET,
        Key=key,
        Body=json.dumps(entries, indent=2).encode(),
    )
    return key


def upload_nextflow_config(cfg, nf_config_str: str) -> str:
    """Upload the rendered nextflow.config to S3 and return its key."""
    import boto3

    s3 = boto3.client("s3", region_name=cfg.REGION)
    key = f"config/{cfg.JOB_NAME}/nextflow.config"
    s3.put_object(
        Bucket=cfg.BUCKET,
        Key=key,
        Body=nf_config_str.encode(),
    )
    return key
