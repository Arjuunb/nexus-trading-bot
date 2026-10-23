"""The HTTP policy rejects execution before a handler and retains auth."""
import asyncio
import os
import unittest
from unittest.mock import patch
from services.acceptance_policy import AcceptanceReadOnlyMiddleware


class AcceptanceHTTP(unittest.TestCase):
    def request(self, method, path, *, enabled=True, authenticated=True):
        calls, messages = [], []
        async def downstream(scope, receive, send):
            calls.append(scope)
            await send({'type': 'http.response.start', 'status': 200 if authenticated else 401, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'{}'})
        async def send(message):
            messages.append(message)
        asyncio.run(AcceptanceReadOnlyMiddleware(downstream, enabled=enabled)(
            {'type': 'http', 'method': method, 'path': path}, None, send))
        return messages, calls

    def test_financial_mutations_aliases_and_unknown_operations_are_denied_before_handlers(self):
        for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            for path in ('/webhook', '/api/v1/webhook', '/paper-v2/orders', '/instances/one/start',
                '/bots/one/start', '/grid/start', '/settings',
                '/api/v2/strategy-intelligence/shadow/registrations',
                '/api/v2/strategy-intelligence/shadow/registrations/one/activate',
                '/api/v2/strategy-intelligence/future-execution-command'):
                with self.subTest(method=method, path=path):
                    messages, calls = self.request(method, path)
                    self.assertEqual(messages[0]['status'], 423)
                    self.assertEqual(calls, [])

    def test_financial_reads_that_may_start_a_feed_are_denied(self):
        for path in ('/price-action/visual', '/smc/visual', '/market/live', '/instances/one/status'):
            messages, calls = self.request('GET', path)
            self.assertEqual(messages[0]['status'], 423)
            self.assertFalse(calls)

    def test_allowed_research_and_auth_operations_keep_downstream_authentication(self):
        for method, path in [('GET', '/api/v2/strategy-intelligence/cohorts'),
            ('POST', '/api/v2/strategy-intelligence/weekly-research'),
            ('POST', '/api/v2/strategy-intelligence/research-jobs'),
            ('POST', '/api/v2/strategy-intelligence/matched-experiments/one/reconcile'),
            ('POST', '/auth/supabase/session'), ('POST', '/auth/supabase/refresh')]:
            messages, calls = self.request(method, path, authenticated=False)
            self.assertEqual(messages[0]['status'], 401)
            self.assertEqual(len(calls), 1)

    def test_similar_prefixes_and_unknown_research_commands_are_denied(self):
        for path in ('/api/v2/strategy-intelligenceevil/research-jobs',
            '/api/v2/strategy-intelligence/research-jobs/one/start',
            '/api/v2/strategy-intelligence/research-jobs/one/run/extra'):
            self.assertEqual(self.request('POST', path)[0][0]['status'], 423)

    def test_flag_off_preserves_normal_routing(self):
        self.assertEqual(self.request('POST', '/webhook', enabled=False)[0][0]['status'], 200)

    def test_captured_gate_cannot_be_disabled_by_environment_change(self):
        async def forbidden(*_):
            raise AssertionError('Financial handler reached')
        messages = []
        async def send(message):
            messages.append(message)
        policy = AcceptanceReadOnlyMiddleware(forbidden, enabled=True)
        with patch.dict(os.environ, {'NEXUS_ACCEPTANCE_READ_ONLY': '0'}):
            asyncio.run(policy({'type': 'http', 'method': 'POST', 'path': '/webhook'}, None, send))
        self.assertEqual(messages[0]['status'], 423)

    def test_websocket_cannot_start_execution_or_a_public_feed(self):
        async def forbidden(*_):
            raise AssertionError('WebSocket handler reached')
        messages = []
        async def send(message):
            messages.append(message)
        asyncio.run(AcceptanceReadOnlyMiddleware(forbidden, enabled=True)({'type': 'websocket'}, None, send))
        self.assertEqual(messages, [{'type': 'websocket.close', 'code': 1008}])
