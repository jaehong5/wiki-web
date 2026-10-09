"""Local, request-scoped protection for private notebook values.

This module never writes files, logs, or performs network I/O. It identifies
structured/labeled private values and conservative private-note scopes. Arbitrary
unlabeled prose cannot be classified reliably; use ``<!-- private -->`` blocks
for such content. A vault must belong to exactly one request.
"""
from __future__ import annotations

import re
import secrets
from collections.abc import Iterable

TOKEN_RE = re.compile(r'\[\[PRIVATE_[0-9a-f]{32}\]\]')
_LITERAL_TOKEN_RE = re.compile(r'\[\[\s*PRIVATE_[^\r\n]*?(?:\]\]|$)', re.I | re.M)
HIDDEN = '[보호된 값]'

from privacy_analysis import Analysis, analyze, source_lines

def _known_pattern(value: str) -> str:
    escaped = re.escape(value)
    if value.isdecimal():
        # Korean particles can directly follow a CVC/PIN. Avoid matching an
        # unrelated longer number or an ASCII identifier containing the digits.
        return r'(?<![A-Za-z_\d])' + escaped + r'(?![A-Za-z_\d])'
    # One-character PIN fragments must not redact every occurrence in words.
    # Longer punctuation-bearing secrets are always exact substrings.
    if len(value) < 4:
        # A known Unicode secret also needs protection before Korean particles.
        # Known secrets take precedence over collisions in Korean prose.
        return r'(?<![A-Za-z_\d])' + escaped + r'(?![A-Za-z_\d])'
    return escaped


