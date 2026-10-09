import json
import os
from pathlib import Path
import socket
import sys
import threading
import subprocess
import tempfile
from unittest.mock import patch
import time
from types import SimpleNamespace
import unittest
import urllib.error
import urllib.request

from cryptography.hazmat.primitives.asymmetric import rsa
import jwt

from test_support import load_test_app

app = load_test_app()


class SyntheticNotesTests(unittest.TestCase):
    def setUp(self):
        fixture = tempfile.TemporaryDirectory(prefix='wiki-test-notes-')
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name)
        self.source_text = '# Example\nSYNTHETIC-UNCHANGED-CREDENTIAL\nThird line\n'
        (self.root / 'Example.md').write_text(self.source_text, encoding='utf-8')
        root_patch = patch.object(app, 'ROOT', self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        # Admission tests must not inherit previous tests' rate/job state.
        for state in (app.JOBS, app.RATES):
            state_patch = patch.dict(state, clear=True)
            state_patch.start()
            self.addCleanup(state_patch.stop)


class HTTPTestSupport:
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.original_jwks = app.JWKS
        app.JWKS = SimpleNamespace(get_signing_key_from_jwt=lambda _: SimpleNamespace(key=cls.key.public_key()))
        cls.server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = 'http://127.0.0.1:' + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        app.JWKS = cls.original_jwks

    def token(self, **overrides):
        claims = dict(sub='test-user', aud=app.CONFIG['audience'], iss=app.CONFIG['issuer'],
                      exp=int(time.time())+60, iat=int(time.time()))
        claims.update(overrides)
        return jwt.encode(claims, self.key, algorithm='RS256')

    def request(self, path, token=None, body=None, origin=app.ORIGIN):
        headers = {}
        if token:
            headers['Cf-Access-Jwt-Assertion'] = token
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers.update({'Content-Type':'application/json', 'Origin':origin})
        req = urllib.request.Request(self.url+path, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=3) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

class AccessTests(HTTPTestSupport, SyntheticNotesTests):
    def test_missing_token_blocked(self):
        self.assertEqual(self.request('/api/status')[0], 401)

    def test_fake_token_blocked(self):
        self.assertEqual(self.request('/api/status', 'fake-token')[0], 401)

    def test_other_application_token_blocked(self):
        self.assertEqual(self.request('/api/status', self.token(aud='monitor'))[0], 401)

    def test_expired_token_blocked(self):
        self.assertEqual(self.request('/api/status', self.token(exp=int(time.time())-60))[0], 401)

    def test_valid_token_accepted(self):
        status, data = self.request('/api/status', self.token())
        self.assertEqual(status, 200)
        self.assertGreater(data['notes'], 0)

    def test_cross_origin_query_blocked(self):
        self.assertEqual(self.request('/api/query', self.token(), {'question':'test'}, 'https://evil.example')[0], 403)

    def test_path_traversal_blocked(self):
        self.assertEqual(self.request('/api/source?path=../../.codex/auth.json', self.token())[0], 404)

    def test_known_source_keeps_line_numbers(self):
        status, data = self.request('/api/source?path=Example.md', self.token())
        self.assertEqual(status, 200)
        self.assertEqual(data['text'], self.source_text)
        self.assertEqual(data['text'].splitlines()[1], 'SYNTHETIC-UNCHANGED-CREDENTIAL')

    def test_job_owner_isolation(self):
        ident = 'a' * 32
        app.JOBS[ident] = {'owner': 'another-user', 'status': 'completed', 'answer':'private', 'created':time.monotonic()}
        try:
            self.assertEqual(self.request('/api/jobs/'+ident, self.token())[0], 404)
        finally:
            del app.JOBS[ident]

    def test_expired_job_cannot_be_read(self):
        ident = 'b' * 32
        owner = app.principal(self.token())
        app.JOBS[ident] = {'owner':owner,'status':'completed','answer':'test','created':time.monotonic()-7200}
        self.assertEqual(self.request('/api/jobs/'+ident,self.token())[0],404)
        self.assertNotIn(ident,app.JOBS)

    def test_periodic_cleanup_without_requests(self):
        ident = 'c' * 32
        app.JOBS[ident]={'owner':'test','status':'completed','created':0,'finished':time.monotonic()-7200}
        stop=threading.Event()
        thread=threading.Thread(target=app.cleanup_loop,args=(stop,0.01))
        thread.start()
        try:
            deadline=time.monotonic()+1
            while ident in app.JOBS and time.monotonic()<deadline:
                time.sleep(0.01)
            self.assertNotIn(ident,app.JOBS)
        finally:
            stop.set(); thread.join()

    def test_oversized_request_returns_413(self):
        status,data=self.request('/api/query',self.token(),{'question':'가'*15000})
        self.assertEqual(status,413)
        self.assertIn('너무 큽니다',data['error'])

    def test_deep_request_json_returns_400_without_job_or_cancellation_admission(self):
        # A small byte body can still exceed the JSON parser's nesting limit.
        # Send real bytes over HTTP rather than failing in the client's encoder.
        depth = 10000
        body = b'{"question":"example","history":' + b'[' * depth + b'0' + b']' * depth + b'}'
        self.assertLess(len(body), app.BODY_LIMIT)
        token = self.token()
        headers = {'Content-Type': 'application/json', 'Origin': app.ORIGIN,
                   'Cf-Access-Jwt-Assertion': token}
        with patch.object(app, 'run_job') as run:
            for path in ('/api/query', '/api/jobs/' + '9' * 32 + '/cancel'):
                with self.subTest(path=path):
                    request = urllib.request.Request(self.url + path, data=body, headers=headers)
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        urllib.request.urlopen(request, timeout=3)
                    self.assertEqual(caught.exception.code, 400)
                    self.assertIn('error', json.load(caught.exception))
        run.assert_not_called()
        self.assertEqual(app.JOBS, {})
        self.assertEqual(app.RATES, {})
        self.assertTrue(app.LIMIT.acquire(False))
        app.LIMIT.release()
        self.assertEqual(self.request('/api/status', token)[0], 200)

    def test_repeated_submission_runs_once(self):
        ident='d'*32
        gate,done=threading.Event(),threading.Event()
        calls=[]
        def fake_run(job,question,history):
            calls.append(question)
            gate.wait(2)
            with app.LOCK:
                job.update(status='completed',answer='test',sources=[],finished=time.monotonic())
                app.LIMIT.release()
            done.set()
        try:
            with patch.object(app,'run_job',fake_run):
                body={'question':'test','history':[],'request_id':ident}
                first=self.request('/api/query',self.token(),body)
                again=self.request('/api/query',self.token(),body)
                self.assertEqual(first[0],202)
                self.assertEqual(again[0],200)
                self.assertEqual(again[1]['id'],ident)
                self.assertEqual(self.request('/api/query',self.token(),dict(body,question='different'))[0],409)
                self.assertEqual(calls,['test'])
        finally:
            gate.set(); done.wait(3)
            app.JOBS.pop(ident,None)

    def test_source_uses_answer_snapshot(self):
        ident='e'*32
        owner=app.principal(self.token())
        app.JOBS[ident]={'owner':owner,'status':'completed','created':time.monotonic(),
                         'snapshots':{'example.md':'original snapshot'}}
        try:
            status,data=self.request('/api/source?path=example.md&job='+ident,self.token())
            self.assertEqual(status,200)
            self.assertEqual(data['text'],'original snapshot')
            self.assertEqual(self.request('/api/source?path=example.md&job='+ident,self.token(sub='different'))[0],404)
        finally:
            app.JOBS.pop(ident,None)

    def test_public_job_never_contains_internal_state(self):
        job={'id':'f'*32,'status':'running','question':'test','owner':'private',
             'process':object(),'cancel_event':threading.Event(),'snapshots':{'private.md':'secret'},
             'safe_history':[{'role':'assistant','content':'safe'}],
             'history_secrets':('secret',),'_privacy':object(),'cancel_before_admission':True}
        self.assertEqual(set(app.public_job(job)),{'id','status','question'})

    def test_pending_cancel_requires_explicit_flag_and_preserves_owner_isolation(self):
        ident = '4' * 32
        token = self.token()
        for body in ({}, {'pending': False}, {'pending': 1}, {'pending': 'true'}):
            self.assertEqual(self.request('/api/jobs/' + ident + '/cancel', token, body)[0], 404)
            self.assertNotIn(ident, app.JOBS)
        status, data = self.request('/api/jobs/' + ident + '/cancel', token, {'pending': True})
        self.assertEqual((status, data), (200, {'id': ident, 'status': 'cancelled'}))
        self.assertEqual(self.request('/api/jobs/' + ident, token), (200, data))
        other = self.token(sub='different-owner')
        self.assertEqual(self.request('/api/jobs/' + ident, other)[0], 404)
        self.assertEqual(self.request('/api/jobs/' + ident + '/cancel', other, {'pending': True})[0], 404)
        with patch.object(app, 'run_job') as run:
            self.assertEqual(self.request('/api/query', other, {'question': 'example', 'request_id': ident})[0], 409)
        run.assert_not_called()
        self.assertEqual(app.JOBS[ident]['owner'], app.principal(token))
        self.assertEqual(app.RATES, {})

    def test_pending_cancel_precedes_history_rate_and_worker_admission(self):
        ident = '5' * 32
        token = self.token()
        owner = app.principal(token)
        self.assertEqual(self.request('/api/jobs/' + ident + '/cancel', token, {'pending': True})[0], 200)
        app.RATES[owner].extend([time.monotonic()] * 20)
        # Neither an expired history reference nor an occupied worker should
        # prevent an already acknowledged cancellation from being returned.
        with patch.object(app, 'LIMIT', threading.BoundedSemaphore(0)), patch.object(app, 'run_job') as run:
            status, data = self.request('/api/query', token, {
                'question': 'example', 'request_id': ident, 'history': [{'job_id': '0' * 32}]})
        self.assertEqual((status, data), (200, {'id': ident, 'status': 'cancelled'}))
        run.assert_not_called()
        self.assertEqual(len(app.RATES[owner]), 20)
        self.assertNotIn('question', app.JOBS[ident])

    def test_pending_cancel_capacity_is_bounded_and_expiry_frees_only_old_markers(self):
        token = self.token()
        ids = ['6' * 32, '7' * 32, '8' * 32]
        with patch.object(app, 'PENDING_CANCEL_LIMIT', 2):
            for ident in ids[:2]:
                self.assertEqual(self.request('/api/jobs/' + ident + '/cancel', token, {'pending': True})[0], 200)
            finished = app.JOBS[ids[0]]['finished']
            self.assertEqual(self.request('/api/jobs/' + ids[2] + '/cancel', token, {'pending': True})[0], 429)
            self.assertEqual(set(app.JOBS), set(ids[:2]))
            # Retries of an existing marker remain idempotent at capacity and
            # cannot extend its retention time indefinitely.
            self.assertEqual(self.request('/api/jobs/' + ids[0] + '/cancel', token, {'pending': True})[0], 200)
            self.assertEqual(app.JOBS[ids[0]]['finished'], finished)
            app.JOBS[ids[0]]['finished'] = time.monotonic() - app.JOB_TTL - 1
            self.assertEqual(self.request('/api/jobs/' + ids[2] + '/cancel', token, {'pending': True})[0], 200)
        self.assertEqual(set(app.JOBS), set(ids[1:]))
        self.assertEqual(app.RATES, {})

    def test_pending_cancel_owner_quota_preserves_other_owner_capacity_and_marker_ttl(self):
        first, second = self.token(), self.token(sub='second-owner')
        ids = [f'{number:032x}' for number in range(1, 6)]
        with patch.object(app, 'PENDING_CANCEL_LIMIT', 3), patch.object(app, 'PENDING_CANCEL_OWNER_LIMIT', 2):
            for ident in ids[:2]:
                self.assertEqual(self.request('/api/jobs/' + ident + '/cancel', first, {'pending': True})[0], 200)
            snapshots = {ident: dict(app.JOBS[ident]) for ident in ids[:2]}
            # The first owner must leave room for a different owner, even when
            # its next cancellation arrives before the other owner's request.
            self.assertEqual(self.request('/api/jobs/' + ids[2] + '/cancel', first, {'pending': True})[0], 429)
            self.assertEqual(app.JOBS, snapshots)
            self.assertEqual(self.request('/api/jobs/' + ids[3] + '/cancel', second, {'pending': True})[0], 200)
            second_marker = dict(app.JOBS[ids[3]])
            self.assertEqual(self.request('/api/jobs/' + ids[4] + '/cancel', second, {'pending': True})[0], 429)
            # Retries cannot refresh either created/finished time or evict a
            # valid marker, including when both owner and global quotas are full.
            self.assertEqual(self.request('/api/jobs/' + ids[0] + '/cancel', first, {'pending': True})[0], 200)
            self.assertEqual(app.JOBS[ids[0]], snapshots[ids[0]])
            self.assertEqual(app.JOBS[ids[1]], snapshots[ids[1]])
            self.assertEqual(app.JOBS[ids[3]], second_marker)
            # Only the expired marker frees both its owner's and global quota.
            app.JOBS[ids[0]]['finished'] = time.monotonic() - app.JOB_TTL - 1
            self.assertEqual(self.request('/api/jobs/' + ids[2] + '/cancel', first, {'pending': True})[0], 200)
            self.assertEqual(set(app.JOBS), {ids[1], ids[2], ids[3]})
            self.assertEqual(app.JOBS[ids[1]], snapshots[ids[1]])
            self.assertEqual(app.JOBS[ids[3]], second_marker)
        self.assertEqual(app.RATES, {})

    def test_pending_cancel_of_existing_job_keeps_normal_worker_cancellation(self):
        ident = 'a' * 32
        token = self.token()
        event = threading.Event()
        app.JOBS[ident] = {'id': ident, 'owner': app.principal(token), 'status': 'running',
                          'created': time.monotonic(), 'cancel_event': event}
        with patch.object(app, 'PENDING_CANCEL_LIMIT', 1), patch.object(app, 'PENDING_CANCEL_OWNER_LIMIT', 1):
            pending_id = 'b' * 32
            self.assertEqual(self.request('/api/jobs/' + pending_id + '/cancel', token, {'pending': True})[0], 200)
            marker = dict(app.JOBS[pending_id])
            status, data = self.request('/api/jobs/' + ident + '/cancel', token, {'pending': True})
            self.assertEqual(app.JOBS[pending_id], marker)
        self.assertEqual((status, data), (200, {'id': ident, 'status': 'cancelling'}))
        self.assertTrue(event.is_set())
        self.assertNotIn('cancel_before_admission', app.JOBS[ident])

    def test_delayed_disconnected_query_cannot_run_after_pending_cancellation(self):
        ident = 'b' * 32
        token = self.token()
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        observed = {}
        original_authenticate = app.Handler.authenticate
        original_reply = app.Handler.reply
        def authenticate(handler):
            if handler.headers.get('X-Synthetic-Delay') == 'original-query':
                entered.set()
                if not release.wait(3):
                    raise AssertionError('Delayed test request was not released')
            return original_authenticate(handler)
        def reply(handler, status, data):
            delayed = handler.headers.get('X-Synthetic-Delay') == 'original-query'
            if delayed:
                observed.update(status=status, data=data)
            try:
                original_reply(handler, status, data)
            except (BrokenPipeError, ConnectionResetError):
                if not delayed:
                    raise
            finally:
                if delayed:
                    finished.set()
        body = json.dumps({'question': 'example', 'request_id': ident, 'history': []}).encode()
        headers = ('POST /api/query HTTP/1.1\r\nHost: localhost\r\n'
                   'Content-Type: application/json\r\nOrigin: ' + app.ORIGIN + '\r\n'
                   'Cf-Access-Jwt-Assertion: ' + token + '\r\n'
                   'X-Synthetic-Delay: original-query\r\n'
                   'Content-Length: ' + str(len(body)) + '\r\n\r\n').encode()
        client = socket.create_connection(('127.0.0.1', self.server.server_port), timeout=3)
        with patch.object(app.Handler, 'authenticate', authenticate), patch.object(app.Handler, 'reply', reply), \
             patch.object(app, 'LIMIT', threading.BoundedSemaphore(1)), patch.object(app, 'run_job') as run:
            try:
                client.sendall(headers + body)
                self.assertTrue(entered.wait(2))
                client.close()  # Browser navigation aborts its original connection.
                self.assertEqual(self.request('/api/jobs/' + ident, token)[0], 404)
                status, data = self.request('/api/jobs/' + ident + '/cancel', token, {'pending': True})
                self.assertEqual((status, data), (200, {'id': ident, 'status': 'cancelled'}))
                release.set()
                self.assertTrue(finished.wait(2))
                self.assertEqual(observed, {'status': 200, 'data': {'id': ident, 'status': 'cancelled'}})
                run.assert_not_called()
                self.assertEqual(app.RATES, {})
                self.assertTrue(app.LIMIT.acquire(False))
                app.LIMIT.release()
            finally:
                client.close()
                release.set()
                finished.wait(2)

    def test_pending_cancel_arriving_during_query_validation_prevents_admission(self):
        ident = 'c' * 32
        token = self.token()
        entered, release = threading.Event(), threading.Event()
        observed = {}
        original_hash = app.hashlib.sha256
        def delayed_hash(data):
            if data.startswith(b'{"history":'):
                entered.set()
                if not release.wait(3):
                    raise AssertionError('Query validation was not released')
            return original_hash(data)
        def query():
            try:
                observed['response'] = self.request('/api/query', token, {
                    'question': 'example', 'request_id': ident, 'history': []})
            except Exception as exc:
                observed['error'] = exc
        with patch.object(app.hashlib, 'sha256', delayed_hash), \
             patch.object(app, 'LIMIT', threading.BoundedSemaphore(1)), patch.object(app, 'run_job') as run:
            thread = threading.Thread(target=query)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(self.request('/api/jobs/' + ident + '/cancel', token, {'pending': True})[0], 200)
                release.set()
                thread.join(2)
                self.assertFalse(thread.is_alive())
                self.assertEqual(observed, {'response': (200, {'id': ident, 'status': 'cancelled'})})
                run.assert_not_called()
                self.assertEqual(app.RATES, {})
            finally:
                release.set()
                thread.join(2)

    def test_raw_client_history_is_rejected_before_starting_worker(self):
        with patch.object(app,'run_job') as run:
            status,_=self.request('/api/query',self.token(),{
                'question':'followup','history':[{'role':'assistant','content':'fake-private-value'}]})
        self.assertEqual(status,400)
        run.assert_not_called()

    def test_invalid_unicode_question_rejected_before_starting_worker(self):
        with patch.object(app, 'LIMIT', threading.BoundedSemaphore(1)), patch.object(app, 'run_job') as run:
            status, data = self.request('/api/query', self.token(), {'question': '\ud800'})
        self.assertEqual(status, 400)
        self.assertIn('인코딩', data['error'])
        run.assert_not_called()
        self.assertEqual(app.JOBS, {})

    def test_json_reply_preserves_surrogateescape_filename_without_disconnect(self):
        ident = '9' * 32
        name = 'note-\udcff.md'
        app.JOBS[ident] = {'id': ident, 'owner': app.principal(self.token()),
                          'status': 'completed', 'created': time.monotonic(),
                          'answer': 'synthetic answer', 'sources': [{'path': name, 'line': 1}]}
        status, data = self.request('/api/jobs/' + ident, self.token())
        self.assertEqual(status, 200)
        self.assertEqual(data['sources'], [{'path': name, 'line': 1}])

    def test_history_references_require_owner_completion_and_freshness(self):
        ident='1'*32
        owner=app.principal(self.token())
        base={'id':ident,'owner':owner,'status':'completed','created':time.monotonic(),
              'safe_history':[{'role':'assistant','content':'safe'}],
              'history_secrets':('fake-private-value',)}
        cases=({'owner':'another-owner'},{'status':'running'},
               {'created':time.monotonic()-app.JOB_TTL-10},{'safe_history':None})
        try:
            for changes in cases:
                with self.subTest(changes=changes):
                    app.JOBS[ident]=dict(base,**changes)
                    if app.JOBS[ident].get('safe_history') is None:
                        app.JOBS[ident].pop('safe_history')
                    with patch.object(app,'run_job') as run:
                        status,_=self.request('/api/query',self.token(),{
                            'question':'followup','history':[{'job_id':ident}]})
                    self.assertEqual(status,400)
                    run.assert_not_called()
            app.JOBS.pop(ident,None)
            self.assertEqual(self.request('/api/query',self.token(),{
                'question':'followup','history':[{'job_id':ident}]})[0],400)
        finally:
            app.JOBS.pop(ident,None)

    def test_history_reference_passes_only_server_authored_messages(self):
        previous_id='2'*32
        new_id='3'*32
        owner=app.principal(self.token())
        safe=[{'role':'user','content':'service password'},
              {'role':'assistant','content':'[보호된 값]'}]
        app.JOBS[previous_id]={'id':previous_id,'owner':owner,'status':'completed',
            'created':time.monotonic(),'answer':'fake-private-value',
            'safe_history':safe,'history_secrets':('fake-private-value',)}
        done=threading.Event()
        observed={}
        def run(job,question,history):
            observed.update(history=history,secrets=job['history_secrets'])
            with app.LOCK:
                job.update(status='completed',answer='safe',finished=time.monotonic())
                app.LIMIT.release()
            done.set()
        try:
            with patch.object(app,'run_job',run):
                status,_=self.request('/api/query',self.token(),{
                    'question':'followup','request_id':new_id,'history':[{'job_id':previous_id}]})
                self.assertEqual(status,202)
                self.assertTrue(done.wait(2))
            self.assertEqual(observed['history'],safe)
            self.assertNotIn('fake-private-value',json.dumps(observed['history']))
            self.assertEqual(observed['secrets'],{'fake-private-value'})
        finally:
            app.JOBS.pop(previous_id,None)
            app.JOBS.pop(new_id,None)


class WorkerTests(SyntheticNotesTests):
    def job(self, **overrides):
        job = {'status': 'running', 'created': time.monotonic(),
               'cancel_event': threading.Event(), 'deadline': time.monotonic() + 60}
        job.update(overrides)
        return job

    def assert_slot_released(self):
        self.assertTrue(app.LIMIT.acquire(False))
        app.LIMIT.release()

    def run_model_result(self, result):
        job = self.job()
        def invoke(current, prompt, schema):
            if schema is app.SELECT_SCHEMA:
                return {'paths': ['doc_0001'], 'catalog_only': False}
            return result(current, prompt) if callable(result) else result
        self.assertTrue(app.LIMIT.acquire(False))
        with patch.object(app, 'invoke', invoke):
            app.run_job(job, 'example', [])
        self.assert_slot_released()
        return job

    def test_invalid_unicode_and_empty_model_answers_fail_cleanly(self):
        for answer in ('\ud800', '', ' \n\t'):
            with self.subTest(answer=repr(answer)):
                job = self.run_model_result({'answer': answer, 'sources': []})
                self.assertEqual(job['status'], 'failed')
                self.assertNotIn('answer', job)
                self.assertNotIn('snapshots', job)
                # Even the terminal error is safe to send using UTF-8 JSON.
                json.dumps(app.public_job(job), ensure_ascii=False).encode('utf-8')

    def test_model_answer_limit_counts_utf8_bytes(self):
        for answer in ('x' * (app.ANSWER_LIMIT + 1), '가' * (app.ANSWER_LIMIT // 3 + 1)):
            with self.subTest(bytes=len(answer.encode())):
                job = self.run_model_result({'answer': answer, 'sources': []})
                self.assertEqual(job['status'], 'failed')
                self.assertIn('너무 큽니다', job['error'])
        answer = '가' * (app.ANSWER_LIMIT // 3)
        job = self.run_model_result({'answer': answer, 'sources': []})
        self.assertEqual(job['status'], 'completed', job.get('error'))
        self.assertEqual(job['answer'], answer)

    def test_excess_citations_rejected_and_valid_citations_stay_ordered(self):
        source = {'path': 'doc_0001', 'line': 1}
        job = self.run_model_result({'answer': 'answer', 'sources': [source] * (app.SOURCE_LIMIT + 1)})
        self.assertEqual(job['status'], 'failed')
        self.assertIn('너무 큽니다', job['error'])
        job = self.run_model_result({'answer': 'answer', 'sources': [
            {'path': 'doc_0001', 'line': 3}, source, source,
            {'path': 'doc_0001', 'line': True}, {'path': 'doc_0001', 'line': 4},
            {'path': 'doc_9999', 'line': 1}, None]})
        self.assertEqual(job['status'], 'completed', job.get('error'))
        self.assertEqual(job['sources'], [{'path': 'Example.md', 'line': 3},
                                         {'path': 'Example.md', 'line': 1}])

    def test_private_token_expansion_is_bounded_before_restore(self):
        # A small model answer previously amplified into a multi-megabyte
        # plaintext response; exercise it with a bounded synthetic fixture.
        secret = 'x' * 2000
        (self.root / 'Example.md').write_text('Password: ' + secret)
        def repeated(job, prompt):
            payload = json.loads(prompt.split('\n', 1)[1])
            token = app.TOKEN_RE.search(payload['evidence']['notes'][0]['lines'][0]).group()
            return {'answer': (token + ' ') * 1000, 'sources': []}
        with patch.object(app.PrivacyVault, 'restore', side_effect=AssertionError('Unbounded restore called')):
            job = self.run_model_result(repeated)
        self.assertEqual(job['status'], 'failed')
        self.assertIn('복원된 답변이 너무 큽니다', job['error'])
        self.assertNotIn('answer', job)
        self.assertNotIn('protected_values', job)

    def test_private_token_expansion_allows_exact_limit_and_rejects_one_extra_byte(self):
        token = '[[PRIVATE_' + 'a' * 32 + ']]'
        value = '가' * 1000
        limit = len(('prefix ' + value + ' suffix').encode())
        with patch.object(app, 'RESTORED_ANSWER_LIMIT', limit):
            self.assertEqual(app.restore_answer(self.job(), 'prefix ' + token + ' suffix', {token: value}),
                             'prefix ' + value + ' suffix')
            with self.assertRaisesRegex(ValueError, '복원된 답변이 너무 큽니다'):
                app.restore_answer(self.job(), 'prefix ' + token + ' suffix!', {token: value})

    def test_cancel_and_deadline_during_answer_postprocessing_prevent_completion(self):
        for method in ('filter_tokens', 'values_for', 'secrets_for'):
            for reason in ('cancel', 'timeout'):
                with self.subTest(method=method, reason=reason):
                    current = {}
                    original = getattr(app.PrivacyVault, method)
                    def interrupt(vault, *args):
                        result = original(vault, *args)
                        if reason == 'cancel':
                            current['job']['cancel_event'].set()
                        else:
                            current['job']['deadline'] = 0
                        return result
                    def result(job, prompt):
                        current['job'] = job
                        return {'answer': 'synthetic answer', 'sources': []}
                    with patch.object(app.PrivacyVault, method, interrupt):
                        job = self.run_model_result(result)
                    self.assertEqual(job['status'], 'cancelled' if reason == 'cancel' else 'failed')
                    if reason == 'timeout':
                        self.assertIn('시간이 초과', job['error'])
                    self.assertNotIn('answer', job)
                    self.assertNotIn('snapshots', job)

    def test_cancel_and_deadline_interrupt_real_history_secret_scan_and_release_worker(self):
        # A long single synthetic value exercises periodic work checkpoints in
        # the actual matcher. No sleep, large registry, or fake scan is needed.
        (self.root / 'Example.md').write_text('Password: SyntheticSecret' + 'x' * 4096)
        original_secrets = app.PrivacyVault.secrets_for
        original_matches = app.PrivacyVault._matches
        original_checkpoint = app.checkpoint
        for reason in ('cancel', 'timeout'):
            with self.subTest(reason=reason):
                state = {'history': False, 'scanning': False, 'checks': 0, 'returned': False}
                def secrets(vault, tokens):
                    state['history'] = True
                    try:
                        result = original_secrets(vault, tokens)
                        state['returned'] = True
                        return result
                    finally:
                        state['history'] = False
                def matches(vault, text, *args, **kwargs):
                    if not state['history']:
                        yield from original_matches(vault, text, *args, **kwargs)
                        return
                    state['scanning'] = True
                    try:
                        yield from original_matches(vault, text, *args, **kwargs)
                    finally:
                        state['scanning'] = False
                def interrupt(job=None):
                    if state['scanning']:
                        state['checks'] += 1
                        # After matcher entry, stop during its periodic scan,
                        # before secrets_for returns to run_job's final checks.
                        if state['checks'] == 3:
                            if reason == 'cancel':
                                job['cancel_event'].set()
                            else:
                                job['deadline'] = 0
                    original_checkpoint(job)
                with patch.object(app.PrivacyVault, 'secrets_for', secrets), \
                     patch.object(app.PrivacyVault, '_matches', matches), \
                     patch.object(app, 'checkpoint', interrupt):
                    job = self.run_model_result({'answer': 'synthetic answer', 'sources': []})
                self.assertEqual(state['checks'], 3)
                self.assertFalse(state['returned'], 'History scan must stop before returning its result')
                self.assertEqual(job['status'], 'cancelled' if reason == 'cancel' else 'failed')
                if reason == 'timeout':
                    self.assertIn('시간이 초과', job['error'])
                for key in ('answer', 'snapshots', 'history_secrets', '_privacy', 'protected_values', 'stage'):
                    self.assertNotIn(key, job)
                self.assertIn('finished', job)

    def test_deadline_at_terminal_commit_prevents_completed_status(self):
        # Expire only when entering the final state lock, after the last
        # postprocessing checkpoint, without relying on wall-clock timing.
        current = {}
        class CommitLock:
            def __enter__(self):
                if current.get('ready'):
                    current['job']['deadline'] = 0
            def __exit__(self, *args):
                return False
        original = app.PrivacyVault.secrets_for
        def secrets(vault, tokens):
            result = original(vault, tokens)
            current['ready'] = True
            return result
        def result(job, prompt):
            current['job'] = job
            return {'answer': 'synthetic answer', 'sources': []}
        with patch.object(app, 'LOCK', CommitLock()), patch.object(app.PrivacyVault, 'secrets_for', secrets):
            job = self.run_model_result(result)
        self.assertEqual(job['status'], 'failed')
        self.assertIn('시간이 초과', job['error'])
        self.assertNotIn('answer', job)

    def test_cli_output_file_is_bounded_before_parsing(self):
        real_popen = subprocess.Popen
        def local_cli(args, **kwargs):
            output = args[args.index('--output-last-message') + 1]
            return real_popen([sys.executable, '-c',
                'from pathlib import Path; import sys; Path(sys.argv[1]).write_text(" " * int(sys.argv[2]) + "{}")',
                output, str(app.MODEL_OUTPUT_LIMIT)], **kwargs)
        with patch.object(app.subprocess, 'Popen', local_cli), self.assertRaisesRegex(ValueError, '너무 큽니다'):
            app.invoke(self.job(), app.prepare_prompt(app.PrivacyVault(), 'synthetic prompt',
                       {'question': '', 'history': [], 'evidence': {'notes': []}}), app.SCHEMA)

    def test_cli_output_limit_includes_growth_after_stat(self):
        output = self.root / 'synthetic-output.json'
        output.write_bytes(b' ' * (app.MODEL_OUTPUT_LIMIT + 10))
        info = output.stat()
        real_read = os.read
        read_sizes = []
        def read(fd, size):
            data = real_read(fd, size)
            read_sizes.append(len(data))
            return data
        with patch.object(app.os, 'fstat', return_value=SimpleNamespace(st_mode=info.st_mode, st_size=0)), \
             patch.object(app.os, 'read', read), self.assertRaisesRegex(ValueError, '너무 큽니다'):
            app.read_model_output(self.job(), output)
        self.assertEqual(sum(read_sizes), app.MODEL_OUTPUT_LIMIT + 1)

    def test_stubborn_process_is_killed_and_reaped(self):
        proc=subprocess.Popen(['/usr/bin/python3','-c',
            'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print("ready",flush=True); time.sleep(30)'],
            stdout=subprocess.PIPE,text=True,start_new_session=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(),'ready')
            start=time.monotonic()
            app.terminate_process(proc,grace=0.05)
            self.assertIsNotNone(proc.poll())
            self.assertLess(time.monotonic()-start,1)
        finally:
            app.terminate_process(proc,grace=0.05)
            proc.stdout.close()

    def test_only_selected_document_body_reaches_answer_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'Relevant.md').write_text('# Relevant\nLicense: LICENSE-ORIGINAL-VALUE')
            (root/'Unrelated.md').write_text('# Unrelated\nUNRELATED-PRIVATE-VALUE')
            prompts=[]
            def fake_invoke(job,prompt,schema):
                prompts.append(prompt)
                if schema is app.SELECT_SCHEMA:
                    payload=json.loads(prompt.split('\n',1)[1])
                    alias=next(item['path'] for item in payload['catalog'] if item['title']=='Relevant.md')
                    return {'paths':[alias],'catalog_only':False}
                payload=json.loads(prompt.split('\n',1)[1])
                note=payload['evidence']['notes'][0]
                token=app.TOKEN_RE.search(note['lines'][1]).group()
                return {'answer':token,'sources':[{'path':note['path'],'line':2}]}
            self.assertTrue(app.LIMIT.acquire(False))
            job={'status':'running','created':time.monotonic()}
            with patch.object(app,'ROOT',root),patch.object(app,'invoke',fake_invoke):
                app.run_job(job,'stored item',[])
            self.assertEqual(job['status'],'completed')
            self.assertNotIn('LICENSE-ORIGINAL-VALUE',prompts[0])
            self.assertNotIn('UNRELATED-PRIVATE-VALUE',''.join(prompts))
            self.assertNotIn('LICENSE-ORIGINAL-VALUE',prompts[1])
            self.assertEqual(job['answer'],'LICENSE-ORIGINAL-VALUE')
            self.assertEqual(list(job['protected_values'].values()),['LICENSE-ORIGINAL-VALUE'])
            self.assertEqual(set(job['snapshots']),{'Relevant.md'})

    def test_flat_note_license_found_without_heading(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'PASEO.md').write_text('Typora License\nSYNTHETIC-KEY-VALUE')
            (root/'Wallet.md').write_text('# Wallet\nUNRELATED-PRIVATE')
            job={'cancel_event':threading.Event(),'deadline':time.monotonic()+60}
            prompts=[]
            def fake_invoke(job,prompt,schema):
                self.assertIs(schema,app.SELECT_SCHEMA)
                prompts.append(prompt)
                payload=json.loads(prompt.split('\n',1)[1])
                return {'paths':[next(item['path'] for item in payload['catalog'] if item['title']=='PASEO.md')],'catalog_only':False}
            with patch.object(app,'ROOT',root),patch.object(app,'invoke',fake_invoke):
                names,items,overview=app.select_documents(job,'Typora 라이센스키',[],app.document_paths())
            self.assertEqual(names,['PASEO.md'])
            self.assertFalse(overview)
            self.assertNotIn('SYNTHETIC-KEY-VALUE',json.dumps(items))
            self.assertEqual(len(prompts),1)
            payload=json.loads(prompts[0].split('\n',1)[1])
            flat=next(item for item in payload['catalog'] if item['title']=='PASEO.md')
            self.assertEqual(flat['headings'],[])
            self.assertIn('typora',flat['matches'])
            self.assertIn('license',flat['matches'])
            self.assertNotIn('SYNTHETIC-KEY-VALUE',prompts[0])

    def test_compound_question_preserves_both_selector_documents(self):
        (self.root/'Typora.md').write_text('Typora License\nSYNTHETIC-LICENSE')
        (self.root/'SSH.md').write_text('# SSH\n주소: ssh.synthetic.invalid')
        calls=[]
        def fake_invoke(job,prompt,schema):
            payload=json.loads(prompt.split('\n',1)[1])
            calls.append(schema)
            if schema is app.SELECT_SCHEMA:
                catalog={item['title']:item for item in payload['catalog']}
                self.assertIn('typora',catalog['Typora.md']['matches'])
                self.assertIn('ssh',catalog['SSH.md']['matches'])
                self.assertNotIn('SYNTHETIC-LICENSE',prompt)
                self.assertNotIn('ssh.synthetic.invalid',prompt)
                return {'paths':[catalog['Typora.md']['path'],catalog['SSH.md']['path']],'catalog_only':False}
            notes={item['title']:item for item in payload['evidence']['notes']}
            self.assertEqual(set(notes),{'Typora.md','SSH.md'})
            token=app.TOKEN_RE.search('\n'.join(notes['Typora.md']['lines'])).group()
            return {'answer':token+', ssh.synthetic.invalid',
                    'sources':[{'path':notes['Typora.md']['path'],'line':2},{'path':notes['SSH.md']['path'],'line':2}]}
        job=self.job()
        self.assertTrue(app.LIMIT.acquire(False))
        with patch.object(app,'invoke',fake_invoke):
            app.run_job(job,'Typora 라이선스랑 SSH 주소 알려줘',[])
        self.assertEqual(job['status'],'completed')
        self.assertEqual(calls,[app.SELECT_SCHEMA,app.SCHEMA])
        self.assertEqual(set(job['snapshots']),{'Typora.md','SSH.md'})
        self.assertEqual(job['answer'],'SYNTHETIC-LICENSE, ssh.synthetic.invalid')
        self.assertEqual(len(job['sources']),2)
        self.assert_slot_released()

    def test_history_followup_uses_context_instead_of_unrelated_keyword(self):
        (self.root/'Alpha.md').write_text('# Alpha\n암호: SYNTHETIC-ALPHA')
        (self.root/'Beta.md').write_text('# Beta\npassword: SYNTHETIC-BETA')
        history=[{'role':'user','content':'Alpha 접속 정보 알려줘'},
                 {'role':'assistant','content':'Alpha 연결 정보를 확인했습니다.'}]
        calls=[]
        def fake_invoke(job,prompt,schema):
            payload=json.loads(prompt.split('\n',1)[1])
            calls.append(schema)
            if schema is app.SELECT_SCHEMA:
                self.assertEqual(payload['history'],history)
                self.assertEqual(payload['question'],'비밀번호도 알려줘')
                catalog={item['title']:item for item in payload['catalog']}
                self.assertIn('Alpha.md',catalog)
                self.assertIn('password',catalog['Beta.md']['matches'])
                self.assertNotIn('SYNTHETIC-ALPHA',prompt)
                self.assertNotIn('SYNTHETIC-BETA',prompt)
                return {'paths':[catalog['Alpha.md']['path']],'catalog_only':False}
            self.assertEqual([item['title'] for item in payload['evidence']['notes']],['Alpha.md'])
            self.assertNotIn('SYNTHETIC-ALPHA',prompt)
            self.assertNotIn('SYNTHETIC-BETA',prompt)
            note=payload['evidence']['notes'][0]
            token=app.TOKEN_RE.search('\n'.join(note['lines'])).group()
            return {'answer':token,'sources':[{'path':note['path'],'line':2}]}
        job=self.job()
        self.assertTrue(app.LIMIT.acquire(False))
        with patch.object(app,'invoke',fake_invoke):
            app.run_job(job,'비밀번호도 알려줘',history)
        self.assertEqual(job['status'],'completed')
        self.assertEqual(calls,[app.SELECT_SCHEMA,app.SCHEMA])
        self.assertEqual(job['answer'],'SYNTHETIC-ALPHA')
        self.assertEqual(set(job['snapshots']),{'Alpha.md'})
        self.assert_slot_released()

    def test_catalog_overview_sends_no_document_bodies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'Notes.md').write_text('# Topic\nPRIVATE-BODY')
            prompts=[]
            def fake_invoke(job,prompt,schema):
                prompts.append(prompt)
                return {'paths':[],'catalog_only':True} if schema is app.SELECT_SCHEMA else {'answer':'Topic','sources':[]}
            self.assertTrue(app.LIMIT.acquire(False))
            job={'status':'running','created':time.monotonic()}
            with patch.object(app,'ROOT',root),patch.object(app,'invoke',fake_invoke):
                app.run_job(job,'overview',[])
            self.assertEqual(job['status'],'completed')
            self.assertNotIn('PRIVATE-BODY',''.join(prompts))

    def test_cancelled_worker_releases_slot(self):
        entered=threading.Event()
        def fake_invoke(job,prompt,schema):
            entered.set()
            job['cancel_event'].wait(1)
            raise app.Cancelled()
        self.assertTrue(app.LIMIT.acquire(False))
        job={'status':'running','created':time.monotonic(),'cancel_event':threading.Event()}
        with patch.object(app,'invoke',fake_invoke):
            thread=threading.Thread(target=app.run_job,args=(job,'test',[]))
            thread.start(); self.assertTrue(entered.wait(1))
            job['cancel_event'].set(); thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(job['status'],'cancelled')
        self.assertTrue(app.LIMIT.acquire(False));app.LIMIT.release()

    def test_real_cancel_reaps_process_and_releases_worker(self):
        ready=threading.Event()
        processes=[]
        real_popen=subprocess.Popen
        def stubborn_popen(*args,**kwargs):
            proc=real_popen(['/usr/bin/python3','-c',
                'import signal,time,sys; signal.signal(signal.SIGTERM,signal.SIG_IGN); print("ready",flush=True); sys.stdin.read(); time.sleep(30)'],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,start_new_session=True)
            self.assertEqual(proc.stdout.readline().strip(),'ready')
            processes.append(proc);ready.set()
            return proc
        self.assertTrue(app.LIMIT.acquire(False))
        job={'status':'running','created':time.monotonic(),'cancel_event':threading.Event()}
        thread=threading.Thread(target=app.run_job,args=(job,'test',[]))
        try:
            with patch.object(app.subprocess,'Popen',stubborn_popen):
                thread.start();self.assertTrue(ready.wait(2))
                start=time.monotonic();job['cancel_event'].set();thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertLess(time.monotonic()-start,4)
            self.assertEqual(job['status'],'cancelled')
            self.assertIsNotNone(processes[0].poll())
            self.assertTrue(app.LIMIT.acquire(False));app.LIMIT.release()
        finally:
            job['cancel_event'].set()
            for proc in processes:
                app.terminate_process(proc,grace=0.05)
                if proc.stdout:proc.stdout.close()
            thread.join(2)

    def test_scan_excludes_nonregular_and_hidden_notes(self):
        os.mkfifo(self.root/'Pipe.md')
        (self.root/'Directory.md').mkdir()
        (self.root/'Link.md').symlink_to(self.root/'Example.md')
        (self.root/'.hidden.md').write_text('hidden')
        for directory in ('.private','Library','_plug'):
            (self.root/directory).mkdir()
            (self.root/directory/'Hidden.md').write_text('hidden')
        self.assertEqual(list(app.document_paths()),['Example.md'])
        with self.assertRaises(ValueError):
            app.read_document(self.root/'Directory.md')
        with self.assertRaises(ValueError):
            app.read_document(self.root/'Link.md')

    def test_fifo_and_replacement_races_reject_before_reading(self):
        # Run in a bounded child so a regression to blocking FIFO open cannot
        # hang the test runner or leave the sole worker slot occupied.
        code='''
import importlib.util
import os
from pathlib import Path
import sys
from unittest.mock import patch
sys.path.insert(0,str(Path(sys.argv[1]).parent))
spec=importlib.util.spec_from_file_location('fixture_app',sys.argv[1])
app=importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)
app.ROOT=Path(sys.argv[2])
fifo=app.ROOT/'Pipe.md'
os.mkfifo(fifo)
def rejected(path):
    with patch.object(app.os,'read',side_effect=AssertionError('Must reject before reading')):
        try:
            app.read_document(path)
        except ValueError:
            return
    raise AssertionError('Nonregular document accepted')
rejected(fifo)
real_open=os.open
for replacement in ('fifo','symlink'):
    note=app.ROOT/(replacement+'.md')
    note.write_text('regular at discovery')
    assert note.name in app.document_paths()
    replaced=[]
    def replace_before_open(path,flags,*args,**kwargs):
        if Path(path)==note or (Path(path)==Path(note.name) and kwargs.get('dir_fd') is not None):
            replaced.append(True)
            note.unlink()
            if replacement=='fifo':
                os.mkfifo(note)
            else:
                note.symlink_to(app.ROOT/'Example.md')
        return real_open(path,flags,*args,**kwargs)
    with patch.object(app.os,'open',replace_before_open):
        rejected(note)
    assert replaced, 'Replacement race was not exercised'
'''
        completed=subprocess.run([sys.executable,'-c',code,str(Path(app.__file__)),str(self.root)],
                                 capture_output=True,text=True,timeout=3)
        self.assertEqual(completed.returncode,0,completed.stderr)

    def test_parent_directory_symlink_replacement_cannot_escape_note_root(self):
        with tempfile.TemporaryDirectory(prefix='wiki-outside-notes-') as tmp:
            outside = Path(tmp)
            (outside / 'Nested.md').write_text('OUTSIDE-SYNTHETIC-SECRET')
            for timing in ('before_parent_open', 'after_parent_open'):
                with self.subTest(timing=timing):
                    parent = self.root / timing
                    parent.mkdir()
                    note = parent / 'Nested.md'
                    note.write_text('ORIGINAL-SYNTHETIC-NOTE')
                    self.assertIn(timing + '/Nested.md', app.document_paths())
                    original_open = os.open
                    replaced = []
                    def replace():
                        parent.rename(self.root / (timing + '-old'))
                        parent.symlink_to(outside, target_is_directory=True)
                        replaced.append(True)
                    def racing_open(path, flags, *args, **kwargs):
                        if Path(path) == note:
                            # Also exercise the old full-path implementation:
                            # substitution happens after resolve(), before open().
                            replace()
                        elif Path(path) == Path(parent.name) and kwargs.get('dir_fd') is not None:
                            if timing == 'before_parent_open':
                                replace()
                            else:
                                fd = original_open(path, flags, *args, **kwargs)
                                replace()
                                return fd
                        return original_open(path, flags, *args, **kwargs)
                    with patch.object(app.os, 'open', racing_open):
                        if timing == 'before_parent_open':
                            with self.assertRaises(ValueError):
                                app.read_document(note)
                        else:
                            self.assertEqual(app.read_document(note), 'ORIGINAL-SYNTHETIC-NOTE')
                    self.assertEqual(replaced, [True])
                    with self.assertRaises(ValueError):
                        app.read_document(note)

    def test_fifo_does_not_hold_worker_slot(self):
        os.mkfifo(self.root/'Pipe.md')
        job=self.job()
        def fake_invoke(job,prompt,schema):
            return ({'paths':['doc_0001'],'catalog_only':False} if schema is app.SELECT_SCHEMA
                    else {'answer':'synthetic answer','sources':[]})
        self.assertTrue(app.LIMIT.acquire(False))
        with patch.object(app,'invoke',fake_invoke):
            app.run_job(job,'example',[])
        self.assertEqual(job['status'],'completed')
        self.assert_slot_released()

    def test_document_size_limit_applies_to_stat_and_actual_read(self):
        note=self.root/'Large.md'
        note.write_bytes(b'x'*app.DOCUMENT_LIMIT)
        self.assertEqual(len(app.read_document(note)),app.DOCUMENT_LIMIT)
        note.write_bytes(b'x'*(app.DOCUMENT_LIMIT+10))
        with patch.object(app.os,'read') as read:
            with self.assertRaisesRegex(ValueError,'너무 큽니다'):
                app.read_document(note)
            read.assert_not_called()
        # Simulate growth after fstat: the descriptor read still has a hard cap.
        info=note.stat()
        real_read=os.read
        sizes=[]
        def tracked_read(fd,size):
            data=real_read(fd,size)
            sizes.append(len(data))
            return data
        with patch.object(app.os,'fstat',return_value=SimpleNamespace(st_mode=info.st_mode,st_size=0)), \
             patch.object(app.os,'read',tracked_read):
            with self.assertRaisesRegex(ValueError,'너무 큽니다'):
                app.read_document(note)
        self.assertEqual(sum(sizes),app.DOCUMENT_LIMIT+1)

    def test_bounded_read_preserves_existing_text_and_line_numbers(self):
        note=self.root/'Windows.md'
        note.write_bytes('암호: SYNTHETIC-EXACT-VALUE\r\nSecond line\rThird line\n'.encode())
        self.assertEqual(app.read_document(note),note.read_text(encoding='utf-8'))
        self.assertEqual(app.read_document(note).splitlines()[0],'암호: SYNTHETIC-EXACT-VALUE')

    def test_document_scan_checks_cancel_and_deadline_between_directories(self):
        for reason in ('cancel','timeout'):
            with self.subTest(reason=reason):
                job=self.job()
                def walk(*args,**kwargs):
                    yield str(self.root),[],['Example.md']
                    if reason=='cancel':
                        job['cancel_event'].set()
                    else:
                        job['deadline']=0
                    yield str(self.root),[],['Example.md']
                expected=app.Cancelled if reason=='cancel' else app.JobTimeout
                with patch.object(app.os,'walk',walk),self.assertRaises(expected):
                    app.document_paths(job)

    def test_read_checks_cancel_and_deadline_between_chunks(self):
        note=self.root/'Chunks.md'
        note.write_bytes(b'x'*140000)
        for reason in ('cancel','timeout'):
            with self.subTest(reason=reason):
                job=self.job()
                real_read=os.read
                reads=[]
                def interrupted_read(fd,size):
                    reads.append(size)
                    chunk=real_read(fd,size)
                    if reason=='cancel':
                        job['cancel_event'].set()
                    else:
                        job['deadline']=0
                    return chunk
                expected=app.Cancelled if reason=='cancel' else app.JobTimeout
                with patch.object(app.os,'read',interrupted_read),self.assertRaises(expected):
                    app.read_document(note,job)
                self.assertEqual(len(reads),1)

    def test_pre_cancelled_and_expired_jobs_release_slot_without_model(self):
        for reason in ('cancel','timeout'):
            with self.subTest(reason=reason):
                job=self.job()
                if reason=='cancel':
                    job['cancel_event'].set()
                else:
                    job['deadline']=0
                self.assertTrue(app.LIMIT.acquire(False))
                with patch.object(app,'invoke') as invoke:
                    app.run_job(job,'example',[])
                invoke.assert_not_called()
                self.assertEqual(job['status'],'cancelled' if reason=='cancel' else 'failed')
                if reason=='timeout':
                    self.assertIn('시간이 초과',job['error'])
                self.assert_slot_released()

    def test_cancel_or_deadline_after_selection_prevents_answer_model(self):
        for reason in ('cancel','timeout'):
            with self.subTest(reason=reason):
                job=self.job()
                calls=[]
                def invoke(current,prompt,schema):
                    calls.append(schema)
                    if reason=='cancel':
                        job['cancel_event'].set()
                    else:
                        job['deadline']=0
                    return {'paths':['doc_0001'],'catalog_only':False}
                self.assertTrue(app.LIMIT.acquire(False))
                with patch.object(app,'invoke',invoke):
                    app.run_job(job,'example',[])
                self.assertEqual(calls,[app.SELECT_SCHEMA])
                self.assertEqual(job['status'],'cancelled' if reason=='cancel' else 'failed')
                self.assert_slot_released()

    def test_cancel_or_deadline_during_load_prevents_answer_model(self):
        for reason in ('cancel','timeout'):
            with self.subTest(reason=reason):
                job=self.job()
                def read(*args):
                    if reason=='cancel':
                        job['cancel_event'].set()
                    else:
                        job['deadline']=0
                    return self.source_text
                self.assertTrue(app.LIMIT.acquire(False))
                with patch.object(app,'read_document',read),patch.object(app,'invoke') as invoke:
                    app.run_job(job,'example',[])
                invoke.assert_not_called()
                self.assertEqual(job['status'],'cancelled' if reason=='cancel' else 'failed')
                self.assertNotIn('snapshots',job)
                self.assert_slot_released()

    def test_invoke_checks_deadline_before_spawning(self):
        with patch.object(app.subprocess,'Popen') as popen,self.assertRaises(app.JobTimeout):
            app.invoke(self.job(deadline=0),'synthetic prompt',app.SELECT_SCHEMA)
        popen.assert_not_called()


class PrivacyFlowTests(SyntheticNotesTests):
    """Exercise the real two-stage worker with synthetic model boundaries."""

    def run_fixture(self, question, callback, history=(), secrets=()):
        job={'status':'running','created':time.monotonic(),
             'history_secrets':tuple(secrets)}
        boundary_errors=[]
        def checked_callback(*args):
            try:
                return callback(*args)
            except Exception as exc:
                boundary_errors.append(exc)
                raise
        self.assertTrue(app.LIMIT.acquire(False))
        with patch.object(app,'invoke',checked_callback):
            app.run_job(job,question,list(history))
        if boundary_errors:
            raise boundary_errors[0]
        self.assertEqual(job['status'],'completed',job.get('error'))
        self.assertTrue(app.LIMIT.acquire(False))
        app.LIMIT.release()
        self.assertNotIn('_privacy',job)
        return job

    def test_all_model_inputs_hide_values_in_paths_headings_questions_and_history(self):
        secret='fixture$SeCreT992!'
        name='Service '+secret+'.md'
        (self.root/name).write_text('# Service '+secret+'\npassword: '+secret+'\nordinary information')
        prompts=[]
        def invoke(job,prompt,schema):
            prompts.append(prompt)
            self.assertNotIn(secret,prompt)
            payload=json.loads(prompt.split('\n',1)[1])
            if schema is app.SELECT_SCHEMA:
                selected=next(item for item in payload['catalog'] if item['title'].startswith('Service '))
                self.assertRegex(selected['path'],r'^doc_\d{4}$')
                self.assertTrue(app.TOKEN_RE.search(selected['title']))
                self.assertTrue(app.TOKEN_RE.search(selected['headings'][0]))
                return {'paths':[selected['path']],'catalog_only':False}
            notes=payload['evidence']['notes']
            self.assertEqual(len(notes),1)
            token=app.TOKEN_RE.search(notes[0]['lines'][1]).group()
            return {'answer':'비밀번호: '+token,'sources':[{'path':notes[0]['path'],'line':2}]}
        history=[{'role':'assistant','content':'Earlier value '+secret}]
        job=self.run_fixture('Service '+secret+' 비밀번호',invoke,history)
        self.assertEqual(len(prompts),2)
        self.assertEqual(job['answer'],'비밀번호: '+secret)
        self.assertEqual(job['sources'],[{'path':name,'line':2}])
        self.assertEqual(list(job['protected_values'].values()),[secret])
        self.assertEqual(job['snapshots'][name],(self.root/name).read_text())
        self.assertNotIn(secret,json.dumps(job['safe_history']))

    def test_only_used_tokens_from_selected_evidence_are_restored(self):
        selected='chosen$Pass983!'
        unused='unused$Pass483!'
        unselected='other$Pass973!'
        (self.root/'Chosen.md').write_text('# Chosen\nPassword: '+selected+'\nToken: '+unused)
        (self.root/'Other.md').write_text('# Password: '+unselected)
        observed={}
        forged='[[PRIVATE_'+'f'*32+']]'
        def invoke(job,prompt,schema):
            payload=json.loads(prompt.split('\n',1)[1])
            for value in (selected,unused,unselected):
                self.assertNotIn(value,prompt)
            if schema is app.SELECT_SCHEMA:
                items={item['title']:item for item in payload['catalog']}
                observed['outside']=app.TOKEN_RE.search(items['Other.md']['headings'][0]).group()
                return {'paths':[items['Chosen.md']['path']],'catalog_only':False}
            note=payload['evidence']['notes'][0]
            token=app.TOKEN_RE.search(note['lines'][1]).group()
            observed['used']=token
            return {'answer':token+' / '+observed['outside']+' / '+forged,
                    'sources':[{'path':note['path'],'line':2}]}
        job=self.run_fixture('Chosen password',invoke)
        self.assertIn(selected,job['answer'])
        self.assertNotIn(unused,job['answer'])
        self.assertNotIn(unselected,job['answer'])
        self.assertNotIn(forged,job['display_answer'])
        self.assertNotIn(observed['outside'],job['display_answer'])
        self.assertEqual(job['protected_values'],{observed['used']:selected})
        self.assertEqual(set(job['snapshots']),{'Chosen.md'})
        self.assertIn(selected,job['history_secrets'])
        self.assertIn(unused,job['history_secrets'])
        self.assertNotIn(unselected,job['history_secrets'])

    def test_request_tokens_are_random_and_old_tokens_cannot_restore(self):
        secret='request$Unique348!'
        (self.root/'Login.md').write_text('# Login\nPassword: '+secret)
        tokens=[]
        def invoke(job,prompt,schema):
            payload=json.loads(prompt.split('\n',1)[1])
            if schema is app.SELECT_SCHEMA:
                item=next(item for item in payload['catalog'] if item['title']=='Login.md')
                return {'paths':[item['path']],'catalog_only':False}
            note=payload['evidence']['notes'][0]
            token=app.TOKEN_RE.search(note['lines'][1]).group()
            previous=tokens[-1] if tokens else ''
            tokens.append(token)
            return {'answer':token+' '+previous,'sources':[]}
        first=self.run_fixture('Login password',invoke)
        second=self.run_fixture('Login password',invoke)
        self.assertNotEqual(tokens[0],tokens[1])
        self.assertNotIn(tokens[0],second['display_answer'])
        self.assertEqual(list(first['protected_values'].values()),[secret])
        self.assertEqual(list(second['protected_values'].values()),[secret])
        self.assertEqual(second['answer'].count(secret),1)

    def test_single_snapshot_prevents_reread_after_document_selection(self):
        secret='snapshot$Old483!'
        replacement='changed$Private991!'
        note=self.root/'Snapshot.md'
        note.write_text('# Snapshot\nPassword: '+secret)
        reads=[]
        real_read=app.read_document
        def read(path,job=None):
            reads.append(path.name)
            return real_read(path,job)
        def invoke(job,prompt,schema):
            self.assertNotIn(secret,prompt)
            self.assertNotIn(replacement,prompt)
            payload=json.loads(prompt.split('\n',1)[1])
            if schema is app.SELECT_SCHEMA:
                note.write_text('# Changed\nUnlabeled '+replacement)
                item=next(item for item in payload['catalog'] if item['title']=='Snapshot.md')
                return {'paths':[item['path']],'catalog_only':False}
            evidence=payload['evidence']['notes'][0]
            token=app.TOKEN_RE.search(evidence['lines'][1]).group()
            return {'answer':token,'sources':[{'path':evidence['path'],'line':2}]}
        with patch.object(app,'read_document',read):
            job=self.run_fixture('Snapshot password',invoke)
        self.assertEqual(reads.count('Snapshot.md'),1)
        self.assertEqual(job['answer'],secret)
        self.assertEqual(job['snapshots']['Snapshot.md'],'# Snapshot\nPassword: '+secret)

    def test_removed_note_secrets_remain_hidden_on_followup(self):
        secret='deleted$Credential432!'
        note=self.root/'Login.md'
        note.write_text('# Login\nPassword: '+secret)
        def first_invoke(job,prompt,schema):
            payload=json.loads(prompt.split('\n',1)[1])
            if schema is app.SELECT_SCHEMA:
                item=next(item for item in payload['catalog'] if item['title']=='Login.md')
                return {'paths':[item['path']],'catalog_only':False}
            evidence=payload['evidence']['notes'][0]
            return {'answer':app.TOKEN_RE.search(evidence['lines'][1]).group(),'sources':[]}
        first=self.run_fixture('Login password',first_invoke)
        self.assertEqual(first['answer'],secret)
        note.unlink()
        calls=[]
        def followup(job,prompt,schema):
            calls.append(schema)
            self.assertNotIn(secret,prompt)
            payload=json.loads(prompt.split('\n',1)[1])
            if schema is app.SELECT_SCHEMA:
                return {'paths':[],'catalog_only':False}
            self.assertEqual(payload['evidence']['notes'],[])
            # Even a secret token echoed from the new question cannot be
            # restored when it was absent from selected note evidence.
            return {'answer':payload['question'],'sources':[]}
        second=self.run_fixture('Show public information',followup,first['safe_history'],first['history_secrets'])
        self.assertIn(secret,second['history_secrets'])
        self.assertNotIn(secret,second['answer'])
        self.assertEqual(second['protected_values'],{})
        self.assertNotIn(secret,json.dumps(second['safe_history']))
        # Only the latest job is carried forward: even after the original job
        # drops out of the recent-history window, its protection set survives.
        third=self.run_fixture('Explain '+secret,followup,second['safe_history'],second['history_secrets'])
        self.assertEqual(calls,[app.SELECT_SCHEMA,app.SCHEMA,app.SELECT_SCHEMA,app.SCHEMA])
        self.assertNotIn(secret,third['answer'])
        self.assertEqual(third['protected_values'],{})
        self.assertNotIn(secret,json.dumps(third['safe_history']))

    def test_catalog_only_cannot_restore_private_catalog_headings(self):
        secret='catalog$Credential923!'
        (self.root/'Overview.md').write_text('# Password: '+secret)
        def invoke(job,prompt,schema):
            self.assertNotIn(secret,prompt)
            payload=json.loads(prompt.split('\n',1)[1])
            if schema is app.SELECT_SCHEMA:
                return {'paths':[],'catalog_only':True}
            text=json.dumps(payload['evidence'])
            token=app.TOKEN_RE.search(text).group()
            return {'answer':token,'sources':[{'path':'doc_0001','line':1}]}
        job=self.run_fixture('overview',invoke)
        self.assertNotIn(secret,job['answer'])
        self.assertEqual(job['protected_values'],{})
        self.assertEqual(job['sources'],[])
        self.assertEqual(job['snapshots'],{})

    def test_unreadable_document_fails_closed_before_any_model_input(self):
        (self.root/'Unreadable.md').write_bytes(b'\xff')
        job={'status':'running','created':time.monotonic()}
        self.assertTrue(app.LIMIT.acquire(False))
        with patch.object(app,'invoke') as invoke:
            app.run_job(job,'question',[])
        invoke.assert_not_called()
        self.assertEqual(job['status'],'failed')
        self.assertNotIn('snapshots',job)
        self.assertNotIn('_privacy',job)
        self.assertTrue(app.LIMIT.acquire(False))
        app.LIMIT.release()

    def assert_protected_block_roundtrip_and_bare_followup(self, name, text, line,
                                                         expected, secrets, *, delete=False):
        path=self.root/name
        path.write_text(text)
        seen=[]
        def invoke(job,prompt,schema):
            seen.append(schema)
            for secret in secrets:
                self.assertNotIn(secret,prompt)
            payload=json.loads(prompt.split('\n',1)[1])
            if schema is app.SELECT_SCHEMA:
                item=next(item for item in payload['catalog'] if item['title']==name)
                return {'paths':[item['path']],'catalog_only':False}
            note=payload['evidence']['notes'][0]
            token=app.TOKEN_RE.search(note['lines'][line-1]).group()
            return {'answer':token,'sources':[{'path':note['path'],'line':line}]}
        first=self.run_fixture('해당 문서의 값을 보여줘',invoke)
        self.assertEqual(first['answer'],expected)
        self.assertEqual(seen,[app.SELECT_SCHEMA,app.SCHEMA])
        if delete:
            path.unlink()
        for secret in secrets:
            followup_seen=[]
            def followup(job,prompt,schema):
                followup_seen.append(schema)
                for value in secrets:
                    self.assertNotIn(value,prompt)
                if schema is app.SELECT_SCHEMA:
                    return {'paths':[],'catalog_only':False}
                return {'answer':'참고 문서가 없습니다.','sources':[]}
            self.run_fixture(secret+' 다시 알려줘',followup,first['safe_history'],first['history_secrets'])
            self.assertEqual(followup_seen,[app.SELECT_SCHEMA,app.SCHEMA])

    def test_explicit_private_slash_row_hides_bare_login_and_password_followup(self):
        row='- Example / demo_login / AmberTulip!42'
        text='<!-- private -->\n## ID / PW\n'+row+'\n<!-- /private -->\n'
        self.assert_protected_block_roundtrip_and_bare_followup(
            'PrivateRow.md',text,3,row,('demo_login','AmberTulip!42'))

    def test_explicit_private_table_hides_bare_cells_after_note_deleted(self):
        row='| Example | table_login | FernRiver!19 |'
        text=('<!-- private -->\n| Service | ID | PW |\n| --- | --- | --- |\n'
              +row+'\n<!-- /private -->\n')
        self.assert_protected_block_roundtrip_and_bare_followup(
            'PrivateTable.md',text,4,row,('table_login','FernRiver!19'),delete=True)

    def test_protected_whole_line_retains_inner_password_after_note_deleted(self):
        row='Password: SYNTHETIC_Only_A73!'
        text='<!-- private -->\n'+row+'\n<!-- /private -->\n'
        self.assert_protected_block_roundtrip_and_bare_followup(
            'PrivateLine.md',text,2,row,('SYNTHETIC_Only_A73!',),delete=True)

    def test_short_credentials_with_korean_particles_never_reach_either_model(self):
        secrets=('876','Qz','별빛','꽃별밤','é9')
        text='# Credentials\nPassword: 876\nToken: Qz\nPassword: 별빛\nToken: 꽃별밤\nPassword: é9\n'
        (self.root/'Short.md').write_text(text)
        questions=('876이 뭐였지', '876은 어디에 쓰지', 'Qz가 어디에 쓰지', 'Qz는 뭐였지',
                   '별빛이 뭐였지', '별빛은 어디에 쓰지', '꽃별밤이 어디에 쓰지', '꽃별밤은 뭐였지',
                   'é9가 어디에 쓰지', 'é9는 뭐였지')
        for question in questions:
            with self.subTest(question=question):
                calls=[]
                def invoke(job,prompt,schema):
                    calls.append(schema)
                    visible=app.TOKEN_RE.sub('',prompt)
                    for secret in secrets:
                        self.assertNotIn(secret,visible)
                    payload=json.loads(prompt.split('\n',1)[1])
                    if schema is app.SELECT_SCHEMA:
                        item=next(item for item in payload['catalog'] if item['title']=='Short.md')
                        return {'paths':[item['path']],'catalog_only':False}
                    note=payload['evidence']['notes'][0]
                    return {'answer':app.TOKEN_RE.search(note['lines'][1]).group(),'sources':[]}
                result=self.run_fixture(question,invoke)
                self.assertEqual(result['answer'],'876')
                self.assertEqual(calls,[app.SELECT_SCHEMA,app.SCHEMA])

    def test_nested_private_block_keeps_outer_content_after_inner_close_private(self):
        first='beforeOuter#247!'
        inside='innerValue#571!'
        after='afterOuter#893!'
        text=('<!-- private -->\n'+first+'\n<!-- private -->\n'+inside
              +'\n<!-- /private -->\n'+after+'\n<!-- /private -->\nOrdinary visible line\n')
        (self.root/'Nested.md').write_text(text)
        calls=[]
        def invoke(job,prompt,schema):
            calls.append(schema)
            for secret in (first,inside,after):
                self.assertNotIn(secret,prompt)
            payload=json.loads(prompt.split('\n',1)[1])
            if schema is app.SELECT_SCHEMA:
                item=next(item for item in payload['catalog'] if item['title']=='Nested.md')
                return {'paths':[item['path']],'catalog_only':False}
            note=payload['evidence']['notes'][0]
            self.assertIn('Ordinary visible line','\n'.join(note['lines']))
            token=app.TOKEN_RE.search(note['lines'][5]).group()
            return {'answer':token,'sources':[{'path':note['path'],'line':6}]}
        job=self.run_fixture('Nested 내용을 보여줘',invoke)
        self.assertEqual(job['answer'],after)
        self.assertEqual(calls,[app.SELECT_SCHEMA,app.SCHEMA])

    def test_ordinary_personal_words_gifts_assets_and_table_labels_stay_visible(self):
        (self.root/'Personal').mkdir()
        content={
            'Personal/Words.md':'# Words\n* Amber skies above\n* 햇살이 내려앉은 창가\n',
            'Personal/Gifts.md':'# Gifts\n## 홍익대학교\n* 문샘플 ( ₩ 30 )\n* 합계 : ₩ 643\n',
            'Personal/Assets.md':'# Assets\n* 합계 : ₩ 15,390,000\n* 탄생축하금 ( ₩ 3,000,000 )\n',
            'Personal/Projects.md':'# Projects\n| Name | Description | Amount |\n| --- | --- | --- |\n| Garden | Weekend plan | 250,000 |\n',
        }
        for name,text in content.items():
            (self.root/name).write_text(text)
        calls=[]
        def invoke(job,prompt,schema):
            calls.append(schema)
            payload=json.loads(prompt.split('\n',1)[1])
            if schema is app.SELECT_SCHEMA:
                chosen=[item['path'] for item in payload['catalog'] if item['title'] in content]
                self.assertEqual(len(chosen),len(content))
                return {'paths':chosen,'catalog_only':False}
            evidence={item['title']:item for item in payload['evidence']['notes']}
            for name,text in content.items():
                actual='\n'.join(line.split(': ',1)[1] for line in evidence[name]['lines'])+'\n'
                self.assertEqual(actual,text)
                self.assertFalse(app.TOKEN_RE.search(actual))
            return {'answer':'Words, gifts, assets and projects remain usable.','sources':[]}
        job=self.run_fixture('Words 문구와 선물 자산 표를 보여줘',invoke)
        self.assertEqual(job['protected_values'],{})
        self.assertEqual(calls,[app.SELECT_SCHEMA,app.SCHEMA])


if __name__ == '__main__':
    unittest.main()
