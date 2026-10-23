"""Loopback-only acceptance server. No production app, providers or credentials."""
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

if os.environ.get("NEXUS_RESEARCH_E2E") != "1":
    raise RuntimeError("isolated research acceptance mode must be explicit")
for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ALERT_DISCORD_WEBHOOK", "ALERT_EMAIL_TO",
            "ALERT_SMTP_HOST", "ALERT_SMTP_USER", "ALERT_SMTP_PASS"):
    os.environ[key] = ""
for key in ("HUB_AUTO_ENGINE", "HUB_ENABLE_EXTERNAL_LIVE", "HUB_USE_LIVE_DATA", "HUB_RESEARCH_AUTOSTART", "HUB_MONITOR_AGENT"):
    os.environ[key] = "0"
root = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(root / "automation-hub"), str(root / "automation-hub/tests")]

from fastapi.middleware.cors import CORSMiddleware
from test_strategy_intelligence_acceptance import acceptance_runtime, acceptance_app
import uvicorn

with TemporaryDirectory(prefix="nexus-task5-") as directory:
    ledger, journal, service, champion, challenger = acceptance_runtime(Path(directory))
    financial_before = ledger.get_authoritative_evidence_snapshot()
    events_before = journal.evidence_events()
    validation_before = journal.context.validation_runs(filters={"owner_id": "owner"})
    app = acceptance_app(service)
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:4173"], allow_credentials=True,
                       allow_methods=["GET", "POST"], allow_headers=["*"])

    @app.get("/__acceptance__/state")
    def state():
        return {"financial_records_unchanged": ledger.get_authoritative_evidence_snapshot() == financial_before,
                "original_events_unchanged": journal.evidence_events() == events_before,
                "validation_records_unchanged": journal.context.validation_runs(filters={"owner_id": "owner"}) == validation_before,
                "actual_episode_count": len(journal.episodes()), "champion": champion["candidate_id"],
                "challenger": challenger["candidate_id"]}

    uvicorn.run(app, host="127.0.0.1", port=18005, log_level="warning")
