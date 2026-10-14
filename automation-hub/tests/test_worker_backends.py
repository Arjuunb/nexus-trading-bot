from services.worker_backends import research_context,shadow_store
from services.postgres_workers import validate_dsn
import pytest


def test_explicit_backend_does_not_fall_back(monkeypatch):
    class Context:pass
    context=Context()
    monkeypatch.setenv('NEXUS_RESEARCH_WORKER_BACKEND','postgresql')
    monkeypatch.delenv('NEXUS_RESEARCH_WORKER_DSN',raising=False)
    with pytest.raises(ValueError,match='DSN_REQUIRED'):research_context(context)
    monkeypatch.setenv('NEXUS_RESEARCH_WORKER_BACKEND','unknown')
    with pytest.raises(ValueError,match='INVALID_RESEARCH'):research_context(context)
    monkeypatch.setenv('NEXUS_SHADOW_WORKER_BACKEND','postgresql')
    monkeypatch.delenv('NEXUS_SHADOW_WORKER_DSN',raising=False)
    with pytest.raises(ValueError,match='DSN_REQUIRED'):shadow_store(None)


def test_backend_authority_cannot_change_in_a_running_context(monkeypatch):
    class Context:pass
    context=Context();monkeypatch.setenv('NEXUS_RESEARCH_WORKER_BACKEND','sqlite')
    assert research_context(context) is context
    monkeypatch.setenv('NEXUS_RESEARCH_WORKER_BACKEND','postgresql')
    with pytest.raises(ValueError,match='REQUIRES_RESTART'):research_context(context)


@pytest.mark.parametrize('dsn',['/tmp/state.sqlite','file:///tmp/state.sqlite','postgresql://user@localhost/',
    'postgresql://user@localhost/db?options=-csearch_path%3Dpublic','http://localhost/db'])
def test_dsn_namespace_is_strict(dsn):
    with pytest.raises(ValueError):validate_dsn(dsn)
