"""One bounded, source-mapped analysis for notes, questions and local history.

Policy classifies values; standard YAML/Markdown parsers interpret syntax. No
YAML objects are constructed and aliases are never expanded. All public spans
refer to the original input, and replacement spans never cross a source line.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from bisect import bisect_right
import re

import yaml
from markdown_it import MarkdownIt
from markdown_it.rules_block import StateBlock
from markdown_it.rules_inline import StateInline


# Numeric identifiers are deliberately conservative: notebook utility is less
# important than leaking a bank/card/resident identifier to a remote model.
_STRUCTURED = [
    re.compile(r'(?<!\d)\d{6}\s*-\s*[1-8]\d{6}(?!\d)'),
    re.compile(r'(?<!\w)(?:\d[ -]?){13,19}(?!\w)'),
    re.compile(r'(?<!\w)[MSR][0-9][0-9A-Z]{7}(?!\w)', re.I),
    re.compile(r'(?<!\w)P\d{12}(?!\w)', re.I),
    re.compile(r'(?<!\w)(?:0x)?[a-f0-9]{32,}(?!\w)', re.I),
    re.compile(r'(?<!\w)(?:sk[-_]|gh[pousr]_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]{8,}(?!\w)'),
    re.compile(r'(?<!\w)eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}(?!\w)'),
    re.compile(r'(?<=://)[^\s/@:]+:[^\s/@]+(?=@)'),
    re.compile(r'(?<!\w)0\.0\.\d{4,}(?!\w)'),
]
_NUMERIC_IDENTIFIER = re.compile(r'(?<!\w)\d{2,6}(?:-\d{2,6}){1,4}(?!\w)')
_SECRET_WORD = r'(?:password|passwd|passphrase|pwd|pw|secret|token|api[ _-]?key|access[ _-]?key|private[ _-]?key|license(?:[ _-]?key)?|licen[cs]e|serial[ _-]?(?:key|number)|mnemonic|seed(?:[ _-]?phrase)?|recovery(?:[ _-]?(?:phrase|key|codes?))?|비밀번호|패스워드|암호|인증[ _-]?(?:키|코드|번호)|복구[ _-]?(?:키|구문|코드)|시드(?:[ _-]?구문)?|라이[선센]스(?:[ _-]?키)?|보안[ _-]?코드|cvc|cvv)'
_SECRET_LABEL = re.compile(r'(?<![A-Za-z])' + _SECRET_WORD + r'(?:s)?(?![A-Za-z])', re.I)
_FIELD_NAME = r'(?:[A-Z_][A-Z0-9_]*_)?' + _SECRET_WORD + r'(?:s)?'
# Balanced wrappers belong to the label, not its value. Keep a named payload
# group so variable-length inline-code delimiters do not affect offsets.
_DECORATED_FIELD_NAME = (r'(?:' + '|'.join(re.escape(mark) + _FIELD_NAME + re.escape(mark)
    for mark in ('***', '___', '**', '__', '*', '_', '"', "'"))
    + r'|(?P<label_ticks>`+)' + _FIELD_NAME + r'(?P=label_ticks)|' + _FIELD_NAME + r')')
# A --flag belongs to the shell adapter, whose word boundary/escaping is
# authoritative. Reinterpreting it as a YAML field can consume later flags.
_FIELD = re.compile(r'(?i)(?<!--)(?<![A-Za-z])' + _DECORATED_FIELD_NAME
                    + r'\s*(?::|=|\s+-\s+)\s*(?=(?P<value>.+))')
_SPACE_FIELD = re.compile(r'(?i)^\s*(?:#{1,6}\s+|[-*+]\s+)?(?:[A-Za-z가-힣][A-Za-z가-힣 _/-]{0,40}?\s+)?'
    + _DECORATED_FIELD_NAME + r'[ \t]+(?=(?P<value>[^:=\r\n].*))')
_BEARER = re.compile(r'(?i)\b(?:authorization\s*:\s*)?(?:bearer|basic)\s+([^\s`"\'<>]+)')
_PREFIX = re.compile(r'^(?:\s*(?:>\s*)?)(?:(?:[-*+] |\d+[.)] ))?')
_PRIVATE_PATH = re.compile(r'(?:^|[/ _.-])(?:family|wallet|passwords?|credentials?|personal[ _.-]*(?:inform(?:ation)?|info)|accounts?|가족|개인정보|지갑|비밀번호|인증정보)(?:$|[/ _.-])', re.I)
_PRIVATE_SECTION = re.compile(r'(?i)(?:\b(?:id\s*[/&]\s*pw|id\s*card|credentials?|accounts?|credit|debit|debut|wallet|metamask|walla|family|personal\s+inform(?:ation)?)\b|주민등록|여권|신분증|카드\s*(?:정보|번호)|계좌|계정\s*(?:정보|목록)|가족\s*정보)')
_SAFE_FIELD = re.compile(r'(?i)^(?:[^\d`~!@#$%^&*=+<>]{0,35}(?:은행|증권|카드)|(?:skypass|asiana\s*club|등록기준지|이름|name|username|user|id|login|email|e-mail|주소|address|account|계좌|번호|생년월일|birthday|birth|passport|여권|주민등록번호|면허|유효기간|expiry|expiration|만료|service|서비스|구글|google|아이핀|농협투자증권))\b', re.I)
_TABLE_LABEL = re.compile(r'(?i)(?:service|서비스|이름|name|id|pw|password|username|계정|비밀번호|token|key|값|value|번호|만료|cvc|cvv|카드|은행|설명|description|expiry|expiration)')
_TABLE_PUBLIC = re.compile(r'(?i)(?:service|서비스|이름|name|카드|은행|설명|description)')
_PROPERTY_FIELD = re.compile(r'(?i)(?:항목|field|property)')
_PROPERTY_VALUE = re.compile(r'(?i)(?:내용|정보|값|value)')
_PROPERTY_ACCOUNT_LABEL = re.compile(r'(?i)(?:id|user(?:name)?|login|e-?mail|account(?:[ _-]*(?:number|no\.?|id))?|passport|계정|아이디|로그인|이메일|계좌(?:\s*번호)?|여권(?:\s*번호)?|주민등록번호|카드\s*번호)')
_PRIVATE_MARKER = re.compile(r'<!--\s*(/?)private\s*-->', re.I)
_FENCE = re.compile(r'[ \t]*(?P<delimiter>`{3,}|~{3,})(?P<info>[^\r\n]*)')
_SAFE_FENCE_LANGUAGES = frozenset(('text plaintext txt json jsonc yaml yml toml ini xml html css '
    'javascript js typescript ts jsx tsx python py bash sh shell zsh powershell ps1 '
    'sql markdown md c cpp csharp cs java go rust ruby php swift kotlin diff').split())


def _trim_span(text: str, start: int, end: int) -> tuple[int, int] | None:
    # Keep Markdown wrappers in evidence so labels, tables and code remain usable.
    while start < end and text[start] in ' \t`':
        start += 1
    while end > start and text[end - 1] in ' \t`':
        end -= 1
    return (start, end) if start < end else None


def _markdown_value_span(text: str, start: int, end: int) -> tuple[int, int] | None:
    """Find a leading balanced code/emphasis value before optional prose."""
    if start >= end:
        return None
    if text[start] == '`':
        wrapper = re.match(r'`+', text[start:end]).group()
        closing = re.compile(r'(?<!`)' + re.escape(wrapper) + r'(?!`)')
    else:
        wrapper = next((mark for mark in ('***', '___', '**', '__', '*', '_')
                        if text.startswith(mark, start, end)), None)
        if wrapper is None:
            return None
        closing = re.compile(re.escape(wrapper))
    inner_start = start + len(wrapper)
    for match in closing.finditer(text, inner_start, end):
        if wrapper[0] != '`':
            cursor = match.start()
            while cursor > inner_start and text[cursor - 1] == '\\':
                cursor -= 1
            if (match.start() - cursor) % 2:
                continue
        if match.start() <= inner_start:
            continue
        return inner_start, match.start()
    return None


def _field_value_span(text: str, start: int, end: int, budget=None) -> tuple[int, int] | None:
    if budget:
        budget.spend(end - start)
    while start < end and text[start] in ' \t':
        start += 1
    wrapped = _markdown_value_span(text, start, end)
    if wrapped:
        return wrapped
    if start < end and text[start] in ('"', "'", '&', '!', '|', '>'):
        try:
            for token in yaml.scan(text[start:end], Loader=yaml.SafeLoader):
                if budget:
                    budget.spend(tokens=1)
                if isinstance(token, yaml.tokens.ScalarToken):
                    if token.style in ('|', '>'):
                        return None
                    a, b = token.start_mark.index, token.end_mark.index
                    if token.style in ('"', "'"):
                        a, b = a + 1, b - 1
                    return _trim_span(text, start + a, start + b)
        except yaml.YAMLError:
            # This is a provisional line span, not a successful decode. The
            # complete structured pass may resolve a multiline value; the
            # final scalar pass must reject any quoted span left unresolved.
            return _trim_span(text, start, end)
    # YAML plain scalar comments start only after whitespace; an embedded '#'
    # is part of the secret. Learning the scalar separately also protects a
    # subsequent question that omits the explanatory comment.
    comment = re.search(r'[ \t]+#', text[start:end])
    if comment:
        end = start + comment.start()
    return _trim_span(text, start, end)


def _value_spans(text: str, start: int, end: int, budget=None) -> list[tuple[int, int]]:
    """Keep the entire private payload and learn a leading wrapped atom too."""
    return list(dict.fromkeys(span for span in
        (_trim_span(text, start, end), _field_value_span(text, start, end, budget)) if span))


def _fence_spans(line: str, budget=None) -> list[tuple[int, int]] | None:
    fence = _FENCE.fullmatch(line)
    if fence is None:
        return None
    info = fence.group('info').strip()
    if not info or info.casefold() in _SAFE_FENCE_LANGUAGES:
        return []
    # Only a known language is public structure. Arbitrary fence info can be a
    # secret, while retaining its delimiter keeps the sanitized Markdown valid.
    start, end = fence.span('info')
    return (_value_spans(line, start, end, budget)
            + [(start + a, start + b) for a, b in _inline_spans(line[start:end], budget)])


def _inline_spans(text: str, budget=None) -> list[tuple[int, int]]:
    if budget:
        budget.spend(len(text))
    spans = []
    spans.extend(m.span() for pattern in _STRUCTURED for m in pattern.finditer(text))
    for match in _NUMERIC_IDENTIFIER.finditer(text):
        value = match.group()
        # Ordinary ISO dates are not identifiers. Explicit blocks, credential
        # labels, private scopes and learned values still protect these strings.
        ordinary = False
        if re.fullmatch(r'\d{4}-\d{2}(?:-\d{2})?', value):
            try:
                date.fromisoformat(value if len(value) == 10 else value + '-01')
                ordinary = True
            except ValueError:
                pass
        # A numeric range needs an explicit prose cue; bare hyphenated account
        # numbers remain protected even when they happen to be ascending.
        prefix = text[max(0, match.start() - 40):match.start()]
        if (re.fullmatch(r'\d{2,6}-\d{2,6}', value)
                and re.search(r'(?:\b(?:range|years?|amounts?)|범위|연도|기간|금액)\s*[:=]?\s*$', prefix, re.I)):
            ordinary = True
        if not ordinary:
            spans.append(match.span())
    for line_match in re.finditer(r'[^\r\n]+', text):
        line, base = line_match.group(), line_match.start()
        for field_pattern in (_FIELD, _SPACE_FIELD):
            for m in field_pattern.finditer(_label_view(line, budget)):
                if m.group('value').lstrip().startswith((':', '=')):
                    continue
                span = _field_value_span(line, *m.span('value'), budget)
                if span:
                    spans.append((base + span[0], base + span[1]))
        for pattern in (_BEARER,):
            for m in pattern.finditer(line):
                spans.append((base + m.start(1), base + m.end(1)))
    return spans


def _table_cells(line: str) -> list[tuple[int, int]]:
    """Offsets of Markdown cells, including empty and escaped-pipe cells."""
    start, end = len(line) - len(line.lstrip()), len(line.rstrip())
    if start < end and line[start] == '|':
        start += 1
    if end > start and line[end - 1] == '|' and (end < 2 or line[end - 2] != '\\'):
        end -= 1
    cells = []
    cursor = start
    escaped = False
    for i in range(start, end):
        char = line[i]
        if char == '|' and not escaped:
            cells.append((cursor, i))
            cursor = i + 1
        escaped = char == '\\' and not escaped
    cells.append((cursor, end))
    return cells


def _table_header_public_columns(values: list[str]) -> set[int] | None:
    values = [_normalized_label(value) for value in values]
    if _property_table_header(values):
        return {0}
    sensitive = {i for i, value in enumerate(values)
                 if _SECRET_LABEL.fullmatch(value) or _PROPERTY_ACCOUNT_LABEL.fullmatch(value)
                 or (_TABLE_LABEL.fullmatch(value) and not _TABLE_PUBLIC.fullmatch(value))}
    if sensitive or (values and all(_TABLE_LABEL.fullmatch(value) for value in values)):
        return set(range(len(values))) - sensitive
    return None


def _property_table_header(values: list[str]) -> bool:
    values = [_normalized_label(value) for value in values]
    return bool(len(values) == 2 and _PROPERTY_FIELD.fullmatch(values[0])
                and _PROPERTY_VALUE.fullmatch(values[1]))


def _normalized_label(value: str) -> str:
    value = value.strip()
    # Peel a single balanced wrapper. Labels have no reason to need arbitrarily
    # nested formatting; avoiding recursive peeling also bounds parser work.
    span = _markdown_value_span(value, 0, len(value))
    if span and span[0] == len(value) - span[1]:
        return value[span[0]:span[1]].strip()
    return value


def _payload_spans(line: str, *, strict: bool = False, table_public_columns: set[int] | None = None, table_column_count: int = 0, table_header: bool = False, budget=None) -> list[tuple[int, int]]:
    """Preserve safe field labels while hiding values in a private section."""
    prefix = _PREFIX.match(line).end()
    tail = line[prefix:]
    if not tail.strip():
        return []
    fence = _fence_spans(line, budget)
    if fence is not None:
        return fence
    if strict:
        return _value_spans(line, prefix, len(line), budget)
    # Table separator rows have no data. Table field labels remain useful.
    if '|' in tail:
        if re.fullmatch(r'[\s|:-]+', tail):
            return []
        cells = _table_cells(tail)
        values = [tail[start:end].strip() for start, end in cells]
        if table_header:
            return []
        spans = []
        for i, (start, end) in enumerate(cells):
            if len(cells) == table_column_count and table_public_columns is not None and i in table_public_columns:
                continue
            spans.extend(_value_spans(line, prefix + start, prefix + end, budget))
        return spans
    # Service / login / password / recovery value.
    if re.search(r'\s/\s', tail):
        parts = list(re.finditer(r'[^/]+', tail))
        spans = []
        for i, part in enumerate(parts):
            if i == 0:
                first = part.group()
                stripped = first.lstrip()
                service = _SAFE_FIELD.match(stripped)
                if service and stripped[service.end():].strip():
                    start = prefix + part.start() + len(first) - len(stripped) + service.end()
                    spans.extend(_value_spans(line, start, prefix + part.end(), budget))
                    continue
                if re.fullmatch(r'[A-Za-z가-힣 ]{1,40}', first.strip()):
                    continue
            spans.extend(_value_spans(line, prefix + part.start(), prefix + part.end(), budget))
        return spans
    # Human-readable key/value labels are retained, but not arbitrary symbolic
    # assignment keys which can contain actual credentials.
    label = re.match(r'([A-Za-z가-힣 /_-]{1,60})(?::|=)\s*', tail)
    if label and (_SECRET_LABEL.search(label.group(1)) or _SAFE_FIELD.fullmatch(label.group(1).strip())):
        return _value_spans(line, prefix + label.end(), len(line), budget)
    # Structured card metadata protects expiry dates and short CVCs too.
    generic = _inline_spans(line, budget)
    if generic:
        # Comma-separated card metadata includes short expiry dates and CVCs
        # which are not distinctive enough for global structured detection.
        if ',' in tail:
            spans = []
            for part in re.finditer(r'[^,]+', tail):
                spans.extend(_value_spans(line, prefix + part.start(), prefix + part.end(), budget))
            return spans
    # Clear field labels such as "SKYPASS 1234" keep their lookup context.
    safe = _SAFE_FIELD.match(tail)
    if safe and safe.end() < len(tail):
        spans = _value_spans(line, prefix + safe.end(), len(line), budget)
        if spans:
            return spans
    return _value_spans(line, prefix, len(line), budget)


def _policy_spans(path: str, text: str, headings: dict, fences: dict, containers: dict, budget) -> list[tuple[int, int]]:
    spans = []
    private_path = bool(_PRIVATE_PATH.search(path) or re.fullmatch(r'personal(?:\.md)?', path.rsplit('/', 1)[-1], re.I))
    scopes: list[tuple[int, bool, tuple]] = []
    private_depth = 0
    pem_block = False
    flat_pending = False
    flat_container = ()
    flat_fence = False
    table_public_columns = None
    table_private = False
    table_property = False
    table_column_count = 0
    atom_public_columns = None
    atom_column_count = 0
    offset = 0
    lines = source_lines(text, keepends=True)
    for index, raw in enumerate(lines):
        budget.spend(len(raw))
        line = raw.rstrip('\r\n')
        container = containers.get(index, ())
        scopes = [scope for scope in scopes if container[:len(scope[2])] == scope[2]]
        if flat_pending and container[:len(flat_container)] != flat_container:
            flat_pending = flat_fence = False
        # Keep table metadata independently of the public view: a whole row
        # (or whole table) can be explicit while its individual credentials
        # still need registration for questions that repeat just one value.
        atom_line = _PRIVATE_MARKER.sub(lambda m: ' ' * len(m.group()), line)
        fence_role = fences.get(index)
        in_fence = fence_role in ('body', 'close')
        fence_open, fence_close = fence_role == 'open', fence_role == 'close'
        next_line = (_PRIVATE_MARKER.sub('', lines[index + 1]).strip()
                     if index + 1 < len(lines) else '')
        separator_cells = [next_line[a:b].strip() for a, b in _table_cells(next_line)]
        table_header = False
        if '|' in atom_line and not (in_fence or fence_open):
            cells = [atom_line[start:end].strip() for start, end in _table_cells(atom_line)]
            # Only a header followed by a Markdown separator establishes
            # columns. A data cell whose value is "Password" is still data.
            table_header = (len(cells) == len(separator_cells)
                            and all(re.fullmatch(r':?-+:?', cell) for cell in separator_cells))
            header_columns = _table_header_public_columns(cells) if table_header else None
            table_header = header_columns is not None
            if header_columns is not None:
                atom_column_count = len(cells)
                atom_public_columns = header_columns
        elif atom_line.strip():
            atom_public_columns, atom_column_count = None, 0
        # Explicit blocks are strict, including labels and headings. Support
        # inline blocks and protect to EOF when a closing marker is missing.
        cursor = 0
        excluded = []
        while cursor < len(line):
            marker = _PRIVATE_MARKER.search(line, cursor)
            if private_depth:
                end = marker.start() if marker else len(line)
                fence = _fence_spans(line[cursor:end], budget)
                if fence is not None:
                    spans.extend((offset + cursor + a, offset + cursor + b) for a, b in fence)
                    excluded.append((cursor, end))
                else:
                    # Keep learning recognizable inner atoms as well as the
                    # full protected payload, so a password repeated alone in
                    # a later question is still hidden locally.
                    spans.extend((offset + cursor + a, offset + cursor + b)
                                 for a, b in _inline_spans(line[cursor:end], budget))
                    spans.extend((offset + cursor + a, offset + cursor + b)
                                 for a, b in _payload_spans(line[cursor:end],
                                     table_public_columns=atom_public_columns,
                                     table_column_count=atom_column_count,
                                     table_header=table_header, budget=budget))
                    span = _trim_span(line, cursor, end)
                    if span:
                        spans.append((offset + span[0], offset + span[1]))
                    excluded.append((cursor, end))
            if marker is None:
                break
            excluded.append(marker.span())
            private_depth = max(0, private_depth - 1) if marker.group(1) else private_depth + 1
            cursor = marker.end()

        # Match ordinary fields in the remaining text without letting a
        # greedy password field swallow the comment delimiters. Contextual
        # rules still run on sibling table cells and values on the same line.
        public = []
        cursor = 0
        for start, end in sorted(excluded):
            if cursor < start:
                public.append((cursor, start))
            cursor = max(cursor, end)
        if cursor < len(line):
            public.append((cursor, len(line)))
        for start, end in public:
            fragment = line[start:end]
            # A field immediately before an explicit value has no payload of
            # its own. Otherwise the whitespace detector can backtrack from
            # "License key:" and incorrectly register "key:" as a secret.
            label = re.sub(r'[`*]', '', fragment).strip()
            if (excluded and _SECRET_LABEL.search(label)
                    and re.fullmatch(r'(?:[-+] |\d+[.)] |#{1,6} )?[\w /()\-]{1,90}\s*[:=]', label)):
                continue
            spans.extend((offset + start + a, offset + start + b)
                         for a, b in _inline_spans(fragment, budget))
        context = list(line)
        for start, end in excluded:
            context[start:end] = ' ' * (end - start)
        line = ''.join(context)

        def add_context_span(start: int, end: int) -> None:
            # Contextual candidates can cross an explicit block. Intersect
            # with public intervals so restored values never contain tags.
            for a, b in public:
                span = _trim_span(line, max(start, a), min(end, b))
                if span:
                    spans.append((offset + span[0], offset + span[1]))

        if '-----BEGIN ' in line and 'PRIVATE KEY-----' in line:
            pem_block = True
        if pem_block:
            span = _trim_span(line, 0, len(line))
            if span:
                add_context_span(*span)
            if '-----END ' in line and 'PRIVATE KEY-----' in line:
                pem_block = False
            offset += len(raw)
            continue
        if '|' in line and not (in_fence or fence_open):
            cell_spans = _table_cells(line)
            cells = [line[start:end].strip() for start, end in cell_spans]
            header_columns = _table_header_public_columns(cells) if table_header else None
            if header_columns is not None:
                table_column_count = len(cells)
                table_public_columns = header_columns
                table_property = _property_table_header(cells)
                # A generic property/value table needs a private filename,
                # section or explicit block. Its shape alone is not a secret.
                # Credential-column tables still establish their own scope.
                table_private = (len(table_public_columns) != len(cells)
                                 and not table_property)
            elif table_property and len(cells) == 2:
                # Property tables may mix ordinary metadata with explicit
                # credential labels. Protect that row without hiding peers.
                label = _normalized_label(cells[0]).rstrip(':').strip()
                if _SECRET_LABEL.search(label) or _PROPERTY_ACCOUNT_LABEL.fullmatch(label):
                    span = _field_value_span(line, *cell_spans[1], budget)
                    if span:
                        add_context_span(*span)
        else:
            table_public_columns = None
            table_private = False
            table_property = False
            table_column_count = 0
        heading = headings.get(index) if not (in_fence or fence_open) else None
        if heading:
            level, title = heading
            scopes = [scope for scope in scopes if scope[2] != container or scope[0] < level]
            protected = bool(_SECRET_LABEL.search(title) or _PRIVATE_SECTION.search(title))
            scopes.append((level, protected, container))
            flat_pending = flat_fence = False
            # Labels are useful; any inline secret was already detected above.
        elif headings.get(index) is not False:
            protected = private_path or table_private or any(private for _, private, _ in scopes)
            clean = _normalized_label(re.sub(r'^[\s>]+|[:]+$', '', line).strip())
            # Plain credential labels, e.g. "Typora License" followed by a key.
            label_only = bool(not (in_fence or fence_open) and clean and len(clean) < 90 and _SECRET_LABEL.search(clean)
                              and not re.search(r'[:=|]', clean)
                              and re.fullmatch(r'[\w /()\-]+', clean)
                              and not _inline_spans(clean, budget))
            if label_only and not protected and not flat_pending:
                flat_pending = True
                flat_container = container
            else:
                if flat_pending and (fence_open or fence_close):
                    for start, end in _fence_spans(line, budget) or ():
                        add_context_span(start, end)
                    if fence_close:
                        flat_pending = flat_fence = False
                    else:
                        flat_fence = True
                elif (protected or flat_pending) and line.strip():
                    for start, end in _payload_spans(line, strict=flat_pending, table_public_columns=table_public_columns, table_column_count=table_column_count, table_header=table_header, budget=budget):
                        add_context_span(start, end)
                    if flat_pending and not flat_fence:
                        flat_pending = False
        offset += len(raw)
    return spans



# Bounds are independent of notebook discovery and the vault's matcher bounds.
# Parsers see each complete source at most once plus disjoint Markdown blocks.
MAX_INPUT_CHARS = 1024 * 1024
MAX_PARSER_WORK = 4 * 1024 * 1024
MAX_PARSER_TOKENS = 65536
MAX_PARSER_DEPTH = 64
MAX_SPANS = 65536


def source_lines(text: str, keepends: bool = False) -> list[str]:
    """CommonMark physical lines: CRLF, LF and CR, without Unicode separators."""
    lines = [line for line in re.findall(r'[^\r\n]*(?:\r\n|\r|\n|$)', text) if line]
    return lines if keepends else [line.rstrip('\r\n') for line in lines]


def _explicit_ranges(text):
    ranges, depth, cursor = [], 0, 0
    for marker in _PRIVATE_MARKER.finditer(text):
        if depth and cursor < marker.start():
            ranges.append((cursor, marker.start()))
        depth = max(0, depth - 1) if marker.group(1) else depth + 1
        if depth > MAX_PARSER_DEPTH:
            raise ValueError('개인정보 문법 중첩 한도를 초과해 AI 전송을 중단했습니다.')
        cursor = marker.end()
    if depth and cursor < len(text):
        ranges.append((cursor, len(text)))
    return ranges


def _inside_explicit(ranges, start, end):
    scope = bisect_right(ranges, (start, float('inf'))) - 1
    return scope >= 0 and ranges[scope][1] >= end


@dataclass(frozen=True)
class ScalarAtom:
    start: int
    end: int
    decoded: str
    syntax_start: int | None = None
    syntax_end: int | None = None


class PrivateSyntaxError(ValueError):
    """An unresolved protected value invalidates the entire privacy context.

    Parser adapters must never convert this into a raw/partially decoded atom.
    PrivacyVault catches it at the common analysis boundary, poisons the vault,
    and rejects later masking, restoration and prepared model input as well.
    """
    def __init__(self):
        super().__init__('민감한 문법을 안전하게 해석할 수 없어 AI 전송을 중단했습니다.')


@dataclass(frozen=True)
class Analysis:
    spans: tuple[tuple[int, int], ...]
    atoms: tuple[ScalarAtom, ...]


def _label_view(text: str, budget=None) -> str:
    """Blank balanced Markdown label delimiters without moving source offsets."""
    view = list(text)
    for match in re.finditer(r'(?<!\w)(?:`+|\*{1,3}|_{1,3})', text):
        if budget:
            budget.spend(len(text) - match.start())
        span = _markdown_value_span(text, match.start(), len(text))
        if not span:
            continue
        start, end = span
        label = text[start:end].strip().rstrip(':=').strip()
        if _SECRET_LABEL.search(label) and re.fullmatch(r'[\w /()\-]+', label):
            width = start - match.start()
            view[match.start():start] = ' ' * width
            view[end:end + width] = ' ' * width
    return ''.join(view)


class _Budget:
    def __init__(self, checkpoint):
        self.checkpoint = checkpoint
        self.work = self.tokens = 0

    def spend(self, work=0, tokens=0):
        self.checkpoint()
        self.work += work
        self.tokens += tokens
        if self.work > MAX_PARSER_WORK or self.tokens > MAX_PARSER_TOKENS:
            raise ValueError('개인정보 문법 분석 한도를 초과해 AI 전송을 중단했습니다.')


def _line_spans(text: str, start: int, end: int):
    """Preserve every original line terminator, including CRLF and Unicode."""
    cursor = start
    for raw in source_lines(text[start:end], keepends=True):
        content = raw.rstrip('\r\n')
        span = _trim_span(text, cursor, cursor + len(content))
        if span:
            left, right = span
            for marker in _PRIVATE_MARKER.finditer(text, left, right):
                if left < marker.start():
                    yield left, marker.start()
                left = marker.end()
            if left < right:
                yield left, right
        cursor += len(raw)


def _markdown_structure(text, budget):
    budget.spend(len(text))
    parser = MarkdownIt('commonmark', {'maxNesting': MAX_PARSER_DEPTH + 1})

    class BoundedBlockState(StateBlock):
        def push(self, *args):
            budget.spend(tokens=1)
            return super().push(*args)

    class BoundedInlineState(StateInline):
        def push(self, *args):
            budget.spend(tokens=1)
            return super().push(*args)

        def pushPending(self):
            budget.spend(tokens=1)
            return super().pushPending()

    def parse_block(src, md, env, output):
        if not src:
            return None
        state = BoundedBlockState(src, md, env, output)
        parser.block.tokenize(state, state.line, state.lineMax)
        return state.tokens

    def parse_inline(src, md, env, output):
        state = BoundedInlineState(src, md, env, output)
        parser.inline.tokenize(state)
        for rule in parser.inline.ruler2.getRules(''):
            budget.spend(1)
            rule(state)
        return state.tokens

    # Replace only these request-local parser instances. The standard grammar
    # still tokenizes; bounded states account for children as they are created.
    parser.block.parse = parse_block
    parser.inline.parse = parse_inline

    def checkpoint_rule(*args):
        budget.spend(1)
        return False

    parser.block.ruler.before('code', 'privacy_budget', checkpoint_rule)
    parser.inline.ruler.before('text', 'privacy_budget', checkpoint_rule)
    tokens = parser.parse(text)
    lines = source_lines(text, keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    headings, regions, fences, containers = {}, set(), {}, {}
    view = list(text)
    for index, token in enumerate(tokens):
        if token.type in ('blockquote_open', 'list_item_open'):
            for line in range(*token.map):
                budget.spend(1)
                containers.setdefault(line, []).append(index)
        if token.level >= MAX_PARSER_DEPTH:
            raise ValueError('개인정보 문법 중첩 한도를 초과해 AI 전송을 중단했습니다.')
        if token.type == 'heading_open':
            first, last = token.map
            title = tokens[index + 1].content
            headings[first] = (int(token.tag[1:]), title)
            for line in range(first + 1, last):
                headings[line] = False
        elif token.type == 'blockquote_open':
            for line in range(*token.map):
                prefix = re.match(r'(?: {0,3}>[ \t]?)+', lines[line])
                if prefix:
                    start = offsets[line]
                    view[start:start + prefix.end()] = ' ' * prefix.end()
        if token.type in ('inline', 'fence', 'code_block') and token.map:
            first, last = token.map
            if token.type == 'fence':
                for line in range(first, last):
                    fences[line] = 'open' if line == first else 'body'
                first += 1
                closing = _FENCE.fullmatch(lines[last - 1].rstrip('\r\n')) if last > first else None
                if (closing and not closing.group('info').strip()
                        and closing.group('delimiter')[0] == token.markup[0]
                        and len(closing.group('delimiter')) >= len(token.markup)):
                    fences[last - 1] = 'close'
                    last -= 1
            if first < last:
                regions.add((offsets[first], offsets[last]))
    return (headings, fences, {line: tuple(scope) for line, scope in containers.items()},
            sorted(regions), ''.join(view))


def _structured_view(view, budget):
    """Markdown inline code/emphasis has precedence over YAML scalar syntax.

    Such complete field lines already have precise Markdown value spans. Omit
    them from YAML inference, where an asterisk means an alias and a backtick
    is invalid; all other lines retain their exact original source offsets.
    """
    out = []
    for raw in source_lines(view, keepends=True):
        line = raw.rstrip('\r\n')
        normalized = _label_view(line, budget)
        markdown_value = any(_markdown_value_span(line, *m.span('value'))
                             for m in _FIELD.finditer(normalized))
        out.append(' ' * len(line) + raw[len(line):] if markdown_value else raw)
    return ''.join(out)


class _SourceLoader(yaml.SafeLoader):
    """Use YAML syntax events and scanner marks, without object construction."""
    def __init__(self, text, budget):
        self.budget = budget
        self.scalar_tokens = {}
        super().__init__(text)

    def get_token(self):
        token = super().get_token()
        self.budget.spend(tokens=1)
        if isinstance(token, yaml.tokens.ScalarToken):
            self.scalar_tokens[token.end_mark.index] = token
        return token

    def fetch_more_tokens(self):
        # Scanner lookahead can encounter a malformed value before emitting
        # the preceding key event. Budget that work independently of events.
        self.budget.spend(tokens=1)
        return super().fetch_more_tokens()


def _yaml_scalars(text, view, start, end, budget, spans, atoms, explicit_ranges):
    """Interpret YAML/JSON scalar roles; anchors and tags cannot obscure atoms."""
    source = view[start:end]
    budget.spend(len(source))
    try:
        loader = _SourceLoader(source, budget)
    except yaml.YAMLError:
        raise PrivateSyntaxError() from None
    frames, anchors = [], {}

    def context():
        if not frames:
            return False, False
        frame = frames[-1]
        return frame['private'] or (frame['mapping'] and not frame['key'] and frame['sensitive']), frame['mapping'] and frame['key']

    def advance(value=''):
        if frames and frames[-1]['mapping']:
            frame = frames[-1]
            if frame['key']:
                frame['sensitive'] = bool(_SECRET_LABEL.search(value))
            frame['key'] = not frame['key']

    completed, recovery = True, end
    try:
        while loader.check_event():
            event = loader.get_event()
            budget.spend(tokens=1)
            private, is_key = context()
            if isinstance(event, (yaml.events.MappingStartEvent, yaml.events.SequenceStartEvent)):
                if event.anchor:
                    anchors[event.anchor] = None  # Collection aliases are never expanded.
                advance()
                frames.append({'mapping': isinstance(event, yaml.events.MappingStartEvent),
                               'key': True, 'sensitive': False, 'private': private})
                if len(frames) > MAX_PARSER_DEPTH:
                    raise ValueError('개인정보 문법 중첩 한도를 초과해 AI 전송을 중단했습니다.')
            elif isinstance(event, (yaml.events.MappingEndEvent, yaml.events.SequenceEndEvent)):
                frames.pop()
            elif isinstance(event, yaml.events.ScalarEvent):
                token = loader.scalar_tokens.get(event.end_mark.index)
                if token is not None:
                    a, b = token.start_mark.index + start, token.end_mark.index + start
                    if token.style in ('"', "'"):
                        a, b = a + 1, b - 1
                    elif token.style in ('|', '>'):
                        first_newline = text.find('\n', a, b)
                        a = first_newline + 1 if first_newline >= 0 else b
                    decoded = event.value
                    if token.style is None:
                        wrapped = _markdown_value_span(text, a, b)
                        if wrapped:
                            a, b = wrapped
                            decoded = text[a:b]
                    if event.anchor:
                        anchors[event.anchor] = decoded
                    explicit = (_inside_explicit(explicit_ranges, a, b)
                                and (frames or token.style in ('"', "'")))
                    if (private or explicit) and not is_key and a < b and not _PRIVATE_MARKER.search(text, a, b):
                        spans.extend(_line_spans(text, a, b))
                        atoms.append(ScalarAtom(a, b, decoded,
                                                start + token.start_mark.index,
                                                start + token.end_mark.index))
                advance(event.value)
            elif isinstance(event, yaml.events.AliasEvent):
                decoded = anchors.get(event.anchor)
                a, b = start + event.start_mark.index, start + event.end_mark.index
                private = private or _inside_explicit(explicit_ranges, a, b)
                if (private or is_key) and decoded is None:
                    raise PrivateSyntaxError()
                if private:
                    spans.append((a, b))
                    atoms.append(ScalarAtom(a, b, decoded, a, b))
                advance(decoded or '')
            elif isinstance(event, yaml.events.DocumentStartEvent):
                anchors.clear()
    except yaml.YAMLError as exc:
        completed = False
        problem = getattr(exc, 'problem_mark', None)
        if problem:
            line = source.rfind('\n', 0, problem.index) + 1
            if line == 0:
                newline = source.find('\n', problem.index)
                line = newline + 1 if newline >= 0 else len(source)
            recovery = start + line
        # A recognized private key followed by malformed syntax is never
        # exported partially. Scanner context marks identify the opening quote
        # or scalar; otherwise protect from the next value through this block.
        private, _ = context()
        pending_key = None
        value_pending = bool(frames and frames[-1]['mapping'] and not frames[-1]['key'])
        for token in loader.tokens:
            if isinstance(token, yaml.tokens.ScalarToken):
                pending_key = token.value
            elif isinstance(token, yaml.tokens.ValueToken):
                private = private or bool(pending_key and _SECRET_LABEL.search(pending_key))
                value_pending = True
                pending_key = None
        mark = getattr(exc, 'context_mark', None) or getattr(exc, 'problem_mark', None)
        if value_pending and mark:
            at = start + mark.index
            private = private or _inside_explicit(explicit_ranges, at, at + 1)
        if private:
            # There is no reliable decoded atom for invalid credential syntax.
            # Reject the request instead of exporting a partially understood
            # source whose bare value could escape later history protection.
            raise PrivateSyntaxError() from None
    finally:
        loader.dispose()
    return completed, recovery


def _shell_words(text, budget):
    """Read POSIX literal words, recording unsupported syntax without execution.

    Unlike shlex, double-quoted backslashes follow shell rules for $, `, ", \\
    and newline. Expansion, operators and unterminated words are unresolved,
    never approximated by their source spelling. Only a selected credential
    operand makes an unresolved word a fatal privacy error.
    """
    cursor = 0
    while cursor < len(text):
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        start, quote, decoded, unresolved = cursor, None, [], False
        while cursor < len(text):
            if not cursor % 256:
                budget.spend(1)
            char = text[cursor]
            if quote is None and char.isspace():
                break
            if quote == "'":
                if char == "'":
                    quote = None
                else:
                    decoded.append(char)
                cursor += 1
                continue
            if char == '\\':
                following = text[cursor + 1:cursor + 2]
                if not following:
                    unresolved = True
                    cursor += 1
                elif quote is None or following in '$`"\\\n':
                    if following != '\n':
                        decoded.append(following)
                    cursor += 2
                else:
                    decoded.append(char)
                    cursor += 1
                continue
            if char in ('"', "'"):
                if char == quote:
                    quote = None
                elif quote is None:
                    quote = char
                else:
                    decoded.append(char)
            else:
                if char in '$`' or (quote is None and char in '*?[]{}~;|&<>()'):
                    unresolved = True
                decoded.append(char)
            cursor += 1
        if start == cursor:
            break
        budget.spend(tokens=1)
        # A standalone backslash-newline is a continuation, not an operand.
        if decoded or quote or unresolved or text[start:cursor].strip('\\\n'):
            yield start, cursor, ''.join(decoded), unresolved or quote is not None


