"""Retry journal reconciliation independently of the trading worker's cycles."""
from __future__ import annotations

import logging
import threading


class EvidenceRecoveryLoop:
    """A stoppable at-least-once metadata consumer with no order interface."""

    def __init__(self, capture, ledger, *, stop_event, worker_alive, interval_seconds=30.0,
                 after_reconcile=None):
        if interval_seconds <= 0:
            raise ValueError("recovery interval must be positive")
        self.capture = capture
        self.ledger = ledger
        self.stop_event = stop_event
        self.worker_alive = worker_alive
        self.interval_seconds = interval_seconds
        self.last_report = None
        self.after_reconcile = after_reconcile
        self.last_intelligence_result = None
        self._thread = None

    def run_once(self):
        self.last_report = self.capture.reconcile_report(self.ledger)
        if self.after_reconcile is not None:
            try:
                self.last_intelligence_result = self.after_reconcile(self.ledger, self.last_report)
            except Exception as exc:
                self.last_intelligence_result = {"status": "ERROR", "error": type(exc).__name__}
                logging.getLogger(__name__).exception("strategy_context_processing_failed")
        return self.last_report

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return False
        self._thread = threading.Thread(target=self._run, name="paper-evidence-recovery", daemon=True)
        self._thread.start()
        return True

    def _run(self):
        while not self.stop_event.wait(self.interval_seconds):
            if not self.worker_alive():
                return
            try:
                self.run_once()
            except Exception:
                logging.getLogger(__name__).exception("strategy_evidence_periodic_recovery_failed")

