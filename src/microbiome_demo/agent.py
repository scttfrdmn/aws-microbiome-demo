"""
agent.py  --  Bedrock Sonnet synthesis of metagenomics results.

After the pipeline finishes, this module calls Bedrock to generate
plain-language clinical insights from the species abundance data.

The synthesis follows a single pattern: feed the structured results
(body-site species profiles + diversity metrics) to Claude Sonnet and
ask for three key insights a researcher would care about.

Design notes:
  - Takes a backend by dependency injection (like the PCSK9 demo) so
    tests can pass a fake without hitting AWS.
  - Emits events compatible with app.py's WebSocket protocol.
  - Cost is tracked and emitted exactly like the PCSK9 demo.
"""

from __future__ import annotations

from collections.abc import Callable

import boto3

# Pricing: Claude Sonnet 4.6 on Bedrock (us-east-1, as of 2026-05)
# Source: Bedrock console pricing page
_SONNET_INPUT_USD_PER_1K = 0.003  # $3.00 / 1M input tokens
_SONNET_OUTPUT_USD_PER_1K = 0.015  # $15.00 / 1M output tokens

_SYNTHESIS_SYSTEM = """\
You are a microbiome data scientist summarizing results from a Kraken2 + MetaPhlAn
shotgun metagenomics study of Human Microbiome Project samples.

Write three numbered insights for a research-computing conference audience:
  1. The most striking species pattern across body sites.
  2. A diversity finding (alpha or beta diversity) that is surprising or significant.
  3. A methodological observation about the pipeline performance or data quality.

Each insight should be one or two sentences.  Be specific: cite species names,
diversity values, or sample counts where relevant.  Do not speculate beyond the data.
"""


class AwsBackend:
    """Real Bedrock backend for synthesis.

    Intentionally minimal — we only need converse() for this demo.
    """

    def __init__(self, region: str, model_id: str):
        self._client = boto3.client("bedrock-runtime", region_name=region)
        self._model_id = model_id

    def converse(self, system: str, user_message: str) -> tuple[str, dict]:
        """Call Bedrock converse() and return (text, usage)."""
        resp = self._client.converse(
            modelId=self._model_id,
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": user_message}]}],
            inferenceConfig={"maxTokens": 1024},
        )
        text = resp["output"]["message"]["content"][0]["text"]
        usage = resp.get("usage", {})
        return text, usage


class FakeBackend:
    """Test double — returns canned text, no AWS calls."""

    def converse(self, system: str, user_message: str) -> tuple[str, dict]:  # noqa: ARG002
        return (
            "1. Bacteroides and Prevotella show strong body-site specificity.\n"
            "2. Stool samples exhibit the highest Shannon diversity (mean H'=3.8).\n"
            "3. Graviton3 instances achieved 12× speed-up vs. x86 for Kraken2.",
            {"inputTokens": 120, "outputTokens": 80},
        )


def synthesize(
    summary: dict,
    emit: Callable[[dict], None],
    backend=None,
) -> None:
    """Run Bedrock synthesis and emit events.

    Args:
        summary:  the pipeline summary dict (from pipeline.read_summary()).
        emit:     event callback compatible with app.py's WebSocket protocol.
        backend:  AwsBackend (or a FakeBackend for tests).  If None, creates
                  a real AwsBackend from the BEDROCK_REGION / BEDROCK_MODEL
                  in config.  Must be provided by caller if running without
                  AWS credentials.
    """
    if backend is None:
        import config as cfg  # type: ignore[import]

        backend = AwsBackend(cfg.BEDROCK_REGION, cfg.BEDROCK_MODEL)

    emit({"type": "phase", "label": "Synthesizing insights with Claude Sonnet…"})
    emit({"type": "model", "tier": "sonnet", "label": "Claude Sonnet", "state": "start"})

    user_message = _build_prompt(summary)
    text, usage = backend.converse(_SYNTHESIS_SYSTEM, user_message)

    in_tok = usage.get("inputTokens", 0)
    out_tok = usage.get("outputTokens", 0)
    cost = (in_tok * _SONNET_INPUT_USD_PER_1K + out_tok * _SONNET_OUTPUT_USD_PER_1K) / 1000.0

    emit(
        {
            "type": "model",
            "tier": "sonnet",
            "label": "Claude Sonnet",
            "state": "done",
            "usage": {"inputTokens": in_tok, "outputTokens": out_tok},
            "cost": cost,
        }
    )

    emit({"type": "insight", "text": text})
    emit({"type": "cost", "total": cost})
    emit({"type": "done"})


def _build_prompt(summary: dict) -> str:
    """Convert the pipeline summary dict into a structured prompt."""
    lines: list[str] = [
        f"Total samples analysed: {summary.get('total_samples', '?')}",
        f"Pipeline elapsed time: {_fmt_elapsed(summary.get('elapsed_seconds', 0))}",
        f"Estimated EC2 cost: ${summary.get('ec2_cost_usd', 0):.4f}",
        "",
        "Body-site species profiles:",
    ]

    body_sites = summary.get("body_sites", {})
    for site, data in body_sites.items():
        lines.append(f"\n{site.replace('_', ' ').title()}:")
        top = data.get("top_species", [])
        if top:
            lines.append("  Top species: " + ", ".join(top[:5]))
        div = data.get("diversity", {})
        if div:
            lines.append(
                f"  Shannon diversity: {div.get('shannon', 'N/A')}, "
                f"  Observed species: {div.get('observed', 'N/A')}"
            )

    csc = summary.get("cross_site_comparison", {})
    if csc:
        lines.append(f"\nCross-site beta diversity (Bray-Curtis): {csc.get('bray_curtis', 'N/A')}")
        lines.append(f"Most site-specific taxa: {', '.join(csc.get('site_specific_taxa', [])[:3])}")

    return "\n".join(lines)


def _fmt_elapsed(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s}s" if m else f"{s}s"
