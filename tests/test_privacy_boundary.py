"""Privacy contracts at both model calls, using only temporary synthetic notes."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from test_support import load_test_app


app = load_test_app()


class PrivacyBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        cls.pem = key.private_bytes(serialization.Encoding.PEM,
                                    serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()).decode('ascii')

    def setUp(self):
        fixture = tempfile.TemporaryDirectory(prefix='wiki-privacy-boundary-')
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name)
        for target in (patch.object(app, 'ROOT', self.root), patch.dict(app.JOBS, clear=True)):
            target.start()
            self.addCleanup(target.stop)

    def run_worker(self, question, history=(), history_secrets=(), answer=None,
                   selection=None):
        """Replace just the external model; retain real preparation and restoration."""
        calls = []

        def invoke(job, prompt, schema):
            payload = json.loads(prompt.split('\n', 1)[1])
            calls.append(payload)
            if schema is app.SELECT_SCHEMA:
                if selection:
                    return selection(payload)
                chosen = next((item for item in payload['catalog'] if item['title'] == 'Garden.md'),
                              payload['catalog'][0])
                return {'paths': [chosen['path']], 'catalog_only': False}
            if answer:
                return answer(payload, calls)
            note = payload['evidence']['notes'][0]
            return {'answer': '\n'.join(line.split(': ', 1)[1] for line in note['lines']),
                    'sources': [{'path': note['path'], 'line': line}
                                for line in range(1, len(note['lines']) + 1)]}

        job = {'id': uuid.uuid4().hex, 'owner': 'synthetic-owner', 'status': 'running',
               'created': time.monotonic(), 'history_secrets': history_secrets}
        self.assertTrue(app.LIMIT.acquire(False))
        with patch.object(app, 'invoke', invoke):
            app.run_job(job, question, list(history))
        self.assertEqual(job['status'], 'completed',
                         str(job.get('error')) + '; captured model calls=' + str(len(calls)))
        self.assertEqual(len(calls), 2)
        self.assertTrue(app.LIMIT.acquire(False), 'Worker slot must be returned')
        app.LIMIT.release()
        return job, calls

    def assert_hidden(self, calls, values):
        # Inspect decoded payload fields too: JSON escaping must not hide a leak
        # from the assertion, and case-folded catalog matches are outbound data.
        def strings(value):
            if isinstance(value, str):
                yield value
            elif isinstance(value, list):
                for child in value:
                    yield from strings(child)
            elif isinstance(value, dict):
                for child in value.values():
                    yield from strings(child)

        for stage, payload in zip(('selector', 'answer'), calls):
            content = '\n'.join(strings(payload)).casefold()
            for value in values:
                self.assertNotIn(value.casefold(), content, stage + ' exposed a synthetic value')

    def test_malformed_sensitive_input_stops_before_models_and_identifies_origin(self):
        cases = (
            '{"password": "SyntheticFirst9!\nSyntheticSecond9!\n',
            'tool --password "SyntheticFirst9!\nSyntheticSecond9!\n',
            'tool --password="SyntheticFirst9!\nSyntheticSecond9!\n',
            '| Service | Password |\n|---|---|\n| Example | "SyntheticFirst9! |\n',
            'tool --password "$SYNTHETIC_PASSWORD"\n',
            'tool --password "$(printf SyntheticNeverExecute9)"\n',
            'tool --password SyntheticGlob*\n',
        )
        for source in cases:
            for explicit in (False, True):
                malformed = '<!-- private -->\n' + source + '<!-- /private -->\n' if explicit else source
                for channel, label in (('note', 'Garden.md'), ('question', '질문'),
                                       ('history', '대화 이력')):
                    with self.subTest(source=source, explicit=explicit, channel=channel):
                        (self.root / 'Garden.md').write_text(malformed if channel == 'note' else 'Public note\n')
                        job = {'id': uuid.uuid4().hex, 'owner': 'synthetic-owner', 'status': 'running',
                               'created': time.monotonic()}
                        self.assertTrue(app.LIMIT.acquire(False))
                        with patch.object(app, 'invoke') as invoke:
                            app.run_job(job, malformed if channel == 'question' else 'Show the note',
                                        [{'role': 'user', 'content': malformed}] if channel == 'history' else [])
                        self.assertEqual(job['status'], 'failed')
                        invoke.assert_not_called()
                        self.assertIn(label, job['error'])
                        self.assertNotIn('Synthetic', job['error'])
                        self.assertTrue(app.LIMIT.acquire(False), 'Failed analysis must return worker slot')
                        app.LIMIT.release()

    def formats(self):
        secret = 'SyntheticPetal9!'
        encoded = r'Synthetic\u0050etal9!'
        # name, original spelling, values/fragments forbidden outbound, bare follow-up
        cases = [
            ('plain-with-comment', 'password: ' + secret + ' # local annotation\nhost: public.example\n',
             (secret,), secret),
            ('yaml-double-multiline', 'password: "FirstPrivatePart\n  ' + secret + '"\nhost: public.example\n',
             ('FirstPrivatePart', secret), 'FirstPrivatePart ' + secret),
            ('yaml-single-multiline', "password: 'FirstPrivatePart\n  " + secret + "'\nhost: public.example\n",
             ('FirstPrivatePart', secret), 'FirstPrivatePart ' + secret),
            ('yaml-anchor', 'password: &local ' + secret + '\nhost: public.example\n',
             (secret,), secret),
            ('yaml-tag-escape', 'password: !!str "' + encoded + '"\nhost: public.example\n',
             (encoded, secret), secret),
            ('yaml-tagged-block', 'password: &local !!str |-\n  ' + secret + '\nhost: public.example\n',
             (secret,), secret),
            ('yaml-single-quote-escape', "password: 'Synthetic''Petal9!'\nhost: public.example\n",
             ("Synthetic''Petal9!", "Synthetic'Petal9!"), "Synthetic'Petal9!"),
            ('json-escaped-key-and-value', '{"pass\\u0077ord": "' + encoded + '", "host": "public.example"}\n',
             (encoded, secret), secret),
            ('markdown-label-colon', '**Password:** ' + secret + '\npublic.example\n',
             (secret,), secret),
            ('setext-private-section', 'Passwords\n---------\n' + secret + '\n\nPublic\n------\npublic.example\n',
             (secret,), secret),
            ('blockquote-private-section', '> ## Passwords\n> ' + secret + '\n> ## Public\n> public.example\n',
             (secret,), secret),
            ('quoted-cli-value', "tool --password 'FirstPrivatePart " + secret + "' --host public.example\n",
             ('FirstPrivatePart', secret), 'FirstPrivatePart ' + secret),
            ('shell-escaped-dollar', 'tool --password "Synthetic\\$Petal9!" --host public.example\n',
             (r'Synthetic\$Petal9!', 'Synthetic$Petal9!'), 'Synthetic$Petal9!'),
            ('shell-equals-escaped-dollar', 'tool --password="Synthetic\\$Petal9!" --host public.example\n',
             (r'Synthetic\$Petal9!', 'Synthetic$Petal9!'), 'Synthetic$Petal9!'),
            ('private-shell-escaped-dollar', '<!-- private -->\ntool --password "Synthetic\\$Petal9!"\n<!-- /private -->\npublic.example\n',
             (r'Synthetic\$Petal9!', 'Synthetic$Petal9!'), 'Synthetic$Petal9!'),
            ('explicit-block', '<!-- private -->\n' + secret + '\n<!-- /private -->\npublic.example\n',
             (secret,), secret),
            ('private-fenced-comments', '## Passwords\n````text\n```\n# generated comment\n~~~\n' + secret + '\n````\n## Public\npublic.example\n',
             (secret,), secret),
            ('private-arbitrary-fence-info', '<!-- private -->\n~~~' + secret + '\n~~~\n<!-- /private -->\npublic.example\n',
             (secret,), secret),
            ('private-labeled-fence-info', '<!-- private -->\n~~~python password=`' + secret + '` (router)\n~~~\n<!-- /private -->\npublic.example\n',
             (secret,), secret),
            ('annotated-credential-table', '| Service | **Password** | Notes |\n|-|-|-|\n| Example | `' + secret + '` (router) | public.example |\n',
             (secret,), secret),
            ('mixed-property-table', '| Property | Value |\n|---|---|\n| Host | public.example |\n| Password | ' + secret + ' |\n',
             (secret,), secret),
        ]
        pem_lines = tuple(line for line in self.pem.splitlines() if not line.startswith('-----'))
        cases.append(('pem', self.pem + 'public.example\n', pem_lines, pem_lines[len(pem_lines) // 2]))
        return cases

    def test_generated_metadata_collision_allows_followups_but_masks_actual_content(self):
        for secret in ('user', 'assistant', 'doc_0001', '1'):
            with self.subTest(secret=secret):
                (self.root / 'Garden.md').write_text('Password: ' + secret + '\npublic.example\n')
                first, _ = self.run_worker('Show the garden note')
                self.assertIn(secret, first['answer'])
                (self.root / 'Garden.md').write_text('public.example\n')
                following, calls = self.run_worker('Repeat ' + secret,
                                                   first['safe_history'], first['history_secrets'])
                for call in calls:
                    self.assertNotIn(secret, app.TOKEN_RE.sub('', call['question']))
                    self.assertEqual([message['role'] for message in call['history']], ['user', 'assistant'])
                self.assertEqual(following['status'], 'completed')

    def test_private_punctuation_does_not_reclassify_replacement_boundaries(self):
        original = "# Garden\nPassword: `'`\nPassword: 'SyntheticBoundary9'\npublic.example\n"  # gitleaks:allow -- fictional fixture
        (self.root / 'Garden.md').write_text(original)
        first, calls = self.run_worker('Show the garden note')
        self.assertEqual(first['answer'], original.rstrip('\n'))
        self.assert_hidden(calls, ('SyntheticBoundary9',))
        (self.root / 'Garden.md').write_text('public.example\n')
        following, calls = self.run_worker("Repeat 'SyntheticBoundary9'", first['safe_history'], first['history_secrets'])
        self.assertEqual(following['status'], 'completed')
        self.assert_hidden(calls, ('SyntheticBoundary9',))

    def test_format_and_input_channel_matrix_with_deleted_source_followup(self):
        for name, original, hidden, repeated in self.formats():
            for channel in ('document', 'question', 'history'):
                with self.subTest(format=name, channel=channel):
                    replacement = self.root / 'Public.md'
                    replacement.unlink(missing_ok=True)
                    note = self.root / 'Garden.md'
                    body = original if channel == 'document' else '# Garden\npublic.example\n'
                    note.write_text(body)
                    question = original if channel == 'question' else 'Show the garden note'
                    history = [{'role': 'user', 'content': original}] if channel == 'history' else []
                    before = note.read_bytes()
                    job, calls = self.run_worker(question, history)
                    self.assert_hidden(calls, hidden)
                    self.assert_hidden([{'history': job['safe_history']}] * 2, hidden)
                    exported = (calls[-1]['evidence']['notes'][0]['lines'] if channel == 'document'
                                else calls[-1]['question'] if channel == 'question'
                                else calls[-1]['history'][0]['content'])
                    public = '| Host | public.example |' if name == 'mixed-property-table' else 'public.example'
                    self.assertIn(public, json.dumps(exported))
                    self.assertEqual(job['answer'], body.rstrip('\n'))
                    self.assertEqual(job['sources'], [{'path': 'Garden.md', 'line': line}
                                                     for line in range(1, len(body.splitlines()) + 1)])
                    self.assertEqual(job['snapshots']['Garden.md'], body)
                    self.assertEqual(note.read_bytes(), before)
                    if channel != 'document':
                        continue
                    # Only server-authored history crosses into the next job.
                    app.JOBS[job['id']] = job
                    note.unlink()
                    replacement.write_text('# Replacement\npublic.example\n')
                    with app.LOCK:
                        messages, retained = app.resolve_history(job['owner'], [{'job_id': job['id']}])
                    followup, calls = self.run_worker('Repeat ' + repeated, messages, retained)
                    self.assert_hidden(calls, (repeated,))
                    self.assert_hidden([{'history': followup['safe_history']}] * 2, (repeated,))
                    self.assertEqual(followup['protected_values'], {})
                    self.assertFalse(note.exists())
                    self.assertEqual(replacement.read_text(), '# Replacement\npublic.example\n')

    def test_registration_is_shared_across_question_history_catalog_and_evidence(self):
        secret = 'SyntheticCrossInput9!'
        for learned_in in ('question', 'history'):
            with self.subTest(learned_in=learned_in):
                (self.root / 'Garden.md').write_text('# Garden ' + secret + '\nRepeat ' + secret + '\npublic.example\n')
                question = 'password: ' + secret if learned_in == 'question' else 'Repeat ' + secret
                history = [{'role': 'user', 'content': 'password: ' + secret}] if learned_in == 'history' else []
                job, calls = self.run_worker(question, history)
                self.assert_hidden(calls, (secret, secret.rstrip('!')))
                self.assertIn(secret, job['answer'], 'Selected source occurrences remain restorable')
                self.assertIn('public.example', job['answer'])

    def test_only_selected_evidence_tokens_restore_for_normal_and_catalog_answers(self):
        selected, unselected = 'SyntheticSelected9!', 'SyntheticUnselected9!'
        question_value, history_value = 'SyntheticQuestion9!', 'SyntheticHistory9!'
        (self.root / 'Garden.md').write_text('Password: ' + selected + '\npublic.example\n')
        (self.root / 'Other.md').write_text('# Password: ' + unselected + '\n')
        for catalog_only in (False, True):
            with self.subTest(catalog_only=catalog_only):
                offered = set()

                def selection(payload):
                    chosen = next(item for item in payload['catalog'] if item['title'] == 'Garden.md')
                    return {'paths': [] if catalog_only else [chosen['path']], 'catalog_only': catalog_only}

                def answer(payload, calls):
                    for call in calls:
                        offered.update(app.TOKEN_RE.findall(json.dumps(call)))
                    return {'answer': '\n'.join(sorted(offered)) + '\n[[PRIVATE_' + 'f' * 32 + ']]',
                            'sources': []}

                job, calls = self.run_worker('password: ' + question_value,
                                            [{'role': 'user', 'content': 'password: ' + history_value}],
                                            answer=answer, selection=selection)
                self.assertGreaterEqual(len(offered), 3 if catalog_only else 4)
                self.assert_hidden(calls, (selected, unselected, question_value, history_value))
                expected = set() if catalog_only else {selected}
                self.assertEqual(set(job['protected_values'].values()), expected)
                self.assertEqual(set(app.TOKEN_RE.findall(job['display_answer'])), set(job['protected_values']))
                for excluded in (unselected, question_value, history_value):
                    self.assertNotIn(excluded, job['answer'])
                self.assertTrue(all(unselected not in value for value in job['history_secrets']))
                self.assert_hidden([{'history': job['safe_history']}] * 2,
                                   (selected, unselected, question_value, history_value))

    def test_selection_uses_one_snapshot_and_original_line_numbers(self):
        note = self.root / 'Garden.md'
        # U+2028 is text within a CommonMark/browser source line, not a new
        # physical line. Keep privacy scopes and citations on that same map.
        original = 'prefix\u2028tail\n## Credentials\nSyntheticSnapshot9!\n## Public\npublic.example\n'
        note.write_text(original)

        def selection(payload):
            note.write_text('# Changed after selection\nPassword: SyntheticReplacement9!\n')
            return {'paths': [payload['catalog'][0]['path']], 'catalog_only': False}

        job, calls = self.run_worker('Show the garden note', selection=selection)
        self.assert_hidden(calls, ('SyntheticSnapshot9!', 'SyntheticReplacement9!'))
        self.assertEqual(job['answer'], original.rstrip('\n'))
        self.assertEqual(job['snapshots'], {'Garden.md': original})
        self.assertEqual(job['sources'], [{'path': 'Garden.md', 'line': line} for line in (1, 2, 3, 4, 5)])
        self.assertIn('SyntheticReplacement9!', note.read_text())


if __name__ == '__main__':
    unittest.main()
