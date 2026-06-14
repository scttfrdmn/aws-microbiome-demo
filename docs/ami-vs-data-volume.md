# Design note: how task instances get the Kraken2 DB (AMI vs. data volume)

Status: design discussion, not implemented. Captures the tradeoffs and a
recommended direction for when this is revisited.

## The problem this addresses

Each pipeline task runs on its own ephemeral EC2 instance (via spawn / nf-spawn),
and several tasks fan out ~100-wide per run. The genuinely heavy, slow-to-provision
thing those instances need is the **Kraken2 `k2_pluspf` database — ~11.9 GB
compressed, ~16 GB extracted**. Everything else a task needs (Nextflow, Docker,
the spawn CLI, the nf-spawn plugin, the nf-core/taxprofiler pipeline) is small and
fast to install or is already pulled at runtime.

Today (`build_ami.py`) we bake **one custom AMI** that welds together three things:
1. the OS (AL2023, per-arch),
2. the tools/pipeline, and
3. the 16 GB Kraken2 DB on the root volume.

`config.py` then sets `VOLUME_SIZE = 40` and the `process_single` (FETCH_FASTQ)
block uses `volumeSize = 400` (fasterq-dump scratch for ~50 GB SRAs). The custom
AMI is built and snapshotted via `spawn launch` + `spawn ami create`.

## Why the current AMI approach is defensible

- **Repeated fan-out amortizes the bake.** One-time ~20-30 min + ~$2-3 bake, then
  every task across every run skips the ~12 GB DB fetch + tool install. For a
  100-instance-per-run, run-repeatedly workload with a stable DB, this is the
  AMI's sweet spot.
- Fastest possible cold start for a Kraken2 task — the DB is already on the
  root volume at boot.

## Why it's awkward (the friction we actually hit)

The custom AMI does **two jobs welded together** — "carry a 16 GB data blob" and
"be the machine image" — and the coupling caused real pain:
- An **arch split**: separate x86 and arm64 AMIs, each a separate bake.
- An arm64 AMI that was "FETCH_FASTQ-only" and **lacked the DB**, requiring a full
  rebake just to add a data blob to an image.
- DB or tool updates force a **full machine-image rebuild**, not a data update.
- We own AMI version drift and per-arch detection, instead of leaning on the stock
  AL2023 image spawn auto-detects.

## The better-factored direction (recommended): DB on its own EBS volume

Put the Kraken2 DB on a **dedicated, right-sized EBS volume** (created once from a
snapshot), and let task instances mount it read-only on a **stock AL2023 AMI**.
Then:

- **Right-size each volume independently.** The DB volume is sized for the DB
  (~20 GB). The spore **root** volumes shrink to just what a task needs
  (OS + container + scratch) instead of carrying a 16 GB DB baked into every
  root snapshot. FETCH_FASTQ still gets its large scratch volume; Kraken2 gets
  DB-mount + modest root; the light steps get small roots. Today every task's
  root is sized to hold the baked DB even when it doesn't use it.
- **Decouple data from machine.** Update the DB by re-snapshotting one volume; no
  AMI rebuild. Base image stays the stock AL2023 spawn auto-detects — no custom
  AMI, no arch-split bake.
- **More spawn-idiomatic.** Lean on `--ami auto` + an attached data volume rather
  than handing spawn a hand-maintained custom AMI.

### The blocker: spawn / nf-spawn don't support this yet

As of spawn 0.45 / nf-spawn 0.2.12, the wiring isn't there:

- **spawn `launch`** exposes `--volume-size` (root EBS size only), `--efs-id`,
  and `--fsx-id` / `--fsx-create` — but **no "attach an EBS volume from a
  snapshot" / block-device-mapping flag.** You can size the root or mount EFS/FSx;
  you cannot attach a pre-populated EBS data volume.
- **nf-spawn** passes only `ext.instanceType / region / ttl / spot / ami /
  volumeSize` through to `spawn launch`. There is **no `ext` for an extra volume,
  snapshot ID, EFS, or FSx** — so even if spawn supported it, a Nextflow task
  couldn't request it per-process today.

So "DB on its own EBS volume" needs an upstream feature in **both**: a spawn flag
(e.g. `--attach-volume snap-xxx:/opt/databases:ro` or block-device-mapping
support) and an nf-spawn `ext.volumes` (or similar) that forwards it. Worth filing
on nf-spawn/spawn if we want to pursue it.

## Option comparison

| | Custom AMI (today) | Stock AMI + DB on EBS snapshot/volume (recommended) | Stock AMI + EFS/FSx for DB | Stock AMI + per-task S3 fetch |
|---|---|---|---|---|
| Kraken2 cold-start | Fastest (DB on root) | Fast (attach pre-populated volume) | Fast-ish (network FS; FSx-Lustre good for read-heavy) | Slowest (~12 GB fetch+extract per cold task) |
| DB update | Full AMI rebake | Re-snapshot one volume | Update the FS copy | Change S3 key |
| Volume sizing | Every root carries the 16 GB DB | DB volume + small spore roots, each right-sized | Small roots; DB on shared FS | Small roots; DB transient |
| Base image | Custom AMI per arch (drift, rebake) | Stock AL2023 (`--ami auto`) | Stock AL2023 | Stock AL2023 |
| Supported by spawn/nf-spawn today? | Yes (`--ami`, `--volume-size`) | **No** — needs attach-volume flag + nf-spawn `ext` | Partially — spawn has `--efs-id`/`--fsx-id`, but nf-spawn forwards neither | Yes (fetch in task script, like FETCH_FASTQ does from RODA) |
| Best when | Repeated fan-out, stable DB, task-side simplicity | Repeated fan-out, DB/tools evolve independently, want small right-sized volumes | DB shared across many concurrent readers; very large refs | One-off / infrequent runs |

