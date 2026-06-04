# HMP Microbiome Demo — Live AWS Analysis

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![AWS Graviton3](https://img.shields.io/badge/AWS-Graviton3-orange.svg)](https://aws.amazon.com/ec2/graviton/)
[![spawn](https://img.shields.io/badge/powered%20by-spawn-5c5cff.svg)](https://spore.host)

A live demo that runs **real** AWS compute against **real** Human Microbiome Project data
and shows every dollar as it happens.

---

## What happens during the demo

### Beat 1 — Start Analysis

Pressing **Start Analysis** triggers:

1. **vCPU quota query** via [truffle](https://spore.host) — the account's actual EC2 quota
   determines the Nextflow `queueSize` (max concurrent tasks)
2. **Head node launch** — a single `t4g.small` instance launches via
   [spawn](https://spore.host).  It runs Nextflow with the
   [nf-spawn](https://github.com/spore-host/nf-spawn) executor plugin.

### Beat 2 — Per-sample EC2 instances appear

Nextflow reads the 100-sample samplesheet and dispatches tasks.
For each HMP sample, **nf-spawn launches a dedicated EC2 instance**:

```
FETCH_FASTQ  →  t4g.medium  (SRA download + fasterq-dump)
```

Each instance:
- Reads its SRA file **directly from RODA** (`s3://sra-pub-run-odp/`) — no staging, no copying, $0 data cost
- Converts SRA → FASTQ using fasterq-dump (inside the nf-core/taxprofiler Docker container)
- Writes FASTQs to the S3 work directory
- Self-terminates when done

The dashboard shows instances appearing in spawn list as the queue fills.

### Beat 3 — Classification pipeline

Once FASTQs are staged in S3, nf-core/taxprofiler runs — again dispatching
**one EC2 instance per task**:

```
fastp/FastQC  →  t4g.large   (quality trimming)
Kraken2       →  c7g.4xlarge (needs 32 GB RAM; database pre-staged on AMI)
MetaPhlAn     →  c7g.2xlarge (marker gene profiling)
MultiQC       →  t4g.large   (report generation)
```

Intermediate FASTQs pass through the shared S3 work directory — no instance
talks directly to another.

### Beat 4 — Bedrock synthesis

When all samples complete, Bedrock Claude Sonnet reads the classification results
and generates three plain-language insights about microbiome diversity across the
three body sites (gut/stool, oral, nasal).

---

## Architecture

```
Local machine (FastAPI dashboard)
        │
        ▼
Head node  t4g.small  (Nextflow + nf-spawn plugin)
        │
        ├── FETCH_FASTQ ×100   t4g.medium  ──► RODA (s3://sra-pub-run-odp/)
        │       [parallel, up to queueSize at once]      no egress, $0 data
        │
        ├── fastp/FastQC ×100  t4g.large
        ├── Kraken2 ×100       c7g.4xlarge  (Kraken2 DB pre-staged on AMI)
        ├── MetaPhlAn ×100     c7g.2xlarge
        └── MultiQC ×1         t4g.large
                │
                ▼
        S3 results bucket  →  Bedrock Claude Sonnet  →  3 insights
```

All intermediate data (FASTQs, trimmed reads, classification outputs) passes
through the S3 work directory (`s3://bucket/work/job/`).  Every task instance
self-terminates after writing its outputs to S3.

---

## Prerequisites

```bash
brew install spore-host/tap/spawn   # spawn CLI — launches EC2 instances
brew install uv                     # Python package manager
```

AWS credentials configured as the `aws` profile.  Needs EC2, S3, Bedrock,
and EC2 Service Quotas permissions.

---

## Setup (run once before the talk)

```bash
cp config.example.py config.py
# Edit config.py: set REGION, ACCOUNT_ID, BUCKET
make install
```

### Bake the AMI

The AMI pre-installs everything so task instances boot ready to run — no
software download during the demo:

- Nextflow + nf-spawn plugin (Nextflow executor for spawn)
- Kraken2 `k2_pluspf_16GB` database (11.9 GB → pre-staged on EBS)
- spawn CLI (for the head node to dispatch tasks)
- Docker (nf-core containers run inside Docker)
- Python + boto3 (for S3 progress reporting)

```bash
make ami        # ~2-3 hours, ~$2-3 EC2 cost (c7g.4xlarge bake instance)
# Paste the printed AMI_ID into config.py
```

The bake takes longer than typical because the Kraken2 database download
(11.9 GB from a public S3 bucket) is the bottleneck.

### Rehearse without AWS

```bash
make demo-fake  # simulates the full pipeline with fake data, no AWS calls
```

---

## Running the demo

```bash
make demo
```

Opens `http://127.0.0.1:8001` automatically.  Press **Start Analysis**.

Expected timeline for 100 samples:
- **0-5 min** — vCPU quota query, head node launch, nf-spawn plugin load
- **5-20 min** — FETCH_FASTQ instances downloading SRA files from RODA
- **20-35 min** — Kraken2 + MetaPhlAn classification (parallel)
- **35-40 min** — MultiQC + Bedrock synthesis

---

## Teardown

```bash
make teardown   # stops any running instances, empties + deletes the S3 bucket
```

The S3 bucket holds only Nextflow work files and result JSONs (a few GB at most,
cleaned up immediately after the demo).

The AMI is **not** deleted automatically — EBS snapshots cost ~$0.05/GB-month
(about $2/month for the 40 GB snapshot).  Deregister when you're done with it:

```bash
aws ec2 deregister-image --image-id <AMI_ID> --region us-east-1
```

---

## Cost estimate (100 samples)

| Component | Qty | Duration | Cost |
|-----------|-----|----------|------|
| Head node (t4g.small) | 1 | ~40 min | ~$0.01 |
| FETCH_FASTQ (t4g.medium) | 100 | ~15 min each | ~$0.84 |
| fastp/FastQC (t4g.large) | 100 | ~5 min each | ~$0.56 |
| Kraken2 (c7g.4xlarge) | 100 | ~10 min each | ~$9.67 |
| MetaPhlAn (c7g.2xlarge) | 100 | ~8 min each | ~$4.35 |
| MultiQC (t4g.large) | 1 | ~2 min | ~$0.002 |
| Bedrock Sonnet synthesis | 1 call | — | ~$0.003 |
| HMP data from RODA | — | — | **$0** |

**Total per run: ~$15.**  This is the "do it for real" cost.  For a rehearsal,
`SAMPLE_COUNT=5` reduces it to ~$1.

---

## Why RODA?

[SRA Open Data](https://registry.opendata.aws/ncbi-sra/) (`s3://sra-pub-run-odp/`)
is hosted by AWS in `us-east-1`.  EC2 instances in the same region read it at full
S3 bandwidth with **no egress charges**.  Every FETCH_FASTQ instance reads its own
sample independently — no coordination, no staging, no copying.  This is what a
public data commons is for.

## Why Nextflow + nf-spawn?

Nextflow provides the workflow DAG (task dependencies, retry logic, work dir management).
nf-spawn replaces the Nextflow executor — instead of AWS Batch or a Slurm cluster,
each task gets its own ephemeral EC2 instance via [spawn](https://spore.host).

This means:
- No queue to configure, no compute environment to maintain
- Instances are purpose-sized per task (Kraken2 needs 32 GB RAM; fasterq-dump doesn't)
- Every instance self-terminates the moment its task completes
- Cost is per-second, per-task — no idle capacity

## Why k2_pluspf_16GB?

Kraken2's `k2_pluspf` database includes bacteria, archaea, viruses, fungi, and protozoa —
everything relevant for human gut, oral, and nasal microbiome profiling.
At 11.9 GB compressed → 16 GB uncompressed, it fits entirely in the 32 GB RAM of a
`c7g.4xlarge`, enabling in-memory classification (the Kraken2 throughput bottleneck).

The full standard database (75 GB) adds plant genomes and irrelevant taxa and doesn't
fit in 32 GB RAM without paging.

## Why ARM64 / Graviton3?

Graviton3 `c7g` instances deliver ~30-40% better price/performance than equivalent
x86 `c5` instances for CPU-bound bioinformatics workloads.  Nextflow, Docker, and
all nf-core containers support ARM64.
