# 0004 — Beta diversity: aggregate to genus, and use the mean (not the median)

**Status:** accepted
**Date:** 2026-06-24

## Context

The biology validation (`analyze_study.py`) asks: do the classified profiles
recover real Human Microbiome Project body-site structure? Two checks matter —
**alpha** diversity (Shannon, per sample) and **beta** diversity (Bray–Curtis
between samples: communities should cluster *within* a body site and differ
*between* sites, i.e. `within < between`).

Run naively on the N=30 x86 profiles, the first beta-diversity result was
`within ≈ between ≈ 0.99` for **both** Kraken2 and MetaPhlAn — no separation — yet
the "✓ separates" verdict fired anyway off a meaningless third-decimal gap
(0.994 vs 0.996). Two real bugs, found by drilling into the distribution.

## Bug 1 — Bray–Curtis must run at genus level, not species level

A hand-computed Bray–Curtis between two stool samples gave a sensible **0.36**
(shared mass 0.64). But the aggregate sat at ~0.99. The cause: **species-level
profiles barely overlap between subjects** — two healthy guts share genera but
carry different *species/strains*, so species-level Bray–Curtis saturates at ~1.0
within a site, identical to between sites. The signal is erased.

**Fix.** Collapse each profile to **genus** before Bray–Curtis (`_to_genus()`
sums species abundances per genus). Genera recur across subjects of the same body
site, so within-site dissimilarity drops below between-site.

## Bug 2 — Use the mean of the Bray–Curtis distribution, not the median

Even at genus level the per-pair distribution is **bimodal**: pairs sharing
dominant genera land at 0.3–0.6, but the many low-biomass / disjoint pairs still
saturate at 1.0. The **median** is pinned at ~0.99 by the saturated tail for both
within and between — erasing the signal again.

**Fix.** Report the **mean**, which integrates the whole distribution. It recovers
the validating signal cleanly:

| classifier | within-site (mean) | between-site (mean) | separates? |
|------------|-------------------:|--------------------:|:----------:|
| Kraken2 | 0.809 | 0.871 | ✅ |
| MetaPhlAn | 0.843 | 0.896 | ✅ |

The saturated median is still reported alongside, labelled, so the bimodality is
visible rather than hidden.

## Related: the `anterior_nares` caveat (not a bug)

HMP anterior_nares runs are low-biomass / host-DNA-heavy, and these particular
runs are dominated by oral+gut taxa (Veillonella, Haemophilus, Fusobacterium,
Prevotella, Bacteroides) rather than canonical nasal *Staphylococcus* /
*Corynebacterium*. Verified at species level on SRR061502; both classifiers agree.
This is documented nares contamination/low-biomass biology — **reported as a
caveat, not flagged as a pipeline failure.** Stool and buccal validate cleanly
against HMP-expected genera, so the pipeline is sound; nares is the known-hard
body site.

## Lesson

Diversity metrics on shotgun taxonomic profiles are sensitive to (a) the
**taxonomic rank** you compare at and (b) the **summary statistic** over a
saturated, bimodal pairwise distribution. Both defaults (species-level, median)
silently produced a null result that a naive threshold then mislabelled as
success. Always inspect the distribution before trusting a single separation number.