def _shell_scalars(text, budget, spans, atoms):
    if '--' not in text:
        return
    budget.spend(len(text))
    sensitive_next = False
    for start, end, value, unresolved in _shell_words(text, budget):
        if sensitive_next:
            if unresolved:
                raise PrivateSyntaxError()
            a, b = start, end
            if text[a:a + 1] in ('"', "'") and text[b - 1:b] == text[a]:
                a, b = a + 1, b - 1
            spans.extend(_line_spans(text, a, b))
            atoms.append(ScalarAtom(a, b, value, start, end))
            sensitive_next = False
        elif value.startswith('--'):
            flag, separator, decoded = value[2:].partition('=')
            if _SECRET_LABEL.fullmatch(flag):
                if unresolved:
                    raise PrivateSyntaxError()
                if separator:
                    a = text.find('=', start, end) + 1
                    b = end
                    if text[a:a + 1] in ('"', "'") and text[b - 1:b] == text[a]:
                        a, b = a + 1, b - 1
                    spans.extend(_line_spans(text, a, b))
                    atoms.append(ScalarAtom(a, b, decoded, start, end))
                else:
                    sensitive_next = True


def _quoted_atoms(text, spans, budget, resolved):
    """Resolve remaining protected scalars or reject the whole analysis.

    Source coverage assigns one dialect to each resolved scalar. A shell word
    or a multiline YAML scalar must not be reinterpreted one line at a time as
    standalone YAML. Markdown code spans are explicitly literal values.
    """
    coverage = []
    for atom in sorted((a for a in resolved if a.syntax_start is not None),
                       key=lambda a: (a.syntax_start, a.syntax_end)):
        a, b = atom.syntax_start, atom.syntax_end
        if coverage and a <= coverage[-1][1]:
            coverage[-1] = (coverage[-1][0], max(b, coverage[-1][1]))
        else:
            coverage.append((a, b))
    seen = set()
    for start, end in spans:
        # Table/Markdown policy already chooses exact value spans. Their
        # adjacent quotes are syntax, not part of the locally restored payload.
        a, b = start, end
        if a and b < len(text) and text[a - 1] in ('"', "'") and text[b] == text[a - 1]:
            a, b = a - 1, b + 1
        if (a, b) in seen or text[a:a + 1] not in ('"', "'", '!', '&'):
            continue
        seen.add((a, b))
        scope = bisect_right(coverage, (a, float('inf'))) - 1
        if scope >= 0 and coverage[scope][1] > a:
            # A field's provisional span may also include a trailing comment
            # or annotation. Its resolved prefix owns the scalar's dialect.
            continue
        if start and text[start - 1] == '`':
            opening = start - 1
            while opening and text[opening - 1] == '`':
                opening -= 1
            budget.spend(len(text) - opening)
            literal = _markdown_value_span(text, opening, len(text))
            if literal and literal[0] == start:
                left, right = literal
                yield ScalarAtom(left, right, text[left:right], opening, right + start - opening)
                continue
        budget.spend(b - a)
        try:
            for token in yaml.scan(text[a:b], Loader=yaml.SafeLoader):
                budget.spend(tokens=1)
                if isinstance(token, yaml.tokens.ScalarToken):
                    yield ScalarAtom(start, end, token.value, a, b)
                    break
            else:
                raise PrivateSyntaxError()
        except yaml.YAMLError:
            raise PrivateSyntaxError() from None


