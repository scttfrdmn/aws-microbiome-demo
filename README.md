# HMP Microbiome on AWS — live demo + Graviton benchmark

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![AWS Graviton](https://img.shields.io/badge/AWS-Graviton-orange.svg)](https://aws.amazon.com/ec2/graviton/)
[![spawn](https://img.shields.io/badge/powered%20by-spawn-5c5cff.svg)](https://spore.host)

Run a **real** nf-core/taxprofiler metagenomics pipeline on **real** Human
Microbiome Project data on AWS — one ephemeral EC2 instance per task via
[Nextflow](https://www.nextflow.io/) + [nf-spawn](https://github.com/spore-host/nf-spawn),
reading HMP data straight from the AWS Open Data registry at $0 egress.

This repo is **two things** built on the same pipeline and code:

| | What it is | Start here |
|---|------------|-----------|
| 🎤 **The live demo** | A talk-friendly FastAPI dashboard: press *Start Analysis*, watch instances appear, see cost accrue, finish with a Bedrock Claude synthesis of the biology. | **[docs/demo.md](docs/demo.md)** |
| 📊 **The benchmark study** | A from-scratch, fully-instrumented measurement: Graviton (arm64) vs x86, FSx-backed DBs, time·data·cost for every lifecycle phase, with fan-out `N` as the knob — *how to do this properly*. | **[docs/results.md](docs/results.md)** |

## The headline numbers (N=30, native-vs-native, both arches clean 30/30)

- **Runtime: roughly at parity** per-stage (MetaPhlAn within ~2%).
- **Cost: arm64 ~16–17% cheaper** end-to-end — near-equal throughput at lower $/hr.
- **FSx Lustre scaled to 56 concurrent DB readers** where EBS+FSR hit a ~10-reader
  credit cliff.
- **Biology validates:** stool & buccal recover HMP-expected genera; body sites
  separate by genus-level Bray–Curtis (within < between) on both classifiers.

Full breakdown → **[docs/results.md](docs/results.md)**.

## Documentation map

```
docs/
  demo.md            ← run the live demo (setup, AMI, teardown, design FAQ)
  methodology.md     ← how the benchmark is kept honest (fairness controls, protocol)
  results.md         ← canonical N=30 results: lifecycle cost, arch comparison, biology
  blog/end-to-end.md ← the full story: architecture + the four sharp edges we hit
  decisions/         ← decision records (the "why" behind each design choice — see README)
    0001 DB delivery: AMI → EBS+FSR → FSx Lustre
    0002 timing: bill instance lifetime, not trace realtime
    0003 fan-out: ECR pull-through cache + stage reference data once
    0004 biology: genus-level + mean Bray–Curtis (two diversity-stat bugs)
  ami-vs-data-volume.md  ← earlier design note on DB-on-AMI vs data volume
benchmark/
  README.md          ← how to run the measurement harness
  *.py               ← harness (build_fsx_db, lifecycle_metrics, analyze_study, diff_traces)
  results/lifecycle/ ← results of record (MEASUREMENTS.md + per-leg JSON)
  results/_archive/  ← superseded N=3 EBS+FSR pilots, kept for provenance
```

## Quick start

**Live demo:**
```bash
cp config.example.py config.py   # set REGION, ACCOUNT_ID, BUCKET
make install
make demo-fake                   # rehearse with no AWS calls
make demo                        # → http://127.0.0.1:8001, press Start Analysis
```
Full instructions, AMI bake, and teardown: **[docs/demo.md](docs/demo.md)**.

**Benchmark study:** see **[benchmark/README.md](benchmark/README.md)** and
**[docs/methodology.md](docs/methodology.md)**.

## Built on

[Nextflow](https://www.nextflow.io/) · [nf-core/taxprofiler](https://nf-co.re/taxprofiler)
2.0.0 · [spawn + nf-spawn + truffle](https://spore.host) ·
[aarchbio](https://github.com/playgroundlogic/aarchbio) (native arm64 containers) ·
[SRA Open Data on AWS](https://registry.opendata.aws/ncbi-sra/) · Amazon Bedrock.
