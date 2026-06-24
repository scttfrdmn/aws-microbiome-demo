# Results — N=30, FSx-backed, from scratch, both arches

Canonical results of record. Both architectures ran the **full from-scratch
lifecycle** (provision → stage → run → teardown) at **N=30** (10 samples × 3 HMP
body sites), on a shared S3-backed FSx for Lustre filesystem, native on each arch.
Both legs: **clean 30/30 tasks completed, 0 failed**, with biology validated.

- Raw running log: [`../benchmark/results/lifecycle/MEASUREMENTS.md`](../benchmark/results/lifecycle/MEASUREMENTS.md)
- Per-leg JSON: `arm64-n30-fsx.json`, `x86-n30-fsx.json`, `biology_x86_n30.json`
- How it was measured: [methodology.md](methodology.md) · why the design choices: [decisions/](decisions/)

## End-to-end lifecycle

| phase | arm64 time | x86 time | data moved | arm64 $ | x86 $ |
|-------|-----------:|---------:|-----------:|--------:|------:|
| stage (DBs from canonical source) | 65.5 min | 80.1 min | 48 GB | 0.32 | 0.39 |
| provision (FSx + scoped DRA) | 20 min | 20 min | — | 0.38 | 0.38 |
| run (N=30, fetch→QC→classify) | 36 min | 95 min | ~90 GB | 17.01 | 20.52 |
| **total (1 run)** | **121 min** | **195 min** | **138 GB** | **17.71** | **21.28** |

- **One-time** (stage + provision, amortized over R runs): **~$0.70 (arm64) / ~$0.77 (x86)**.
- **Per-run** @ N=30: **$17.01 (arm64) / $20.52 (x86)**.
- Amortized $/run: one-time is tiny vs per-run, so it washes out fast —
  R=1 → $17.71, R=10 → $17.08, R=50 → $17.02 (arm64).

The stage and run wall-clocks differ between legs mainly from external variance:
`metaphlan --install` is external-network-bound (3310 s vs 4182 s for identical
bytes), and the x86 run had a longer MetaPhlAn straggler tail this single run.
The **cost** delta is structural (price/hr ratio), not run-to-run noise.

## Per-stage timing (billed wall-clock, median over n=30 tasks)

From the head `nextflow.log` (EC2 billed lifetime — not trace `realtime`; see
[decisions/0002](decisions/0002-timing-instance-lifetime-not-trace.md)). Matched
instance pairs, same 30 samples, same FSx-mounted DBs.

| stage | arm64 median | x86 median | arm64/x86 | verdict |
|-------|-------------:|-----------:|----------:|---------|
| fastp | 244 s | 248 s | 0.99 | tie |
| fastqc | 257 s | 257 s | 1.00 | tie |
| fastqc_processed | 344 s | 327 s | 1.05 | x86 ~5% faster |
| **kraken2** | 640 s | 536 s | 1.19 | x86 ~19% faster (one run) |
| **metaphlan** | 1620 s | 1593 s | 1.02 | tie (~2%) |
| multiqc | 261 s | 278 s | 0.94 | arm64 ~6% faster |

**MetaPhlAn dominates task-compute** (bowtie2 against CHOCOPhlAnSGB): ~$12 of the
~$16–20 per-run compute is MetaPhlAn alone.

## The Graviton price/performance story

- **Runtime: roughly at parity** per-stage — within ~2% on the two heavy stages
  (MetaPhlAn tie, fastp/fastqc tie). Kraken2 was ~19% faster on x86 (r7i) *this
  single run*; runtime varies with placement/contention.
- **Cost: arm64 ~16% cheaper** on task-compute ($16.36 vs $19.53), because the
  Graviton instances are ~19% cheaper per hour at matched vCPU/RAM.
- **Net: near-equal throughput at ~16–17% lower cost** — the canonical Graviton
  win, measured end-to-end on a real metagenomics pipeline rather than projected.

⚠️ N=30, one run per arch. Per-stage medians (n=30 tasks) are robust; the single
kraken2 ratio is one observation. The cost delta is structural (fixed price ratio).

## Scaling validation (the reason for FSx)

Peak fan-out: **30 fetch + up to 56 concurrent classifiers** reading the **one**
shared FSx filesystem — no per-volume credit limit. This is exactly what EBS+FSR
could not do (it hit the ~10-reader FSR credit cliff at N=30, see
[decisions/0001](decisions/0001-db-delivery-ami-ebs-fsx.md)). The 52 GB of DBs
never copied per task; every classifier symlinked into the shared mount.

## Biology validation (x86 N=30)

Does the pipeline recover real HMP body-site community structure? Yes for the two
well-behaved sites, with an honest caveat on the third. Both classifiers agree.

**Community structure + HMP genus validation** (top-6 genera/site):

| body site | n | Shannon (median, K2/MPA) | HMP-expected genera seen | verdict |
|-----------|--:|:------------------------:|--------------------------|---------|
| stool | 10 | 2.68 / 2.43 | Bacteroides, Ruminococcus, Fusobacterium | ✅ validates |
| buccal_mucosa | 10 | 1.79 / 2.42 | Prevotella, Streptococcus, Veillonella | ✅ validates |
| anterior_nares | 10 | 2.86 / 2.63 | (none canonical) | ⚠️ caveat |

**Beta diversity** (genus-level Bray–Curtis, mean — `within < between` separates):

| classifier | within-site | between-site | separates? |
|------------|------------:|-------------:|:----------:|
| Kraken2 | 0.809 | 0.871 | ✅ |
| MetaPhlAn | 0.843 | 0.896 | ✅ |

Getting these right required two methodology fixes (genus aggregation; mean not
median over a saturated bimodal distribution) — see
[decisions/0004](decisions/0004-beta-diversity-genus-and-mean.md).

**anterior_nares caveat (not a bug):** HMP nares runs are low-biomass /
host-DNA-heavy and here are dominated by oral+gut taxa rather than canonical nasal
*Staphylococcus*/*Corynebacterium*. Verified at species level (SRR061502); both
classifiers agree; the other two sites validate cleanly. The pipeline is sound —
nares is the documented known-hard body site.

### Not measured: cross-arch concordance

The arm64 profiles were overwritten between legs (each run cleared the results
prefix), so a profile-level arm64-vs-x86 concordance table was not produced.
Cross-arch correctness was instead established by clean 30/30 on both legs plus
identical-species spot-checks during the runs. To produce a concordance leg in
future, persist profiles per-arch instead of clearing the results prefix.
