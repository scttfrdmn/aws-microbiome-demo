# Graviton port + benchmark — status & handoff

## TL;DR

The **aarchbio Graviton port is complete and committed** — the demo's
nf-core/taxprofiler pipeline is configured to run 100% native on Graviton
(arm64), no QEMU. The **x86-vs-arm64 benchmark is paused**, blocked upstream by
[nf-spawn#37](https://github.com/spore-host/nf-spawn/issues/37) (nf-spawn does
not stage Nextflow `path` inputs), which prevents the demo's taxprofiler stage
from completing end-to-end on *any* architecture.

## What's done (the port)

| Item | Status |
|------|--------|
| arm64 AMI w/ Kraken2 k2_pluspf DB | ✅ baked — `ami-0776208daf5785e85` (set as `AMI_ID_ARM64`) |
| `build_ami.py` arch-aware (c7g.4xlarge bake, arm64 spawn rpm, arm64 AMI) | ✅ committed `5d4ceb7` |
| `nextflow_config.py` all-Graviton + 6 aarchbio container overrides | ✅ committed `52019d5` |
| FETCH_FASTQ `volumeSize` 80→400 (disk-limit fix) | ✅ in `52019d5` |
| Benchmark harness `benchmark/diff_traces.py` + README | ✅ committed `965b0e2` |
| Container analysis (all 6 tools native arm64 on aarchbio) | ✅ verified; Kraken2 mull runtime-verified |

### Container coverage (taxprofiler 2.0.0 → quay.io/aarchbio)

| Tool | aarchbio tag | note |
|------|--------------|------|
| fastp | `0.24.0--h7dc49d2_1` | Wave-pinned upstream → per-process override |
| fastqc | `0.12.1--hdfd78af_0` | biocontainers |
| metaphlan | `4.1.1--pyhdfd78af_0` | biocontainers |
| kraken2 | `2.1.5--pl5321h1e84f2d_0` | **mull** (kraken2+pigz+coreutils), runtime-verified |
| multiqc | `1.32--pyhdfd78af_2` | Wave-pinned upstream → per-process override |
| krakentools | `1.2--pyh7e72e81_0` | biocontainers |
| KRAKEN2STANDARDREPORT | `ubuntu:20.04` (stock multi-arch) | nf-core/ubuntu:20.04 is amd64-only |

## What's blocked (the benchmark)

**[nf-spawn#37](https://github.com/spore-host/nf-spawn/issues/37):** nf-spawn's
task-staging script only does `aws s3 sync <task's-own-workDir>` and never runs
Nextflow's input localization (`nxf_stage`). So any stock nf-core module taking a
staged `path` input (e.g. taxprofiler FASTQC `input: path reads`) runs with its
inputs missing → dangling symlink → fails. The demo's custom FETCH_FASTQ only
works because it `aws s3 cp`s RODA itself, sidestepping staging.

This blocks the x86-vs-arm64 benchmark: the pipeline can't complete stage 2 on
x86 *or* arm64 until nf-spawn stages inputs.

### To resume the benchmark once nf-spawn#37 is fixed

1. Update the nf-spawn plugin version in `nextflow_config.py` + `build_ami.py`.
2. Run the **arm64 leg** (current committed config) — it *is* the diff-(b)
   rehearsal: `SAMPLE_COUNT=5 AWS_PROFILE=aws uv run python run_headless.py`,
   then `aws s3 cp s3://$BUCKET/results/$JOB_NAME/trace.tsv benchmark/results/arm64.tsv`.
3. For the **x86 leg**, temporarily revert to x86 instances + `{ami_id}` (no
   aarchbio overrides) — a clean all-x86 baseline — capture `x86.tsv`, then
   restore the committed arm64 config.
4. `python benchmark/diff_traces.py benchmark/results/x86.tsv benchmark/results/arm64.tsv --json benchmark/results/comparison.json --n 5`

Fairness controls (no t4g, matched c7i↔c7g / r7i↔r7g pairs, same accessions) are
documented in `benchmark/README.md`.

## Related bugs filed

- [aws-microbiome-demo#1](https://github.com/scttfrdmn/aws-microbiome-demo/issues/1)
  — stale `withName:'KRAKEN2_CLASSIFY'` selector (real process is `KRAKEN2_KRAKEN2`); no-op publishDir.
- [nf-spawn#37](https://github.com/spore-host/nf-spawn/issues/37) — input staging (the benchmark blocker).
