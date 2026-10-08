# Copyright (c) 2026, Oracle and/or its affiliates.
"""Read retries must not duplicate writes or turn missing evidence into approval."""
import http.client
import io
import unittest
import urllib.error
from unittest.mock import patch

from scripts.ce_merge.github import GitHub
from scripts.ce_merge.policy import Blocked


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.api = GitHub({'repository': 'example/ce', 'maintainers': ['owner']})
        self.api.token = lambda fork=False: 'test-token'
        self.sleep = patch('scripts.ce_merge.github.time.sleep').start()
        self.addCleanup(patch.stopall)

    def test_disconnected_permission_read_recovers(self):
        with patch('scripts.ce_merge.github.urllib.request.urlopen', side_effect=[
                http.client.RemoteDisconnected('connection closed'),
                io.BytesIO(b'{"permission":"admin"}')]) as opened:
            self.assertTrue(self.api.can_integrate('owner'))
        self.assertEqual(opened.call_count, 2)
        self.sleep.assert_called_once_with(1)

    def test_exhausted_read_still_blocks_permission(self):
        with patch('scripts.ce_merge.github.urllib.request.urlopen',
                   side_effect=http.client.RemoteDisconnected('private diagnostic')) as opened:
            with self.assertRaisesRegex(Blocked, 'read unavailable after 3 attempts') as error:
                self.api.can_integrate('owner')
        self.assertNotIn('private diagnostic', str(error.exception))
        self.assertEqual(opened.call_count, 3)
        self.assertEqual([c.args for c in self.sleep.call_args_list], [(1,), (2,)])

    def test_connection_failures_retry_only_reads(self):
        for error in (urllib.error.URLError('offline'), TimeoutError(), ConnectionResetError(),
                      http.client.IncompleteRead(b'partial', 20)):
            with self.subTest(error=type(error).__name__), patch(
                    'scripts.ce_merge.github.urllib.request.urlopen',
                    side_effect=[error, io.BytesIO(b'{"ok":true}')]) as opened:
                self.assertEqual(self.api.request('GET', '/test'), {'ok': True})
                self.assertEqual(opened.call_count, 2)

    def test_failure_while_reading_response_is_retried(self):
        class Interrupted(io.BytesIO):
            def read(self, *args):
                raise http.client.IncompleteRead(b'partial', 20)
        interrupted = Interrupted()
        with patch('scripts.ce_merge.github.urllib.request.urlopen',
                   side_effect=[interrupted, io.BytesIO(b'{}')]) as opened:
            self.assertEqual(self.api.request('GET', '/test'), {})
        self.assertTrue(interrupted.closed)
        self.assertEqual(opened.call_count, 2)

    def test_gateway_errors_retry_reads_and_close_response(self):
        for code in (502, 503, 504):
            body = io.BytesIO(b'private server response')
            with self.subTest(code=code), patch('scripts.ce_merge.github.urllib.request.urlopen',
                    side_effect=[urllib.error.HTTPError('https://api.github.com/test', code,
                                                       'gateway error', {}, body), io.BytesIO(b'{}')]):
                self.assertEqual(self.api.request('GET', '/test'), {})
                self.assertTrue(body.closed)

    def test_auth_not_found_and_rate_limits_do_not_retry(self):
        for code in (401, 403, 404, 429):
            with self.subTest(code=code), patch('scripts.ce_merge.github.urllib.request.urlopen',
                    side_effect=urllib.error.HTTPError('https://api.github.com/test', code,
                                                       'private diagnostic', {}, io.BytesIO())) as opened:
                with self.assertRaisesRegex(Blocked, f'^GitHub API returned HTTP {code}$'):
                    self.api.request('GET', '/test')
                self.assertEqual(opened.call_count, 1)
        self.sleep.assert_not_called()

    def test_uncertain_writes_and_graphql_are_not_replayed(self):
        for method, path in (('POST', '/check-runs'), ('POST', '/graphql'), ('PATCH', '/pulls/1'),
                             ('DELETE', '/labels/test')):
            with self.subTest(method=method, path=path), patch(
                    'scripts.ce_merge.github.urllib.request.urlopen',
                    side_effect=http.client.RemoteDisconnected('private diagnostic')) as opened:
                with self.assertRaisesRegex(ConnectionError, 'request outcome may be unknown') as error:
                    self.api.request(method, path, {})
                self.assertNotIn('private diagnostic', str(error.exception))
                self.assertEqual(opened.call_count, 1)
        self.sleep.assert_not_called()

    def test_gateway_write_is_not_replayed(self):
        with patch('scripts.ce_merge.github.urllib.request.urlopen', side_effect=urllib.error.HTTPError(
                'https://api.github.com/test', 503, 'unavailable', {}, io.BytesIO())) as opened:
            with self.assertRaisesRegex(Blocked, 'HTTP 503'):
                self.api.request('POST', '/check-runs', {})
        self.assertEqual(opened.call_count, 1)
        self.sleep.assert_not_called()


if __name__ == '__main__':
    unittest.main()
