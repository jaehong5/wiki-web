"""Behavior at HTTP and real child-process boundaries, with synthetic data only."""
import collections
import json
from pathlib import Path
import re
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric import rsa
import jwt

from test_server import app, HTTPTestSupport, SyntheticNotesTests


class AdmissionTests(HTTPTestSupport, SyntheticNotesTests):
    def test_valid_shaped_token_signed_by_different_key_is_rejected(self):
        foreign = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        claims = dict(sub='test-user', aud=app.CONFIG['audience'], iss=app.CONFIG['issuer'],
                      exp=int(time.time()) + 60, iat=int(time.time()))
        token = jwt.encode(claims, foreign, algorithm='RS256')
        self.assertEqual(self.request('/api/status', token)[0], 401)

    def test_wrong_issuer_and_missing_required_claims_are_rejected(self):
        base = dict(sub='test-user', aud=app.CONFIG['audience'], iss=app.CONFIG['issuer'],
                    exp=int(time.time()) + 60, iat=int(time.time()))
        cases = [dict(base, iss='https://foreign.invalid')]
        cases.extend({key: value for key, value in base.items() if key != missing}
                     for missing in ('exp', 'iat', 'sub', 'aud', 'iss'))
        for claims in cases:
            with self.subTest(claims=sorted(claims), issuer=claims.get('iss')):
                token = jwt.encode(claims, self.key, algorithm='RS256')
                self.assertEqual(self.request('/api/status', token)[0], 401)

    def test_twentieth_request_allowed_twenty_first_blocked_and_expired_budget_recovers(self):
        token = self.token()
        owner = app.principal(token)
        app.RATES[owner] = collections.deque([time.monotonic()] * 19)
        done = threading.Event()
        calls = []

        def complete(job, question, history):
            calls.append(job['id'])
            with app.LOCK:
                job.update(status='completed', finished=time.monotonic())
                app.LIMIT.release()
            done.set()

        first = {'question': 'synthetic', 'request_id': '4' * 32}
        with patch.object(app, 'run_job', complete):
            self.assertEqual(self.request('/api/query', token, first)[0], 202)
            self.assertTrue(done.wait(2))
            self.assertEqual(len(app.RATES[owner]), 20)
            self.assertEqual(self.request('/api/query', token, first)[0], 200)
            self.assertEqual(len(app.RATES[owner]), 20, 'Idempotent retry must not consume budget')
            self.assertEqual(self.request('/api/query', token,
                             dict(first, request_id='5' * 32))[0], 429)
            self.assertEqual(calls, ['4' * 32])
            app.RATES[owner] = collections.deque([time.monotonic() - 3601] * 20)
            done.clear()
            self.assertEqual(self.request('/api/query', token,
                             dict(first, request_id='6' * 32))[0], 202)
            self.assertTrue(done.wait(2))
            self.assertEqual(len(app.RATES[owner]), 1)

    def test_only_one_live_request_across_owners_and_cancelling_keeps_slot(self):
        gate, started = threading.Event(), threading.Event()
        calls = []

        def held(job, question, history):
            calls.append(job['id'])
            started.set()
            gate.wait(5)
            with app.LOCK:
                job.update(status='cancelled' if job['cancel_event'].is_set() else 'completed',
                           finished=time.monotonic())
                app.LIMIT.release()

        owner_token = self.token()
        other_token = self.token(sub='second-owner')
        first = {'question': 'held request', 'request_id': '7' * 32}
        try:
            with patch.object(app, 'run_job', held):
                self.assertEqual(self.request('/api/query', owner_token, first)[0], 202)
                self.assertTrue(started.wait(2))
                self.assertEqual(self.request('/api/query', other_token,
                                 dict(first, request_id='8' * 32))[0], 409)
                self.assertEqual(self.request('/api/query', owner_token, first)[0], 200)
                status, data = self.request('/api/jobs/' + first['request_id'] + '/cancel', owner_token, {})
                self.assertEqual((status, data['status']), (200, 'cancelling'))
                self.assertEqual(self.request('/api/query', other_token,
                                 dict(first, request_id='9' * 32))[0], 409)
                self.assertEqual(calls, [first['request_id']])
        finally:
            gate.set()
            end = time.monotonic() + 3
            while any(job.get('status') not in app.TERMINAL for job in app.JOBS.values()) and time.monotonic() < end:
                time.sleep(0.01)
        self.assertTrue(all(job['status'] in app.TERMINAL for job in app.JOBS.values()))
        self.assertTrue(app.LIMIT.acquire(False))
        app.LIMIT.release()

    def test_cancellation_requires_the_job_owner(self):
        ident = 'a' * 32
        token = self.token()
        event = threading.Event()
        app.JOBS[ident] = {'id': ident, 'owner': app.principal(token), 'status': 'running',
                           'created': time.monotonic(), 'cancel_event': event}
        route = '/api/jobs/' + ident + '/cancel'
        self.assertEqual(self.request(route, self.token(sub='other-owner'), {})[0], 404)
        self.assertFalse(event.is_set())
        self.assertEqual(app.JOBS[ident]['status'], 'running')
        status, data = self.request(route, token, {})
        self.assertEqual((status, data['status']), (200, 'cancelling'))
        self.assertTrue(event.is_set())


