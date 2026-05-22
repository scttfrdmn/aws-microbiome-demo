# HMP Microbiome Demo — Live AWS Metagenomics Analysis

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![AWS Graviton3](https://img.shields.io/badge/AWS-Graviton3-orange.svg)](https://aws.amazon.com/ec2/graviton/)
[![spawn](https://img.shields.io/badge/powered%20by-spawn-5c5cff.svg)](https://spore.host)

A five-minute live demo that runs **real** AWS compute against **real** Human Microbiome
Project data and shows every dollar as it happens.

**What it does:**
1. Launches 8× Graviton3 `c7g.4xlarge` instances via [spawn](https://spore.host), each running
   [nf-core/taxprofiler](https://nf-co.re/taxprofiler) (Kraken2 + MetaPhlAn) against a pre-baked AMI.
2. Each worker pulls its assigned HMP samples **directly from RODA**
   (`s3://sra-pub-run-odp/`) — no data staging, no copying, no S3 storage cost for the data.
3. Streams live progress to a local dashboard — cost meter ticking, worker dots going green.
4. When done, calls Bedrock Claude Sonnet to synthesize three plain-language insights.

**Target runtime:** ~15-20 minutes wall clock for 100 samples across 3 body sites.

## Prerequisites

```bash
brew install spore-host/tap/spawn   # the spawn CLI (auto-provisions EC2)
brew install uv                     # fast Python package manager
```

AWS credentials configured as the `aws` profile (`~/.aws/credentials`).  Needs EC2,
S3, and Bedrock permissions.

## Setup (run once before the talk)

```bash
cp config.example.py config.py
# edit config.py: set REGION, ACCOUNT_ID, BUCKET
make install
```

### Bake the AMI

Builds an Amazon Linux 2023 ARM64 AMI with Nextflow, nf-core/taxprofiler Singularity image,
and the Kraken2 `k2_pluspf_16GB` database pre-staged — so demo instances boot ready to run.

```bash
make ami             # ~30-45 minutes, ~$1-2 EC2 cost
# After completion, paste the AMI_ID into config.py
```

That's it — no corpus staging step.  Workers pull HMP data from RODA at runtime.

## Running the demo

```bash
make demo
```

Opens a browser to `http://127.0.0.1:8000`.  Press **Start Analysis** to launch the job.

## Teardown

```bash
make teardown        # stops any running instances, deletes the S3 results bucket
```

The S3 bucket holds only tiny SRR slice lists and result JSONs (~a few MB total).
The AMI itself is not deleted automatically (no ongoing hourly charge; EBS snapshots
cost ~$0.05/GB-month).  Deregister manually if desired:

```bash
aws ec2 deregister-image --image-id <AMI_ID> --region us-east-1
```

## Project layout

```
build_ami.py            bake the pre-installed worker AMI
teardown.py             clean up all AWS resources
config.example.py       copy → config.py and fill in

src/microbiome_demo/
  accessions.py         curated HMP SRR accession list (34 stool, 33 oral, 33 nasal)
  app.py                FastAPI server: /ws WebSocket + /api/start + /api/results
  spawn.py              programmatic wrapper around the spawn CLI
  pipeline.py           poll S3 for Nextflow progress; compute EC2 cost
  agent.py              Bedrock Sonnet synthesis of microbiome analysis results
  worker_script.py      generates the cloud-init bash script for each EC2 instance
                        (pulls SRA files directly from RODA, no staging)
  static/index.html     Alpine.js live dashboard (no build step)

tests/                  pytest suite (no AWS calls; uses fakes)
Makefile                shortcuts for all common operations
```

## Cost estimate

| Component | Cost |
|-----------|------|
| 8× c7g.4xlarge × 20 min | ~$1.74 |
| Bedrock Sonnet synthesis | ~$0.003 |
| S3 results storage (~5 MB of JSON) | negligible |
| AMI bake (one-time) | ~$1-2 |
| HMP data (from RODA) | **$0** — public dataset, no egress within us-east-1 |

**Total per demo run: ~$1.74.**

## Why read from RODA directly?

[SRA Open Data](https://registry.opendata.aws/ncbi-sra/) (`s3://sra-pub-run-odp/`) is an
AWS public dataset hosted in `us-east-1`.  EC2 instances in the same region read it at
full S3 bandwidth with no data transfer fees.  There is no reason to copy 10 GB of SRA
files into your own bucket first — that would waste time, storage cost, and defeat the
purpose of a public data commons.

## Why k2_pluspf_16GB?

Kraken2's `k2_pluspf` database includes bacteria, archaea, viruses, fungi, and protozoa —
everything relevant for human microbiome profiling.  At 11.9 GB compressed → 16 GB
uncompressed, it fits entirely in the 32 GB RAM of a `c7g.4xlarge`, enabling in-memory
classification which is the throughput bottleneck for Kraken2.

## Why ARM64 / Graviton3?

Graviton3 `c7g` instances deliver ~30% better price-performance than equivalent x86
`c5` instances for CPU-bound bioinformatics workloads like Kraken2.  Nextflow and
Singularity both support ARM64.  The nf-core/taxprofiler container has ARM64 images.
