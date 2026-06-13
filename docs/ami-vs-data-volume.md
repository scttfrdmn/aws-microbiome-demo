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
