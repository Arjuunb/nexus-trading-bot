"""The v2 intelligence surface reads caches under server-resolved ownership."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


class Manager:
    def __init__(self):
        self.mine = SimpleNamespace(id="mine", owner_id="alice", simulation_session_id="current")
        self.theirs = SimpleNamespace(id="theirs", owner_id="bob", simulation_session_id="other")
        self.store = SimpleNamespace(available=True, simulation_sessions=self.sessions)
        self.session_reads = []

    def sessions(self, instance_id):
        self.session_reads.append(instance_id)
        return [{"id": "current", "instance_id": "mine"},
                {"id": "archived", "instance_id": "mine"}] if instance_id == "mine" else []

    def instance_for(self, instance_id, owner_id):
        found = {"mine": self.mine, "theirs": self.theirs}.get(instance_id)
        if found is None or found.owner_id != owner_id:
            raise KeyError(instance_id)
        return found

    def owned_instances(self, owner_id):
        return [item for item in (self.mine, self.theirs) if item.owner_id == owner_id]

    def status(self, *_args, **_kwargs):
        raise AssertionError("API must not call status: it can persist financial metadata")


def cohort(**changes):
    return {"strategy_id": "adaptive_trend_pullback", "strategy_version": "1.0.0",
            "config_fingerprint": "a" * 64, "instance_id": "mine", "lab_id": None,
            "simulation_session_id": "current", "execution_mode": "forward_paper",
            "source_kind": "forward_paper", "owner_id": "alice",
            "account_id": "instance:mine:current", "symbol": None, **changes}


class CachedService:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.payload = {"contract_version": "strategy_intelligence.v2",
                        "calculation_timestamp": "2026-10-09T00:00:00+00:00",
                        "evidence_quality": "PARTIAL", "cache_status": "READY",
                        "cost_coverage": {"fees": "COMPLETE", "funding": "UNKNOWN", "slippage": "UNKNOWN"},
                        "sample_confidence": "INSUFFICIENT", "profitability_verified": False,
                        "rows": [{"cohort": cohort(), "group": {"session": "LONDON"},
                                  "trade_count": 1, "net_pnl": "0.123456789123456789",
                                  "profitability_verified": False}], "contexts": [],
                        "cohorts": [cohort()]}

    def read(self, view, scope, filters):
        self.calls.append((view, deepcopy(scope), deepcopy(filters)))
        if self.fail:
            raise OSError("isolated cache unavailable")
        return deepcopy(self.payload)


@pytest.fixture
def api():
    from routers.strategy_intelligence import create_router
    manager, service = Manager(), CachedService()
    app = FastAPI()
    app.include_router(create_router(service_provider=lambda: service,
                                    manager_provider=lambda: manager,
                                    owner_resolver=lambda _request: "alice"))
    return TestClient(app), manager, service


def query(**changes):
    return {"instance_id": "mine", "strategy_id": "adaptive_trend_pullback",
            "strategy_version": "1.0.0", "config_fingerprint": "a" * 64,
            "execution_mode": "forward_paper", "source_kind": "forward_paper", **changes}


BASE = "/api/v2/strategy-intelligence"


def test_exact_cohort_is_server_resolved_and_reads_one_cache(api):
    client, manager, service = api
    response = client.get(BASE + "/performance", params=query(group_by="symbol,session,trend_regime"))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["contract_version"] == "strategy_intelligence.v2"
    assert payload["rows"][0]["net_pnl"] == "0.123456789123456789"
    assert payload["profitability_verified"] is False
    assert service.calls == [("performance", {"owner_id": "alice", "instance_id": "mine",
                                              "simulation_session_id": "current",
                                              "account_id": "instance:mine:current"},
                             {"strategy_id": "adaptive_trend_pullback", "strategy_version": "1.0.0",
                              "config_fingerprint": "a" * 64, "execution_mode": "forward_paper",
                              "source_kind": "forward_paper", "lab_id": None, "symbol": None,
                              "group_by": ["symbol", "session", "trend_regime"]})]
    assert manager.session_reads == []


@pytest.mark.parametrize("instance_id", ["theirs", "absent"])
def test_foreign_and_missing_instance_have_the_same_non_enumerating_response(api, instance_id):
    client, manager, service = api
    result = client.get(BASE + "/performance", params=query(instance_id=instance_id))
    assert result.status_code == 404
    assert result.json() == {"detail": "Trading instance not found"}
    assert service.calls == [] and manager.session_reads == []


def test_archived_session_is_authorized_against_owning_instance(api):
    client, manager, service = api
    result = client.get(BASE + "/performance", params=query(simulation_session_id="archived"))
    assert result.status_code == 200
    assert service.calls[0][1]["account_id"] == "instance:mine:archived"
    assert manager.session_reads == ["mine"]
    service.calls.clear()
    result = client.get(BASE + "/performance", params=query(simulation_session_id="other"))
    assert result.status_code == 404
    assert service.calls == []


@pytest.mark.parametrize("forged", ["owner_id", "account_id", "history_complete", "completeness_report",
                                  "profitability_verified", "source_watermark"])
def test_callers_cannot_assert_scope_or_completeness(api, forged):
    client, _, service = api
    response = client.get(BASE + "/performance", params=query(**{forged: "true"}))
    assert response.status_code == 422
    assert service.calls == []


def test_cohort_discovery_filters_foreign_scope_and_invalid_account(api):
    client, _, service = api
    service.payload["cohorts"] += [cohort(owner_id="bob", instance_id="theirs"),
                                   cohort(account_id="forged"),
                                   cohort(simulation_session_id="other"),
                                   cohort(simulation_session_id="archived", account_id="instance:mine:archived")]
    result = client.get(BASE + "/cohorts")
    assert result.status_code == 200
    assert result.json()["cohorts"] == [cohort(), cohort(simulation_session_id="archived", account_id="instance:mine:archived")]
    assert service.calls[0][1] == {"owner_id": "alice"}


def test_instance_discovery_keeps_authorized_archived_sessions(api):
    client, _, service = api
    service.payload["cohorts"].append(cohort(simulation_session_id="archived", account_id="instance:mine:archived"))
    result = client.get(BASE + "/cohorts", params={"instance_id": "mine"})
    assert result.status_code == 200
    assert service.calls[0][1] == {"owner_id": "alice", "instance_id": "mine"}
    assert result.json()["cohorts"] == [cohort(), cohort(simulation_session_id="archived", account_id="instance:mine:archived")]


def test_missing_service_never_invents_verified_performance():
    from routers.strategy_intelligence import create_router
    app = FastAPI()
    app.include_router(create_router(service_provider=lambda: None,
                                    manager_provider=Manager,
                                    owner_resolver=lambda _request: "alice"))
    result = TestClient(app).get(BASE + "/performance", params=query())
    assert result.status_code == 200
    payload = result.json()
    assert payload["rows"] == [] and payload["calculation_timestamp"] is None
    assert payload["evidence_quality"] == "UNKNOWN"
    assert payload["cost_coverage"]["funding"] == "UNKNOWN"
    assert payload["profitability_verified"] is False
    assert payload["cache_status"] == "PENDING"


def test_cache_failure_returns_unknown_without_leaking_failure_details(api):
    client, _, service = api
    service.fail = True
    result = client.get(BASE + "/performance", params=query())
    assert result.status_code == 200
    assert result.json()["cache_status"] == "ERROR"
    assert result.json()["evidence_quality"] == "UNKNOWN"
    assert result.json()["rows"] == []
    assert "isolated cache unavailable" not in result.text


@pytest.mark.parametrize("group_by", ["owner_id", "symbol,symbol", "symbol,fees", "", "symbol,session,trend_regime,volatility_regime,direction"])
def test_unsupported_or_unbounded_grouping_is_rejected(api, group_by):
    client, _, service = api
    result = client.get(BASE + "/performance", params=query(group_by=group_by))
    assert result.status_code == 422
    assert service.calls == []


@pytest.mark.parametrize("limit", [0, 501])
def test_context_history_is_bounded(api, limit):
    client, _, service = api
    result = client.get(BASE + "/context", params=query(limit=limit))
    assert result.status_code == 422
    assert service.calls == []


def test_strategy_path_cannot_override_another_strategy_filter(api):
    client, _, service = api
    result = client.get(BASE + "/strategy/other/breakdown", params=query())
    assert result.status_code == 422
    assert service.calls == []


def test_no_identity_is_a_401_even_in_a_standalone_router():
    from routers.strategy_intelligence import create_router
    app = FastAPI()
    app.include_router(create_router(service_provider=CachedService,
                                    manager_provider=Manager,
                                    owner_resolver=lambda _request: None))
    result = TestClient(app).get(BASE + "/performance", params=query())
    assert result.status_code == 401


def test_api_has_no_state_changing_routes(api):
    client, _, _ = api
    for endpoint in ("/cohorts", "/performance", "/context", "/sessions", "/regimes", "/volatility"):
        assert client.post(BASE + endpoint, json={}).status_code == 405


def test_registered_app_resolves_authenticated_multi_user_owner(monkeypatch):
    import app as hub_app
    import webhook_api
    manager, service = Manager(), CachedService()
    monkeypatch.setenv("HUB_MULTI_USER", "1")
    monkeypatch.setattr(hub_app.settings, "auth_mode", "legacy")
    monkeypatch.setattr(hub_app, "_user", lambda _request: "alice")
    monkeypatch.setattr(webhook_api, "instance_manager", manager)
    monkeypatch.setattr(webhook_api, "strategy_intelligence", service, raising=False)
    response = TestClient(hub_app.app).get(BASE + "/performance", params=query())
    assert response.status_code == 200
    assert service.calls[0][1]["owner_id"] == "alice"
    assert response.json()["profitability_verified"] is False


def test_registered_app_does_not_accept_webhook_credential_as_intelligence_access(monkeypatch):
    import app as hub_app
    import webhook_api
    manager, service = Manager(), CachedService()
    monkeypatch.setattr(hub_app.settings, "auth_mode", "legacy")
    monkeypatch.setattr(hub_app.settings, "admin_key", "isolated-control-only")
    monkeypatch.setattr(hub_app, "_user", lambda _request: None)
    monkeypatch.setattr(webhook_api, "instance_manager", manager)
    monkeypatch.setattr(webhook_api, "strategy_intelligence", service, raising=False)
    response = TestClient(hub_app.app).get(BASE + "/performance", params=query(),
                                         headers={"x-webhook-secret": "isolated-webhook-only"})
    assert response.status_code == 401
    assert service.calls == []


def test_control_credential_authorizes_only_deployment_owner(monkeypatch):
    import app as hub_app
    import webhook_api
    from services.tenancy import OWNER_TENANT
    manager, service = Manager(), CachedService()
    manager.mine.owner_id = OWNER_TENANT
    monkeypatch.setenv("HUB_MULTI_USER", "1")
    monkeypatch.setattr(hub_app.settings, "auth_mode", "legacy")
    monkeypatch.setattr(hub_app.settings, "admin_key", "isolated-control-only")
    monkeypatch.setattr(hub_app, "_user", lambda _request: None)
    monkeypatch.setattr(webhook_api, "instance_manager", manager)
    monkeypatch.setattr(webhook_api, "strategy_intelligence", service, raising=False)
    response = TestClient(hub_app.app).get(BASE + "/performance", params=query(),
                                         headers={"x-webhook-secret": "isolated-control-only"})
    assert response.status_code == 200
    assert service.calls[0][1]["owner_id"] == OWNER_TENANT
    service.calls.clear()
    foreign = TestClient(hub_app.app).get(BASE + "/performance", params=query(instance_id="theirs"),
                                        headers={"x-webhook-secret": "isolated-control-only"})
    assert foreign.status_code == 404 and service.calls == []


def test_existing_supabase_legacy_engine_restriction_is_preserved(monkeypatch):
    import app as hub_app
    import webhook_api
    from services.supabase_auth import Principal
    manager, service = Manager(), CachedService()
    principal = Principal(id="alice", email="person@example.test", email_confirmed=True,
                          full_name="Person", role="user")
    monkeypatch.setattr(hub_app.settings, "auth_mode", "supabase")
    monkeypatch.setattr(hub_app, "_supabase_principal", lambda _request: principal)
    monkeypatch.setattr(hub_app, "_user", lambda _request: principal.id)
    monkeypatch.setattr(webhook_api, "instance_manager", manager)
    monkeypatch.setattr(webhook_api, "strategy_intelligence", service, raising=False)
    response = TestClient(hub_app.app).get(BASE + "/performance", params=query())
    assert response.status_code == 403
    assert service.calls == []


def test_repeated_query_fields_cannot_select_an_ambiguous_identity(api):
    client, _, service = api
    values = list(query().items()) + [("instance_id", "theirs")]
    response = client.get(BASE + "/performance", params=values)
    assert response.status_code == 422
    assert service.calls == []


def test_openapi_exposes_the_versioned_query_contract(api):
    client, _, _ = api
    schema = client.get("/openapi.json").json()
    parameters = schema["paths"][BASE + "/performance"]["get"]["parameters"]
    assert {item["name"] for item in parameters} == set(query()) | {
        "simulation_session_id", "lab_id", "symbol", "group_by"}
    assert all(item["in"] == "query" for item in parameters)


@pytest.mark.parametrize("field", ["strategy_id", "execution_mode", "source_kind"])
@pytest.mark.parametrize("value", [None, "", "   "])
def test_required_exact_cohort_filters_reject_missing_or_blank_values(api, field, value):
    client, _, service = api
    params = query()
    if value is None:
        params.pop(field)
    else:
        params[field] = value
    result = client.get(BASE + "/performance", params=params)
    assert result.status_code == 422
    assert service.calls == []


def _persisted_service(tmp_path):
    """Observed test episodes/context flow through the real immutable stores."""
    from datetime import datetime, timezone
    from data.journal_store import JournalStore
    from services.market_context_classifier import classifier_definition
    from services.strategy_evidence import StrategyEvidence
    from services.strategy_identity import configuration_fingerprint
    from services.strategy_intelligence_metrics import EvidenceCohort
    from services.strategy_intelligence_service import StrategyIntelligenceService
    from services.strategy_intelligence_v2 import calculate_context_performance

    path = tmp_path / "scoped-journal.db"
    journal = JournalStore(path)
    service = StrategyIntelligenceService(journal)
    definition = classifier_definition()
    journal.context.save_classifier_version(definition)
    now = datetime.now(timezone.utc).isoformat()
    cohorts = []
    for owner, instance, session, symbol, profit in (("alice", "mine", "current", "BTCUSDT", "9"),
                                                   ("bob", "theirs", "other", "ETHUSDT", "777")):
        identity = cohort(owner_id=owner, instance_id=instance, simulation_session_id=session,
                          account_id=f"instance:{instance}:{session}", config_fingerprint=configuration_fingerprint({}))
        exact = EvidenceCohort(**identity)
        trade_id, position_id = f"trade-{instance}", f"position-{instance}"
        context = {**identity, "strategy_config_hash": exact.config_fingerprint, "configuration": {},
                   "identity_status": "observed", "risk_amount_at_entry": "10", "funding_coverage": "UNKNOWN"}
        observer = StrategyEvidence(journal)
        opened = {"action": "opened", "execution_id": f"open-{instance}", "symbol": symbol,
                  "side": "long", "price": "100", "size": "2", "position_id": position_id,
                  "trade_id": trade_id, "fee": "0", "pnl": "0", "executed_at": "2026-09-01T10:01:00Z", "receipt": {}}
        observer.observe_fill(SimpleNamespace(**opened), context)
        closed = {**opened, "action": "closed", "execution_id": f"close-{instance}", "price": "105",
                  "pnl": profit, "fee": "1", "executed_at": "2026-09-01T11:00:00Z"}
        observer.observe_fill(SimpleNamespace(**closed), context)
        episode_id = journal.episode_for_trade(trade_id)["episode_id"]
        journal.context.record_market_context({
            **identity, "strategy_config_hash": exact.config_fingerprint, "symbol": symbol,
            "trade_id": trade_id, "episode_id": episode_id, "entry_timeframe": "5m", "higher_timeframe": "1h",
            "direction": "long", "session": "LONDON", "trend_regime": "BULL", "volatility_regime": "NORMAL",
            "structure_regime": "UNKNOWN", "context_quality": "VALID", "evidence_quality": "PARTIAL",
            "classifier_id": definition["classifier_id"], "classifier_version": definition["classifier_version"],
            "parameter_hash": definition["parameter_hash"], "classification_timestamp": now,
            "signal_timestamp": "2026-09-01T10:00:00Z", "entry_timestamp": "2026-09-01T10:01:00Z",
            "market_data_timestamp": "2026-09-01T09:59:59Z", "last_closed_candle_timestamp": "2026-09-01T09:59:59Z"})
        scope = {key: identity[key] for key in ("owner_id", "account_id", "instance_id", "simulation_session_id")}
        result = calculate_context_performance(journal.episodes(), journal.context.market_contexts(),
                                              cohort=exact, group_by=["symbol"], calculation_timestamp=now)
        result.update(run_id=f"run-{instance}", cache_key=service._cache_key(exact, ("symbol",)),
                      input_watermark=f"observed-test-input-{instance}", calculated_at=now,
                      contract_version="strategy_intelligence.v2", scope=scope, cohort=identity, group_by=["symbol"])
        journal.context.record_intelligence_run(result)
        cohorts.append(identity)
    return path, journal, service, cohorts


def test_registered_api_reads_persisted_groups_and_context_without_reconciliation_or_writes(monkeypatch, tmp_path):
    import app as hub_app
    import webhook_api
    path, journal, service, identities = _persisted_service(tmp_path)
    manager = Manager()
    monkeypatch.setenv("HUB_MULTI_USER", "1")
    monkeypatch.setattr(hub_app.settings, "auth_mode", "legacy")
    monkeypatch.setattr(hub_app, "_user", lambda _request: "alice")
    monkeypatch.setattr(webhook_api, "instance_manager", manager)
    monkeypatch.setattr(webhook_api, "strategy_intelligence", service)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("A cached GET must never calculate, reconcile, mutate, or scan complete history")

    monkeypatch.setattr(service, "refresh", forbidden)
    monkeypatch.setattr(journal, "get_evidence_completeness_snapshot", forbidden)
    monkeypatch.setattr(journal, "episodes", forbidden)
    monkeypatch.setattr(journal, "transaction", forbidden)
    monkeypatch.setattr(webhook_api.ledger, "get_authoritative_evidence_snapshot", forbidden)
    before = path.read_bytes()
    client = TestClient(hub_app.app)
    discovery = client.get(BASE + "/cohorts")
    assert discovery.status_code == 200
    assert discovery.json()["cohorts"] == [identities[0]]
    params = query(config_fingerprint=identities[0]["config_fingerprint"])
    performance = client.get(BASE + "/performance", params=params)
    assert performance.status_code == 200
    assert performance.json()["groups"][0]["metrics"]["net_pnl"] == "9"
    assert performance.json()["profitability_verified"] is False
    assert performance.json()["cache_status"] == "RECONCILIATION_REQUIRED"
    contexts = client.get(BASE + "/context", params=params)
    assert contexts.status_code == 200
    assert [row["trade_id"] for row in contexts.json()["contexts"]] == ["trade-mine"]
    assert contexts.json()["contexts"][0]["signal_timestamp"] == "2026-09-01T10:00:00+00:00"
    for result in (discovery, performance, contexts):
        assert "ETHUSDT" not in result.text and "777" not in result.text
        assert result.headers["cache-control"] == "no-store"
    assert path.read_bytes() == before


def test_old_persisted_cache_remains_visible_but_unverified_after_restart(monkeypatch, tmp_path):
    from routers.strategy_intelligence import create_router
    _, journal, service, identities = _persisted_service(tmp_path)
    restarted = type(service)(journal)
    app = FastAPI()
    app.include_router(create_router(service_provider=lambda: restarted, manager_provider=Manager,
                                    owner_resolver=lambda _request: "alice"))
    response = TestClient(app).get(BASE + "/performance", params=query(config_fingerprint=identities[0]["config_fingerprint"]))
    assert response.status_code == 200
    assert response.json()["groups"][0]["metrics"]["completed_episode_count"] == 1
    assert response.json()["cache_status"] == "RECONCILIATION_REQUIRED"
    assert response.json()["profitability_verified"] is False
    assert all(group["profitability"]["verified"] is False for group in response.json()["groups"])


def test_corrupt_cache_key_cannot_expose_another_owners_persisted_result(tmp_path):
    from datetime import datetime, timedelta, timezone
    from routers.strategy_intelligence import create_router
    from services.strategy_intelligence_metrics import EvidenceCohort
    _, journal, service, identities = _persisted_service(tmp_path)
    owned_key = service._cache_key(EvidenceCohort(**identities[0]), ("symbol",))
    foreign_key = service._cache_key(EvidenceCohort(**identities[1]), ("symbol",))
    corrupted = journal.context.get_latest_intelligence_run(foreign_key)
    corrupted.update(run_id="wrong-scope-cache-key", cache_key=owned_key,
                     calculated_at=(datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat())
    journal.context.record_intelligence_run(corrupted)
    app = FastAPI()
    app.include_router(create_router(service_provider=lambda: service, manager_provider=Manager,
                                    owner_resolver=lambda _request: "alice"))
    result = TestClient(app).get(BASE + "/performance", params=query(config_fingerprint=identities[0]["config_fingerprint"]))
    assert result.status_code == 200
    assert result.json()["cohort"]["owner_id"] == "alice"
    assert result.json()["groups"][0]["metrics"]["net_pnl"] == "9"
    assert "ETHUSDT" not in result.text and "777" not in result.text


@pytest.mark.parametrize("changed", ["strategy_version", "config_fingerprint", "execution_mode", "simulation_session_id"])
def test_cache_payload_must_match_every_requested_identity_dimension(tmp_path, changed):
    from datetime import datetime, timedelta, timezone
    from routers.strategy_intelligence import create_router
    from services.strategy_intelligence_metrics import EvidenceCohort
    _, journal, service, identities = _persisted_service(tmp_path)
    key = service._cache_key(EvidenceCohort(**identities[0]), ("symbol",))
    conflicting = journal.context.get_latest_intelligence_run(key)
    conflicting["cohort"][changed] = "different-observed-identity"
    conflicting.update(run_id="mismatched-cache-payload", calculated_at=(datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat())
    journal.context.record_intelligence_run(conflicting)
    app = FastAPI()
    app.include_router(create_router(service_provider=lambda: service, manager_provider=Manager,
                                    owner_resolver=lambda _request: "alice"))
    result = TestClient(app).get(BASE + "/performance", params=query(config_fingerprint=identities[0]["config_fingerprint"]))
    assert result.status_code == 200
    assert result.json()["groups"] == []
    assert result.json()["profitability_verified"] is False
    assert "different-observed-identity" not in result.text