class PrivacyVault:
    # Bound both registry storage and the matcher, independently of notebook
    # size. Crossing a bound rejects the whole request; never mask only a prefix.
    MAX_VALUES = 4096
    MAX_VALUE_BYTES = 1024 * 1024
    MAX_MATCHER_STATES = 262144
    MAX_MATCHER_WORK = 4 * 1024 * 1024
    MAX_WRAPPER_DEPTH = 32
    MAX_MATCHES = 65536
    MAX_ALIASES = 8192

    def __init__(self, checkpoint=None):
        self._values: dict[str, str] = {}
        self._tokens: dict[str, str] = {}
        self._aliases: dict[str, set[str]] = {}
        self._alias_count = 0
        self._value_bytes = 0
        self._matcher = None
        self._dirty = False
        self._sealed = False
        self._failed = False
        self._checkpoint = checkpoint or (lambda: None)

    def _check(self) -> None:
        if self._failed:
            raise ValueError('개인정보 보호 처리 한도를 초과해 AI 전송을 중단했습니다.')
        self._checkpoint()

    def _fail(self) -> None:
        self._failed = True
        self._matcher = None
        raise ValueError('개인정보 보호 처리 한도를 초과해 AI 전송을 중단했습니다.')

    def _candidates(self, part: str) -> Iterable[str]:
        # Only the original, whitespace-trimmed value and innermost atom are
        # useful. Retaining every intermediate wrapper makes storage quadratic.
        yield part
        left, right = 0, len(part)
        while left < right and part[left].isspace():
            left += 1
        while right > left and part[right - 1].isspace():
            right -= 1
        if left or right != len(part):
            yield part[left:right]
        depth = 0
        while left < right:
            self._check()
            wrapper = next((mark for mark in ('**', '__', '"', "'", '`', '*', '_')
                            if right - left > len(mark) * 2
                            and part.startswith(mark, left, right)
                            and part.endswith(mark, left, right)), None)
            if wrapper is None:
                break
            depth += 1
            if depth > self.MAX_WRAPPER_DEPTH:
                self._fail()
            left += len(wrapper)
            right -= len(wrapper)
        if depth:
            yield part[left:right]

    def learn_value(self, value: str) -> None:
        self._check()
        if not isinstance(value, str) or not value or value.isspace():
            return
        # A token never spans newlines. This keeps evidence line numbers stable.
        for part in source_lines(value):
            self._check()
            for candidate in self._candidates(part):
                if candidate and not candidate.isspace() and candidate not in self._values:
                    if len(candidate) > self.MAX_VALUE_BYTES:
                        self._fail()
                    try:
                        size = len(candidate.encode('utf-8'))
                    except UnicodeError:
                        self._fail()
                    if (len(self._values) >= self.MAX_VALUES
                            or self._value_bytes + size > self.MAX_VALUE_BYTES):
                        self._fail()
                    token = '[[PRIVATE_' + secrets.token_hex(16) + ']]'
                    self._values[candidate] = token
                    self._tokens[token] = candidate
                    self._value_bytes += size
                    self._dirty = True

    def _analyze(self, text: str, path: str = '') -> Analysis:
        self._check()
        try:
            return analyze(text, path, self._check)
        except ValueError:
            self._failed = True
            self._matcher = None
            raise

    def _register(self, text: str, analysis: Analysis) -> None:
        for start, end in analysis.spans:
            self.learn_value(text[start:end])
        for atom in analysis.atoms:
            self._check()
            self.learn_value(atom.decoded)
            decoded_parts = {part for part in source_lines(atom.decoded) if part in self._values}
            # Each independently selected source line carries its decoded atom.
            # This also retains a folded multiline scalar after source deletion.
            for part in source_lines(text[atom.start:atom.end]):
                for raw in self._candidates(part):
                    if raw not in self._values:
                        continue
                    aliases = self._aliases.setdefault(raw, set())
                    for decoded in decoded_parts - {raw} - aliases:
                        if self._alias_count >= self.MAX_ALIASES:
                            self._fail()
                        aliases.add(decoded)
                        self._alias_count += 1

    def learn_document(self, path: str, text: str) -> None:
        self._check()
        if self._sealed:
            raise ValueError('Privacy document registration is already complete.')
        self._register(text, self._analyze(text, path))
        for candidate in (path, re.sub(r'\.md$', '', path, flags=re.I)):
            self._register(candidate, self._analyze(candidate))

    def seal(self) -> None:
        self._check()
        self._sealed = True
        self._compile()

    def _compile(self) -> None:
        self._check()
        if not self._dirty:
            return
        # Aho-Corasick uses one state per distinct prefix. Terminal links refer
        # to suffix matches instead of copying suffix-output lists at each node.
        from collections import deque
        self._matcher = None
        nodes, failure, terminal, output = [{}], [0], [None], [0]
        steps = 0
        for value in self._values:
            state = 0
            for char in value:
                steps += 1
                if not steps % 256:
                    self._check()
                    if steps > self.MAX_MATCHER_WORK:
                        self._fail()
                following = nodes[state].get(char)
                if following is None:
                    if len(nodes) >= self.MAX_MATCHER_STATES:
                        self._fail()
                    following = len(nodes)
                    nodes[state][char] = following
                    nodes.append({})
                    failure.append(0)
                    terminal.append(None)
                    output.append(0)
                state = following
            terminal[state] = value, value.isdecimal() or len(value) < 4
        queue = deque(nodes[0].values())
        while queue:
            state = queue.popleft()
            steps += 1
            if not steps % 256:
                self._check()
                if steps > self.MAX_MATCHER_WORK:
                    self._fail()
            for char, following in nodes[state].items():
                queue.append(following)
                fallback = failure[state]
                while fallback and char not in nodes[fallback]:
                    fallback = failure[fallback]
                    steps += 1
                    if not steps % 256:
                        self._check()
                        if steps > self.MAX_MATCHER_WORK:
                            self._fail()
                failure[following] = nodes[fallback].get(char, 0)
                suffix = failure[following]
                output[following] = suffix if terminal[suffix] else output[suffix]
        self._check()
        if steps > self.MAX_MATCHER_WORK:
            self._fail()
        self._matcher = nodes, failure, terminal, output
        self._dirty = False

    @staticmethod
    def _identifier_char(char: str) -> bool:
        return char == '_' or char.isdecimal() or 'A' <= char <= 'Z' or 'a' <= char <= 'z'

    def _matches(self, text: str, budget: list[int] | None = None, *,
                 left_identifier=False, right_identifier=False) -> Iterable[tuple[int, int, str]]:
        self._compile()
        if self._matcher is None:
            return
        nodes, failure, terminal, output = self._matcher
        state, steps = 0, 0
        budget = [0] if budget is None else budget
        for pos, char in enumerate(text):
            steps += 1
            if not steps % 256:
                self._check()
            while state and char not in nodes[state]:
                state = failure[state]
                steps += 1
                if not steps % 256:
                    self._check()
            state = nodes[state].get(char, 0)
            found = state if terminal[state] else output[state]
            while found:
                steps += 1
                budget[0] += 1
                if not steps % 256:
                    self._check()
                if budget[0] > self.MAX_MATCHES:
                    self._fail()
                value, boundary = terminal[found]
                start, end = pos + 1 - len(value), pos + 1
                if not boundary or not (
                        (self._identifier_char(text[start - 1]) if start else left_identifier)
                        or (self._identifier_char(text[end]) if end < len(text) else right_identifier)):
                    yield start, end, value
                found = output[found]
        self._check()

    def _sanitize(self, text: str, analysis: Analysis) -> str:
        self._check()
        self._register(text, analysis)
        intervals = [(start, end, False) for start, end in analysis.spans]
        intervals.extend((start, end, False) for start, end, _ in self._matches(text))
        intervals.extend((m.start(), m.end(), True) for m in _LITERAL_TOKEN_RE.finditer(text))
        if len(intervals) > self.MAX_MATCHES:
            self._fail()
        if not intervals:
            return text
        # Overlapping candidates are protected as one value; never recursively
        # run regex replacement over inserted placeholders.
        merged: list[tuple[int, int, bool]] = []
        for index, (start, end, literal) in enumerate(sorted(intervals)):
            if not index % 256:
                self._check()
            if start >= end:
                continue
            if merged and start < merged[-1][1]:
                old_start, old_end, old_literal = merged[-1]
                merged[-1] = (old_start, max(end, old_end), old_literal or literal)
            else:
                merged.append((start, end, literal))
        output = []
        cursor = 0
        for start, end, literal in merged:
            self._check()
            output.append(text[cursor:start])
            raw = text[start:end]
            if literal:
                # Preserve line separators even for a malicious malformed marker.
                output.append(HIDDEN)
            else:
                self.learn_value(raw)
                # Generic matches are single-line; explicit blocks are split by
                # their line parser. Assert this invariant rather than leak.
                if raw not in self._values:
                    raise ValueError('Private value cannot span evidence lines.')
                output.append(self._values[raw])
            cursor = end
        output.append(text[cursor:])
        return ''.join(output)

    def sanitize(self, text: str) -> str:
        self._check()
        return self._sanitize(text, self._analyze(text))

    def sanitize_document(self, path: str, text: str) -> str:
        self._check()
        return self._sanitize(text, self._analyze(text, path))

    def tokens_for(self, text: str) -> set[str]:
        self._check()
        return {m.group() for m in TOKEN_RE.finditer(text) if m.group() in self._tokens}

    def values_for(self, tokens: Iterable[str]) -> dict[str, str]:
        self._check()
        result = {}
        for token in tokens:
            self._check()
            if token in self._tokens:
                result[token] = self._tokens[token]
        return result

    def secrets_for(self, tokens: Iterable[str]) -> set[str]:
        """Selected originals and their learned atoms for local history only.

        Compound token values can contain separately learned passwords or card
        metadata. Retaining those atoms prevents a source deletion from making
        a later partial repetition visible to the remote model.
        """
        self._check()
        retained = set(self.values_for(tokens).values())
        pending, scanned = list(retained), set()
        budget = [0]
        while pending:
            self._check()
            original = pending.pop()
            if original in scanned:
                continue
            scanned.add(original)
            # Scan each selected original once for every overlapping atom,
            # rather than searching it separately for every known value.
            contained = {value for _, _, value in self._matches(original, budget)}
            retained.update(contained)
            for raw in contained | {original}:
                self._check()
                for decoded in self._aliases.get(raw, ()):
                    retained.add(decoded)
                    if decoded not in scanned:
                        pending.append(decoded)
        self._check()
        return retained

    def filter_tokens(self, text: str, allowed_tokens: Iterable[str]) -> str:
        self._check()
        allowed = set(allowed_tokens) & self._tokens.keys()
        return _LITERAL_TOKEN_RE.sub(lambda m: m.group() if m.group() in allowed else HIDDEN, text)

    def restore(self, text: str, allowed_tokens: Iterable[str] = ()) -> str:
        self._check()
        allowed = set(allowed_tokens) & self._tokens.keys()
        return _LITERAL_TOKEN_RE.sub(
            lambda m: self._tokens[m.group()] if m.group() in allowed else HIDDEN, text)

    def contains_known_value(self, text: str) -> bool:
        self._check()
        return next(iter(self._matches(text)), None) is not None

    def contains_unmasked_value(self, text: str) -> bool:
        """Audit literal content using the same boundaries as its source.

        Known replacement tokens are opaque regions, not empty strings. Joining
        their neighbors or treating brackets as new word boundaries can invent
        a match that did not exist before masking. Keep the original adjacent
        character classes, reset matching across each token, and share the match
        budget across chunks. Unknown/forged tokens receive no exemption.
        """
        self._check()
        cursor, left, budget = 0, False, [0]
        for token in TOKEN_RE.finditer(text):
            self._check()
            original = self._tokens.get(token.group())
            if original is None:
                continue
            right = self._identifier_char(original[0])
            if next(self._matches(text[cursor:token.start()], budget,
                                  left_identifier=left, right_identifier=right), None) is not None:
                return True
            cursor, left = token.end(), self._identifier_char(original[-1])
        return next(self._matches(text[cursor:], budget, left_identifier=left), None) is not None
