# 0002 — Per-stage timing: bill EC2 instance lifetime, not Nextflow trace `realtime`

**Status:** accepted
**Date:** 2026-06-16 (diagnosed), enforced in the N=30 lifecycle

## Context

The whole point of the benchmark is a defensible **per-stage** time and cost for
each pipeline step (fastp, FastQC, Kraken2, MetaPhlAn, MultiQC), per architecture.
The obvious source is Nextflow's execution trace (`trace.tsv`), which has
`realtime` and `duration` columns per task.

## The trap

On the spawn executor those columns are **wrapper-local and meaningless for real
runtime**:

- `realtime` is measured by the Nextflow task *wrapper running on the head node*,
  which only launches a remote instance, waits, and collects results. It clocks
  **sub-second** for tasks whose actual compute ran 40+ minutes on a separate EC2
  instance.
- The per-stage start→complete *envelope* also collapses: at small N all the
  classifier tasks finalize near-simultaneously on the head while the real
  multi-minute compute happened on now-terminated instances.

Running `diff_traces.py` against these columns reports **fiction**. The N=3 pilot
([`../../benchmark/results/_archive/RESULTS-n3-ebs-fsr.md`](../../benchmark/results/_archive/RESULTS-n3-ebs-fsr.md))
explicitly flagged this and refused to publish per-stage numbers from it.

## Decision

The authoritative per-stage signal is **EC2 instance billed lifetime** —
launch → terminate for each task's dedicated instance. We recover it from the
**head node's `.nextflow.log`**, which records nf-spawn's lifecycle lines
(`Submitting` / task `completed`) with real timestamps, and cross-check against
the instance count. That billed wall-clock × the instance's on-demand $/hr is the
per-stage cost.

- `analyze_study.py timing_report()` parses the head `nextflow.log` for
  submit→done per task, computes per-stage median / min / max billed seconds, and
  runs a Mann–Whitney U across the two arches.
- The head log is uploaded to `results/<job>/nextflow.head.log` at the end of every
  run so the signal survives instance teardown (you cannot get `LaunchTime` back
  once instances age out of `describe-instances`).

## Consequences

- Trace `realtime` is still collected (it's free) but is **never** used for
  per-stage timing on the spawn executor. It's noted as wrapper-local wherever it
  appears.
- Any timing claim is qualified by instance type + AZ + placement, because billed
  lifetime includes scheduling/boot variance. Medians over n=30 tasks/stage are
  robust; single-run per-stage *ratios* (e.g. one arch 19% faster on Kraken2) are
  flagged as one-run observations, while the **cost** delta is structural (the
  per-hour price ratio is fixed).

See [[benchmark-timing-on-spawn-executor]] in memory for the original diagnosis.
