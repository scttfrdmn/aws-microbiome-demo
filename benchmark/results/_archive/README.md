# Archived results — superseded pilots

These are earlier, **superseded** benchmark artifacts, kept for provenance. They
are NOT the results of record. The canonical results live one level up in
[`../lifecycle/MEASUREMENTS.md`](../lifecycle/MEASUREMENTS.md) (the from-scratch
N=30 FSx lifecycle, both arches).

| dir / file | what it was | why superseded |
|------------|-------------|----------------|
| `RESULTS-n3-ebs-fsr.md` | N=3, EBS+FSR DB volumes, 2026-06-16 | EBS+FSR hit the FSR credit cliff at wide fan-out; pivoted to FSx Lustre. Also pre-dates the instance-lifetime timing fix, so its own text warns per-stage timing isn't trustworthy. |
| `arm64-n3-tsv/`, `x86-n3-tsv/` | N=3 Nextflow `trace.tsv` per arch | trace `realtime` is wrapper-local on the spawn executor — not real per-stage time. Replaced by EC2 billed-lifetime timing in `../lifecycle/`. |
| `arm64-unfsr-n3/` | N=3 arm64 run with FSR *disabled* | demonstrated the lazy-load penalty (~6–8 MB/s un-warmed DB volumes); a diagnostic, not a result. |
| `*-staging-n3/` | per-sample RODA staging timings, N=3 | early data-movement probe; the lifecycle records now carry staging as a measured phase. |

The decision trail (EBS → FSR → FSx, and the timing trap) is written up in
[`../../../docs/decisions/`](../../../docs/decisions/) and the blog post
[`../../../docs/blog/end-to-end.md`](../../../docs/blog/end-to-end.md).
