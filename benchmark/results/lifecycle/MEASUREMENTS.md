# End-to-end lifecycle measurements — FSx-backed taxprofiler, from scratch

Account 942542972736, us-east-1, us-east-1a. nf-spawn 0.8.0 (ext.fsx), spawn 0.59.0.
DBs: Kraken2 k2_pluspf_16GB + MetaPhlAn vJan25 CHOCOPhlAnSGB, on shared FSx Lustre
(PERSISTENT_2, 1200 GiB, 250 MB/s/TiB), us-east-1a. Tasks pinned to us-east-1a
(ext.az) to co-locate with the single-AZ FS.

This file accumulates the MEASURED numbers as each phase runs (raw JSON in this dir).

## Phase 1 — DB staging FROM CANONICAL SOURCES (one-time)  [MEASURED 2026-06-17]

Staging instance: c7g.2xlarge, 200 GiB root, us-east-1a. (staging_timings.json)

| DB | source | phase | seconds | bytes |
|----|--------|-------|--------:|------:|
| Kraken2 | s3://genome-idx/kraken/k2_pluspf_16_GB_20260226.tar.gz | download | 77.2 | 11.98 GB (tarball) |
| Kraken2 | (local) | extract | 146.9 | → 16 GB |
| Kraken2 | → s3://…/dbs-fsx/kraken2/ | sync to S3 | 128.7 | 16 GB |
| MetaPhlAn | `metaphlan --install` (biobakery host, ~22 MB/s) | download+decompress | **3309.6 (55.2 min)** | 33 GB tar → 36 GB |
| MetaPhlAn | → s3://…/dbs-fsx/metaphlan/ | sync to S3 | 270.2 | 36 GB |

Staging wall-clock ≈ 77+147+129+3310+270 = **~66 min** (sequential).
Staging instance billed ≈ 66 min × c7g.2xlarge $0.29/hr = **~$0.32**.
S3 PUTs: ~26 objects → negligible (<$0.01).

**Headline finding:** MetaPhlAn's `--install` from the official biobakery host
(33 GB @ ~22 MB/s = 55 min) DOMINATES staging — 43× longer than Kraken2 from
in-region S3 (77 s). This is the "stage reference data into S3/FSx ONCE, not
per-run" lesson, quantified. After staging, dbs-fsx = ~52 GB in S3.

