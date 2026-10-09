"""Small synthetic fixtures exercise privacy work bounds and cancellation."""
import unittest
from unittest.mock import patch

from privacy import PrivacyVault, TOKEN_RE


class PrivacyResourceTests(unittest.TestCase):
    def test_nested_wrappers_keep_only_original_and_innermost_atom(self):
        secret = 'nested-secret!'
        value = secret
        for index in range(20):
            mark = '**' if index % 2 else '`'
            value = mark + value + mark
        vault = PrivacyVault()
        vault.learn_value('  ' + value + '  ')
        vault.seal()
        self.assertLessEqual(len(vault._values), 3)
        for query in (secret, value, '`' + secret + '`', '**' + secret + '**'):
            safe = vault.sanitize(query)
            self.assertNotIn(secret, TOKEN_RE.sub('', safe))
            self.assertEqual(vault.restore(safe, vault.tokens_for(safe)), query)

    def test_excessive_wrapper_depth_fails_closed_before_partial_export(self):
        vault = PrivacyVault()
        # Well inside the public 4,000-character question limit.
        value = '**' * (vault.MAX_WRAPPER_DEPTH + 1) + 'depth-secret!' + '**' * (vault.MAX_WRAPPER_DEPTH + 1)
        self.assertLess(len(value), 4000)
        with self.assertRaises(ValueError):
            vault.sanitize('password: `' + value + '`')
        for action in (lambda: vault.sanitize('ordinary'), vault.seal,
                       lambda: vault.values_for(()), lambda: vault.restore('ordinary')):
            with self.assertRaises(ValueError):
                action()

    def test_unique_value_limit_rejects_request_instead_of_skipping_new_secret(self):
        vault = PrivacyVault()
        vault.MAX_VALUES = 3
        for value in ('first-secret!', 'second-secret!', 'third-secret!'):
            vault.learn_value(value)
        with self.assertRaises(ValueError):
            vault.learn_value('fourth-secret!')
        with self.assertRaises(ValueError):
            vault.sanitize('fourth-secret!')

    def test_learned_text_budget_counts_utf8_bytes_not_codepoints(self):
        vault = PrivacyVault()
        vault.MAX_VALUE_BYTES = 12
        vault.learn_value('가나다')  # Nine UTF-8 bytes.
        with self.assertRaises(ValueError):
            vault.learn_value('라마')
        with self.assertRaises(ValueError):
            vault.sanitize('라마')

    def test_matcher_state_limit_fails_closed_during_sealing(self):
        vault = PrivacyVault()
        vault.MAX_MATCHER_STATES = 12
        vault.learn_value('a-distinct-long-secret')
        with self.assertRaises(ValueError):
            vault.seal()
        with self.assertRaises(ValueError):
            vault.contains_known_value('a-distinct-long-secret')

    def test_matcher_build_work_limit_is_checked_before_publication(self):
        vault = PrivacyVault()
        vault.MAX_MATCHER_WORK = 12
        vault.learn_value('bounded-build-secret!')
        with self.assertRaises(ValueError):
            vault.seal()
        with self.assertRaises(ValueError):
            vault.sanitize('bounded-build-secret!')

    def test_decoded_alias_budget_rejects_the_whole_request(self):
        vault = PrivacyVault()
        vault.MAX_ALIASES = 1
        vault.learn_document('One.md', 'password: "One\\u0031-Secret!"')
        with self.assertRaises(ValueError):
            vault.learn_document('Two.md', 'password: "Two\\u0032-Secret!"')
        with self.assertRaises(ValueError):
            vault.sanitize('Two2-Secret!')

    def test_repetitive_overlapping_matches_have_an_explicit_work_bound(self):
        vault = PrivacyVault()
        vault.MAX_MATCHES = 16
        for length in range(4, 10):
            vault.learn_value('a' * length)
        vault.seal()
        with self.assertRaises(ValueError):
            vault.sanitize('a' * 32)

    def test_history_match_budget_is_shared_across_selected_originals(self):
        vault = PrivacyVault()
        for value in ('alpha-secret!', 'beta-secret!', 'gamma-secret!'):
            vault.learn_value(value)
        vault.seal()
        vault.MAX_MATCHES = 2
        with self.assertRaises(ValueError):
            vault.secrets_for(vault._tokens)

    def test_history_preserves_all_overlapping_atoms_and_excludes_other_secrets(self):
        vault = PrivacyVault()
        selected = 'prefix-overlap-core-suffix'
        nested = {'overlap-core', 'core-suffix', 'overlap'}
        for value in nested | {selected, 'elsewhere-secret!'}:
            vault.learn_value(value)
        vault.seal()
        safe = vault.sanitize(selected)
        self.assertEqual(vault.restore(safe, vault.tokens_for(safe)), selected)
        self.assertEqual(vault.secrets_for(vault.tokens_for(safe)), nested | {selected})

    def test_matcher_agrees_with_boundary_and_overlapping_literal_semantics(self):
        import re
        from privacy import _known_pattern
        values = {'876', 'Qz', '별빛', 'long-secret!', 'secret!', 'abab', 'baba'}
        text = '876은 1876 _876 876８ Qz가 aQzb 별빛으로 long-secret! abababa'
        vault = PrivacyVault()
        for value in values:
            vault.learn_value(value)
        vault.seal()
        expected = {(match.start(), match.end(), value) for value in values
                    for match in re.finditer('(?=(' + _known_pattern(value) + '))', text)}
        # The lookahead's whole match is empty, so use the captured value length.
        expected = {(start, start + len(value), value) for start, _, value in expected}
        self.assertEqual(set(vault._matches(text)), expected)

    def test_history_scan_checks_cancellation_before_completing(self):
        active = False
        checkpoints = 0

        class Cancelled(Exception):
            pass

        def checkpoint():
            nonlocal checkpoints
            if active:
                checkpoints += 1
                if checkpoints == 3:
                    raise Cancelled()

        vault = PrivacyVault(checkpoint=checkpoint)
        selected = 'private-start!' + ('x' * 1024) + 'private-end!'
        vault.learn_value(selected)
        vault.learn_value('private-start!')
        vault.learn_value('private-end!')
        vault.seal()
        real_matches = vault._matches

        def observed_scan(text, budget=None):
            nonlocal active
            active = True
            yield from real_matches(text, budget)

        with patch.object(vault, '_matches', observed_scan), self.assertRaises(Cancelled):
            vault.secrets_for((vault._values[selected],))
        self.assertEqual(checkpoints, 3)

    def test_decoded_question_aliases_survive_selected_history_roundtrip(self):
        vault = PrivacyVault()
        safe = vault.sanitize('password: "Rose\\u0037-Secret!"')
        selected = vault.tokens_for(safe)
        retained = vault.secrets_for(selected)
        self.assertIn('Rose7-Secret!', retained)
        next_vault = PrivacyVault()
        for value in retained:
            next_vault.learn_value(value)
        self.assertNotIn('Rose7-Secret!', next_vault.sanitize('Repeat Rose7-Secret!'))

    def test_parser_bounds_reject_all_further_export_with_small_fixtures(self):
        import privacy_analysis
        cases = (
            ('MAX_INPUT_CHARS', 16, 'password: SyntheticBudget9!'),
            ('MAX_PARSER_WORK', 32, 'password: SyntheticBudget9!'),
            ('MAX_PARSER_TOKENS', 4, 'password: SyntheticBudget9!'),
            ('MAX_PARSER_DEPTH', 3, 'password: [[[["SyntheticBudget9!"]]]]'),
            ('MAX_SPANS', 1, 'password: FirstBudget9!\ntoken: SecondBudget9!\n'),
        )
        for limit, bound, source in cases:
            for channel in ('document', 'message'):
                with self.subTest(limit=limit, channel=channel):
                    vault = PrivacyVault()
                    with patch.object(privacy_analysis, limit, bound):
                        with self.assertRaises(ValueError) as failure:
                            if channel == 'document':
                                vault.learn_document('Garden.md', source)
                            else:
                                vault.sanitize(source)
                    self.assertNotIn('SyntheticBudget', str(failure.exception))
                    with self.assertRaises(ValueError):
                        vault.sanitize('public')
                    with self.assertRaises(ValueError):
                        vault.values_for(())

    def test_syntax_analysis_checks_cancellation_during_parser_tokens(self):
        from privacy_analysis import _SourceLoader
        calls = 0

        class Cancelled(Exception):
            pass

        original = _SourceLoader.get_token

        def cancelled_token(loader):
            nonlocal calls
            calls += 1
            if calls == 5:
                raise Cancelled()
            return original(loader)

        vault = PrivacyVault()
        with patch.object(_SourceLoader, 'get_token', cancelled_token):
            with self.assertRaises(Cancelled):
                vault.sanitize('password: "SyntheticCancel9!"\n')
        self.assertEqual(calls, 5)

    def test_invalid_parser_input_has_no_secret_bearing_error_message(self):
        vault = PrivacyVault()
        with self.assertRaises(ValueError) as failure:
            vault.sanitize('password: SyntheticInvalid9!\x00')
        self.assertNotIn('SyntheticInvalid9!', str(failure.exception))
        with self.assertRaises(ValueError):
            vault.restore('public')

    def test_markdown_inline_child_tokens_are_bounded_during_construction(self):
        import privacy_analysis
        text = ' '.join('**ordinary**' for _ in range(8))
        vault = PrivacyVault()
        with patch.object(privacy_analysis, 'MAX_PARSER_TOKENS', 12):
            with self.assertRaises(ValueError):
                vault.sanitize(text)
        with self.assertRaises(ValueError):
            vault.sanitize('public')

    def test_outbound_audit_preserves_original_boundaries_around_replacements(self):
        vault = PrivacyVault()
        vault.learn_value("'")
        original = "Password: 'SyntheticBoundary9'"  # gitleaks:allow -- fictional fixture
        vault.learn_document('Garden.md', original)
        vault.seal()
        safe = vault.sanitize_document('Garden.md', original)
        # The quotes were next to identifier characters in the source. Token
        # brackets must not turn those structural quotes into exposed values.
        self.assertTrue(vault.contains_known_value(safe))
        self.assertFalse(vault.contains_unmasked_value(safe))
        self.assertEqual(vault.restore(safe, vault.tokens_for(safe)), original)
        self.assertTrue(vault.contains_unmasked_value("Exposed: '"))
        self.assertTrue(vault.contains_unmasked_value(safe + ' SyntheticBoundary9'))
        vault.learn_value('PRIVATE')
        self.assertFalse(vault.contains_unmasked_value(safe))
        self.assertTrue(vault.contains_unmasked_value('[[PRIVATE_' + '0' * 32 + ']]'))

    def test_outbound_audit_does_not_join_text_across_opaque_values(self):
        vault = PrivacyVault()
        vault.learn_value('middle')
        vault.learn_value('SyntheticJoined9')
        safe = vault.sanitize('SyntheticmiddleJoined9')
        self.assertFalse(vault.contains_unmasked_value(safe))
        self.assertTrue(vault.contains_unmasked_value('SyntheticJoined9'))


if __name__ == '__main__':
    unittest.main()