def analyze(text: str, path: str = '', checkpoint=None) -> Analysis:
    """Classify every outbound text input through the same fail-closed boundary."""
    checkpoint = checkpoint or (lambda: None)
    checkpoint()
    if len(text) > MAX_INPUT_CHARS:
        raise ValueError('개인정보 분석 입력 한도를 초과해 AI 전송을 중단했습니다.')
    budget = _Budget(checkpoint)
    explicit_ranges = _explicit_ranges(text)
    headings, fences, containers, regions, view = _markdown_structure(text, budget)
    # Explicit ranges are authoritative inside the policy pass: inferred
    # candidates are intersected with remaining public source intervals.
    spans = _policy_spans(path, text, headings, fences, containers, budget)
    atoms = []
    # Private markers are neutral syntax for the structured parser. Blanking
    # rather than deleting them keeps every mark tied to original offsets.
    view = _PRIVATE_MARKER.sub(lambda m: ' ' * len(m.group()), view)
    view = _structured_view(view, budget)
    completed, recovery = _yaml_scalars(text, view, 0, len(text), budget, spans, atoms, explicit_ranges)
    if not completed:
        for start, end in regions:
            if (start, end) == (0, len(text)):
                start = recovery
            while start < end:
                completed, following = _yaml_scalars(text, view, start, end, budget, spans, atoms, explicit_ranges)
                if completed or following <= start:
                    break
                start = following
    _shell_scalars(text, budget, spans, atoms)
    spans = sorted(set(span for a, b in spans for span in _line_spans(text, a, b)))
    atoms.extend(_quoted_atoms(text, spans, budget, atoms))
    if len(spans) > MAX_SPANS or len(atoms) > MAX_SPANS:
        raise ValueError('개인정보 분석 결과 한도를 초과해 AI 전송을 중단했습니다.')
    checkpoint()
    atoms = [atom for atom in atoms if not _PRIVATE_MARKER.search(text, atom.start, atom.end)]
    return Analysis(tuple(spans), tuple(dict.fromkeys(atoms)))
