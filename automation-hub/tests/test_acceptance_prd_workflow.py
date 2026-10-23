"""Existing Task 5 synthetic acceptance behind the explicit HTTP policy."""
from services.acceptance_policy import AcceptanceReadOnlyMiddleware
import test_strategy_intelligence_acceptance as workflow


def test_full_prd_metadata_workflow_abstains_behind_acceptance_gate(tmp_path, monkeypatch):
    original = workflow.acceptance_app
    def app(service):
        result = original(service)
        result.add_middleware(AcceptanceReadOnlyMiddleware, enabled=True)
        return result
    monkeypatch.setattr(workflow, 'acceptance_app', app)
    workflow.test_full_api_workflow_preserves_frozen_identity_finances_and_abstains_on_actual_cost_gaps(tmp_path)