class ProcessContractTests(SyntheticNotesTests):
    def fake_cli(self, mode='answer'):
        """Execute a real local process implementing just the CLI file contract."""
        script = self.root / 'synthetic-cli.py'
        capture = self.root / 'synthetic-cli-input.jsonl'
        script.write_text('''#!/usr/bin/python3
import json, re, sys
from pathlib import Path
mode = MODE
args = sys.argv[1:]
prompt = sys.stdin.read()
schema = json.loads(Path(args[args.index('--output-schema') + 1]).read_text())
with Path(CAPTURE).open('a') as stream:
    stream.write(json.dumps({'args':args, 'stdin':prompt, 'schema':schema, 'cwd':str(Path.cwd())}) + '\\n')
print('SYNTHETIC-PRIVATE-STDOUT')
print('SYNTHETIC-PRIVATE-STDERR', file=sys.stderr)
output = Path(args[args.index('--output-last-message') + 1])
if mode == 'nonzero': sys.exit(7)
if mode == 'missing': sys.exit(0)
if mode == 'invalid': output.write_text('{not json'); sys.exit(0)
if mode == 'answer': result = {'answer':'synthetic result','sources':[]}
else:
    payload = json.loads(prompt.split('\\n', 1)[1])
    if 'paths' in schema['properties']:
        item = next(item for item in payload['catalog'] if item['title'] == 'Example.md')
        result = {'paths':[item['path']], 'catalog_only':False}
    else:
        note = payload['evidence']['notes'][0]
        cited = next(line for line in note['lines'] if re.search(r'\\[\\[PRIVATE_[0-9a-f]{32}\\]\\]', line))
        token = re.search(r'\\[\\[PRIVATE_[0-9a-f]{32}\\]\\]', cited).group()
        result = {'answer':token, 'sources':[{'path':note['path'], 'line':int(cited.split(':',1)[0])}]}
output.write_text(json.dumps(result))
'''.replace('MODE', repr(mode)).replace('CAPTURE', repr(str(capture))))
        script.chmod(0o700)
        config_patch = patch.dict(app.CONFIG, codex=str(script))
        config_patch.start()
        self.addCleanup(config_patch.stop)
        return capture

    def job(self):
        return {'status': 'running', 'created': time.monotonic(),
                'cancel_event': threading.Event(), 'deadline': time.monotonic() + 10}

    def test_actual_cli_receives_exact_stdin_schema_and_restricted_arguments(self):
        capture = self.fake_cli()
        prompt = app.prepare_prompt(app.PrivacyVault(), 'SYNTHETIC exact prompt',
                                    {'question': '한글 [[PRIVATE_' + 'a' * 32 + ']]',
                                     'history': [], 'evidence': {'notes': []}})
        real_popen = subprocess.Popen
        with patch.object(app.subprocess, 'Popen', wraps=real_popen) as spawn:
            result = app.invoke(self.job(), prompt, app.SCHEMA)
        self.assertEqual(result, {'answer': 'synthetic result', 'sources': []})
        invocation = json.loads(capture.read_text())
        self.assertEqual(invocation['stdin'], prompt)
        self.assertEqual(invocation['schema'], app.SCHEMA)
        args = invocation['args']
        self.assertEqual(args[0], 'exec')
        self.assertEqual(args[-1], '-')
        for flag in ('--ignore-user-config', '--ignore-rules', '--ephemeral', '--skip-git-repo-check'):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index('--sandbox') + 1], 'read-only')
        self.assertEqual(args[args.index('-m') + 1], app.CONFIG['model'])
        disabled = {args[i + 1] for i, arg in enumerate(args[:-1]) if arg == '--disable'}
        self.assertTrue({'shell_tool', 'apps', 'multi_agent', 'view_image', 'browser_use',
                         'computer_use', 'code_mode_host', 'skill_search', 'daemon_auto_start'} <= disabled)
        self.assertIn('web_search="disabled"', args)
        self.assertIn('model_reasoning_effort="medium"', args)
        self.assertNotIn(prompt, args)
        kwargs = spawn.call_args.kwargs
        self.assertEqual(kwargs['stdout'], subprocess.DEVNULL)
        self.assertEqual(kwargs['stderr'], subprocess.DEVNULL)
        self.assertTrue(kwargs['start_new_session'])
        self.assertNotEqual(Path(invocation['cwd']), self.root)
        self.assertFalse(Path(invocation['cwd']).exists(), 'CLI scratch data must be removed')

    def test_nonzero_missing_and_invalid_cli_output_fail_without_exposing_output(self):
        for mode in ('nonzero', 'missing', 'invalid'):
            with self.subTest(mode=mode):
                capture = self.fake_cli(mode)
                with self.assertRaises(ValueError) as error:
                    app.invoke(self.job(), app.prepare_prompt(app.PrivacyVault(), 'synthetic prompt',
                               {'question': '', 'history': [], 'evidence': {'notes': []}}), app.SCHEMA)
                self.assertNotIn('SYNTHETIC-PRIVATE', str(error.exception))
                last = json.loads(capture.read_text().splitlines()[-1])
                self.assertFalse(Path(last['cwd']).exists())

    def test_model_boundary_rejects_unprepared_or_newly_sensitive_input_before_spawn(self):
        vault = app.PrivacyVault()
        prompt = app.prepare_prompt(vault, 'Application-owned instructions',
                                    {'question': 'SyntheticBoundarySecret9!',
                                     'history': [], 'evidence': {'notes': []}})
        vault.learn_value('SyntheticBoundarySecret9!')
        for candidate in ('unprepared raw text', prompt):
            with self.subTest(prepared=isinstance(candidate, app.PreparedPrompt)), \
                 patch.object(app.subprocess, 'Popen') as spawn, self.assertRaises(ValueError):
                app.invoke(self.job(), candidate, app.SCHEMA)
            spawn.assert_not_called()

    def test_model_boundary_audits_nested_content_without_treating_line_numbers_as_secrets(self):
        vault = app.PrivacyVault()
        vault.learn_value('1')
        vault.learn_value('SyntheticBoundarySecret9!')
        line = app.EvidenceLine(1, vault.sanitize('Password: SyntheticBoundarySecret9!'))
        prepared = app.prepare_prompt(vault, 'Application-owned instructions',
                                     {'question': '', 'history': [],
                                      'evidence': {'notes': [{'path': 'doc_0001', 'title': 'Garden.md', 'lines': [line]}]}})
        self.assertTrue(isinstance(prepared, app.PreparedPrompt))
        self.assertNotIn('SyntheticBoundarySecret9!', prepared)
        self.assertTrue(json.loads(prepared.split('\n', 1)[1])['evidence']['notes'][0]['lines'][0].startswith('1: '))
        with self.assertRaises(ValueError):
            app.prepare_prompt(vault, 'Application-owned instructions',
                               {'question': '', 'history': [], 'evidence': {'notes': [
                                   {'path': 'doc_0001', 'title': 'Garden.md',
                                    'lines': [app.EvidenceLine(1, 'SyntheticBoundarySecret9!')]}]}})

    def test_model_schema_separates_generated_metadata_from_every_content_field(self):
        import copy
        vault = app.PrivacyVault()
        for value in ('user', 'assistant', 'doc_0001', '1'):
            vault.learn_value(value)
        catalog = [{'path': 'doc_0001', 'title': 'Garden.md', 'headings': ['Garden'], 'matches': ['garden']}]
        base = {'question': 'Show the garden', 'history': [
            {'role': 'user', 'content': 'Garden question'},
            {'role': 'assistant', 'content': 'Garden response'}]}
        payloads = [dict(base, catalog=catalog), dict(base, evidence={'catalog': catalog}),
                    dict(base, evidence={'notes': [{'path': 'doc_0001', 'title': 'Garden.md',
                                                   'lines': [app.EvidenceLine(1, 'Garden text')]}]})]
        for payload in payloads:
            try:
                app.prepare_prompt(vault, 'Application-owned instructions', payload).validate()
            except ValueError as exc:
                self.fail('Generated metadata must not collide with private content: ' + str(exc))
        targets = [('question',), ('history', 0, 'content'), ('history', 1, 'content'),
                   ('catalog', 0, 'title'), ('catalog', 0, 'headings', 0), ('catalog', 0, 'matches', 0)]
        for target in targets:
            with self.subTest(content_field=target):
                payload = copy.deepcopy(payloads[0])
                field = payload
                for key in target[:-1]:
                    field = field[key]
                field[target[-1]] = 'user'
                with self.assertRaises(ValueError):
                    app.prepare_prompt(vault, 'Application-owned instructions', payload)
        for field in ('title', 'lines'):
            payload = copy.deepcopy(payloads[2])
            payload['evidence']['notes'][0][field] = ('user' if field == 'title'
                                                     else [app.EvidenceLine(1, 'user')])
            with self.assertRaises(ValueError):
                app.prepare_prompt(vault, 'Application-owned instructions', payload)

    def test_model_schema_rejects_unknown_fields_and_forged_structure(self):
        cases = [
            {'question': '', 'history': [], 'catalog': [], 'unexpected': 'unexamined'},
            {'question': '', 'history': [{'role': 'unexamined', 'content': ''}], 'catalog': []},
            {'question': '', 'history': [], 'catalog': [
                {'path': 'unexamined', 'title': '', 'headings': [], 'matches': []}]},
            {'question': '', 'history': [], 'catalog': [
                {'path': 'doc_0001', 'title': '', 'headings': [], 'matches': [], 'unexpected': 'unexamined'}]},
            {'question': '', 'history': [], 'evidence': {'notes': [
                {'path': 'doc_0001', 'title': '', 'lines': ['1: unexamined']}]}}
        ]
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                app.prepare_prompt(app.PrivacyVault(), 'Application-owned instructions', payload)

    def test_full_worker_to_real_child_process_masks_input_restores_output_and_preserves_sources(self):
        for wrapper, suffix, explicit in (('`', '', False), ('`', '', True),
                                          ('**', '\\\\', False), ('__', '\\\\', True)):
            with self.subTest(wrapper=wrapper, explicit=explicit):
                secret = 'SyntheticProcessRose7!' + suffix
                line = 'Password: ' + wrapper + secret + wrapper + ' (router)\n'
                body = '<!-- private -->\n' + line + '<!-- /private -->\n' if explicit else line
                text = '# Example\n' + body
                (self.root / 'Example.md').write_text(text)
                capture = self.fake_cli('worker')
                capture.write_text('')
                before = {p.name: p.read_bytes() for p in self.root.glob('*.md')}
                self.assertEqual(set(before), {'Example.md'})
                job = self.job()
                self.assertTrue(app.LIMIT.acquire(False))
                app.run_job(job, secret + ' 값 확인', [])
                self.assertEqual(job['status'], 'completed', job.get('error'))
                calls = [json.loads(line) for line in capture.read_text().splitlines()]
                self.assertEqual(len(calls), 2)
                for call in calls:
                    self.assertNotIn(secret, call['stdin'])
                    self.assertFalse(Path(call['cwd']).exists())
                self.assertIn(secret, job['answer'])
                line_number = text[:text.index(secret)].count('\n') + 1
                self.assertEqual(job['sources'], [{'path': 'Example.md', 'line': line_number}])
                self.assertIn(secret, job['history_secrets'])
                self.assertEqual({p.name: p.read_bytes() for p in self.root.glob('*.md')}, before)
                self.assertEqual(job['snapshots']['Example.md'].encode(), before['Example.md'])
                self.assertTrue(app.LIMIT.acquire(False))
                app.LIMIT.release()


