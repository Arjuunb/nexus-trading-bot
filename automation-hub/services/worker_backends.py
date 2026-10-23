"""Explicit metadata authority selection; never falls back after a PG failure."""
import os
import hashlib


def research_context(sqlite_context):
    backend=os.environ.get('NEXUS_RESEARCH_WORKER_BACKEND','sqlite')
    profile=(backend,hashlib.sha256((os.environ.get('NEXUS_RESEARCH_WORKER_DSN') or '').encode()).hexdigest())
    old=getattr(sqlite_context,'_worker_backend_profile',profile)
    if old!=profile:raise ValueError('WORKER_BACKEND_CHANGE_REQUIRES_RESTART')
    if backend=='sqlite':
        sqlite_context._worker_backend_profile=profile
        return sqlite_context
    if backend!='postgresql':raise ValueError('INVALID_RESEARCH_WORKER_BACKEND')
    from services.postgres_workers import PgResearchContext
    dsn=os.environ.get('NEXUS_RESEARCH_WORKER_DSN')
    if not dsn:raise ValueError('POSTGRESQL_WORKER_DSN_REQUIRED')
    current=getattr(sqlite_context,'_postgres_research_context',None)
    if current is None:
        current=PgResearchContext(dsn)
        sqlite_context._postgres_research_context=current
        sqlite_context._worker_backend_profile=profile
    return current


def shadow_store(sqlite_store):
    backend=os.environ.get('NEXUS_SHADOW_WORKER_BACKEND','sqlite')
    if backend=='sqlite':
        from services.shadow_observation_store import ShadowObservationStore
        return ShadowObservationStore(sqlite_store() if callable(sqlite_store) else sqlite_store)
    if backend!='postgresql':raise ValueError('INVALID_SHADOW_WORKER_BACKEND')
    from services.postgres_workers import PgShadowObservationStore
    dsn=os.environ.get('NEXUS_SHADOW_WORKER_DSN')
    if not dsn:raise ValueError('POSTGRESQL_WORKER_DSN_REQUIRED')
    return PgShadowObservationStore(dsn)
