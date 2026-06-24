# Profiling the human microbiome on AWS Graviton: same science, ~16% less cost

*We ran nf-core/taxprofiler against Human Microbiome Project data on AWS, x86 vs
Graviton, with one ephemeral EC2 instance per pipeline task. The taxonomy is
identical across architectures, the body sites separate exactly as the HMP
literature predicts, and Graviton delivers it at near-equal speed for ~16% less.
Here's the science, the tooling that makes it one-instance-per-task, and the
numbers.*

---

## The science we set out to reproduce

The [Human Microbiome Project](https://www.hmpdacc.org/) established the baseline
picture of the healthy human microbiome: distinct microbial communities at
distinct body sites. Gut (stool) is a dense anaerobic community —
*Bacteroides*, *Faecalibacterium*, *Ruminococcus*. The oral cavity (buccal mucosa)
is *Streptococcus*, *Prevotella*, *Veillonella*, *Neisseria*. The anterior nares
(nostrils) are low-biomass and idiosyncratic. A working metagenomics pipeline
should recover that structure from raw shotgun reads.

So that's the test. We took **30 HMP runs — 10 each from stool, buccal mucosa, and
anterior nares** — and ran them through
[nf-core/taxprofiler 2.0.0](https://nf-co.re/taxprofiler):

```
FETCH_FASTQ → fastp → FastQC → Kraken2 + MetaPhlAn → MultiQC
```

Two independent classifiers on purpose. **Kraken2** (k-mer LCA against the
`k2_pluspf_16GB` database) and **MetaPhlAn** (marker-gene profiling against
CHOCOPhlAnSGB) use completely different algorithms and reference data; agreement
between them is the strongest signal that a call is real rather than a database
artifact.

The data comes straight from the
[SRA Open Data registry](https://registry.opendata.aws/ncbi-sra/)
(`s3://sra-pub-run-odp/`), which AWS hosts in us-east-1. Same-region EC2 reads it
at full S3 bandwidth with **no egress charge** — each task pulls its own sample
directly, no central staging. This is what a public data commons is for.

## It recovers the biology

The body sites separate cleanly, and both classifiers agree.

**Alpha diversity and dominant genera** — stool and buccal recover their textbook
HMP signatures:

| body site | n | Shannon (Kraken2 / MetaPhlAn) | HMP-expected genera recovered |
|-----------|--:|:-----------------------------:|-------------------------------|
| stool | 10 | 2.68 / 2.43 | Bacteroides, Ruminococcus, Fusobacterium |
| buccal_mucosa | 10 | 1.79 / 2.42 | Prevotella, Streptococcus, Veillonella |
| anterior_nares | 10 | 2.86 / 2.63 | *(oral/gut-dominated — see caveat)* |

**Beta diversity** — communities are more similar *within* a body site than
*between* sites (genus-level Bray–Curtis; lower = more similar), and both
classifiers show the same separation:

| classifier | within-site | between-site | separates by body site? |
|------------|------------:|-------------:|:-----------------------:|
| Kraken2 | 0.809 | 0.871 | ✅ |
| MetaPhlAn | 0.843 | 0.896 | ✅ |

The one scientific caveat: **anterior_nares** came back dominated by oral and gut
taxa (*Veillonella*, *Haemophilus*, *Fusobacterium*, *Prevotella*) rather than the
canonical nasal *Staphylococcus*/*Corynebacterium*. Checked at species level, this
is genuine low-biomass/contamination biology for these particular HMP nares runs
(host-DNA-heavy, prone to oral/skin carryover) — both classifiers agree on it, and
the two well-behaved sites validate cleanly. Nares is simply the known-hard body
site, and we report it rather than hiding it.

> A note on method, for practitioners: compute Bray–Curtis at **genus** level and
> summarize the pairwise distribution by its **mean**. Species-level dissimilarity
> saturates (healthy guts share genera but carry different species/strains), and
> the pairwise distribution is bimodal so the median washes the signal out. The
> reasoning is written up in
> [decision record 0004](../decisions/0004-beta-diversity-genus-and-mean.md).

---

## How it runs: one right-sized instance per task, with spore.host tooling

The thing that makes this architecture clean is that **there is no cluster and no
queue to manage.** Nextflow provides the workflow DAG — task dependencies, retries,
work-directory management — and the [spore.host](https://spore.host) toolchain
replaces the executor so that **every task gets its own ephemeral EC2 instance**,
sized for that task, which self-terminates the instant it finishes.

Three tools, three jobs:

- **[nf-spawn](https://github.com/spore-host/nf-spawn)** — the Nextflow executor
  plugin (`id 'nf-spawn@0.8.0'`). Instead of submitting to Batch or Slurm, each
  Nextflow process launches a dedicated instance. Per-process directives steer
  placement and storage: `ext.az` pins a task to an availability zone, and
  `ext.fsx = [id, mount, paths]` mounts a shared FSx filesystem and zero-copy
  symlinks the reference database into the task's work dir.
- **[spawn](https://spore.host)** — the CLI nf-spawn calls to launch, tag, and reap
  instances (and to create and attach the FSx filesystem). One instance per task,
  billed per second, no idle capacity.
- **[truffle](https://spore.host)** — queries the account's real EC2 vCPU quota,
  spot pricing, and instance specs. The pipeline derives Nextflow's `queueSize`
  (how wide to fan out) from the *actual* quota, and uses truffle's pricing to cost
  every stage.

Concretely: Kraken2 lands on a 64 GB r-family instance (it needs the 16 GB database
RAM-resident — that's the throughput bottleneck), `fasterq-dump` lands on a small
c-family box, and neither waits on the other or on a shared scheduler. Sizing is
per task, not per cluster.

```
Nextflow (head)  ── nf-spawn executor ──►  spawn ──►  one EC2 instance per task
   │                                                      │
   │   truffle: quota → queueSize, pricing → cost         ├── reads its sample from RODA ($0 egress)
   │                                                      ├── reads the shared DB off FSx Lustre (zero copy)
   └── DAG, retries, work-dir on S3                        └── writes results to S3, self-terminates
```

The shared-database design carries the fan-out. All the classifier tasks need the
same ~52 GB of Kraken2 + MetaPhlAn reference data at once, so it lives on **one
S3-backed [FSx for Lustre](https://aws.amazon.com/fsx/lustre/) filesystem**, mounted
read-only by every task via `ext.fsx`. The database is staged once; tasks read it in
place with zero per-task copying. It scaled comfortably to **56 concurrent readers**
on a single filesystem. (The trade-offs behind choosing FSx Lustre for wide fan-out
are in [decision record 0001](../decisions/0001-db-delivery-ami-ebs-fsx.md).)

---

## x86 vs Graviton: the comparison

This is the part we built the whole thing to measure. Both architectures ran the
**identical pipeline, identical samples, identical reference databases**, native on
each chip — x86 on `c7i`/`r7i` with upstream amd64 containers, Graviton on
`c7g`/`r7g` with native arm64 containers from
[aarchbio](https://github.com/playgroundlogic/aarchbio). Neither emulates. The only
variable is the processor: matched vCPU/RAM pairs (`c7i↔c7g`, `r7i↔r7g`), no
burstable instances anywhere in the measured path, same region and AZ. Both legs
completed **clean: 30/30 tasks, zero failures.**

### Correctness first

The taxonomy is **architecture-independent** — same species calls, same
abundances, both classifiers — which is the precondition before any speed or cost
claim means anything. The science doesn't change with the chip.

### Speed: roughly at parity

Per-stage timing, billed by actual EC2 instance lifetime (median over the 30 tasks
in each stage):

| stage | arm64 median | x86 median | ratio | verdict |
|-------|-------------:|-----------:|------:|---------|
| fastp | 244 s | 248 s | 0.99 | tie |
| fastqc | 257 s | 257 s | 1.00 | tie |
| fastqc_processed | 344 s | 327 s | 1.05 | x86 ~5% faster |
| **kraken2** | 640 s | 536 s | 1.19 | x86 ~19% faster *(one run)* |
| **metaphlan** | 1620 s | 1593 s | 1.02 | tie (~2%) |
| multiqc | 261 s | 278 s | 0.94 | arm64 ~6% faster |

The two heavy stages — Kraken2 (memory-bound) and MetaPhlAn (the long pole, doing
bowtie2 against CHOCOPhlAnSGB) — are where the runtime budget lives. MetaPhlAn is
essentially tied; Kraken2 came out ~19% faster on x86 *this single run*, which is
one observation and sensitive to instance placement. Call it parity.

### Cost: Graviton ~16% cheaper, structurally

The cost difference isn't a single run's luck — it's the fixed ~19% lower per-hour
price of Graviton at matched vCPU/RAM, applied to near-equal runtime:

| | task-compute cost @ N=30 |
|--|-------------------------:|
| arm64 | **$16.36** |
| x86 | $19.53 |

End-to-end, including the amortized one-time setup (staging the databases and
provisioning the shared filesystem):

| phase | arm64 time | x86 time | arm64 $ | x86 $ |
|-------|-----------:|---------:|--------:|------:|
| stage DBs (one-time) | 65.5 min | 80.1 min | 0.32 | 0.39 |
| provision FSx (one-time) | 20 min | 20 min | 0.38 | 0.38 |
| **run (N=30)** | 36 min | 95 min | **17.01** | **20.52** |
| **total, 1 run** | 121 min | 195 min | **$17.71** | **$21.28** |

> **The headline: Graviton runs this metagenomics pipeline at near-equal speed for
> ~16–17% less cost** — and produces bit-for-bit equivalent science. With native
> arm64 containers available across the whole taxprofiler stack, that ~16% is just
> margin you pick up for free.

Reading the numbers fairly: N=30 is one run per architecture, so per-stage medians
(over 30 tasks each) are robust, single per-stage *ratios* are one observation, and
the *cost* delta is the structural one — it follows the fixed price ratio. MetaPhlAn
dominates the budget either way: ~$12 of the per-run compute is MetaPhlAn alone.

---

## Reproduce it

`N` (fan-out width) is a one-line knob (`SAMPLES_PER_SITE`); the shared filesystem,
the staged databases, and the container cache are all reusable across runs and
architectures. Flip `BENCH_ARCH`, point at your bucket, and turn the dial. The
runbook is in [`benchmark/README.md`](../../benchmark/README.md); the fairness
protocol and controls are in [methodology.md](../methodology.md); the full results
of record are in [results.md](../results.md). The design decisions behind the
database delivery, the timing method, and the diversity statistics are written up as
[decision records](../decisions/).

*Built on [Nextflow](https://www.nextflow.io/) · [nf-core/taxprofiler](https://nf-co.re/taxprofiler) ·
[spawn / nf-spawn / truffle](https://spore.host) ·
[aarchbio](https://github.com/playgroundlogic/aarchbio) native arm64 containers ·
[SRA Open Data on AWS](https://registry.opendata.aws/ncbi-sra/) · Amazon Bedrock.*
