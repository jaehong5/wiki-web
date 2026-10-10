import json
import threading
import time
import urllib.request
import unittest
from unittest.mock import patch

from model_stream import answer_prefix, safe_preview
from test_server import HTTPTestSupport, SyntheticNotesTests, app


class PreviewTests(unittest.TestCase):
    def test_every_split_of_json_escapes_unicode_and_private_tokens(self):
        text = '한글 "quoted" \\ path\n😀 [[PRIVATE_' + 'a' * 32 + ']] 다음'
        for ascii_only in (False, True):
            wire = json.dumps({'answer': text, 'sources': []}, ensure_ascii=ascii_only)
            for i in range(1, len(wire)):
                prefix = answer_prefix(wire[:i])
                self.assertTrue(text.startswith(prefix), (i, prefix))
                preview = safe_preview(prefix)
                self.assertNotIn('PRIVATE_', preview)
                self.assertNotIn('aaaa', preview)
                preview.encode('utf-8')
            self.assertEqual(answer_prefix(wire), text)

    def test_does_not_stream_sources_or_nested_answer_fields(self):
        for wire in ('{"sources":[{"answer":"wrong"}],"answer":"right"}',
                     '{"answer":null}', '{"answer":"bad\\xescape"}',
                     '{"answer":"raw\nnewline"}'):
            self.assertEqual(answer_prefix(wire), '')


class JobStreamTests(HTTPTestSupport, SyntheticNotesTests):
    def test_stream_requires_authentication_and_matching_owner(self):
        ident = 'd' * 32
        app.JOBS[ident] = {'id': ident, 'owner': app.principal(self.token()), 'status': 'running',
                          'created': time.monotonic(), 'preview': 'owner only'}
        self.assertEqual(self.request('/api/jobs/' + ident + '?stream=1')[0], 401)
        self.assertEqual(self.request('/api/jobs/' + ident + '?stream=1', self.token(sub='other'))[0], 404)

    def test_flushes_preview_before_completion_and_disconnect_does_not_cancel_job(self):
        ident = 'e' * 32
        job = {'id': ident, 'owner': app.principal(self.token()), 'status': 'running', 'stage': 'answering',
               'created': time.monotonic(), 'preview': 'partial [보호된 값]'}
        app.JOBS[ident] = job
        req = urllib.request.Request(self.url + '/api/jobs/' + ident + '?stream=1',
                                     headers={'Cf-Access-Jwt-Assertion': self.token()})
        with urllib.request.urlopen(req, timeout=2) as stream:
            self.assertIn('application/x-ndjson', stream.headers['Content-Type'])
            self.assertEqual(stream.headers['X-Accel-Buffering'], 'no')
            first = json.loads(stream.readline())
            self.assertEqual(first['status'], 'running')
            self.assertNotIn('protected_values', first)
            self.assertNotIn('sources', first)
            with app.LOCK:
                job.update(status='completed', answer='done', sources=[])
                job.pop('preview')
            final = json.loads(stream.readline())
            self.assertEqual(final['answer'], 'done')
        job.update(status='running', preview='second connection')
        with urllib.request.urlopen(req, timeout=2) as stream:
            self.assertEqual(json.loads(stream.readline())['status'], 'running')
        self.assertEqual(job['status'], 'running')
        # Let the disconnected handler observe a terminal state and release its slot.
        job['status'] = 'cancelled'

    def test_concurrent_stream_limit_is_bounded_without_starting_work(self):
        ident = 'f' * 32
        app.JOBS[ident] = {'id': ident, 'owner': app.principal(self.token()), 'status': 'running',
                          'created': time.monotonic()}
        with patch.object(app, 'STREAMS', threading.BoundedSemaphore(0)):
            self.assertEqual(self.request('/api/jobs/' + ident + '?stream=1', self.token())[0], 503)


if __name__ == '__main__':
    unittest.main()
