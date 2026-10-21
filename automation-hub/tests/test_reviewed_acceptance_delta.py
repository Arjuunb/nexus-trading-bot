"""A reviewed startup delta must not become a blanket source-freeze waiver."""
import json
from pathlib import Path
import shutil

import pytest

from reviewed_acceptance_delta import approved_delta, reviewed_hash


@pytest.fixture
def reviewed_tree(tmp_path):
    hub = Path(__file__).parents[1]
    root = hub.parent
    proposal = root / "validation/task6-readonly-proposal/source-review.json"
    record = json.loads(proposal.read_text())
    paths = [entry["path"] for entry in record["files"] + record["new_files"]]
    paths += ["automation-hub/data/pr6_real_paper_freeze.json",
              "validation/task6-readonly-proposal/source-review.json",
              "validation/task6-readonly-approved/source-delta.json"]
    for relative in paths:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / relative, target)
    return tmp_path / "automation-hub"


def test_unreviewed_fill_change_is_never_substituted(reviewed_tree):
    original = "a" * 64
    assert reviewed_hash(reviewed_tree, "execution/paper_broker_v2.py", original,
                         method="PaperBrokerV2._fill") == original


def test_constructor_update_requires_exact_predecessor(reviewed_tree):
    with pytest.raises(AssertionError, match="predecessor mismatch"):
        reviewed_hash(reviewed_tree, "execution/paper_broker_v2.py", "a" * 64,
                      method="PaperBrokerV2.__init__")


@pytest.mark.parametrize("mutation", ["unapproved", "add_fill_waiver", "alter_historical_pin"])
def test_invalid_approval_record_cannot_waive_freeze(reviewed_tree, mutation):
    path = reviewed_tree.parent / "validation/task6-readonly-approved/source-delta.json"
    record = json.loads(path.read_text())
    if mutation == "unapproved":
        record["status"] = "PROPOSED"
    elif mutation == "add_fill_waiver":
        record["method_updates"]["execution/paper_broker_v2.py"]["PaperBrokerV2._fill"] = {
            "prior_sha256": "a" * 64, "sha256": "b" * 64}
    else:
        record["historical_freeze_sha256"] = "a" * 64
    path.write_text(json.dumps(record))
    with pytest.raises(AssertionError):
        approved_delta(reviewed_tree)


def test_runtime_drift_after_source_review_is_rejected(reviewed_tree):
    path = reviewed_tree / "app.py"
    path.write_text(path.read_text() + "\n# unreviewed runtime change\n")
    with pytest.raises(AssertionError, match="app.py"):
        approved_delta(reviewed_tree)
