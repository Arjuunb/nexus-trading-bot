"""A failed attach must not end shadow research for the process lifetime.

The observer subscribes by loading REST history first, so one network blip at
container boot -- the likeliest moment for one -- used to leave it permanently
dead: it ran once, failed, detached, and nothing retried. Both paper labs
recover through their own supervisor threads; this one had none, so the PA/SMC
comparison silently stopped accumulating until someone restarted the process.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from bot.types import Bar
from services.forward_paper_hub import ForwardPaperMarketDataHub
from services.research_observer import ResearchObservationRuntime
from services.shadow_research import ShadowResearchStore

UTC = timezone.utc


class _FlakyFeed:
    """Refuses to start until ``fail_starts`` attempts have been made."""

    instances: list["_FlakyFeed"] = []
    fail_starts = 0

    def __init__(self, _loader, *, bar_sink=None, quote_sink=None,
                 event_sink=None, **_kwargs):
        self.bar_sink, self.quote_sink, self.event_sink = bar_sink, quote_sink, event_sink
        self.running = False
        self.reliable = True
        _FlakyFeed.instances.append(self)

    def start(self, _symbol, _timeframe):
        if _FlakyFeed.fail_starts > 0:
            _FlakyFeed.fail_starts -= 1
            return False          # mirrors a failed REST bootstrap
        self.running = True
        return True

    def stop(self):
        self.running = False

    def status(self):
        return {"state": "SYNCHRONIZED" if self.reliable else "STALE_CANDLES",
                "reliable": self.reliable, "new_entries_paused": not self.reliable}

    def snapshot(self):
        return {"closed_bars": [], "forming": None, "quote": {}}


def _hub():
    return ForwardPaperMarketDataHub(lambda *_a, **_k: [], stream_factory=_FlakyFeed)


def test_unsupervised_observer_stays_detached_after_a_failed_attach(tmp_path):
    """The old behaviour, kept explicit so a regression is visible."""
    _FlakyFeed.instances.clear()
    _FlakyFeed.fail_starts = 99
    observer = ResearchObservationRuntime(
        _hub(), ShadowResearchStore(tmp_path / "a.db"))
    try:
        assert observer.start() is False
        assert observer.attached() is False
        status = observer.status()
        # DETACHED, not BLOCKED: no candle will ever arrive without a reattach.
        assert status["state"] == "DETACHED"
        assert status["attached"] is False
        assert status["supervised"] is False
    finally:
        observer.stop()


def test_supervised_observer_reattaches_after_a_failed_boot(tmp_path):
    _FlakyFeed.instances.clear()
    # Three subscriptions, one failed start each, then recovery.
    _FlakyFeed.fail_starts = 3
    observer = ResearchObservationRuntime(
        _hub(), ShadowResearchStore(tmp_path / "b.db"),
        supervise=True, poll_seconds=0.05)
    try:
        assert observer.start() is False      # boot attempt fails, as in prod
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not observer.attached():
            time.sleep(0.05)
        assert observer.attached(), "supervisor never reattached the observer"
        assert observer.status()["state"] in {"OBSERVING", "BLOCKED"}
    finally:
        observer.stop()


def test_supervised_observer_observes_candles_after_recovery(tmp_path):
    _FlakyFeed.instances.clear()
    _FlakyFeed.fail_starts = 3
    hub = _hub()
    observer = ResearchObservationRuntime(
        hub, ShadowResearchStore(tmp_path / "c.db"),
        supervise=True, poll_seconds=0.05)
    try:
        observer.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not observer.attached():
            time.sleep(0.05)
        assert observer.attached()

        hub._channels[("BTCUSDT", "1h")].stream.bar_sink(
            Bar(datetime(2026, 9, 3, 10, tzinfo=UTC), 99, 102, 98, 101, 1))
        hub._channels[("BTCUSDT", "4h")].stream.bar_sink(
            Bar(datetime(2026, 9, 3, 8, tzinfo=UTC), 98, 103, 97, 102, 1))
        hub._channels[("BTCUSDT", "5m")].stream.bar_sink(
            Bar(datetime(2026, 9, 3, 12, 30, tzinfo=UTC), 100, 102, 99, 101, 1))

        status = observer.status()
        assert status["state"] == "OBSERVING"
        assert status["last_observation"]["decisions"], "no variant was evaluated"
    finally:
        observer.stop()


def test_stop_ends_supervision(tmp_path):
    _FlakyFeed.instances.clear()
    _FlakyFeed.fail_starts = 0
    observer = ResearchObservationRuntime(
        _hub(), ShadowResearchStore(tmp_path / "d.db"),
        supervise=True, poll_seconds=0.05)
    observer.start()
    observer.stop()
    assert observer._supervisor_thread is None
    assert observer.attached() is False
