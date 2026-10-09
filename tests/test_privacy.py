"""Privacy-boundary tests use synthetic strings only."""
import unittest

from privacy import HIDDEN, PrivacyVault, TOKEN_RE


class PrivacyTests(unittest.TestCase):
    def vault(self, docs):
        vault = PrivacyVault()
        for path, text in docs.items():
            vault.learn_document(path, text)
        vault.seal()
        return vault

    def assert_roundtrip(self, path, original):
        vault = self.vault({path: original})
        safe = vault.sanitize_document(path, original)
        self.assertNotEqual(safe, original)
        self.assertEqual(len(safe.splitlines()), len(original.splitlines()))
        self.assertEqual(vault.restore(safe, vault.tokens_for(safe)), original)
        return vault, safe

    def test_labeled_credential_and_exact_roundtrip(self):
        original = '# Service\nPassword: example-Only-Secret_999\nNormal server description.\n'
        vault, safe = self.assert_roundtrip('Infrastructure.md', original)
        self.assertNotIn('example-Only-Secret_999', safe)
        self.assertIn('Password:', safe)
        self.assertIn('Normal server description.', safe)
        self.assertFalse(vault.contains_known_value(safe))

    def test_known_values_redacted_in_questions_history_and_other_documents(self):
        vault = self.vault({'Secrets.md': 'Password: synthetic-one-secret'})
        for text in ['My password is synthetic-one-secret?', 'Earlier answer: synthetic-one-secret',
                     '# synthetic-one-secret title']:
            safe = vault.sanitize(text)
            self.assertNotIn('synthetic-one-secret', safe)
            self.assertTrue(vault.tokens_for(safe))
        self.assertNotIn('synthetic-one-secret', vault.sanitize_document('Unrelated.md', 'copy: synthetic-one-secret'))

    def test_family_unfamiliar_fields_conservatively_protected(self):
        original = '# Family\n## Sample Person\n- A-new-unlabeled-Secret!\n- 등록기준지 : Example private address 77\n'
        vault, safe = self.assert_roundtrip('Family.md', original)
        self.assertIn('Sample Person', safe)
        self.assertIn('등록기준지', safe)
        self.assertNotIn('A-new-unlabeled-Secret!', safe)
        self.assertNotIn('Example private address 77', safe)

    def test_wallet_seed_and_unlabeled_private_key(self):
        seed = 'amber banana canyon dream echo fabric garden harbor island jewel kitten lemon'
        key = 'abc123ef' * 8
        original = '# Wallet\n## Metamask\n- ' + seed + '\n- ' + key + '\n'
        vault, safe = self.assert_roundtrip('Wallet.md', original)
        self.assertNotIn(seed, safe)
        self.assertNotIn(key, safe)
        self.assertNotIn(seed, vault.sanitize(seed))

    def test_personal_slash_accounts_register_individual_values(self):
        original = '# Personal Inform.\n## ID / PW\n- ExampleBank / demo_login / Demo_pass!42 / 928364\n'
        vault, safe = self.assert_roundtrip('Personal Inform.md', original)
        self.assertIn('ExampleBank', safe)
        for value in ['demo_login', 'Demo_pass!42', '928364']:
            self.assertNotIn(value, safe)
            self.assertNotIn(value, vault.sanitize('Earlier value: ' + value))

    def test_card_expiry_and_cvc_redacted_individually(self):
        original = '# Credit / Debit Card\n- 4111-2222-3333-4444, 04/37, 876\n'
        vault, safe = self.assert_roundtrip('Personal.md', original)
        for value in ['4111-2222-3333-4444', '04/37', '876']:
            self.assertNotIn(value, TOKEN_RE.sub('', safe))
            self.assertNotIn(value, TOKEN_RE.sub('', vault.sanitize('value ' + value)))

    def test_card_table_preserves_labels_but_hides_values(self):
        original = '# Accounts\n| Service | ID | PW |\n| --- | --- | --- |\n| ExampleBank | sampleuser | Table-Password-9 |\n'
        vault, safe = self.assert_roundtrip('Example.md', original)
        self.assertIn('| Service | ID | PW |', safe)
        self.assertIn('ExampleBank', safe)
        self.assertNotIn('sampleuser', safe)
        self.assertNotIn('Table-Password-9', safe)

    def test_flat_license_heading_hides_next_value(self):
        original = '# Tools\nTypora License\n\nSYNTHETIC-LICENCE-2026\n\nNormal text.\n'
        vault, safe = self.assert_roundtrip('Tools.md', original)
        self.assertIn('Typora License', safe)
        self.assertNotIn('SYNTHETIC-LICENCE-2026', safe)
        self.assertIn('Normal text.', safe)

    def test_private_heading_scope_ends_at_peer_heading(self):
        original = '# Tools\n## Passwords\n- alpha-for-private-use\n### More\n- beta-for-private-use\n## Notes\nordinary information\n'
        vault, safe = self.assert_roundtrip('Tools.md', original)
        self.assertNotIn('alpha-for-private-use', safe)
        self.assertNotIn('beta-for-private-use', safe)
        self.assertIn('ordinary information', safe)

    def test_explicit_private_blocks_protect_unstructured_prose_and_headings(self):
        original = '# Public\nordinary information\n<!-- private -->\n# Personal arbitrary heading\nAny arbitrary secret prose.\n<!-- /private -->\nVisible after.\n'
        vault, safe = self.assert_roundtrip('Notes.md', original)
        self.assertNotIn('Personal arbitrary heading', safe)
        self.assertNotIn('Any arbitrary secret prose.', safe)
        self.assertIn('ordinary information', safe)
        self.assertIn('Visible after.', safe)

    def test_inline_private_block_and_unclosed_block_fail_closed(self):
        original = 'Visible <!-- private -->hidden-one<!-- /private --> visible.\n<!-- private -->\nremaining hidden prose\n'
        vault, safe = self.assert_roundtrip('Notes.md', original)
        self.assertNotIn('hidden-one', safe)
        self.assertNotIn('remaining hidden prose', safe)
        self.assertIn(' visible.', safe)

    def test_inline_labeled_value_restores_without_comment_delimiters(self):
        for label in ('Password:', 'Token =', 'License key:'):
            with self.subTest(label=label):
                secret = 'synthetic-inline-secret!'
                original = f'- {label} <!-- private -->{secret}<!-- /private -->\n'
                vault, safe = self.assert_roundtrip('Services.md', original)
                self.assertIn(label, safe)
                self.assertIn('<!-- private -->', safe)
                self.assertIn('<!-- /private -->', safe)
                tokens = vault.tokens_for(safe)
                self.assertEqual(list(vault.values_for(tokens).values()), [secret])
                token = next(iter(tokens))
                self.assertEqual(vault.restore(token, tokens), secret)
                self.assertNotIn(secret, vault.sanitize('Please repeat ' + secret))

    def test_inline_table_keeps_neighboring_private_cells_protected(self):
        original = ('| Service | ID | PW |\n|---|---|---|\n'
                    '| Example | synthetic-account-name | '
                    '<!-- private -->synthetic-table-secret!<!-- /private --> |\n')
        vault, safe = self.assert_roundtrip('Services.md', original)
        self.assertIn('| Example |', safe)
        self.assertNotIn('synthetic-account-name', safe)
        self.assertNotIn('synthetic-table-secret!', safe)
        values = set(vault.values_for(vault.tokens_for(safe)).values())
        self.assertEqual(values, {'synthetic-account-name', 'synthetic-table-secret!'})
        self.assertFalse(vault.contains_known_value('Service ID PW Example'))

    def test_inline_block_preserves_private_section_context_for_siblings(self):
        original = ('## Credentials\n'
                    '- Example / synthetic-login-name / '
                    '<!-- private -->synthetic-secret-value<!-- /private -->\n')
        vault, safe = self.assert_roundtrip('Services.md', original)
        self.assertIn('Example', safe)
        self.assertNotIn('synthetic-login-name', safe)
        self.assertNotIn('synthetic-secret-value', safe)
        self.assertTrue(all('<!--' not in value for value in
                            vault.values_for(vault.tokens_for(safe)).values()))

    def test_explicit_block_around_fence_preserves_language_and_payload(self):
        for fence in ('```text', '```json', '~~~python'):
            with self.subTest(fence=fence):
                close = fence[:3]
                original = ('## Example\n<!-- private -->\n' + fence + '\n'
                            'synthetic-fenced-secret!\n' + close + '\n<!-- /private -->\n')
                vault, safe = self.assert_roundtrip('Services.md', original)
                self.assertIn(fence, safe)
                self.assertNotIn('synthetic-fenced-secret!', safe)
                self.assertEqual(list(vault.values_for(vault.tokens_for(safe)).values()),
                                 ['synthetic-fenced-secret!'])
                self.assertEqual(vault.sanitize('text json python Example'), 'text json python Example')

    def test_explicit_block_also_registers_inner_credential_atoms(self):
        secret = 'synthetic-inner-secret!'
        original = '<!-- private -->\nPassword: ' + secret + '\n<!-- /private -->\n'
        vault, safe = self.assert_roundtrip('Services.md', original)
        self.assertNotIn(secret, safe)
        self.assertNotIn(secret, vault.sanitize('Repeat ' + secret))
        self.assertNotIn(secret, vault.sanitize_document('Other.md', 'Remember ' + secret))

    def test_explicit_slash_row_registers_each_credential_atom(self):
        original = ('<!-- private -->\n## ID / PW\n'
                    '- Example / synthetic_login / synthetic-password! / 876\n'
                    '<!-- /private -->\n')
        vault, safe = self.assert_roundtrip('Services.md', original)
        for value in ('synthetic_login', 'synthetic-password!', '876'):
            self.assertNotIn(value, TOKEN_RE.sub('', vault.sanitize(value + '이 맞아?')))
        retained = vault.secrets_for(vault.tokens_for(safe))
        self.assertTrue({'synthetic_login', 'synthetic-password!', '876'} <= retained)
        self.assertNotIn('Example', retained)

    def test_explicit_whole_table_registers_inner_cells_without_service_labels(self):
        original = ('<!-- private -->\n| Service | ID | PW |\n|---|---|---|\n'
                    '| Example | synthetic_user | synthetic-secret! |\n<!-- /private -->\n')
        vault, safe = self.assert_roundtrip('Services.md', original)
        for value in ('synthetic_user', 'synthetic-secret!'):
            self.assertNotIn(value, vault.sanitize('Repeat ' + value))
        retained = vault.secrets_for(vault.tokens_for(safe))
        self.assertTrue({'synthetic_user', 'synthetic-secret!'} <= retained)
        self.assertNotIn('Example', retained)

    def test_selected_compound_history_values_include_atoms_but_not_other_secrets(self):
        selected = ('<!-- private -->\n'
                    '- Example / selected_login / selected-password! / 876\n'
                    '<!-- /private -->\n')
        other = 'Password: unrelated-password!\n'
        vault = self.vault({'Selected.md': selected, 'Other.md': other})
        safe = vault.sanitize_document('Selected.md', selected)
        retained = vault.secrets_for(vault.tokens_for(safe) | {'[[PRIVATE_' + 'f' * 32 + ']]'})
        self.assertTrue({'selected_login', 'selected-password!', '876'} <= retained)
        self.assertNotIn('unrelated-password!', retained)
        self.assertEqual(vault.secrets_for(()), set())
        next_request = self.vault({})
        for value in retained:
            next_request.learn_value(value)
        for value in ('selected_login', 'selected-password!', '876'):
            self.assertNotIn(value, TOKEN_RE.sub('', next_request.sanitize(value + '은?')))

    def test_nested_blocks_keep_outer_content_private_after_inner_close(self):
        original = ('Visible before.\n<!-- private -->\nouter-one\n'
                    '<!-- private -->\ninner-value\n<!-- /private -->\n'
                    'outer-two\n<!-- /private -->\nVisible after.\n')
        vault, safe = self.assert_roundtrip('Notes.md', original)
        for value in ('outer-one', 'inner-value', 'outer-two'):
            self.assertNotIn(value, safe)
            self.assertNotIn(value, vault.sanitize('Repeat ' + value))
        self.assertIn('Visible before.', safe)
        self.assertIn('Visible after.', safe)
        self.assertTrue(all('<!--' not in value for value in vault.values_for(vault.tokens_for(safe)).values()))

    def test_unclosed_nested_outer_block_protects_remaining_lines(self):
        original = ('Visible <!-- private -->outer-one <!-- private -->inner-value'
                    '<!-- /private --> outer-two\nremaining-value\n')
        _, safe = self.assert_roundtrip('Notes.md', original)
        for value in ('outer-one', 'inner-value', 'outer-two', 'remaining-value'):
            self.assertNotIn(value, safe)
        self.assertIn('Visible ', safe)

    def test_new_credentials_in_question_are_locally_registered(self):
        vault = self.vault({})
        safe = vault.sanitize('Please find password: NEW-SYNTHETIC-PASSWORD!')
        self.assertNotIn('NEW-SYNTHETIC-PASSWORD!', safe)
        self.assertTrue(vault.tokens_for(safe))
        self.assertEqual(set(vault.values_for(vault.tokens_for(safe)).values()), {'NEW-SYNTHETIC-PASSWORD!'})

    def test_service_env_and_authorization_values(self):
        original = 'SERVICE_API_KEY=ExampleToken_98\nAuthorization: Bearer abcdefghijkl98765\nrun --password=Some-New-Password_66\n'  # gitleaks:allow -- fictional fixture
        vault, safe = self.assert_roundtrip('Infra.md', original)
        for value in ['ExampleToken_98', 'abcdefghijkl98765', 'Some-New-Password_66']:  # gitleaks:allow -- fictional fixture assertions
            self.assertNotIn(value, safe)

    def test_uri_credentials_protected(self):
        original = 'database: postgres://demouser:examplepass@db.test:5432/data\n'
        vault, safe = self.assert_roundtrip('Infra.md', original)
        self.assertNotIn('demouser:examplepass', safe)
        self.assertIn('@db.test:5432/data', safe)

    def test_pem_private_key_protected_with_line_count(self):
        original = '-----BEGIN PRIVATE KEY-----\nsyntheticBase64Line001\nsyntheticBase64Line002\n-----END PRIVATE KEY-----\n'  # gitleaks:allow -- deliberately invalid PEM fixture
        vault, safe = self.assert_roundtrip('Infra.md', original)
        self.assertNotIn('syntheticBase64Line001', safe)
        self.assertNotIn('syntheticBase64Line002', safe)

    def test_structured_identifiers_not_outbound(self):
        original = 'Resident 990101-1234567\nPassport M12345678\nBank 2222-333-444444\n'
        vault, safe = self.assert_roundtrip('Notes.md', original)
        self.assertNotIn('990101-1234567', safe)
        self.assertNotIn('M12345678', safe)
        self.assertNotIn('2222-333-444444', safe)

    def test_unlabeled_hex_api_and_jwt_values_are_detected_without_private_context(self):
        values = {
            'hex': 'a1b2c3d4' * 4,
            'prefixed_hex': '0x' + 'a1b2c3d4' * 8,
            'api_key': 'sk-syntheticExample1234',
            'github_token': 'ghp_syntheticExample1234',
            'github_pat': 'github_pat_syntheticExample1234',
            'slack_token': 'xoxb-syntheticExample1234',
            'jwt': 'eyJsyntheticHeader.syntheticPayload.syntheticSignature',
        }
        for kind, value in values.items():
            for mode in ('question', 'document'):
                with self.subTest(kind=kind, mode=mode):
                    # No labels, filename scope, explicit block or prior
                    # registration may rescue a broken format detector here.
                    vault = self.vault({})
                    original = 'Unrelated text (' + value + ') remains visible.'
                    safe = (vault.sanitize(original) if mode == 'question'
                            else vault.sanitize_document('Garden.md', original))
                    self.assertNotIn(value, TOKEN_RE.sub('', safe))
                    self.assertEqual(len(vault.tokens_for(safe)), 1)
                    self.assertIn('Unrelated text (', safe)
                    self.assertIn(') remains visible.', safe)
                    self.assertEqual(vault.restore(safe, vault.tokens_for(safe)), original)

    def test_structured_token_lookalikes_and_identifier_boundaries_remain_public(self):
        for value in ('a' * 31, 'g' + 'a1b2c3d4' * 4, 'a1b2c3d4' * 4 + 'g',
                      'sk-short', 'ghp_short', 'prefix_sk-syntheticExample1234',
                      'eyJabc.syntheticPayload.syntheticSignature',
                      'eyJsyntheticHeader.onlyTwoSegments'):
            with self.subTest(value=value):
                vault = self.vault({})
                self.assertEqual(vault.sanitize(value), value)
                self.assertEqual(vault.sanitize_document('Garden.md', value), value)

    def test_selected_evidence_allowlist_blocks_other_registered_values(self):
        vault = self.vault({'A.md': 'Password: SELECTED-SECRET-777',
                            'B.md': 'Password: UNSELECTED-SECRET-888'})
        selected = vault.sanitize_document('A.md', 'Password: SELECTED-SECRET-777')
        other = vault.sanitize_document('B.md', 'Password: UNSELECTED-SECRET-888')
        answer = next(iter(vault.tokens_for(selected))) + ' ' + next(iter(vault.tokens_for(other)))
        restored = vault.restore(answer, vault.tokens_for(selected))
        self.assertIn('SELECTED-SECRET-777', restored)
        self.assertNotIn('UNSELECTED-SECRET-888', restored)
        self.assertIn(HIDDEN, restored)

    def test_tokens_are_request_specific_and_unguessable_shape(self):
        a, b = self.vault({}), self.vault({})
        a.learn_value('same-test-value')
        b.learn_value('same-test-value')
        first, second = a.sanitize('same-test-value'), b.sanitize('same-test-value')
        self.assertNotEqual(first, second)
        self.assertRegex(first, '^' + TOKEN_RE.pattern + '$')
        self.assertEqual(a.restore(second, a.tokens_for(first)), HIDDEN)

    def test_forged_and_malformed_placeholders_neutralized(self):
        vault = self.vault({'A.md': 'Password: secret-example'})
        safe = vault.sanitize('Password: secret-example')
        token = next(iter(vault.tokens_for(safe)))
        self.assertEqual(vault.sanitize(token), HIDDEN)
        self.assertEqual(vault.restore('[[PRIVATE_not-a-real-token]]', {token}), HIDDEN)
        self.assertEqual(vault.filter_tokens('[[PRIVATE_missing-end', {token}), HIDDEN)
        self.assertEqual(vault.filter_tokens(token, {token}), token)

    def test_restoration_is_single_pass(self):
        vault = self.vault({})
        vault.learn_value('inner-secret-value')
        inner_token = vault.sanitize('inner-secret-value')
        vault.learn_value('outer value ' + inner_token)
        # Get the outer token from the local registry without feeding a literal
        # token through sanitize(), which intentionally neutralizes such input.
        outer_token = next(t for t, v in vault._tokens.items() if v == 'outer value ' + inner_token)
        restored = vault.restore(outer_token, {outer_token, inner_token})
        self.assertEqual(restored, 'outer value ' + inner_token)
        self.assertNotIn('inner-secret-value', restored)

    def test_longest_overlapping_registered_value_roundtrips(self):
        vault = self.vault({})
        vault.learn_value('part-of-secret')
        vault.learn_value('full-part-of-secret-end')
        safe = vault.sanitize('full-part-of-secret-end')
        self.assertEqual(len(vault.tokens_for(safe)), 1)
        self.assertEqual(vault.restore(safe, vault.tokens_for(safe)), 'full-part-of-secret-end')

    def test_imported_known_value_is_sanitized(self):
        vault = self.vault({})
        vault.learn_value('Earlier-Question-Secret_123')
        self.assertNotIn('Earlier-Question-Secret_123', vault.sanitize('Earlier-Question-Secret_123 again?'))

    def test_numeric_values_allow_korean_particles_but_not_longer_numbers(self):
        vault = self.vault({})
        for value in ('876', '123456'):
            vault.learn_value(value)
            for suffix in ('이', '은', '으로', '인데', '?'):
                safe = TOKEN_RE.sub('', vault.sanitize(value + suffix))
                self.assertNotIn(value, safe)
                self.assertIn(suffix, safe)
        for ordinary in ('98765', '1876', '8761', 'a876b', '_876', '876_', '８876', '876８', '91234567'):
            self.assertEqual(vault.sanitize(ordinary), ordinary)
        safe = TOKEN_RE.sub('', vault.sanitize('value 876, quantity 98765, label9876'))
        self.assertNotIn('value 876', safe)
        self.assertIn('quantity 98765', safe)
        self.assertIn('label9876', safe)

    def test_short_ascii_secrets_allow_korean_particles_not_larger_ascii_words(self):
        vault = self.vault({})
        vault.learn_value('Qz')
        for phrase in ('Qz가', 'Qz는', 'Qz로', '이 값 Qz!'):
            self.assertNotIn('Qz', TOKEN_RE.sub('', vault.sanitize(phrase)))
        for ordinary in ('aQzb', '_Qz', 'Qz_', 'Qz19', '19Qz'):
            self.assertEqual(vault.sanitize(ordinary), ordinary)

    def test_short_unicode_secrets_remain_private_with_korean_particles(self):
        for value in ('별빛', '꽃별밤', 'é9'):
            with self.subTest(value=value):
                original = '<!-- private -->' + value + '<!-- /private -->'
                vault, _ = self.assert_roundtrip('Example.md', original)
                for suffix in ('이', '는', '으로', '인지', '?'):
                    safe = TOKEN_RE.sub('', vault.sanitize(value + suffix + ' 맞아?'))
                    self.assertNotIn(value, safe)
                    self.assertIn(suffix, safe)

    def test_property_table_headers_do_not_pollute_unrelated_asset_tables(self):
        for field, value_label in (('항목', '내용'), ('항목', '정보'), ('항목', '값'),
                                   ('Field', 'Value'), ('Property', 'Value')):
            with self.subTest(field=field, value_label=value_label):
                sensitive = (f'| {field} | {value_label} |\n|---|---|\n'
                             '| 여권 | synthetic-document-id! |\n'
                             '| 생년월일 | 1980-03-12 |\n')
                ordinary = '# Assets\n| 항목 | 금액 |\n|---|---|\n| Savings | ₩ 15,390,000 |\n'
                vault = self.vault({'Personal/Family.md': sensitive, 'Personal/Assets.md': ordinary})
                safe = vault.sanitize_document('Personal/Family.md', sensitive)
                self.assertIn(f'| {field} | {value_label} |', safe)
                self.assertIn('| 여권 |', safe)
                self.assertIn('| 생년월일 |', safe)
                self.assertNotIn('synthetic-document-id!', safe)
                self.assertNotIn('1980-03-12', safe)
                self.assertEqual(vault.sanitize_document('Personal/Assets.md', ordinary), ordinary)
                self.assertFalse(vault.contains_known_value(field))
        # These words remain protectable when deliberately marked as values.
        vault, safe = self.assert_roundtrip('Example.md', '<!-- private -->항목<!-- /private -->')
        self.assertNotIn('항목', safe)
        self.assertNotIn('항목', TOKEN_RE.sub('', vault.sanitize('항목이 맞아?')))

    def test_property_table_shape_requires_private_context(self):
        for field, value_label in (('항목', '내용'), ('항목', '정보'), ('항목', '값'),
                                   ('Field', 'Value'), ('Property', 'Value')):
            table = (f'| {field} | {value_label} |\n|---|---|\n'
                     '| 위치 | Balcony |\n| Watering | Every morning |\n')
            for path in ('Garden.md', 'Personal/Words.md'):
                with self.subTest(field=field, value_label=value_label, path=path):
                    original = '# Garden\n' + table
                    vault = self.vault({path: original})
                    self.assertEqual(vault.sanitize_document(path, original), original)
                    self.assertEqual(vault.sanitize('Balcony Every morning'), 'Balcony Every morning')
            for path, original in (
                    ('Personal/Family.md', table),
                    ('Garden.md', '## Credentials\n' + table),
                    ('Garden.md', '<!-- private -->\n' + table + '<!-- /private -->\n')):
                with self.subTest(field=field, value_label=value_label, context=original[:20], path=path):
                    vault, safe = self.assert_roundtrip(path, original)
                    for value in ('Balcony', 'Every morning'):
                        self.assertNotIn(value, safe)
                        self.assertNotIn(value, vault.sanitize('Repeat ' + value))

    def test_property_table_protects_credential_rows_but_keeps_ordinary_peers(self):
        credential_labels = ('Password', 'Token', 'API key', 'ID', 'Username',
                             'Account number', '계좌번호')
        secrets = {label: f'SyntheticGarden{index}!' for index, label in enumerate(credential_labels)}
        for field, value_label in (('항목', '내용'), ('항목', '정보'), ('항목', '값'),
                                   ('Field', 'Value'), ('Property', 'Value')):
            original = (f'| {field} | {value_label} |\n|---|---|\n'
                        '| Name | Rose |\n| Place | Balcony |\n'
                        + ''.join(f'| {label} | {secret} |\n' for label, secret in secrets.items())
                        + '| Schedule | Every morning |\n| Description | Ordinary instructions. |\n')
            with self.subTest(field=field, value_label=value_label):
                vault, safe = self.assert_roundtrip('Garden.md', original)
                for label, secret in secrets.items():
                    self.assertIn('| ' + label + ' |', safe)
                    self.assertNotIn(secret, safe)
                    self.assertNotIn(secret, vault.sanitize(secret + ' 이것 맞아?'))
                for row in ('| Name | Rose |', '| Place | Balcony |',
                            '| Schedule | Every morning |', '| Description | Ordinary instructions. |'):
                    self.assertIn(row, safe)

    def test_property_credential_row_with_wrapped_value_and_annotation(self):
        for wrapper in ('`', '**', '__', '"'):
            secret = 'SyntheticGarden9!'
            original = ('| 항목 | 내용 |\n|---|---|\n'
                        '| Password | ' + wrapper + secret + wrapper + ' (router) |\n'
                        '| Place | Balcony |\n')
            with self.subTest(wrapper=wrapper):
                vault, safe = self.assert_roundtrip('Garden.md', original)
                self.assertNotIn(secret, safe)
                self.assertNotIn(secret, vault.sanitize(secret + ' 이것 맞아?'))
                self.assertIn('(router)', safe)
                self.assertIn('| Place | Balcony |', safe)

    def test_personal_parent_preserves_words_gifts_assets_and_ordinary_dates(self):
        cases = {
            'Personal/Words.md': '# Words\n- Ordinary favorite phrase\n- 바람에 꽃잎처럼\n',
            'Personal/Gifts.md': '# Gifts\n| Date | Name | Amount |\n|---|---|---|\n| 2026-10-09 | Example | ₩ 30,000 |\n',
            'Personal/Assets.md': '# Assets\n## Savings\nTotal: ₩ 15,390,000\n- 2304 contribution: ₩ 1,200,000\n- Month: 2026-10\n- years: 2023-2026\n- range: 100-200\n',
        }
        vault = self.vault(cases)
        for name, content in cases.items():
            with self.subTest(path=name):
                self.assertEqual(vault.sanitize_document(name, content), content)

    def test_sensitive_paths_fields_and_explicit_values_survive_personal_path_refinement(self):
        for name in ('Personal.md', 'Personal/Family.md', 'Personal/Wallet.md',
                     'Personal/Personal Information.md', 'Credentials/Example.md'):
            with self.subTest(path=name):
                vault, safe = self.assert_roundtrip(name, '- synthetic-unlabeled-secret!\n')
                self.assertNotIn('synthetic-unlabeled-secret!', safe)
        for original in ('Password: 2026-10-09\n',
                         '<!-- private -->2026-10-09<!-- /private -->\n',
                         '## Credentials\n- 2026-10-09\n'):
            vault, safe = self.assert_roundtrip('Personal/Words.md', original)
            self.assertNotIn('2026-10-09', safe)
            self.assertNotIn('2026-10-09', vault.sanitize('Remember 2026-10-09'))

    def test_range_exceptions_require_prose_cue_and_bare_accounts_remain_private(self):
        ordinary = 'range: 1000-2000\n범위: 100-200\nyears: 2023-2026\n'
        vault = self.vault({})
        self.assertEqual(vault.sanitize(ordinary), ordinary)
        for value in ('1234-5678', '1000-2000', '2222-333-444444'):
            self.assertNotIn(value, vault.sanitize(value))

    def test_iso_date_exceptions_reject_invalid_months_days_and_leap_dates(self):
        for value in ('2026-00', '2026-13', '0000-01', '2026-02-30',
                      '2023-02-29', '2026-00-09', '2026-10-00'):
            with self.subTest(value=value):
                vault = self.vault({})
                safe = vault.sanitize(value)
                self.assertNotIn(value, safe)
                self.assertEqual(vault.restore(safe, vault.tokens_for(safe)), value)
        for value in ('2024-02-29', '2026-10-09', '2026-12'):
            with self.subTest(value=value):
                vault = self.vault({})
                self.assertEqual(vault.sanitize(value), value)

    def test_json_credentials_individually_protected(self):
        original = '{"password": "Json-Secret_1", "token": "Json-Secret_2"}\n'
        vault, safe = self.assert_roundtrip('Infra.md', original)
        for value in ['Json-Secret_1', 'Json-Secret_2']:
            self.assertNotIn(value, safe)
            self.assertNotIn(value, vault.sanitize(value))

    def test_whitespace_credentials_protected(self):
        for original in ['password WordSecret998\n', 'Typora License SYNTHETIC-LICENSE\n',
                         '# Password swordfish\n']:
            vault, safe = self.assert_roundtrip('Example.md', original)
            for value in ['WordSecret998', 'SYNTHETIC-LICENSE', 'swordfish']:
                self.assertNotIn(value, safe)

    def test_sensitive_filename_value_protected(self):
        vault = self.vault({'password swordfish.md': 'ordinary body'})
        self.assertNotIn('swordfish', vault.sanitize('password swordfish.md'))
        self.assertNotIn('swordfish', vault.sanitize('What was swordfish?'))

    def test_credentials_section_supported(self):
        original = '# Server\n## Credentials\n- admin / swordfish\n## Documentation\nordinary information\n'
        vault, safe = self.assert_roundtrip('Server.md', original)
        self.assertNotIn('swordfish', safe)
        self.assertIn('ordinary information', safe)

    def test_family_service_username_slash_password(self):
        original = '# Family\n## Example Child\n- 농협투자증권 child_user_1 / Child-Password_9\n'
        vault, safe = self.assert_roundtrip('Family.md', original)
        for value in ['child_user_1', 'Child-Password_9']:
            self.assertNotIn(value, safe)
            self.assertNotIn(value, vault.sanitize(value))
        self.assertIn('농협투자증권', safe)

    def test_flat_license_fully_protected_despite_structured_submatch(self):
        original = 'Typora License\nSYNTHETIC-LICENSE\n'
        vault, safe = self.assert_roundtrip('Typora.md', original)
        self.assertNotIn('SYNTHETIC', safe)
        self.assertNotIn('-LICENSE', safe)

    def test_markdown_or_json_wrapped_secrets_learn_inner_atoms(self):
        for original in ['Password: "swordfish"', '{"password": "swordfish"}', 'Password: **swordfish**']:
            vault = self.vault({'Example.md': original})
            self.assertNotIn('swordfish', vault.sanitize('swordfish 이것 맞아?'))
        for imported in ['"swordfish"', '**swordfish**', '`swordfish`', "'swordfish'"]:
            vault = self.vault({})
            vault.learn_value(imported)
            self.assertNotIn('swordfish', vault.sanitize('swordfish 이것 맞아?'))
            safe = vault.sanitize(imported)
            self.assertEqual(vault.restore(safe, vault.tokens_for(safe)), imported)

    def test_wrapped_credential_before_annotation_registers_bare_value(self):
        secret = 'SyntheticRose7!'
        for wrapper in ('`', '``', '*', '**', '***', '_', '__', '___', '"', "'"):
            for label in ('Password: ', 'password ', 'API_KEY='):
                line = label + wrapper + secret + wrapper + ' (router)\n'
                for explicit in (False, True):
                    original = ('<!-- private -->\n' + line + '<!-- /private -->\n'
                                if explicit else line)
                    with self.subTest(wrapper=wrapper, label=label, explicit=explicit):
                        vault, safe = self.assert_roundtrip('Garden.md', original)
                        self.assertNotIn(secret, safe)
                        self.assertNotIn(secret, vault.sanitize(secret + ' 이것 맞아?'))
                        self.assertIn(secret, vault.secrets_for(vault.tokens_for(safe)))
                        if not explicit:
                            self.assertIn('(router)', safe)
                            self.assertEqual(set(vault.values_for(vault.tokens_for(safe)).values()), {secret})

    def test_contextual_annotated_credentials_register_bare_atoms(self):
        secret = 'SyntheticApricot9!'
        for wrapper in ('`', '``', '**', '__', '"', "'"):
            value = wrapper + secret + wrapper + ' (router)'
            table = '| Service | PW |\n|---|---|\n| Example | ' + value + ' |\n'
            cases = [('Notes.md', table),
                     ('Notes.md', '<!-- private -->\n' + table + '<!-- /private -->\n'),
                     ('Notes.md', '## Credentials\n- Example / demo_user / ' + value + '\n'),
                     ('Family.md', 'ID: ' + value + '\n'),
                     ('Notes.md', 'Password\n' + value + '\n')]
            for path, original in cases:
                with self.subTest(wrapper=wrapper, path=path, context=original[:30]):
                    vault, safe = self.assert_roundtrip(path, original)
                    self.assertNotIn(secret, safe)
                    self.assertNotIn(secret, vault.sanitize('Repeat ' + secret))
                    self.assertIn(secret, vault.secrets_for(vault.tokens_for(safe)))
                    self.assertFalse(vault.contains_known_value('Service PW Example'))

    def test_credential_table_extra_columns_and_markdown_headers_preserve_public_peers(self):
        secret = 'SyntheticApricot9!'
        for header in ('Password', '**Password**', '`Password`', '__Password__', 'API key'):
            for first in (False, True):
                headings = [header, 'Service', 'Notes'] if first else ['Service', header, 'Notes']
                values = [secret, 'Example', 'Router upstairs'] if first else ['Example', secret, 'Router upstairs']
                original = ('| ' + ' | '.join(headings) + ' |\n|---|---|---|\n| '
                            + ' | '.join(values) + ' |\n')
                with self.subTest(header=header, first=first):
                    vault, safe = self.assert_roundtrip('Notes.md', original)
                    self.assertIn(original.splitlines()[0], safe)
                    self.assertIn('Example', safe)
                    self.assertIn('Router upstairs', safe)
                    self.assertNotIn(secret, safe)
                    self.assertNotIn(secret, vault.sanitize('Repeat ' + secret))

    def test_table_data_named_like_header_does_not_reset_sensitive_columns(self):
        original = ('| Service | Password | Notes |\n|---|---|---|\n'
                    '| Example | token | First router |\n'
                    '| Another | SyntheticApricot9! | Second router |\n')
        vault, safe = self.assert_roundtrip('Notes.md', original)
        self.assertNotIn('token', safe)
        self.assertNotIn('SyntheticApricot9!', safe)
        self.assertIn('First router', safe)
        self.assertIn('Second router', safe)

    def test_sensitive_yaml_block_scalars_protect_body_until_dedent(self):
        for indicator in ('|', '|-', '|+', '>', '>-', '>+', '|2-', '>+2'):
            original = ('server:\n  password: ' + indicator + ' # local credential\n'
                        '    SyntheticApricot9!\n\n    SecondPrivateLine!\n'
                        '  host: example.invalid\nsummary: Public description\n')
            with self.subTest(indicator=indicator):
                vault, safe = self.assert_roundtrip('Notes.md', original)
                self.assertIn('password: ' + indicator, safe)
                self.assertIn('host: example.invalid', safe)
                self.assertIn('summary: Public description', safe)
                for secret in ('SyntheticApricot9!', 'SecondPrivateLine!'):
                    self.assertNotIn(secret, safe)
                    self.assertNotIn(secret, vault.sanitize('Repeat ' + secret))
                fresh = self.vault({})
                question = fresh.sanitize(original)
                self.assertNotIn('SyntheticApricot9!', question)
                self.assertNotIn('SecondPrivateLine!', question)
                self.assertEqual(fresh.restore(question, fresh.tokens_for(question)), original)
        ordinary = 'description: |-\n  Public garden instructions\nplace: Balcony\n'
        vault = self.vault({'Notes.md': ordinary})
        self.assertEqual(vault.sanitize_document('Notes.md', ordinary), ordinary)

    def test_silverbullet_short_table_separators_protect_only_sensitive_columns(self):
        secret = 'SyntheticShortTable9!'
        for separator in ('-', '--', '---', ':-', '-:', ':-:'):
            original = ('| Service | PW | Notes |\n|' + '|'.join([separator] * 3)
                        + '|\n| Example | ' + secret + ' | Public router note |\n')
            with self.subTest(separator=separator):
                vault, safe = self.assert_roundtrip('Garden.md', original)
                self.assertNotIn(secret, safe)
                self.assertNotIn(secret, vault.sanitize('Repeat ' + secret))
                self.assertIn('Example', safe)
                self.assertIn('Public router note', safe)
        ordinary = '| Weather | Place |\n|-|-|\n| Sunny | Balcony |\n'
        vault = self.vault({'Garden.md': ordinary})
        self.assertEqual(vault.sanitize_document('Garden.md', ordinary), ordinary)

    def test_fenced_comments_cannot_end_private_section_or_flat_value_scope(self):
        for opener, decoys, closer in (
                ('```text', ['# generated by manager'], '```'),
                ('````text', ['```', '# generated by manager', '~~~', '## Public'], '`````'),
                ('~~~~text', ['~~~', '# generated by manager', '```', '## Public'], '~~~~')):
            for scope in ('## Passwords', 'Password'):
                original = (scope + '\n' + opener + '\n' + '\n'.join(decoys)
                            + '\nSyntheticFenceScope9!\n' + closer
                            + '\n## Public\nOrdinary garden description.\n')
                with self.subTest(opener=opener, scope=scope):
                    vault, safe = self.assert_roundtrip('Garden.md', original)
                    self.assertNotIn('SyntheticFenceScope9!', safe)
                    self.assertNotIn('SyntheticFenceScope9!', vault.sanitize('Repeat SyntheticFenceScope9!'))
                    self.assertIn('Ordinary garden description.', safe)
        public = '```text\n# Passwords\nOrdinary demonstration text.\n```\nPublic prose.\n'
        vault = self.vault({'Garden.md': public})
        self.assertEqual(vault.sanitize_document('Garden.md', public), public)

    def test_balanced_markdown_credential_labels_keep_public_prose(self):
        secret = 'SyntheticLabel9!'
        for wrapper in ('*', '_', '__', '**', '***', '___', '`', '``', '````'):
            for separator in (': ', '= ', ' '):
                original = wrapper + 'Password' + wrapper + separator + secret + '\nOrdinary prose.\n'
                with self.subTest(wrapper=wrapper, separator=separator):
                    vault, safe = self.assert_roundtrip('Garden.md', original)
                    self.assertIn(wrapper + 'Password' + wrapper, safe)
                    self.assertIn('Ordinary prose.', safe)
                    self.assertNotIn(secret, safe)
                    self.assertNotIn(secret, vault.sanitize('Repeat ' + secret))

    def test_yaml_plain_comment_registers_scalar_without_comment(self):
        for secret in ('SyntheticComment9!', 'Synthetic#Literal9!'):
            line = 'password: ' + secret + ' # Public local annotation\n'
            for explicit in (False, True):
                original = '<!-- private -->\n' + line + '<!-- /private -->\n' if explicit else line
                with self.subTest(secret=secret, explicit=explicit):
                    vault, safe = self.assert_roundtrip('Garden.md', original)
                    self.assertNotIn(secret, vault.sanitize('Repeat ' + secret))
                    self.assertIn(secret, vault.secrets_for(vault.tokens_for(safe)))
                    if not explicit:
                        self.assertIn('# Public local annotation', safe)

    def test_quoted_credentials_learn_decoded_aliases_and_keep_exact_source(self):
        cases = [("password: 'Synthetic''Petal9!' # public\n", "Synthetic''Petal9!", "Synthetic'Petal9!"),
                 ('{"password": "Synthetic\\u0052ose9!", "host": "public.example"}\n',
                  'Synthetic\\u0052ose9!', 'SyntheticRose9!'),
                 ('password: "Synthetic\\x52ose9!" # public\n', 'Synthetic\\x52ose9!', 'SyntheticRose9!'),
                 ('| Service | Password |\n|-|-|\n| Example | "Synthetic\\u0052ose9!" |\n',
                  'Synthetic\\u0052ose9!', 'SyntheticRose9!')]
        for original, raw, decoded in cases:
            with self.subTest(raw=raw):
                vault, safe = self.assert_roundtrip('Garden.md', original)
                self.assertNotIn(raw, safe)
                self.assertNotIn(decoded, vault.sanitize('Repeat ' + decoded))
                retained = vault.secrets_for(vault.tokens_for(safe))
                self.assertIn(decoded, retained)
                fresh = self.vault({})
                for value in retained:
                    fresh.learn_value(value)
                self.assertNotIn(decoded, fresh.sanitize('Repeat ' + decoded))
        public = 'description: "Public\\u0020garden"\n'
        vault = self.vault({'Garden.md': public})
        self.assertEqual(vault.sanitize_document('Garden.md', public), public)
        self.assertEqual(vault.sanitize('Public garden'), 'Public garden')

    def test_tagged_anchored_yaml_block_scalars_stop_at_public_dedent(self):
        for properties in ('!!str ', '!<tag:yaml.org,2002:str> ', '&local ',
                           '!!str &local ', '&local !!str '):
            original = ('password: ' + properties + '|- # local\n'
                        '  SyntheticTagged9!\n  SecondPrivateLine!\nhost: public.example\n')
            with self.subTest(properties=properties):
                vault, safe = self.assert_roundtrip('Garden.md', original)
                self.assertIn('password: ' + properties + '|-', safe)
                self.assertIn('host: public.example', safe)
                self.assertNotIn('SyntheticTagged9!', safe)
                self.assertNotIn('SecondPrivateLine!', safe)
                self.assertNotIn('SyntheticTagged9!', vault.sanitize('Repeat SyntheticTagged9!'))
                self.assertNotIn('SyntheticTagged9!', self.vault({}).sanitize(original))

    def test_private_fence_arbitrary_info_is_protected_with_delimiters_preserved(self):
        for delimiter in ('```', '~~~', '````'):
            for info in ('SyntheticApricot9', 'python SyntheticApricot9!', 'json title=SensitiveLabel'):
                original = ('<!-- private -->\n' + delimiter + info + '\n'
                            + delimiter + '\n<!-- /private -->\n')
                with self.subTest(delimiter=delimiter, info=info):
                    vault, safe = self.assert_roundtrip('Notes.md', original)
                    self.assertNotIn(info, safe)
                    self.assertTrue(safe.splitlines()[1].startswith(delimiter))
                    self.assertEqual(safe.splitlines()[2], delimiter)
                    self.assertNotIn(info, vault.sanitize('Repeat ' + info))

    def test_private_fence_info_before_inline_closing_marker_stays_protected(self):
        for opening in ('<!-- private -->\n', '<!-- private -->'):
            original = opening + '~~~SyntheticApricot9<!-- /private -->\n~~~\n'
            with self.subTest(opening=opening):
                vault, safe = self.assert_roundtrip('Notes.md', original)
                self.assertNotIn('SyntheticApricot9', safe)
                self.assertNotIn('SyntheticApricot9', vault.sanitize('Repeat SyntheticApricot9'))
                self.assertEqual(safe.count('~~~'), 2)
                self.assertIn('<!-- /private -->', safe)
                self.assertEqual(set(vault.values_for(vault.tokens_for(safe)).values()), {'SyntheticApricot9'})

    def test_private_fence_labeled_info_registers_bare_atoms(self):
        secret = 'SyntheticFence9!'
        for opening in ('<!-- private -->\n', '<!-- private -->'):
            for closing in ('\n~~~\n<!-- /private -->\n', '<!-- /private -->\n~~~\n'):
                for value in (secret, '`' + secret + '` (router)', '"' + secret + '"'):
                    original = opening + '~~~python password=' + value + closing
                    with self.subTest(opening=opening, closing=closing, value=value):
                        vault, safe = self.assert_roundtrip('Notes.md', original)
                        self.assertNotIn(secret, safe)
                        self.assertNotIn(secret, vault.sanitize('Repeat ' + secret))
                        self.assertIn(secret, vault.secrets_for(vault.tokens_for(safe)))
                        self.assertEqual(vault.sanitize('python Example'), 'python Example')

    def test_emphasis_value_closers_respect_backslash_escape_parity(self):
        for wrapper in ('*', '**', '***', '_', '__', '___'):
            for backslashes in range(5):
                # Odd runs escape the first apparent closer, so a later
                # delimiter closes the value. Even runs do not escape it.
                secret = 'RoseLeaf' + '\\' * backslashes
                if backslashes % 2:
                    secret += wrapper + 'StillPrivate'
                original = 'Password: ' + wrapper + secret + wrapper + ' (router)\n'
                with self.subTest(wrapper=wrapper, backslashes=backslashes):
                    vault, safe = self.assert_roundtrip('Garden.md', original)
                    self.assertNotIn(secret, safe)
                    self.assertNotIn(secret, vault.sanitize(secret + ' 이것 맞아?'))
                    self.assertEqual(set(vault.values_for(vault.tokens_for(safe)).values()), {secret})
                    self.assertIn('(router)', safe)

    def test_password_first_table_column_protected(self):
        original = '## Passwords\n| Password | Description |\n| --- | --- |\n| swordfish | old |\n'
        vault, safe = self.assert_roundtrip('Example.md', original)
        self.assertNotIn('swordfish', safe)
        self.assertIn('old', safe)

    def test_credential_table_without_sensitive_heading(self):
        original = '# Server inventory\n| Service | Password | Description |\n| --- | --- | --- |\n| Example | table_secret_987 | old |\n'
        vault, safe = self.assert_roundtrip('Example.md', original)
        self.assertNotIn('table_secret_987', safe)
        self.assertIn('Example', safe)
        self.assertIn('old', safe)

    def test_passport_pattern_does_not_match_ordinary_words(self):
        vault = self.vault({})
        self.assertEqual(vault.sanitize('reference something SYNTHETIC'), 'reference something SYNTHETIC')

    def test_empty_service_table_cell_does_not_expose_password_column(self):
        for row in ['|| swordfish |', '| | swordfish |', '|example|swordfish|']:
            original = '## Credentials\n| Service | Password |\n| --- | --- |\n' + row + '\n'
            vault, safe = self.assert_roundtrip('Example.md', original)
            self.assertNotIn('swordfish', safe)

    def test_escaped_pipes_and_malformed_table_columns_are_conservative(self):
        for row, secret in [('| demo | part\\|secret | note |', 'part\\|secret'),
                            ('| demo | part|secret | note |', 'secret')]:
            original = '## Credentials\n| Service | Password | Description |\n| --- | --- | --- |\n' + row + '\n'
            vault, safe = self.assert_roundtrip('Example.md', original)
            self.assertNotIn(secret, safe)

    def test_unknown_private_payload_with_colon_is_fully_protected(self):
        original = '# Wallet\n- alpha:beta\n'
        vault, safe = self.assert_roundtrip('Wallet.md', original)
        self.assertNotIn('alpha', safe)
        self.assertNotIn('beta', safe)

    def test_unrelated_note_not_modified(self):
        vault = self.vault({})
        ordinary = '# Gardening\nWater herbs each morning.\n- Basil likes sunlight.\n'
        self.assertEqual(vault.sanitize_document('Gardening.md', ordinary), ordinary)

    def test_shared_analysis_recovers_structured_blocks_after_ordinary_prose(self):
        secret = 'SyntheticRecovery9!'
        cases = [
            ('Please inspect these fields\npassword: "FirstPart\n  ' + secret
             + '"\nhost: public.example\n', 'FirstPart ' + secret),
            ('Please inspect these fields\n{"pass\\u0077ord": "' + secret
             + '", "host": "public.example"}\n', secret),
            ("tool --password \\\n 'FirstPart " + secret + "'\n", 'FirstPart ' + secret),
        ]
        for original, decoded in cases:
            for channel in ('document', 'message'):
                with self.subTest(original=original, channel=channel):
                    vault = PrivacyVault()
                    safe = (vault.sanitize_document('Garden.md', original)
                            if channel == 'document' else vault.sanitize(original))
                    self.assertNotIn(secret, safe)
                    self.assertEqual(vault.restore(safe, vault.tokens_for(safe)), original)
                    self.assertEqual(len(safe.splitlines()), len(original.splitlines()))
                    self.assertNotIn(decoded, vault.sanitize('Repeat ' + decoded))
                    self.assertIn(decoded, vault.secrets_for(vault.tokens_for(safe)))
                    if 'public.example' in original:
                        self.assertIn('public.example', safe)

    def test_explicit_boundaries_remain_authoritative_inside_scalar_syntax(self):
        original = ('Password: <!-- private -->SyntheticExplicit9!'
                    '<!-- /private --> public explanation.\n')
        vault, safe = self.assert_roundtrip('Garden.md', original)
        self.assertIn('<!-- private -->', safe)
        self.assertIn('<!-- /private --> public explanation.', safe)
        self.assertNotIn('SyntheticExplicit9!', safe)
        self.assertEqual(set(vault.values_for(vault.tokens_for(safe)).values()),
                         {'SyntheticExplicit9!'})

    def test_unterminated_private_scalar_rejects_without_private_diagnostics(self):
        for prefix in ('password: ', 'password: !!str ', '{"pass\\u0077ord": ',
                       '<!-- private -->\nordinary: '):
            original = prefix + '"PrivateFirst\n  PrivateContinuation9!\n'
            vault = PrivacyVault()
            with self.assertRaises(ValueError) as failure:
                vault.sanitize(original)
            self.assertNotIn('PrivateFirst', str(failure.exception))
            self.assertNotIn('PrivateContinuation9!', str(failure.exception))
            with self.assertRaises(ValueError):
                vault.secrets_for(())

    def test_scalar_analysis_offsets_keep_tags_separate_from_decoded_atoms(self):
        from privacy_analysis import analyze
        original = 'password: &local !!str "Synthetic\\u0050etal9!"\nhost: public.example\n'
        analysis = analyze(original)
        atoms = [atom for atom in analysis.atoms if atom.decoded == 'SyntheticPetal9!']
        self.assertTrue(atoms)
        self.assertTrue(all(original[atom.start:atom.end] == 'Synthetic\\u0050etal9!'
                            for atom in atoms))
        self.assertTrue(all('\n' not in original[a:b] for a, b in analysis.spans))

    def test_explicit_neutral_scalar_aliases_survive_selected_history(self):
        for body in ('ordinary: "Synthetic\\u0050etal9!"',
                     '{"ordinary": "Synthetic\\u0050etal9!"}'):
            original = '<!-- private -->\n' + body + '\n<!-- /private -->\npublic.example\n'
            vault, safe = self.assert_roundtrip('Garden.md', original)
            selected = vault.secrets_for(vault.tokens_for(safe))
            self.assertIn('SyntheticPetal9!', selected)
            followup = PrivacyVault()
            for secret in selected:
                followup.learn_value(secret)
            self.assertNotIn('SyntheticPetal9!', followup.sanitize('Repeat SyntheticPetal9!'))
            self.assertIn('public.example', safe)

    def test_sensitive_collection_aliases_reject_without_expanding(self):
        for collection in ('[SyntheticAlpha9!, SyntheticBeta9!]',
                           '{first: SyntheticAlpha9!, second: SyntheticBeta9!}'):
            for reference in ('password: *alias\n',
                              '<!-- private -->\nordinary: *alias\n<!-- /private -->\n'):
                original = 'shared: &alias ' + collection + '\n' + reference
                vault = PrivacyVault()
                with self.assertRaises(ValueError) as failure:
                    vault.sanitize(original)
                self.assertNotIn('SyntheticAlpha9!', str(failure.exception))
                with self.assertRaises(ValueError):
                    vault.sanitize('public')

    def test_scalar_aliases_used_as_keys_keep_private_value_roles(self):
        original = 'label: &sensitive password\n*sensitive: SyntheticPetal9!\nhost: public.example\n'
        vault, safe = self.assert_roundtrip('Garden.md', original)
        self.assertNotIn('SyntheticPetal9!', safe)
        self.assertNotIn('SyntheticPetal9!', vault.sanitize('Repeat SyntheticPetal9!'))
        self.assertIn('public.example', safe)

    def test_commonmark_physical_lines_control_scope_and_source_offsets(self):
        from privacy_analysis import source_lines
        for separator in ('\u2028', '\u2029', '\x85'):
            original = ('prefix' + separator + 'tail\n## Credentials\nSyntheticPetal9!\n'
                        '## Public\npublic.example\n')
            vault, safe = self.assert_roundtrip('Garden.md', original)
            self.assertEqual(len(source_lines(safe)), 5)
            self.assertNotIn('SyntheticPetal9!', safe)
            self.assertIn('public.example', safe)
            self.assertEqual(vault.restore(safe, vault.tokens_for(safe)), original)

    def test_private_heading_scope_ends_when_markdown_container_ends(self):
        for original in (
                '> ## Credentials\n> SyntheticPetal9!\n\npublic.example\n',
                '- ## Credentials\n  SyntheticPetal9!\n\n- public.example\n'):
            vault, safe = self.assert_roundtrip('Garden.md', original)
            self.assertNotIn('SyntheticPetal9!', safe)
            self.assertIn('public.example', safe)

    def test_literal_shell_decoding_matches_posix_shell_without_expanding(self):
        import subprocess
        # Fixed synthetic literal words only. Never pass notebook or user input
        # to a shell; this independent local oracle tests the supported grammar.
        words = [r'"Synthetic\$Petal9!"', r'"Synthetic\`Petal9!"',
                 r'"Synthetic\\Petal9!"', r'"Synthetic\qPetal9!"',
                 r'"Synthetic\"Petal9!"', r"'Synthetic$Petal9!'",
                 r"'Synthetic'\''Petal9!'", r'Synthetic\ Petal9!',
                 'Synthetic\\\nPetal9!', r'"Synthetic"\$\'Petal9!\'']
        for word in words:
            with self.subTest(word=word):
                expected = subprocess.check_output(['bash', '--noprofile', '--norc', '-c',
                    'printf %s ' + word], text=True)
                original = 'tool --password ' + word + ' --host public.example\n'
                vault, safe = self.assert_roundtrip('Garden.md', original)
                self.assertIn(expected, vault.secrets_for(vault.tokens_for(safe)))
                self.assertNotIn(expected, vault.sanitize('Repeat ' + expected))

    def test_literal_markdown_code_does_not_reinterpret_quote_or_expansion_characters(self):
        for value in ("'SyntheticLiteral9!", '"SyntheticLiteral9!', '${SyntheticLiteral9}'):
            for original in ('Password: `' + value + '` (literal)\n',
                             '<!-- private -->\n`' + value + '` (literal)\n<!-- /private -->\n'):
                with self.subTest(original=original):
                    vault, safe = self.assert_roundtrip('Garden.md', original)
                    self.assertIn(value, vault.secrets_for(vault.tokens_for(safe)))

    def test_unresolved_scalar_invalidates_context_and_all_future_exports(self):
        from model_input import prepare_prompt
        cases = ('tool --password "SyntheticFirst9!\n',
                 '| Service | Password |\n|---|---|\n| Example | "SyntheticFirst9! |\n',
                 'password: "SyntheticFirst9!\n')
        for source in cases:
            with self.subTest(source=source):
                vault = self.vault({'Prior.md': 'Password: SyntheticKnown9!\n'})
                with self.assertRaises(ValueError):
                    vault.sanitize(source)
                for export in (lambda: vault.sanitize('ordinary text'),
                               lambda: vault.values_for(()),
                               lambda: vault.restore('ordinary text'),
                               lambda: prepare_prompt(vault, 'Application instructions',
                                   {'question': '', 'history': [], 'catalog': []})):
                    with self.assertRaises(ValueError):
                        export()


if __name__ == '__main__':
    unittest.main()
