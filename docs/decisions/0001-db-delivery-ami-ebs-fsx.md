# 0001 — Reference-DB delivery: AMI → EBS+FSR → FSx Lustre

**Status:** accepted (FSx Lustre is the answer for wide fan-out)
**Date:** 2026-06-17

## Context

Every pipeline task runs on its own ephemeral EC2 instance (one instance per
Nextflow task, via spawn / nf-spawn). The classifiers need large, static
reference databases:

- **Kraken2 `k2_pluspf_16GB`** — ~16 GB extracted, must be RAM-resident for speed.
- **MetaPhlAn CHOCOPhlAnSGB (vJan25)** — ~36 GB of Bowtie2 indexes.

At a fan-out of N samples, *many* task instances need the same ~52 GB of DB at the
same time. How that DB reaches each instance is the single biggest cost/throughput
lever in the whole pipeline. We tried three approaches, in order.

## Option A — bake the DB into the AMI

Weld OS + tools + the 16 GB Kraken2 DB onto one custom AMI (`build_ami.py`).

- ✅ Fastest cold start (DB already on root volume, zero copy at run time).
- ✅ No per-run data movement.
- ❌ Bigger AMI snapshot; a DB refresh means a re-bake (~20–30 min, ~$2–3).
- ❌ MetaPhlAn's 36 GB pushes the root volume large; two DBs on one AMI is unwieldy.

Fine for a fixed demo. Too rigid once the DB is a moving part and you want to swap
DB versions without re-baking.

## Option B — per-task S3 download

Each task `aws s3 cp`s the DB from S3 to its own disk at startup.

- ❌ **The worker is running and billed while it copies.** Copy-time is wasted
  compute-$ on an expensive classifier instance, paid N times. For MetaPhlAn's
  ~40 GB at ~200 MB/s that's ~205 s × N of pure copy on an r-family instance.

Rejected: it converts cheap data movement into expensive idle compute.

## Option C(i) — EBS snapshot + Fast Snapshot Restore (FSR)

Pre-build the DB onto an EBS volume, snapshot it, and have each task attach a
volume restored from that snapshot. nf-spawn `ext.volumes` + the #55 zero-copy
symlink mean the task reads the DB in place — no copy.

This *worked at N=3* and was clean. Two sharp edges surfaced scaling up:

1. **Un-warmed FSR volumes lazy-load at ~6–8 MB/s** — the first read of each block
   faults in from S3. A 16 GB DB read cold is ~40 min; the classifier crawls.
   *Fix:* enable FSR so volumes are pre-warmed (`FastRestored=true`).
2. **FSR is per-AZ**, so tasks must be pinned to the FSR-enabled AZ
   ([`ext.az`](https://github.com/spore-host/nf-spawn) — nf-spawn#62, released
   0.7.0). Without pinning, tasks land in other AZs and get cold volumes.

Then the wall at scale:

3. **FSR has a ~10-volume credit bucket per snapshot.** At N=30, ~50 volumes
   restored from 2 snapshots **drained the FSR credits** → most volumes were NOT
   fast-restored → back to ~6–8 MB/s lazy-load. EBS+FSR has a hard ceiling around
   ~10 concurrent fast-restored readers. This is the **FSR credit cliff**.

## Decision — FSx for Lustre (Option C(ii))

One S3-backed FSx for Lustre filesystem (PERSISTENT_2, 1200 GiB, 250 MB/s/TiB),
populated once from S3 via a Data Repository Association, mounted **read-only by
all N task instances**.

- ✅ **No per-volume credit bucket → no cliff.** Validated at **56 concurrent
  readers** (30 fetch + up to 56 classifiers) on the single shared FS — the exact
  thing EBS+FSR couldn't do.
- ✅ The DB lives once on the shared FS; the 52 GB never copies per task.
- ✅ Throughput is a dial (125–1000 MB/s/TiB), not a credit balance.
- ✅ Single-AZ FS → still pin tasks to that AZ (`ext.az`), but for *locality*, not
  credit conservation.

Cost: FSx bills storage-GB-hours while it exists (~$0.24/hr for 1200 GiB). That's
a one-time/amortized cost per benchmark, not per-run-per-sample, and spawn's
ttl-reaper reclaims orphaned filesystems.

### Wiring (so it stays reproducible)

- nf-spawn `ext.fsx = [id:'fs-…', mount:'/fsx', paths:['kraken2']]` per classifier
  (forwarded to `spawn launch --fsx-id`), released in **nf-spawn 0.8.0** (#67).
  `paths` is **required** for the #55 zero-copy symlink — without it nf-spawn only
  exposes bare `/fsx` and the staged `db_path` gets copied instead of symlinked.
- `db_path` in `databases.csv` is an **`s3://` marker**, not a head-local path —
  otherwise Nextflow's FilePorter bulk-copies the 56 GB foreign path on the head
  before any task runs (the nf-spawn#65 deadlock). The head only needs
  `exists:true`; the tasks read off `/fsx`.

## Decision matrix

| approach | copy on worker | $ during copy | scaling limit |
|----------|---------------|---------------|---------------|
| A — baked AMI | none | $0 | DB refresh = re-bake |
| B — per-task S3 download | full DB | **wasted compute × N** | none, but expensive |
| C(i) — EBS + FSR | none (symlink) | ~$0 | **~10-reader FSR credit cliff** |
| **C(ii) — FSx Lustre** | none (symlink) | ~$0 | **FSx throughput dial; 56 readers proven** |

**Rule of thumb:** N ≲ 8 → EBS+FSR is fine and cheaper. Wide fan-out → FSx Lustre.
Fixed/rarely-changing DB and a fixed demo → baked AMI.

## Consequences / upstream trail

- nf-spawn#62 (`ext.az`) — released 0.7.0.
- nf-spawn#67 (`ext.fsx` forwarding + mount in #55 staging) — released 0.8.0.
- spawn#206/#208 (`--fsx-create` AZ handling, PERSISTENT_2 offering) — fixed 0.59.0.
- DRA scope gotcha: `spawn --fsx-create` scopes the DRA to the **bucket root**, not
  `--fsx-import-path`. Delete and recreate the DRA scoped to the DB prefix, or it
  imports the whole bucket.
