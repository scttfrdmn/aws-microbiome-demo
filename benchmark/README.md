# Demo Graviton benchmark — x86 vs arm64 (real EC2, nf-core/taxprofiler)

The authentic head-to-head this demo was built to enable: run the **same
pipeline, same samples, same data** on x86 and on Graviton, measure **both**
per-stage wall-clock and dollars, and present the real ratio — not a price
projection. Protocol of record: aarchbio `benchmark/demo-graviton-protocol.md`.

Both legs are **native** — x86 runs native amd64 containers, arm64 runs
[aarchbio](https://github.com/playgroundlogic/aarchbio) native arm64 containers.
Neither emulates. This measures native-vs-native price/performance, the honest
number; the emulation tax is a separate, already-known story.

## Fairness controls (only architecture may differ)

- **Same samples** — `HMP_ACCESSIONS[:5]` is deterministic; both legs use the
  identical 5 accessions.
- **Same instance spec, differ only in family** — `c7i↔c7g`, `r7i↔r7g` at
  identical vCPU/RAM. Never `c7i.2xlarge` vs `c7g.4xlarge` (that confounds arch
  with size).
- **NO burstable (t-family) instances anywhere in the measured path.** t4g/t3 are
  credit-based + shared-core — the same workload varies ~2× with credit state, so
  you'd measure credit balance, not architecture. Every measured stage and the
  head node run on a fixed-performance family (see the instance table below).
- **Same region/AZ** (us-east-1) — same RODA locality.
- **Same pipeline version** — both resolve nf-core/taxprofiler 2.0.0.

## Instance pairs (the controlled variable)

| Stage (taxprofiler 2.0.0)        | label          | x86 leg      | arm64 leg    |
|----------------------------------|----------------|--------------|--------------|
| FETCH_FASTQ                      | process_single | c7i.large    | c7g.large    |
| fastp / FastQC / MetaPhlAn       | process_medium | c7i.2xlarge  | c7g.2xlarge  |
| Kraken2                          | process_high   | r7i.2xlarge  | r7g.2xlarge  |
| MultiQC / krakentools / std-rep  | process_single | c7i.large    | c7g.large    |
| **head node** (Nextflow only)    | —              | c7i.large    | c7g.large    |

FETCH_FASTQ's container is `ncbi/sra-tools` (multi-arch upstream, not aarchbio) —
it's measured and reported, but it isn't part of the "aarchbio unblocks it" claim.

## Procedure

1. **x86 baseline** — on the pre-diff-(b) config (all x86), `SAMPLE_COUNT=5`.
   Set `HEAD_INSTANCE_TYPE = "c7i.large"` (config.py default). Run the demo, then
   pull the trace:
   ```
   SAMPLE_COUNT=5 AWS_PROFILE=aws uv run python run_headless.py
   aws s3 cp s3://$BUCKET/results/$JOB_NAME/trace.tsv benchmark/results/x86.tsv
   ```
2. **arm64** — apply diff (b) to `nextflow_config.py`, set
   `HEAD_INSTANCE_TYPE = "c7g.large"`, set `AMI_ID_ARM64` to the rebaked AMI, then:
   ```
   SAMPLE_COUNT=5 AWS_PROFILE=aws uv run python run_headless.py
   aws s3 cp s3://$BUCKET/results/$JOB_NAME/trace.tsv benchmark/results/arm64.tsv
   ```
   (This run *is* the diff-(b) rehearsal — it exercises every step native: light
   steps on the rebaked AMI, the Kraken2 mull doing real classification, and the
   `ubuntu:20.04` override for the standard-report step.)
3. **Diff** the two traces:
   ```
   python benchmark/diff_traces.py benchmark/results/x86.tsv benchmark/results/arm64.tsv \
       --json benchmark/results/comparison.json --n 5
   ```

## Honesty requirements (baked into diff_traces.py)

- **N=5 is a pilot, not a census** — printed on every report; variance is real.
- **Per-stage, not just total** — Kraken2 (memory-bound) is the swing factor we
  can't predict from price alone.
- **Negative results stay in** — a stage slower on arm64 is flagged `⚠ arm64
  SLOWER`, never dropped.
- **Failed tasks invalidate a comparison** — any nonzero exit on either leg flags
  `✗ FAILED TASKS`; a "faster" run that silently failed a step isn't faster.
- **Price/hr ratio is fixed (~19%); the runtime ratio is what's measured** —
  `$/run = price/hr × measured duration`. The two are reported separately.
- **Median + min–max, never a mean or single number.**