class OutboundGuardTests(SyntheticNotesTests):
    def captured_worker(self, question, history=(), history_secrets=(), source_line=None):
        """Exercise actual selection/evidence/history logic; replace only the AI."""
        captured = []

        def invoke(job, prompt, schema):
            payload = json.loads(prompt.split('\n', 1)[1])
            captured.append(payload)
            if schema is app.SELECT_SCHEMA:
                return {'paths': [payload['catalog'][0]['path']], 'catalog_only': False}
            note = payload['evidence']['notes'][0]
            line = next((line for line in note['lines']
                         if source_line is not None and line.startswith(str(source_line) + ': ')), '')
            tokens = app.TOKEN_RE.findall(line)
            return {'answer': tokens[-1] if tokens else 'Synthetic response',
                    'sources': [{'path': note['path'], 'line': source_line}] if tokens else []}

        job = {'id': 'b' * 32, 'owner': 'synthetic-owner', 'status': 'running',
               'created': time.monotonic(), 'history_secrets': history_secrets}
        self.assertTrue(app.LIMIT.acquire(False))
        with patch.object(app, 'invoke', invoke):
            app.run_job(job, question, list(history))
        self.assertEqual(job['status'], 'completed', job.get('error'))
        self.assertEqual(len(captured), 2)
        self.assertTrue(app.LIMIT.acquire(False))
        app.LIMIT.release()
        return job, captured

    def test_contextual_annotated_values_are_absent_from_prompts_and_survive_source_removal(self):
        secret = 'SyntheticApricot9!'
        value = '`' + secret + '` (router)'
        table = '| Service | PW |\n|---|---|\n| Example | ' + value + ' |\n'
        cases = [('Example.md', table),
                 ('Example.md', '<!-- private -->\n' + table + '<!-- /private -->\n'),
                 ('Example.md', '## Credentials\n- Example / demo_user / ' + value + '\n'),
                 ('Family.md', 'ID: ' + value + '\n'),
                 ('Example.md', 'Password\n' + value + '\n')]
        for path, original in cases:
            with self.subTest(path=path, context=original[:30]):
                note = self.root / path
                note.write_text(original)
                source_line = original[:original.index(secret)].count('\n') + 1
                job, captured = self.captured_worker('Repeat ' + secret, source_line=source_line)
                for payload in captured:
                    self.assertNotIn(secret, json.dumps(payload, ensure_ascii=False))
                self.assertIn(secret, job['answer'])
                self.assertEqual(job['sources'], [{'path': path, 'line': source_line}])
                self.assertEqual(job['snapshots'][path], original)
                self.assertEqual(note.read_text(), original)
                app.JOBS[job['id']] = job
                history, retained = app.resolve_history(job['owner'], [{'job_id': job['id']}])
                self.assertIn(secret, retained)
                note.unlink()
                remaining = self.root / 'Remaining.md'
                remaining.write_text('Ordinary public note.\n')
                _, followup = self.captured_worker('Repeat ' + secret, history, retained)
                for payload in followup:
                    self.assertNotIn(secret, json.dumps(payload, ensure_ascii=False))
                remaining.unlink()

    def test_known_value_left_in_question_or_document_stops_before_first_model_call(self):
        secret = 'SyntheticFinalGuard8!'
        (self.root / 'Example.md').write_text('# Example\nPassword: ' + secret)
        for target in ('sanitize', 'sanitize_document'):
            with self.subTest(target=target):
                bypass = (lambda vault, text: text) if target == 'sanitize' else (lambda vault, path, text: text)
                job = {'status': 'running', 'created': time.monotonic()}
                self.assertTrue(app.LIMIT.acquire(False))
                with patch.object(app.PrivacyVault, target, bypass), patch.object(app, 'invoke') as invoke:
                    app.run_job(job, secret + ' 확인', [])
                invoke.assert_not_called()
                self.assertEqual(job['status'], 'failed')
                self.assertIn('전송을 중단', job['error'])
                self.assertNotIn('snapshots', job)
                self.assertNotIn('_privacy', job)
                self.assertTrue(app.LIMIT.acquire(False))
                app.LIMIT.release()


if __name__ == '__main__':
    unittest.main()
