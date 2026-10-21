"""Exact successor hashes for the user-reviewed Task 6 constructors only.

Historical manifests remain byte-for-byte intact. Any other method, changed
predecessor hash, altered proposal source or undeclared file still fails.
"""
import hashlib
import json


PROPOSAL_COMMIT = "b5ccdd418eedd0e151c72ac2f8093e7be8c48fcf"
HISTORICAL_FREEZE_SHA256 = "4edb229abe5c458a80e83708e86bd914f822b3e8852c6040b054354b0b9ef5ce"
ALLOWED = {
    "services/price_action_lab.py": {
        "PriceActionPaperAccount.__init__", "PriceActionLabRuntime.__init__"},
    "services/smc_strategy_lab.py": {
        "SMCPaperAccount.__init__", "SMCStrategyLabRuntime.__init__"},
    "execution/paper_broker_v2.py": {"PaperBrokerV2.__init__"},
}


def approved_delta(root):
    directory = root.parent / "validation"
    delta = json.loads((directory / "task6-readonly-approved/source-delta.json").read_text())
    assert delta["delta_id"] == "task6-readonly-acceptance.v1"
    assert delta["status"] == "USER_APPROVED"
    assert delta["approved_proposal_commit"] == PROPOSAL_COMMIT
    assert delta["alpha_rules_changed"] is False and delta["fill_timing_changed"] is False
    assert set(delta["file_updates"]) == set(ALLOWED)
    assert {p: set(m) for p, m in delta["method_updates"].items()} == ALLOWED
    assert delta["historical_freeze_sha256"] == HISTORICAL_FREEZE_SHA256
    assert hashlib.sha256((root / "data/pr6_real_paper_freeze.json").read_bytes()).hexdigest() == HISTORICAL_FREEZE_SHA256
    # The reviewed proposal contains all eleven application-file hashes and
    # the new mode module. Test adaptations never authorize runtime changes.
    proposal = json.loads((directory / "task6-readonly-proposal/source-review.json").read_text())
    for entry in proposal["files"]:
        path = root.parent / entry["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["proposed_file_sha256"], entry["path"]
    for entry in proposal["new_files"]:
        if entry["path"].startswith("automation-hub/services/"):
            assert hashlib.sha256((root.parent / entry["path"]).read_bytes()).hexdigest() == entry["sha256"]
    return delta


def reviewed_hash(root, relative, original, *, method=None):
    delta = approved_delta(root)
    if method is None:
        update = delta["file_updates"].get(relative)
    else:
        update = delta["method_updates"].get(relative, {}).get(method)
    if update is None:
        return original
    assert update["prior_sha256"] == original, (relative, method, "predecessor mismatch")
    return update["sha256"]
