> ⚠️ **SUPERSEDED — archived for provenance.** This is the N=3 EBS+FSR pilot.
> It pre-dates both the FSx pivot (EBS+FSR couldn't scale past the FSR credit
> cliff) and the instance-lifetime timing fix. The results of record are the
> N=30 FSx lifecycle in [`../lifecycle/MEASUREMENTS.md`](../lifecycle/MEASUREMENTS.md).
> See [`README.md`](README.md) in this dir for the full archive index.

# x86-vs-arm64 benchmark — reproducible clean legs, 2026-06-16

Both legs ran the **same** nf-core/taxprofiler 2.0.0 pipeline on the **same** 3 HMP
stool samples (SRR059375/6/7), **same** FSR-warmed DB EBS volumes in us-east-1a,
differing **only** in architecture: arm64 (c7g/r7g + aarchbio native arm64
containers) vs x86 (c7i/r7i + upstream amd64 containers). Neither emulated.

Reproducible from committed code: released **nf-spawn 0.7.0** (`ext.az`, #62) +
the **s3:// db_path marker** zero-copy DB delivery (the demo-side fix for the #65
deadlock) + FSR-in-us-east-1a. Flip `BENCH_ARCH`, enable FSR, run.

## What is solid

- **Both legs completed all 16 tasks, exit 0**, with **biologically identical
  output**: *Fusobacterium pseudoperiodonticum* 12.18% on stool, Kraken2 + MetaPhlAn
  agree across arch. The Graviton port is correct end-to-end.
- **Zero DB copy anywhere**: db_path is an s3:// marker (no head foreign-copy);
  each task symlinks the staged input to its mounted EBS volume (nf-spawn #55).
  Markers auto-cleaned after each run. The 56 GB of DBs never move.
- **FSR + AZ-pinning eliminates the lazy-load**: every DB volume
  `FastRestored=true` (tasks pinned to us-east-1a via `ext.az`), and Kraken2 went
  from ~40 min (un-FSR lazy-load) to **done in the first minutes of dispatch**.
- **Run-level wall-clock** (headless poller, includes ~10 min constant head boot):
  - **arm64: 26.5 min · x86: 26.5 min** (3 samples each) — at N=3 the totals are
    dominated by the shared constant (head boot + RODA fetch + the MetaPhlAn long
    pole); the arch difference does not separate at the run level for this tiny N.

Prior preliminary legs (pre-release build, before the s3:// db_path fix) reported
arm64 32 / x86 45 min — superseded by these reproducible 0.7.0 legs.

## What this run CANNOT support (instrumentation gap)

**Per-stage arch timing is not trustworthy from these artifacts.** On the spawn
executor, Nextflow's trace `realtime`/`duration` are **wrapper-local** (sub-second
for every task, even ones that ran 40+ min remotely), and the per-stage
start→complete *envelope* collapses for the classifiers because all 3 tasks
finalize near-simultaneously on the head while the real multi-minute compute ran
on separate, now-terminated instances. `diff_traces.py` reads those columns, so
running it here would report fiction.

The correct per-stage signal is **EC2 instance billed lifetime** (launch→terminate
per task) — which the demo does not yet record and which can't be retrieved after
the instances age out of `describe-instances`.

### Fix before the next timed run (the real next step)
Capture each task instance's `LaunchTime`→termination from its own metadata (or
poll `describe-instances` into a per-task record while the run is live) and emit it
alongside the staging timings. Then `diff_traces.py` should consume **instance
lifetime**, not the trace realtime column, for per-stage cost+time. See
[[benchmark-timing-on-spawn-executor]].

## Honest bottom line

The architecture port and the volume-backed-DB delivery path are **validated and
correct on both arches**, and the run-level numbers point the expected direction
(arm64 finished the 3-sample pipeline faster here, driven by MetaPhlAn). A
defensible *per-stage* price/performance ratio needs the instance-lifetime
instrumentation above — that's the gating work, not another run on the current
(timing-blind) harness.

## Provenance
- arm64: tools-AMI ami-05d4b3a43247af4b2, aarchbio containers, 32 min, all COMPLETED
- x86: tools-AMI ami-0131c2ccf8412e980, upstream amd64 containers, 45 min, all COMPLETED
- nf-spawn: ext.az shipped in released **0.7.0** (resolution of nf-spawn#62); the
  demo pins 0.7.0. These preliminary legs used a pre-release build; the reproducible
  run waits on an upstream release). spawn head CLI 0.52.0
- DB snapshots snap-05068c70e7ccf7974 (Kraken2) + snap-0463b9471b52ae203 (MetaPhlAn),
  FSR enabled in us-east-1a for both legs, **disabled after** (no standing FSR cost).
- Traces: `benchmark/results/{arm64,x86}/*.tsv`; staging: `benchmark/results/{arm64,x86}-staging/`.
- Un-FSR baseline (first run, lazy-load-dominated): `benchmark/results/arm64-unfsr/`.
