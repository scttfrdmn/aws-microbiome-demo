# Benchmark harness — how to run it

The measurement study (Graviton vs x86, FSx-backed, full lifecycle) that this demo
was built to enable. This page is the **runbook**. For the *why* — fairness
controls, the timing trap, the DB-delivery decision, biology-stat fixes — see:

- **[../docs/methodology.md](../docs/methodology.md)** — protocol + fairness controls
- **[../docs/results.md](../docs/results.md)** — the results of record (N=30)
- **[../docs/decisions/](../docs/decisions/)** — the design decisions
- **[results/lifecycle/MEASUREMENTS.md](results/lifecycle/MEASUREMENTS.md)** — raw running log

> **Status:** complete. Both arches ran clean 30/30 at N=30. See [STATUS.md](STATUS.md).

## The scripts

| script | role | gated? |
|--------|------|--------|
| `build_fsx_db.py` | stage Kraken2 + MetaPhlAn DBs from canonical sources onto S3→FSx, timing every phase | `--plan` prints the plan + user-data; takes **no** action |
| `lifecycle_metrics.py` | record/render per-phase **time · data · cost**; one-time vs per-run + amortization | recorder/analyzer (pure stdlib) |
| `analyze_study.py` | per-stage arch timing (from head `nextflow.log`) + biology validation | analyzer |
| `diff_traces.py` | legacy N=3 trace differ — **superseded** for per-stage timing (trace `realtime` is wrapper-local on the spawn executor) | analyzer |

## Running a lifecycle leg (per arch)

`N` (fan-out) is the knob: set `SAMPLES_PER_SITE = N/3` in `config.py`. Everything
else (FSx, ECR pull-through cache, staged DBs) is reusable across runs.

```bash
# 0. one-time: stage DBs from canonical sources onto FSx (gated — launch yourself)
python benchmark/build_fsx_db.py --plan      # prints the plan + the spawn commands
#    run the printed staging-instance command, then the printed --fsx-create command,
#    scope the DRA to the dbs-fsx prefix, and set FSX_ID/FSX_MOUNT in config.py.

# 1. run the pipeline at N for this arch (set BENCH_ARCH = "arm64" or "x86" in config.py)
SAMPLE_COUNT=$N AWS_PROFILE=aws uv run python run_headless.py

# 2. pull the artifacts that survive instance teardown
aws s3 cp s3://$BUCKET/results/$JOB/nextflow.head.log benchmark/results/lifecycle/$ARCH-n$N/
aws s3 cp s3://$BUCKET/results/$JOB/trace.tsv          benchmark/results/lifecycle/$ARCH-n$N/
aws s3 sync s3://$BUCKET/results/$JOB/                 benchmark/results/lifecycle/$ARCH-n$N/ \
    --exclude "*" --include "*kraken2*report*" --include "*metaphlan*profile*"

# 3. per-stage arch timing + biology
python benchmark/analyze_study.py \
    --arm64-log  benchmark/results/lifecycle/arm64-n$N/nextflow.head.log \
    --x86-log    benchmark/results/lifecycle/x86-n$N/nextflow.head.log \
    --x86-kraken benchmark/results/lifecycle/x86-n$N/kraken2 \
    --x86-metaphlan benchmark/results/lifecycle/x86-n$N/metaphlan \
    --json benchmark/results/lifecycle/study-n$N.json

# 4. render the end-to-end lifecycle report from a recorded leg
python benchmark/lifecycle_metrics.py benchmark/results/lifecycle/arm64-n$N-fsx.json
```

## Results layout

```
results/lifecycle/
  MEASUREMENTS.md            running log of every measured phase (the narrative)
  arm64-n30-fsx.json         per-leg lifecycle record (phases, time, data, cost)
  x86-n30-fsx.json
  staging_timings_*.json     per-arch DB staging phase timings
  biology_x86_n30.json       community structure + diversity + HMP validation
  arm64-n30/ , x86-n30/      head log, trace, per-stage json, classifier profiles
results/_archive/            superseded N=3 EBS+FSR pilots (provenance only)
```

## Fairness in one line

Only architecture differs: same samples, matched `c7i↔c7g`/`r7i↔r7g` pairs, **no
burstable instances** in the measured path, same region/AZ, same taxprofiler 2.0.0,
native containers on both legs. Per-stage timing is **EC2 billed lifetime**, not
trace `realtime`. The full list is in [../docs/methodology.md](../docs/methodology.md).