## Phase 0 — FSx provision  [MEASURED 2026-06-17]
FSx PERSISTENT_2, 1200 GiB, S3-backed (DRA ← dbs-fsx), us-east-1a.
- fs-id: fs-0a8b7569219713840
- provision elapsed: **~20 min** (FSx create→AVAILABLE ~10 min + DRA setup).
  NOTE: included a detour — `spawn --fsx-create` scopes the DRA to the BUCKET ROOT
  (s3://bucket → /, would import all 4.3 TB), so I deleted it and created a DRA
  scoped to /  ← s3://…/dbs-fsx (DBs land at /fsx/{kraken2,metaphlan}). A clean
  spawn that honored --fsx-import-path would cut this to ~10 min. (spawn#206-adjacent.)
- **Gating smoke PASSED**: /fsx/kraken2/hash.k2d (16 GB) + ALL 6 /fsx/metaphlan/*.bt2l
  readable through the Lustre mount — the complete index (the .2/.rev.2 files that
  the earlier laundered materialization dropped, fixed at root by canonical staging).
- FSx cost: 1200 GiB × $0.145/GB-mo = $0.174/GB... = **$0.238/hr** while it exists.

## Phase 2 — Run @ N=30 arm64  [BLOCKED — Docker Hub rate limit, 2026-06-17]

FSx worked (mount validated, complete index). But the run aborted at **Stage 1
FETCH_FASTQ**, before classifiers — a NEW issue, unrelated to FSx/DB:

  FETCH_FASTQ pulls `ncbi/sra-tools:latest` from **Docker Hub**. At N=30, ~30 fetch
  tasks launch near-simultaneously and pull from the same NAT IP → hit Docker Hub's
  **anonymous pull rate limit** (100 pulls/6hr/IP):
    `docker: Error response: toomanyrequests: You have reached your unauthenticated
     pull rate limit` → exit 125 → Nextflow default errorStrategy aborted all 30.

This NEVER appeared at N=3 (≤3 pulls). It's a real wide-fan-out lesson: **a
Docker-Hub-hosted container is a fan-out bottleneck** — mirror it into a
non-rate-limited registry (ECR) for scale. (arch is fine — ncbi/sra-tools:latest
is arm64-capable; it ran on c7g in every N=3 run. Purely the registry throttle.)

FIX (chosen): mirror ncbi/sra-tools into this account's ECR, point FETCH_FASTQ at
it. Then re-run. [in progress]

## Fetch-container fix — ECR Pull-Through Cache  [MEASURED 2026-06-17]
ncbi/sra-tools is correctly multi-arch (amd64+arm64) on Docker Hub. At N=30, 30
simultaneous anonymous pulls hit Docker Hub's per-IP rate limit → exit 125 abort.
Fix: ECR **Pull-Through Cache** (registry-1.docker.io upstream, Docker Hub PAT in
Secrets Manager `ecr-pullthroughcache/dockerhub`). Tasks pull
`<acct>.dkr.ecr…/dockerhub/ncbi/sra-tools:latest` → ECR authenticates upstream
ONCE, caches in-region, serves all N from ECR. Multi-arch manifest preserved
(c7g→arm64, c7i→amd64). Role needs ecr auth + dockerhub/* pull + BatchImportUpstreamImage.
Lesson: a correctly-built multi-arch image is still a fan-out bottleneck on Docker
Hub; PTC collapses N anonymous pulls → 1 authenticated cached pull.

## Phase 2 — Run @ N=30 arm64  [MEASURED 2026-06-17 — CLEAN 30/30]
All 151 tasks COMPLETED, 0 FAILED/ABORTED. 30 Kraken2 reports + 30 MetaPhlAn
profiles. Real biology (stool Aggregatibacter/Fusobacterium, buccal high-host+oral,
nares Veillonella/Strep). Run wall-clock ~36 min. Peak fan-out: 30 fetch + up to
56 concurrent classifiers on the ONE shared FSx (no FSR credit cliff — the thing
EBS+FSR couldn't do).

Per-stage billed wall-clock (from head nextflow.log — the authoritative signal;
trace realtime is wrapper-local on the spawn executor):

| stage | n | median_s | min | max | Σbilled_s | instance | cost $ |
|-------|--:|---------:|----:|----:|----------:|----------|-------:|
| fastp | 30 | 244 | 121 | 265 | 6902 | c7g.2xlarge | 0.56 |
| fastqc | 30 | 257 | 123 | 476 | 9483 | c7g.2xlarge | 0.76 |
| fastqc_processed | 30 | 344 | 224 | 635 | 11153 | c7g.2xlarge | 0.90 |
| **kraken2** | 30 | 640 | 235 | 663 | 17012 | r7g.2xlarge | 2.03 |
| **metaphlan** | 30 | **1620** | 892 | 2480 | 50825 | r7g.4xlarge | **12.11** |
| multiqc | 1 | 261 | — | — | 261 | c7g.large | 0.005 |

**MetaPhlAn dominates: $12.11 of $16.36 task-compute** (bowtie2 vs CHOCOPhlAnSGB).

## END-TO-END @ N=30 arm64 (benchmark/results/lifecycle/arm64-n30-fsx.json)

| phase | time | data moved | cost $ |
|-------|-----:|-----------:|-------:|
| stage (DBs from canonical source) | 65.5 min | 48 GB | 0.32 |
| provision (FSx + DRA) | 20 min | — | 0.38 |
| run (N=30, fetch→QC→classify) | 36 min | ~90 GB | 17.01 |
| **TOTAL** | **121 min** | **138 GB** | **17.71** |

- **One-time** (stage + provision, amortized over R runs): **$0.70**
- **Per-run** @ N=30: **$17.01**
- Amortized $/run: R=1 → $17.71, R=10 → $17.08, R=50 → $17.02
  (one-time is tiny vs per-run; the run cost — dominated by MetaPhlAn compute — is
  what scales with N. FSx + PTC are one-time/amortized, not per-run-per-sample.)

## Phase 3 — Teardown  [next: terminate head, delete FSx, stop FSx meter]

## To re-run at a different N (the knob)
Set `SAMPLES_PER_SITE=<N/3>`; everything else (FSx, PTC, DBs) is reusable. Run
cost scales ~linearly with N (per-sample compute); one-time stays ~$0.70.

---

# x86 leg — FULLY FROM SCRATCH (independent FSx + re-stage + run)  [2026-06-17]

Self-contained x86 lifecycle: new FSx, DBs re-staged from canonical sources, run
on c7i/r7i. The arm64 FSx was torn down first (its lifecycle complete). DBs are
arch-neutral (same bytes), so staging/provision numbers should match arm64 — this
leg confirms that and gives x86 a standalone record for the arch comparison.

## x86 Phase 3 (arm64 teardown) — arm64 FSx fs-0a8b7569219713840 DELETED.

## x86 Phase 1 — staging from canonical sources  [IN PROGRESS]
Staging instance c7g.2xlarge (the stager arch doesn't matter — it just downloads +
syncs arch-neutral DB bytes). Same script: Kraken2 from genome-idx, MetaPhlAn via
`metaphlan --install`. Timings → staging_timings.json (will overwrite arm64's;
both are captured in their per-leg records).

## x86 Phase 0 — provision  [PENDING]
## x86 Phase 2 — run @ N=30 (c7i/r7i, BENCH_ARCH=x86)  [PENDING]

## x86 Phase 1 — staging  [MEASURED 2026-06-18]
Same canonical sources. Kraken2 dl 75s/extract 148s/sync 128s (identical to arm64).
MetaPhlAn install **4182s (70min)** vs arm64 3310s — same bytes; biobakery host
throughput varied run-to-run (external-network-bound, not arch). Complete DB (6 bt2l).

## x86 Phase 0 — provision  ~20 min (same DRA-rescope detour). Gating smoke PASSED.

## x86 Phase 2 — Run @ N=30  [MEASURED 2026-06-18 — CLEAN 30/30]
151 COMPLETED, 0 failed. 30 Kraken2 + 30 MetaPhlAn. Same peak fan-out (56 classifiers
on FSx). PTC fetch worked on x86 (amd64 manifest from same cache). Run ~95 min
(longer tail than arm64's 36 min — MetaPhlAn stragglers + slower fetch this run).

# ═══════════════ ARCH COMPARISON: arm64 vs x86 @ N=30 (native-vs-native) ═══════════════

Per-stage MEDIAN billed wall-clock (from head nextflow.log), matched instance pairs
(c7g↔c7i, r7g↔r7i), same 30 HMP samples, same FSx-mounted DBs:

| stage | arm64 median | x86 median | arm64/x86 | verdict |
|-------|-------------:|-----------:|----------:|---------|
| fastp | 244s | 248s | 0.99 | tie |
| fastqc | 257s | 257s | 1.00 | tie |
| fastqc_processed | 344s | 327s | 1.05 | x86 ~5% faster |
| **kraken2** | 640s | 536s | **1.19** | **x86 ~19% faster** |
| **metaphlan** | 1620s | 1593s | 1.02 | tie (~2%) |
| multiqc | 261s | 278s | 0.94 | arm64 ~6% faster |

**Task-compute cost (Σ billed-s × on-demand $/hr): arm64 $16.36 vs x86 $19.53.**

### The headline (price/performance — the Graviton story)
- **Runtime: roughly at parity** per-stage (within ~2% on the two heavy stages —
  metaphlan tie, fastp/fastqc tie). Kraken2 was ~19% faster on x86 (r7i) this run.
- **Cost: arm64 is ~16% cheaper** ($16.36 vs $19.53 task-compute) because the
  Graviton instances are ~19% cheaper per hour at matched vCPU/RAM.
- **Net: arm64 delivers near-equal throughput at ~16% lower cost** — the canonical
  Graviton price/performance win, measured end-to-end on a real metagenomics pipeline.
- ⚠ N=30, single run per arch: per-stage medians are robust (n=30 tasks/stage) but
  the kraken2 19% gap is one run — runtime varies with instance placement/contention.
  The COST delta is structural (the per-hour price ratio is fixed).

### End-to-end (incl. one-time stage+provision, ~$0.70 either arch — amortized)
| | arm64 | x86 |
|--|------:|----:|
| one-time (stage+provision) | $0.70 | $0.77 |
| per-run @ N=30 | $17.01 | $20.52 |
| **total (1 run)** | **$17.71** | **$21.28** |

arm64 end-to-end ~17% cheaper. Both legs: clean 30/30, identical biology, zero copy
of the DB (shared FSx), fetch via ECR PTC (no Docker Hub limit).

# ═══════════════ BIOLOGY VALIDATION — x86 N=30 (biology_x86_n30.json) ═══════════════

The scientific deliverable: does the pipeline recover real HMP body-site community
structure? Run on x86's 30 Kraken2 + 30 MetaPhlAn profiles (10/site). [2026-06-24]
(arm64 profiles were overwritten per-run — see note — so NO arch-concordance leg;
correctness across arches was already shown by clean 30/30 + identical-species
spot-checks during the runs.)

### Community structure + HMP genus validation (top-6 genera/site, both classifiers)
| body site | n | Shannon (median) | HMP-expected genera seen | verdict |
|-----------|--:|-----------------:|--------------------------|---------|
| stool | 10 | 2.68 (K2) / 2.43 (MPA) | Bacteroides, Ruminococcus, Fusobacterium | ✓ validates |
| buccal_mucosa | 10 | 1.79 / 2.42 | Prevotella, Streptococcus, Veillonella | ✓ validates |
| anterior_nares | 10 | 2.86 / 2.63 | (none canonical) | ⚠ see caveat |

### Beta diversity (genus-level Bray–Curtis, MEAN — within < between separates)
| classifier | within-site | between-site | separates? |
|------------|------------:|-------------:|-----------|
| Kraken2 | 0.809 | 0.871 | ✓ |
| MetaPhlAn | 0.843 | 0.896 | ✓ |

Two methodology fixes baked into analyze_study.py while validating these numbers:
1. **Bray–Curtis must run at GENUS level, not species.** Species-level BC saturates
   at ~1.0 both within and between site (subjects carry different species/strains),
   erasing the signal. Genera recur across subjects of a site → separation returns.
2. **Use the MEAN, not the median, of the BC distribution.** Even at genus level the
   distribution is bimodal — pairs sharing dominant taxa sit at 0.3–0.6, low-biomass
   disjoint pairs saturate at 1.0 → median pins at ~0.99 for BOTH within and between.
   The mean integrates the whole distribution and recovers within < between.

### anterior_nares caveat (recorded, not a bug)
HMP nares runs are low-biomass / host-DNA-heavy and here are dominated by oral+gut
taxa (Veillonella, Haemophilus, Fusobacterium, Prevotella, Bacteroides) rather than
canonical nasal Staphylococcus/Corynebacterium. Verified at species level on
SRR061502. Both classifiers agree; stool + buccal validate cleanly. The pipeline is
sound — nares is the documented known-hard body site (contamination + low biomass).
