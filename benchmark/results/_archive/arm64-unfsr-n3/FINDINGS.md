# arm64 leg — UN-FSR data point (2026-06-15)

First real arm64 benchmark leg. Completed end-to-end (16/16 tasks COMPLETED
exit 0, real species output: *Fusobacterium pseudoperiodonticum* 12.18% on
stool). But DB-delivery was **un-FSR** — DB volumes created from snapshots
without Fast Snapshot Restore, so first-touch blocks lazy-loaded from S3 at
~6-8 MB/s. Kept as the explicit "cold un-FSR volume" data point in the
db_delivery model; NOT a clean arch comparison (DB-load dominates).

## Three benchmark-critical findings

1. **Nextflow trace `realtime`/`start`/`complete` are wrapper-local on the spawn
   executor** — sub-second for all tasks, even ones that ran 40+ min remotely.
   Per-stage wall-clock must come from EC2 instance billed lifetime or
   inter-record timestamp gaps, NOT the trace columns.

2. **Un-FSR DB volumes lazy-load at ~6-8 MB/s** (CloudWatch `VolumeReadBytes`).
   Kraken2 reads the whole 16 GB DB into RAM before classifying → ~38-44 min;
   MetaPhlAn's bowtie2 index → ~72-76 min. Tasks sit `SUBMITTED` with empty work
   dirs meanwhile. Fixed by enabling FSR (reached `enabled` in ~minutes here).

3. **FSR is per-AZ; nf-spawn forwards no `ext.az`/`ext.subnet`** — coverage of
   task instances is best-effort (observed: all 9 tasks landed in us-east-1a).
   Gap to file on nf-spawn.

## Reconstructed wall-clock (submit→complete), un-FSR

| stage | sample | wall-clock |
|-------|--------|-----------|
| FASTP_PAIRED / FASTQC / FASTQC_PROCESSED | all | seconds (no volume) |
| KRAKEN2 | SRR059376 | 38.0 min |
| KRAKEN2 | SRR059377 | 37.9 min |
| KRAKEN2 | SRR059375 | 43.6 min |
| METAPHLAN | SRR059376 | 72.0 min |
| METAPHLAN | SRR059375 | 76.1 min |
| METAPHLAN | SRR059377 | 75.9 min |

Total run: 103 min for 3 samples. EBS read rate ~6-8 MB/s per the stuck Kraken2
volume's CloudWatch `VolumeReadBytes`.

Provenance: tools-AMI ami-05d4b3a43247af4b2 (arm64, nf-spawn 0.6.0, no baked DB),
aarchbio arm64 containers, head spawn CLI 0.52.0, DB snapshots
snap-05068c70e7ccf7974 (Kraken2 k2_pluspf_16gb) + snap-0463b9471b52ae203
(MetaPhlAn mpa_vJan25), un-FSR. Samples SRR059375/6/7 (HMP stool).
