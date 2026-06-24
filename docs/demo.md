# The live demo — HMP microbiome analysis on real AWS compute

A live, talk-friendly demo: press **Start Analysis** and watch real EC2 instances
spin up, run nf-core/taxprofiler against real Human Microbiome Project data from
the AWS Open Data registry, and surface the cost as it happens — capped by a
Bedrock Claude synthesis of the biology.

> This page is the **demo** (a presentation prop). The from-scratch measurement
> study — Graviton vs x86, FSx, lifecycle cost — lives in
> [methodology.md](methodology.md) and [results.md](results.md). The demo and the
> study share the same pipeline and code; they differ in DB delivery (the demo
> bakes the Kraken2 DB into the AMI for a fast, self-contained cold start; the
> study uses shared FSx Lustre for wide fan-out — see
> [decisions/0001](decisions/0001-db-delivery-ami-ebs-fsx.md)).

## What happens during the demo

### Beat 1 — Start Analysis
1. **vCPU quota query** via [truffle](https://spore.host) — the account's actual
   EC2 quota sets the Nextflow `queueSize` (max concurrent tasks).
2. **Head node launch** — a single small instance launches via
   [spawn](https://spore.host), running Nextflow with the
   [nf-spawn](https://github.com/spore-host/nf-spawn) executor plugin.

### Beat 2 — Per-sample EC2 instances appear
Nextflow reads the samplesheet and dispatches tasks. For each HMP sample,
**nf-spawn launches a dedicated EC2 instance** for `FETCH_FASTQ`, which:
- reads its SRA file **directly from RODA** (`s3://sra-pub-run-odp/`) — no staging,
  no copying, $0 data cost (same region, no egress);
- converts SRA → FASTQ with `fasterq-dump` (via the ECR pull-through-cached
  `ncbi/sra-tools` image — see [decisions/0003](decisions/0003-fanout-ecr-ptc-and-stage-once.md));
- writes FASTQs to the S3 work directory and self-terminates.

### Beat 3 — Classification pipeline
Once FASTQs are in S3, nf-core/taxprofiler runs, one EC2 instance per task:
`fastp`/`FastQC` (trim+QC) → **Kraken2** (memory-bound, needs the DB RAM-resident)
→ **MetaPhlAn** (marker-gene profiling) → **MultiQC**. Intermediate data passes
through the shared S3 work dir — no instance talks directly to another.

### Beat 4 — Bedrock synthesis
When all samples complete, Bedrock Claude reads the classification results and
generates plain-language insights about microbiome diversity across the three body
sites (gut/stool, oral, nasal).

## Architecture

```
Local machine (FastAPI dashboard)
        │
        ▼
Head node  (Nextflow + nf-spawn plugin)
        │
        ├── FETCH_FASTQ ×N    ──► RODA (s3://sra-pub-run-odp/)   no egress, $0 data
        ├── fastp / FastQC ×N
        ├── Kraken2 ×N        (Kraken2 DB pre-staged on AMI for the demo)
        ├── MetaPhlAn ×N
        └── MultiQC ×1
                │
                ▼
        S3 results bucket  →  Bedrock Claude  →  insights
```

Every task instance self-terminates after writing its outputs to S3.

## Prerequisites

```bash
brew install spore-host/tap/spawn   # spawn CLI — launches EC2 instances
brew install uv                     # Python package manager
```

AWS credentials configured as the `aws` profile (EC2, S3, Bedrock, EC2 Service
Quotas permissions).

## Setup (once before the talk)

```bash
cp config.example.py config.py      # set REGION, ACCOUNT_ID, BUCKET
make install
make ami        # bake the AMI (~2-3 h; Kraken2 DB download is the bottleneck)
                # paste the printed AMI_ID into config.py
make demo-fake  # optional: rehearse the full pipeline with no AWS calls
```

## Run it

```bash
make demo       # opens http://127.0.0.1:8001 — press Start Analysis
```

Rough timeline for 100 samples: 0–5 min provision, 5–20 min FETCH_FASTQ,
20–35 min classification, 35–40 min MultiQC + Bedrock.

## Teardown

```bash
make teardown   # stops instances, empties + deletes the S3 bucket
```

The AMI is **not** deleted automatically (EBS snapshot ≈ $2/month). Deregister
when done:

```bash
aws ec2 deregister-image --image-id <AMI_ID> --region us-east-1
```

## Cost (100 samples, demo config)

~$15 per full run ("do it for real"); a `SAMPLE_COUNT=5` rehearsal is ~$1. HMP
data from RODA is **$0** (same-region, no egress). For the measured, phase-by-phase
cost breakdown and the Graviton comparison, see [results.md](results.md).

## Design FAQ

**Why RODA?** [SRA Open Data](https://registry.opendata.aws/ncbi-sra/) is hosted by
AWS in us-east-1; same-region EC2 reads it at full S3 bandwidth with no egress.
Each FETCH_FASTQ instance reads its own sample independently — no coordination, no
staging. This is what a public data commons is for.

**Why Nextflow + nf-spawn?** Nextflow gives the workflow DAG (dependencies, retries,
work-dir management); nf-spawn replaces the executor so each task gets its own
purpose-sized ephemeral EC2 instance (Kraken2 needs 32 GB RAM; fasterq-dump
doesn't) that self-terminates the moment it's done — per-second, per-task cost, no
idle capacity, no queue to maintain.

**Why `k2_pluspf_16GB`?** It covers bacteria/archaea/viruses/fungi/protozoa —
everything relevant for gut/oral/nasal profiling — and at 16 GB fits in a
`c7g.4xlarge`'s 32 GB RAM for in-memory classification (the throughput
bottleneck). The full 75 GB standard DB adds plant genomes and irrelevant taxa and
doesn't fit in RAM without paging.

**Why Graviton?** `c7g`/`r7g` deliver comparable or better price/performance vs
`c7i`/`r7i` at the same vCPU/RAM, ~19% cheaper per hour. Nextflow, Docker, and all
nf-core containers support arm64. The full measured comparison is in
[results.md](results.md).
