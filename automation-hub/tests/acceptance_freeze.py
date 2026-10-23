"""Explicit successor hashes; retain the original approved freeze unchanged.

Two successor records apply, in order, on top of pr6_real_paper_freeze.json:
1. data/task6_acceptance_read_only_delta.json (the Strategy Intelligence line);
2. data/production_integration_delta.json (that line merged onto the
   production line: each entry names its prior hash, its new hash and whether
   it is byte-identical to the production commit or the union of both lines).
"""
import json

ALLOWED_METHODS = {
    'services/price_action_lab.py': {'PriceActionPaperAccount.__init__',
        'PriceActionLabRuntime.__init__', 'PriceActionLabRuntime.ensure'},
    'services/smc_strategy_lab.py': {'SMCPaperAccount.__init__', 'SMCStrategyLabRuntime.__init__'},
    'execution/paper_broker_v2.py': {'PaperBrokerV2.__init__', 'PaperBrokerV2.account'},
    'services/price_action_governance.py': {'PriceActionJournalStore.__init__'},
}
INTEGRATION_ORIGINS = {'production', 'merge'}


def proposal(root):
    value = json.loads((root / 'data/task6_acceptance_read_only_delta.json').read_text())
    assert value['status'] == 'PROPOSED_SOURCE_CHANGE_REQUIRES_SEPARATE_DEPLOYMENT_APPROVAL'
    assert value['prior_source_commit'] == '4eceb973e98e1350dd625153753637629453c401'
    assert value['alpha_rules_changed'] is False and value['fill_timing_changed'] is False
    assert set(value['files']) == set(ALLOWED_METHODS)
    for path, names in ALLOWED_METHODS.items():
        assert set(value['files'][path]['method_updates']) == names
    return value


def integration(root):
    value = json.loads((root / 'data/production_integration_delta.json').read_text())
    assert value['status'] == 'INTEGRATION_OF_TWO_REVIEWED_LINES'
    assert value['alpha_rules_changed'] is False and value['fill_timing_changed'] is False
    for path, change in value['files'].items():
        assert change['prior_sha256'] != change['sha256'], path
        assert change['origin'] in INTEGRATION_ORIGINS, path
        for name, update in change.get('method_updates', {}).items():
            assert update['prior_sha256'] != update['sha256'], (path, name)
            assert update['origin'] in INTEGRATION_ORIGINS, (path, name)
        for name, added in change.get('added_methods', {}).items():
            assert added['origin'] in INTEGRATION_ORIGINS, (path, name)
    return value


def added_methods(root, path):
    """Functions the production line added to a pinned file: name -> sha256."""
    change = integration(root)['files'].get(path) or {}
    return {name: added['sha256'] for name, added in change.get('added_methods', {}).items()}


def _step(change, expected, path, method):
    if change is None:
        return expected
    if method is not None:
        change = change.get('method_updates', {}).get(method)
        if change is None:
            return expected
    assert change['prior_sha256'] == expected, (path, method)
    return change['sha256']


def integration_hash(root, path, expected, method=None):
    """Apply only the integration record to a Strategy Intelligence hash."""
    return _step(integration(root)['files'].get(path), expected, path, method)


def successor_hash(root, path, expected, method=None):
    expected = _step(proposal(root)['files'].get(path), expected, path, method)
    return integration_hash(root, path, expected, method)
