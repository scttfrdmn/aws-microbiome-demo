# Decision records

Short records of the load-bearing design decisions behind the benchmark — the
*why*, with the dead ends left in, because the dead ends are the lesson. Distilled
from the running notes; each is self-contained.

| # | Decision | One-line takeaway |
|---|----------|-------------------|
| [0001](0001-db-delivery-ami-ebs-fsx.md) | Reference-DB delivery: AMI → EBS+FSR → FSx Lustre | Wide fan-out needs a shared FSx filesystem; EBS+FSR hits a ~10-reader credit cliff. |
| [0002](0002-timing-instance-lifetime-not-trace.md) | Per-stage timing source | On a remote executor, Nextflow trace `realtime` is wrapper-local; bill EC2 instance lifetime. |
| [0003](0003-fanout-ecr-ptc-and-stage-once.md) | Fan-out traps | Put an ECR pull-through cache in front of Docker Hub; stage reference data once (`metaphlan --install` is 43× Kraken2). |
| [0004](0004-beta-diversity-genus-and-mean.md) | Diversity statistics | Bray–Curtis at genus level, summarized by the mean — species-level + median both produce a false null. |

Earlier, narrower notes kept for provenance: [`../../benchmark/db-delivery-az-decision.md`](../../benchmark/db-delivery-az-decision.md)
(the pre-FSx AZ/FSR decision) and [`../ami-vs-data-volume.md`](../ami-vs-data-volume.md)
(the original AMI-vs-data-volume design discussion).
