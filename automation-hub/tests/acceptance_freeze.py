"""Explicit successor hashes; retain the original approved freeze unchanged."""
import json

ALLOWED_METHODS = {
    'services/price_action_lab.py': {'PriceActionPaperAccount.__init__',
        'PriceActionLabRuntime.__init__', 'PriceActionLabRuntime.ensure'},
    'services/smc_strategy_lab.py': {'SMCPaperAccount.__init__', 'SMCStrategyLabRuntime.__init__'},
    'execution/paper_broker_v2.py': {'PaperBrokerV2.__init__', 'PaperBrokerV2.account'},
    'services/price_action_governance.py': {'PriceActionJournalStore.__init__'},
}


def proposal(root):
    value = json.loads((root / 'data/task6_acceptance_read_only_delta.json').read_text())
    assert value['status'] == 'PROPOSED_SOURCE_CHANGE_REQUIRES_SEPARATE_DEPLOYMENT_APPROVAL'
    assert value['prior_source_commit'] == '4eceb973e98e1350dd625153753637629453c401'
    assert value['alpha_rules_changed'] is False and value['fill_timing_changed'] is False
    assert set(value['files']) == set(ALLOWED_METHODS)
    for path, names in ALLOWED_METHODS.items():
        assert set(value['files'][path]['method_updates']) == names
    return value


def successor_hash(root, path, expected, method=None):
    change = proposal(root)['files'].get(path)
    if change is None:
        return expected
    if method is not None:
        change = change['method_updates'].get(method)
        if change is None:
            return expected
    assert change['prior_sha256'] == expected, (path, method)
    return change['sha256']
