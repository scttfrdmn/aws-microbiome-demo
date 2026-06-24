# 0003 — Fan-out traps: ECR Pull-Through Cache, and stage reference data once

**Status:** accepted
**Date:** 2026-06-17

Two distinct things broke *only at wide fan-out* (N=30) that were invisible at
N=3. Both are general lessons for "one ephemeral instance per task" pipelines.

## Trap 1 — Docker Hub rate limit → ECR Pull-Through Cache

**Symptom.** At N=30, ~30 `FETCH_FASTQ` instances launch near-simultaneously and
each pulls `ncbi/sra-tools:latest` from Docker Hub. They share one NAT egress IP,
so they hit Docker Hub's **anonymous pull rate limit** (100 pulls / 6 hr / IP):

```
docker: Error response from daemon: toomanyrequests: You have reached your
unauthenticated pull rate limit.   → exit 125 → Nextflow aborted all 30 tasks
```

Never seen at N=3 (≤3 pulls). The container is *correctly* multi-arch
(amd64+arm64) — this is purely a registry throttle, not an arch problem.

**Decision.** Use an **ECR Pull-Through Cache** repository with `registry-1.docker.io`
upstream (Docker Hub PAT in Secrets Manager). Tasks pull
`<acct>.dkr.ecr.<region>.amazonaws.com/dockerhub/ncbi/sra-tools:latest`. ECR
authenticates upstream **once**, caches the image in-region, and serves all N
pulls from ECR. The multi-arch manifest is preserved (c7g → arm64, c7i → amd64).

**Lesson.** A correctly-built multi-arch image is *still* a fan-out bottleneck if
it lives on a rate-limited public registry. PTC collapses N anonymous pulls into 1
authenticated, cached pull. (Public ECR Gallery / `quay.io` mirrors are
alternatives; PTC is the most general because it caches *any* Docker Hub image on
demand.)

> ⚠️ A Docker Hub PAT was pasted into the working session that produced this work.
> Rotate it. Store credentials only in Secrets Manager.

## Trap 2 — Stage reference data once, not per run (and `metaphlan --install` dominates)

We staged both DBs **from their canonical sources** (not laundered through old
snapshots) and timed every phase. The numbers make the lesson concrete:

| DB | source | phase | time |
|----|--------|-------|-----:|
| Kraken2 | `s3://genome-idx/kraken/k2_pluspf_16_GB_*.tar.gz` (in-region S3) | download | **77 s** |
| Kraken2 | local | extract | 147 s |
| Kraken2 | → S3 | sync | 129 s |
| MetaPhlAn | `metaphlan --install` (biobakery host, ~22 MB/s) | download+decompress | **55–70 min** |
| MetaPhlAn | → S3 | sync | 270 s |

**`metaphlan --install` dominates staging by ~43×** — 55–70 min from the official
biobakery host (external-network-bound, varied run-to-run: 3310 s arm64 leg vs
4182 s x86 leg for the *same bytes*) versus 77 s for Kraken2 from in-region S3.

**Decision.** Stage both DBs into S3/FSx **once**, as a measured one-time cost
(~$0.32–0.39, amortized over all runs). Never make a per-run, per-task path pull
36 GB from a slow external host. The per-task path stays zero-copy off the shared
FSx mount.

**Lesson.** With ephemeral per-task instances it's tempting to "just fetch what you
need" on each task. For large, static reference data that is the single most
expensive mistake available — either wasted compute (Option B in
[ADR-0001](0001-db-delivery-ami-ebs-fsx.md)) or, for `metaphlan --install`, an
hour of external-network wait multiplied by your fan-out. Stage once; read in place.
