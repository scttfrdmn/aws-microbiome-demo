"""
test_app.py  --  tests for app.py helpers (no AWS calls, no real pipeline).

Tests cover config validation and the fake pipeline event sequence.
The FastAPI endpoints themselves are not tested here (would require
a running server); that's left for integration testing.
"""

from __future__ import annotations

import os
import types

# Ensure DEMO_FAKE is set before importing app so config loading is skipped.
os.environ.setdefault("DEMO_FAKE", "1")

from microbiome_demo.app import _validate_config  # noqa: E402


def _cfg(**kwargs) -> types.SimpleNamespace:
    defaults = dict(
        AMI_ID="ami-0123456789abcdef0",
        BUCKET="my-real-bucket",
        REGION="us-east-1",
        ACCOUNT_ID="123456789012",
    )
    defaults.update(kwargs)
    return types.SimpleNamespace(**defaults)


class TestValidateConfig:
    def test_valid_config_passes(self):
        assert _validate_config(_cfg()) is None

    def test_empty_ami_id_fails(self):
        err = _validate_config(_cfg(AMI_ID=""))
        assert err is not None
        assert "AMI_ID" in err

    def test_placeholder_bucket_fails(self):
        err = _validate_config(_cfg(BUCKET="your-microbiome-demo-bucket"))
        assert err is not None
        assert "BUCKET" in err

    def test_empty_bucket_fails(self):
        err = _validate_config(_cfg(BUCKET=""))
        assert err is not None

    def test_bad_region_fails(self):
        err = _validate_config(_cfg(REGION="localhost"))
        assert err is not None
        assert "REGION" in err

    def test_eu_region_passes(self):
        assert _validate_config(_cfg(REGION="eu-west-1")) is None

    def test_ap_region_passes(self):
        assert _validate_config(_cfg(REGION="ap-southeast-1")) is None


class TestFakePipelineEvents:
    """Verify _run_fake_pipeline emits the expected event sequence."""

    def test_fake_pipeline_emits_required_events(self):

        from microbiome_demo.app import _SUBSCRIBERS, _run_fake_pipeline

        collected: list[dict] = []

        class SyncQueue:
            def put_nowait(self, item):
                collected.append(item)

        sq = SyncQueue()
        _SUBSCRIBERS.append(sq)
        try:
            # Patch time.sleep to skip delays in tests
            import unittest.mock as mock

            import microbiome_demo.app as app_mod

            with mock.patch.object(app_mod.time, "sleep", return_value=None):
                _run_fake_pipeline()
        finally:
            _SUBSCRIBERS.remove(sq)

        types_seen = {e["type"] for e in collected}
        assert "quota" in types_seen, "quota event missing"
        assert "head_launched" in types_seen, "head_launched event missing"
        assert "progress" in types_seen, "progress event missing"
        assert "insight" in types_seen, "insight event missing"
        assert "done" in types_seen, "done event missing"

    def test_fake_pipeline_quota_has_queue_size(self):
        import unittest.mock as mock

        import microbiome_demo.app as app_mod
        from microbiome_demo.app import _SUBSCRIBERS, _run_fake_pipeline

        collected: list[dict] = []

        class SyncQueue:
            def put_nowait(self, item):
                collected.append(item)

        sq = SyncQueue()
        _SUBSCRIBERS.append(sq)
        try:
            with mock.patch.object(app_mod.time, "sleep", return_value=None):
                _run_fake_pipeline()
        finally:
            _SUBSCRIBERS.remove(sq)

        quota_events = [e for e in collected if e["type"] == "quota"]
        assert len(quota_events) == 1
        assert quota_events[0]["queue_size"] > 0

    def test_fake_pipeline_progress_has_data_volumes(self):
        import unittest.mock as mock

        import microbiome_demo.app as app_mod
        from microbiome_demo.app import _SUBSCRIBERS, _run_fake_pipeline

        collected: list[dict] = []

        class SyncQueue:
            def put_nowait(self, item):
                collected.append(item)

        sq = SyncQueue()
        _SUBSCRIBERS.append(sq)
        try:
            with mock.patch.object(app_mod.time, "sleep", return_value=None):
                _run_fake_pipeline()
        finally:
            _SUBSCRIBERS.remove(sq)

        progress_events = [e for e in collected if e["type"] == "progress"]
        assert len(progress_events) > 0
        last = progress_events[-1]
        assert "roda_gb" in last
        assert "fastq_gb" in last
        assert "expansion_ratio" in last
        # By the end all samples should be done
        assert last["tasks_done"] == last["tasks_total"]
