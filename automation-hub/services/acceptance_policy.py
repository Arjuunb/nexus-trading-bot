"""Explicit staging acceptance policy; saved execution intent stays intact."""
import json
import os
from pathlib import Path
import re
import sqlite3


def acceptance_read_only():
    value = os.environ.get('NEXUS_ACCEPTANCE_READ_ONLY', '0')
    if value not in ('', '0', '1'):
        raise RuntimeError('INVALID_NEXUS_ACCEPTANCE_READ_ONLY')
    return value == '1'


def existing_financial_connection(path, *, autocommit=False):
    source = Path(path)
    if not source.is_file() or source.is_symlink():
        raise RuntimeError('EXISTING_FINANCIAL_DATABASE_REQUIRED')
    connection = sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True,
        timeout=10, check_same_thread=False, isolation_level=None if autocommit else '')
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA query_only=ON')
    return connection


def require_financial_columns(connection, requirements):
    for table, columns in requirements.items():
        present = {row[1] for row in connection.execute('PRAGMA table_info("' + table + '")')}
        if not set(columns) <= present:
            raise RuntimeError('INCOMPLETE_FINANCIAL_DATABASE:' + table)


class _ReadOnlyPrimaryQuery:
    _READ_METHODS = frozenset({'select', 'eq', 'neq', 'gt', 'gte', 'lt', 'lte',
        'like', 'ilike', 'is_', 'in_', 'contains', 'contained_by', 'overlaps',
        'match', 'filter', 'or_', 'order', 'range', 'limit', 'single',
        'maybe_single', 'text_search'})

    def __init__(self, query):
        self.__query = query

    def __getattr__(self, name):
        if name == 'execute':
            return self.__query.execute
        if name == 'not_':
            return _ReadOnlyPrimaryQuery(self.__query.not_)
        if name not in self._READ_METHODS:
            raise RuntimeError('ACCEPTANCE_PRIMARY_READ_ONLY')
        def operation(*args, **kwargs):
            return _ReadOnlyPrimaryQuery(getattr(self.__query, name)(*args, **kwargs))
        return operation


class ReadOnlyPrimaryClient:
    """Keep the real primary and tenant filters; reject mutation builders.

    The allowed RPCs are SELECT-only SQL STABLE evidence readers from
    0005_paper_evidence_outbox.sql. Unknown RPCs remain denied.
    """
    def __init__(self, client):
        self.__client = client

    def table(self, name):
        return _ReadOnlyPrimaryQuery(self.__client.table(name))

    def from_(self, name):
        return self.table(name)

    def rpc(self, name, *args, **kwargs):
        if name not in ('paper_evidence_capabilities', 'paper_evidence_snapshot'):
            raise RuntimeError('ACCEPTANCE_PRIMARY_READ_ONLY')
        return _ReadOnlyPrimaryQuery(self.__client.rpc(name, *args, **kwargs))


_PREFIX = '/api/v2/strategy-intelligence'
_READ_EXACT = {'/', '/health', '/version', '/api/version', '/api/v1', '/auth/me',
    '/auth/session', '/user/settings', '/login', '/signup', '/robots.txt',
    '/sitemap.xml', '/site.webmanifest', '/favicon.ico'}
_READ_PREFIXES = ('/app/', '/assets/', '/webui/', '/landing/', '/auth/',
    '/nexus-mark', '/apple-touch', '/icon-', '/maskable-', '/mstile-',
    '/og-image', '/logo-mark')
_AUTH_POST = {'/login', '/logout', '/auth/login', '/auth/logout',
    '/auth/supabase/session', '/auth/supabase/refresh'}
_METADATA_POST = re.compile(r'^(?:'
    r'/weekly-research|/comparisons|/research-champions|/research-candidates|/research-campaigns|'
    r'/research-jobs(?:/[^/]+/(?:run|cancel))?|'
    r'/research-agent/hypotheses(?:/[^/]+/(?:validate|approve|submit))?|'
    r'/matched-experiments(?:/[^/]+/(?:submit|reconcile|cancel))?|'
    r'/paper-reconciliation/(?:preview|reports(?:/[^/]+/review)?)|'
    r'/forward-qualification/(?:eligibility|reports(?:/[^/]+/review)?)|'
    r'/shadow/comparisons)$')


def request_allowed(method, path):
    if method in ('GET', 'HEAD'):
        return (path in _READ_EXACT or path == '/app' or path == _PREFIX
            or path.startswith(_PREFIX + '/') or path.startswith(_READ_PREFIXES))
    if method == 'OPTIONS':
        return True
    if method == 'POST':
        return path in _AUTH_POST or (path.startswith(_PREFIX + '/') and
            _METADATA_POST.fullmatch(path[len(_PREFIX):]) is not None)
    return False


class AcceptanceReadOnlyMiddleware:
    def __init__(self, app, *, enabled):
        self.app = app
        self.enabled = bool(enabled)

    async def __call__(self, scope, receive, send):
        if self.enabled and scope['type'] == 'websocket':
            await send({'type': 'websocket.close', 'code': 1008})
            return
        if self.enabled and scope['type'] == 'http' and not request_allowed(scope['method'], scope['path']):
            body = json.dumps({'detail': 'ACCEPTANCE_READ_ONLY',
                'scope': 'AUTHENTICATED_RESEARCH_METADATA_ONLY; FINANCIAL_EXECUTION_DISABLED'}).encode()
            await send({'type': 'http.response.start', 'status': 423,
                'headers': [(b'content-type', b'application/json'), (b'cache-control', b'no-store')]})
            await send({'type': 'http.response.body', 'body': body})
            return
        await self.app(scope, receive, send)
