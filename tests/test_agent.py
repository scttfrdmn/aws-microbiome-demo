"""
test_agent.py  --  tests for the Bedrock synthesis agent.

No AWS calls.  FakeBackend is used via dependency injection.
"""

from __future__ import annotations

from microbiome_demo.agent import FakeBackend, _build_prompt, synthesize


def test_build_prompt_includes_body_sites(sample_summary):
    """The prompt should mention all three body sites."""
    prompt = _build_prompt(sample_summary)
    assert "stool" in prompt.lower() or "gut" in prompt.lower()
    assert "buccal" in prompt.lower() or "oral" in prompt.lower()
    assert "nares" in prompt.lower() or "nasal" in prompt.lower()


def test_build_prompt_includes_cost(sample_summary):
    """The prompt should include the EC2 cost for context."""
    prompt = _build_prompt(sample_summary)
    assert "$" in prompt or "cost" in prompt.lower()


def test_synthesize_emits_events(sample_summary):
    """synthesize() should emit model start/done, insight, cost, and done events."""
    emitted: list[dict] = []

    synthesize(sample_summary, emit=emitted.append, backend=FakeBackend())

    types = [e["type"] for e in emitted]
    assert "model" in types
    assert "insight" in types
    assert "done" in types

    # Model events should include a start and a done
    model_states = [e.get("state") for e in emitted if e["type"] == "model"]
    assert "start" in model_states
    assert "done" in model_states


def test_synthesize_cost_emitted(sample_summary):
    """synthesize() should emit a cost event with a non-negative value."""
    emitted: list[dict] = []
    synthesize(sample_summary, emit=emitted.append, backend=FakeBackend())

    cost_events = [e for e in emitted if e["type"] == "cost"]
    assert len(cost_events) == 1
    assert cost_events[0]["total"] >= 0.0


def test_synthesize_insight_text(sample_summary):
    """The insight event should contain the text from the backend."""
    emitted: list[dict] = []
    synthesize(sample_summary, emit=emitted.append, backend=FakeBackend())

    insight_events = [e for e in emitted if e["type"] == "insight"]
    assert len(insight_events) == 1
    assert len(insight_events[0]["text"]) > 10
