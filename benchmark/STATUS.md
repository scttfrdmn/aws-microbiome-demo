# Status — COMPLETE

The x86-vs-arm64 Graviton benchmark is **done**. Both arches ran the full
from-scratch FSx-backed lifecycle at N=30, clean 30/30, with biology validated.

- **Results of record:** [`results/lifecycle/MEASUREMENTS.md`](results/lifecycle/MEASUREMENTS.md)
- **Methodology + fairness controls:** [`../docs/methodology.md`](../docs/methodology.md)
- **How to run the harness:** [`README.md`](README.md)
- **The story end-to-end:** [`../docs/blog/end-to-end.md`](../docs/blog/end-to-end.md)
- **Why each design choice (decision records):** [`../docs/decisions/`](../docs/decisions/)

The old "paused on nf-spawn#37 / N=3 EBS+FSR pilot" status is archived under
[`results/_archive/`](results/_archive/). nf-spawn#37 and the EBS+FSR path are
long superseded (#37-era input staging → s3:// markers; EBS+FSR → FSx Lustre).
