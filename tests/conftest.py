"""
conftest.py  --  shared fixtures for the microbiome demo test suite.

No AWS calls are made in tests.  All AWS interactions are replaced with
fakes passed via dependency injection (same pattern as the PCSK9 demo).
"""

from __future__ import annotations

import pytest


@pytest.fixture
def sample_summary() -> dict:
    """A realistic pipeline summary dict for testing synthesis."""
    return {
        "total_samples": 10,
        "completed": 10,
        "elapsed_seconds": 480.0,
        "ec2_cost_usd": 0.02304,
        "body_sites": {
            "stool": {
                "top_species": [
                    "Bacteroides vulgatus",
                    "Prevotella copri",
                    "Faecalibacterium prausnitzii",
                ],
                "diversity": {"shannon": 3.8, "observed": 142},
            },
            "buccal_mucosa": {
                "top_species": [
                    "Streptococcus salivarius",
                    "Veillonella parvula",
                ],
                "diversity": {"shannon": 2.1, "observed": 67},
            },
            "anterior_nares": {
                "top_species": [
                    "Staphylococcus epidermidis",
                    "Corynebacterium accolens",
                ],
                "diversity": {"shannon": 1.6, "observed": 38},
            },
        },
        "cross_site_comparison": {
            "bray_curtis": 0.72,
            "site_specific_taxa": [
                "Bacteroides vulgatus",
                "Staphylococcus epidermidis",
                "Streptococcus salivarius",
            ],
        },
    }
