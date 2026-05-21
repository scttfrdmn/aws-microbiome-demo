# HMP Microbiome Demo — Live AWS Metagenomics Analysis

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![AWS Graviton3](https://img.shields.io/badge/AWS-Graviton3-orange.svg)](https://aws.amazon.com/ec2/graviton/)
[![spawn](https://img.shields.io/badge/powered%20by-spawn-5c5cff.svg)](https://spore.host)

A five-minute live demo that runs **real** AWS compute against **real** Human Microbiome
Project data and shows every dollar as it happens.

**What it does:**
1. Stages 100 HMP shotgun sequencing samples from RODA (`s3://sra-pub-run-odp/`) into your S3 bucket.
2. Launches 8× Graviton3 `c7g.4xlarge` instances via [spawn](https://spore.host), each running
   [nf-core/taxprofiler](https://nf-co.re/taxprofiler) (Kraken2 + MetaPhlAn) against a pre-baked AMI.
3. Streams live progress to a local dashboard — cost meter ticking, worker dots going green.
4. When done, calls Bedrock Claude Sonnet to synthesize three plain-language insights.

**Target runtime:** ~15-20 minutes wall clock for 100 samples across 3 body sites.

## Prerequisites

```bash
brew install spore-host/tap/spawn   # the spawn CLI (auto-provisions EC2)
brew install uv                    # fast Python package manager
```

AWS credentials configured as the `aws` profile (`~/.aws/credentials`).  Needs EC2,
S3, and Bedrock permissions.

## Setup (run once before the talk)

```bash
cp config.example.py config.py
# edit config.py: set REGION, ACCOUNT_ID, BUCKET
```

### 1. Stage the corpus

Copies 100 HMP SRA files from the public RODA bucket into your S3 bucket.
S3→S3 transfer, no laptop bandwidth used.

```bash
make corpus          # ~5-10 minutes, free (intra-region S3→S3)
```

### 2. Bake the AMI

Builds an Amazon Linux 2023 ARM64 AMI with Nextflow, nf-core/taxprofiler Singularity image,
and the Kraken2 `k2_pluspf_16GB` database pre-staged — so demo instances boot ready to run.

```bash
make ami             # ~30-45 minutes, ~$1-2 EC2 cost
# After completion, paste the AMI_ID into config.py
```

### 3. Install Python dependencies

```bash
make install
```

## Running the demo

```bash
make demo
```

Opens a browser to `http://127.0.0.1:8000`.  Press **Start Analysis** to launch the job.

## Teardown

```bash
make teardown        # stops any running instances, deletes the S3 corpus bucket
```

The AMI itself is not deleted automatically (no ongoing hourly charge; EBS snapshots
cost ~$0.05/GB-month).  Deregister manually if desired:

```bash
aws ec2 deregister-image --image-id <AMI_ID> --region us-east-1
```

## Project layout

```
corpus_prep.py          stage HMP samples in S3
build_ami.py            bake the pre-installed worker AMI
teardown.py             clean up all AWS resources
config.example.py       copy → config.py and fill in

src/microbiome_demo/
  app.py                FastAPI server: /ws WebSocket + /api/start + /api/results
  spawn.py              programmatic wrapper around the spawn CLI
  pipeline.py           poll S3 for Nextflow progress; compute EC2 cost
  agent.py              Bedrock Sonnet synthesis of metagenomics results
  worker_script.py      generates the cloud-init bash script for each EC2 instance
  static/index.html     Alpine.js live dashboard (no build step)

tests/                  pytest suite (no AWS calls; uses fakes)
Makefile                shortcuts for all common operations
```

## Cost estimate

| Component | Cost |
|-----------|------|
| 8× c7g.4xlarge × 20 min | ~$1.74 |
| Bedrock Sonnet synthesis | ~$0.003 |
| S3 storage (100 SRA files, ~10 GB) | ~$0.24/month while staged |
| AMI bake (one-time) | ~$1-2 |

**Total per demo run: ~$1.74.**

## Why k2_pluspf_16GB?

Kraken2's `k2_pluspf` database includes bacteria, archaea, viruses, fungi, and protozoa —
everything relevant for human microbiome profiling.  At 11.9 GB compressed → 16 GB
uncompressed, it fits entirely in the 32 GB RAM of a `c7g.4xlarge`, enabling in-memory
classification which is the throughput bottleneck for Kraken2.

The full standard database (75 GB) adds plant genomes and other taxa irrelevant to HMP
samples, with no benefit for this use case.

## Why ARM64 / Graviton3?

Graviton3 `c7g` instances deliver ~30% better price-performance than equivalent x86
`c5` instances for CPU-bound bioinformatics workloads like Kraken2.  Nextflow and
Singularity both support ARM64.  The nf-core/taxprofiler container has ARM64 images.