## Recommendation

Target **stock AMI + Kraken2 DB on its own right-sized EBS volume** (snapshot →
attach read-only), which also lets the spore root volumes shrink to per-task size.
It's the more idiomatic spawn pattern and removes the arch-split / rebake friction.

It is **blocked on upstream support**: spawn needs an attach-EBS-from-snapshot
flag and nf-spawn needs an `ext` to forward it per-process. Until then, the custom
AMI remains the only working option for a no-per-task-download Kraken2 cold start,
and is acceptable given this is a repeated 100-way fan-out with a stable DB.

Interim fallback that needs no upstream change: a stock AMI where Kraken2's task
script fetches the DB from S3 itself (the same self-fetch pattern FETCH_FASTQ
already uses for RODA) — at the cost of the ~12 GB pull on cold Kraken2 tasks.

## Building & serving the DB volume efficiently (EBS direct APIs + FSR)

Two AWS capabilities make the "DB on its own EBS volume" path materially better
than a naive snapshot, and split cleanly across the build side and the read side.

### Build side — create the snapshot WITHOUT a bake instance (EBS direct APIs)

The [EBS direct APIs](https://docs.aws.amazon.com/ebs/latest/APIReference/Welcome.html)
let you create and populate a snapshot directly, no EC2 instance and no attached
volume:

- `StartSnapshot` → `PutSnapshotBlock` (×N) → `CompleteSnapshot` — write the DB
  straight into a snapshot from a laptop or a Lambda.
- `ListChangedBlocks` / `ListSnapshotBlocks` / `GetSnapshotBlock` — read and diff
  snapshot blocks.

Impact: building the Kraken2 DB snapshot no longer needs the `build_ami.py`
launch→download→extract→`spawn ami create`→terminate dance. And it's
**incremental** — a DB version bump writes only changed blocks (`ListChangedBlocks`),
not a fresh 16 GB. This removes the bake instance from the *data* side entirely.

`spawn snapshot create --from <dir|.tar.gz|raw>` (spawn ≥ 0.48.0) packs a
directory/tarball into an ext4 image in-process (pure Go, no `mkfs`, no builder
instance) and streams it into the snapshot. Example used here:
```
spawn snapshot create --from s3://genome-idx/kraken/k2_pluspf_16_GB_*.tar.gz \
    --size 24 --name kraken2-k2pluspf-16gb --region us-east-1
```

**Where you run the build matters (observed).** Running this from a laptop is
slow and RAM-heavy: spawn assembles the full ext4 image in memory before/while
uploading (~5 GB+ RSS for a 16 GB image), and the data path is
S3 → laptop → EBS over the home uplink. For a 16 GB DB it's tolerable
(tens of minutes); for larger references it's impractical from a workstation.
**Recommendation: build the snapshot from a small EC2 instance / Lambda in the
target region**, so S3 → snapshot stays in-region (fast, no local-uplink
bottleneck, ample RAM). A server-side / in-region build mode would be a good
spawn enhancement (avoid round-tripping bytes through the caller). The build is a
**one-time, amortized** cost regardless of where it runs, so this is about build
convenience/latency, not per-run cost.

### Read side — Fast Snapshot Restore so wide fan-out isn't slow

A volume created from a snapshot **lazy-loads blocks from S3 on first access**, so
the first Kraken2 read of the DB on each fresh task pays that latency — and for a
100-way fan-out, 100 volumes-from-the-same-snapshot all do. **Fast Snapshot
Restore (FSR)** pre-warms the snapshot so every volume created from it is
immediately at full performance. FSR is the ingredient that makes the data-volume
path actually match the baked-AMI cold-start speed for wide fan-out (it has a
per-AZ hourly cost while enabled — worth it during a run, disable after).

### Why this is also a general spawn primitive

This isn't Kraken2-specific. "Materialize reference data into an EBS snapshot via
the direct APIs, then attach (FSR-warmed) volumes from it to launched instances"
is a clean, reusable way to get any large reference (BLAST/bowtie2/MetaPhlAn DBs,
ML model weights, …) onto ephemeral spores without baking AMIs or running a bake
instance. Proposed as a spawn feature (see issues below); it complements the
nf-spawn `ext.volumes` ask (#45) — direct APIs make the *build* instance-free, FSR
fixes the *read* path, and `ext.volumes` is still how a Nextflow process requests
the attach.

### Tracking

- **nf-spawn#45** — `ext.volumes` / snapshot-mount per process (the read/attach side).
- **spawn#147** — build-snapshot-from-S3 (EBS direct APIs) + attach FSR-warmed
  volume-from-snapshot (the general primitive).
