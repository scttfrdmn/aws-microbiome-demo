# Methodology — how this benchmark is kept honest

This is the measurement study behind the demo: run a **real** nf-core/taxprofiler
metagenomics pipeline on **real** HMP data, on x86 and on Graviton (arm64), and
report the per-stage and end-to-end time/data/cost — measured, not projected.

If you just want the numbers, see [results.md](results.md). If you want the story,
see [blog/end-to-end.md](blog/end-to-end.md). This page is the *protocol* — the
controls that make the numbers defensible.

## What we measure

The full from-scratch lifecycle, broken into phases, for each architecture:

```
provision (FSx + DRA)  →  stage (DBs from canonical source)  →  run (@ N)  →  teardown
└──────────── one-time, amortized over R runs ───────────┘     └─ per-run ─┘
```

For every phase: **wall-clock time, bytes moved, and cost** (instance
billed-seconds × on-demand $/hr, plus FSx GB-hours and S3 requests where they
matter). `N` (fan-out width = samples) is the tunable knob; runs are recorded as
`benchmark/results/lifecycle/<arch>-n<N>-fsx.json`.

## Fairness controls (only architecture may differ)

- **Same samples.** Both legs use the identical HMP accessions (balanced N/3 per
  body site: stool / buccal_mucosa / anterior_nares).
- **Same instance spec, differ only in family.** `c7i↔c7g`, `r7i↔r7g` at identical
  vCPU/RAM. Never `c7i.2xlarge` vs `c7g.4xlarge` — that confounds architecture
  with size.
- **No burstable (t-family) instances anywhere in the measured path.** t4g/t3 are
  credit-based shared-core; the same workload varies ~2× with credit state, so
  you'd measure credit balance, not architecture. Every measured stage and the
  head node run on a fixed-performance family. (See memory: *no-t4g-for-benchmarking*.)
- **Same region/AZ** (us-east-1, pinned via `ext.az`) — same RODA locality, same
  FSx mount locality.
- **Same pipeline version** — both resolve nf-core/taxprofiler 2.0.0 with the same
  Wave-pinned container digests.
- **Native on both legs.** x86 runs native amd64 containers; arm64 runs native
  arm64 containers ([aarchbio](https://github.com/playgroundlogic/aarchbio) where
  the upstream image is amd64-only). Neither emulates — this is native-vs-native
  price/performance, the honest comparison. The emulation tax is a separate story.

## Instance pairs (the controlled variable)

| Stage (taxprofiler 2.0.0) | label | x86 leg | arm64 leg |
|---------------------------|-------|---------|-----------|
| FETCH_FASTQ | process_single | c7i.large | c7g.large |
| fastp / FastQC / MetaPhlAn | process_medium | c7i.2xlarge | c7g.2xlarge |
| Kraken2 | process_high | r7i.2xlarge | r7g.2xlarge |
| MetaPhlAn (heavy tier) | process_high_memory | r7i.4xlarge | r7g.4xlarge |
| MultiQC / krakentools | process_single | c7i.large | c7g.large |
| head node (Nextflow only) | — | c7i.large | c7g.large |

## Timing: bill instance lifetime, not trace `realtime`

On the spawn executor, Nextflow's trace `realtime`/`duration` are **wrapper-local**
(sub-second even for tasks that ran 40+ min remotely). Per-stage timing comes from
**EC2 billed instance lifetime**, recovered from the head `.nextflow.log` (uploaded
to `results/<job>/nextflow.head.log`). Full rationale:
[decisions/0002](decisions/0002-timing-instance-lifetime-not-trace.md).

## Reference DBs are staged from canonical sources, timed

- **Kraken2 `k2_pluspf_16GB`** ← `s3://genome-idx/kraken/…` (public, in-region).
- **MetaPhlAn vJan25 CHOCOPhlAnSGB** ← `metaphlan --install` (official biobakery host).

They are **not** laundered through old EBS snapshots or work-dirs — staging is a
measured cost with full provenance. The DBs land in S3, an S3-backed FSx for
Lustre filesystem imports them, and every task reads them in place (zero copy).
Why FSx and not EBS+FSR or per-task download:
[decisions/0001](decisions/0001-db-delivery-ami-ebs-fsx.md).

## Honesty requirements (enforced in the harness)

- **Per-stage, not just total** — Kraken2 (memory-bound) and MetaPhlAn (the long
  pole) are the swing factors price alone can't predict.
- **Negative results stay in** — a stage slower on arm64 is reported as such, never
  dropped.
- **Failed tasks invalidate a comparison** — any nonzero exit on either leg flags
  the run; a "faster" run that silently failed a step isn't faster. Both N=30 legs
  were clean 30/30, 0 failed.
- **Median + range, and the price/hr ratio is reported separately from the runtime
  ratio.** `$/run = price/hr × measured duration`; the price ratio (~19%) is fixed,
  the runtime ratio is what's measured.
- **N is stated on every result** — N=30 is one run per arch; per-stage medians
  (n=30 tasks) are robust, single-run per-stage *ratios* are flagged as such, and
  the cost delta is structural.
- **Biology is validated, with caveats kept in** — see
  [decisions/0004](decisions/0004-beta-diversity-genus-and-mean.md) for the two
  diversity-stat bugs found and the honest anterior_nares caveat.

## The harness

| script | role |
|--------|------|
| `benchmark/build_fsx_db.py` | stage DBs from canonical sources onto FSx, timed (gated; `--plan`) |
| `benchmark/lifecycle_metrics.py` | record/render per-phase time·data·cost; one-time vs per-run + amortization |
| `benchmark/analyze_study.py` | per-stage arch timing (from head log) + biology validation |
| `benchmark/diff_traces.py` | (legacy N=3 trace differ; superseded by the above for per-stage timing) |

See [`benchmark/README.md`](../benchmark/README.md) for exact run commands.
