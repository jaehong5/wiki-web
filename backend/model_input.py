"""The single, local boundary for prepared model input.

Only sanitized context is assembled here. Generated evidence line numbers are
kept separate from their text while auditing, so a private PIN such as ``1``
does not collide with a citation number. No network or model calls live here.
"""
from dataclasses import dataclass
import json
import re

@dataclass(frozen=True)
class EvidenceLine:
    number: int
    text: str

    def __post_init__(self):
        if type(self.number) is not int or self.number < 1 or type(self.text) is not str:
            raise ValueError('Invalid evidence line.')


class PreparedPrompt(str):
    def __new__(cls, *args, **kwargs):
        raise TypeError('Use prepare_prompt() with a privacy context.')

    def validate(self):
        # Repeat the audit at the actual subprocess boundary, including any
        # values learned since this immutable prompt was assembled.
        for text in self.audit_texts:
            if self.vault.contains_unmasked_value(text):
                raise ValueError('민감정보 보호 검증을 통과하지 못해 AI 전송을 중단했습니다.')


def prepare_prompt(vault, instructions, payload):
    """Serialize only the two application schemas, tracking content separately.

    Instructions are application-owned constants. Payload strings come from the
    shared privacy analysis; this audit catches values learned after an earlier
    field was sanitized. This is a final guard, not a second secret classifier.
    """
    if type(instructions) is not str or type(payload) is not dict:
        raise ValueError('Invalid model input.')
    texts, count = [], 0

    def content(value):
        if type(value) is not str:
            raise ValueError('Invalid model content.')
        texts.append(value)
        return value

    def record(value, fields):
        if type(value) is not dict or set(value) != set(fields):
            raise ValueError('Invalid model input fields.')
        return value

    def sequence(value):
        nonlocal count
        if type(value) not in (list, tuple):
            raise ValueError('Invalid model input list.')
        count += len(value)
        if count > 250000:
            raise ValueError('모델 입력 구조가 너무 큽니다.')
        return value

    def document_id(value):
        if type(value) is not str or not re.fullmatch(r'doc_[0-9]{4,}', value):
            raise ValueError('Invalid generated document ID.')
        return value

    def catalog(entries):
        result = []
        for entry in sequence(entries):
            record(entry, ('path', 'title', 'headings', 'matches'))
            result.append({'path': document_id(entry['path']), 'title': content(entry['title']),
                           'headings': [content(text) for text in sequence(entry['headings'])],
                           'matches': [content(text) for text in sequence(entry['matches'])]})
        return result

    def notes(entries):
        result = []
        for entry in sequence(entries):
            record(entry, ('path', 'title', 'lines'))
            lines = []
            for line in sequence(entry['lines']):
                if type(line) is not EvidenceLine:
                    raise ValueError('Evidence requires separate line numbers and content.')
                lines.append(f'{line.number}: {content(line.text)}')
            result.append({'path': document_id(entry['path']), 'title': content(entry['title']),
                           'lines': lines})
        return result

    stage = 'catalog' if 'catalog' in payload else 'evidence'
    record(payload, ('question', 'history', stage))
    history = []
    for message in sequence(payload['history']):
        record(message, ('role', 'content'))
        if message['role'] not in ('user', 'assistant'):
            raise ValueError('Invalid generated conversation role.')
        history.append({'role': message['role'], 'content': content(message['content'])})
    wire = {'question': content(payload['question']), 'history': history}
    if stage == 'catalog':
        wire['catalog'] = catalog(payload['catalog'])
    else:
        evidence = payload['evidence']
        kind = 'catalog' if type(evidence) is dict and 'catalog' in evidence else 'notes'
        record(evidence, (kind,))
        wire['evidence'] = {kind: catalog(evidence[kind]) if kind == 'catalog' else notes(evidence[kind])}
    # The wire object is built from validated fields only. A future input field
    # cannot silently bypass auditing: adding it requires updating this schema.
    encoded = json.dumps(wire, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode('utf-8')) > 2 * 1024 * 1024:
        raise ValueError('모델 입력이 너무 큽니다.')
    prompt = str.__new__(PreparedPrompt, instructions.rstrip('\n') + '\n' + encoded)
    prompt.vault, prompt.audit_texts = vault, tuple(texts)
    prompt.validate()
    return prompt
