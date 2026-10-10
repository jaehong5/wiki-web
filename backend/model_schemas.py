"""Application-owned output contracts shared by the web and model processes."""
import re

ANSWER_LIMIT = 64000
SOURCE_LIMIT = 128
DOCUMENT_ID_SCHEMA = {'type': 'string', 'pattern': '^doc_[0-9]{4,}$', 'maxLength': 64}
SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'answer': {'type': 'string', 'minLength': 1, 'maxLength': ANSWER_LIMIT},
        'sources': {'type': 'array', 'maxItems': SOURCE_LIMIT, 'items': {
            'type': 'object', 'additionalProperties': False,
            'properties': {'path': DOCUMENT_ID_SCHEMA, 'line': {'type': 'integer', 'minimum': 1}},
            'required': ['path', 'line'],
        }},
    }, 'required': ['answer', 'sources'],
}
SELECT_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {'paths': {'type': 'array', 'maxItems': 8, 'items': DOCUMENT_ID_SCHEMA},
                   'catalog_only': {'type': 'boolean'}},
    'required': ['paths', 'catalog_only'],
}
SCHEMAS = {'select': SELECT_SCHEMA, 'answer': SCHEMA}


def validate_result(purpose, value):
    def document_id(text):
        return type(text) is str and len(text) <= 64 and re.fullmatch(r'doc_[0-9]{4,}', text)
    valid = type(value) is dict
    if purpose == 'select':
        valid = (valid and set(value) == {'paths', 'catalog_only'}
                 and type(value['catalog_only']) is bool and type(value['paths']) is list
                 and len(value['paths']) <= 8 and all(document_id(p) for p in value['paths']))
    elif purpose == 'answer':
        valid = (valid and set(value) == {'answer', 'sources'}
                 and type(value['answer']) is str and bool(value['answer'].strip())
                 and len(value['answer'].encode('utf-8')) <= ANSWER_LIMIT
                 and type(value['sources']) is list and len(value['sources']) <= SOURCE_LIMIT
                 and all(type(s) is dict and set(s) == {'path', 'line'}
                         and document_id(s['path']) and type(s['line']) is int and s['line'] > 0
                         for s in value['sources']))
    else:
        valid = False
    if not valid:
        raise ValueError('Invalid structured model answer')
    return value
