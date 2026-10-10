"""Bounded previews of a structured answer; never restore protected values here."""
import json
import re

from model_schemas import ANSWER_LIMIT


def answer_prefix(text):
    # Schema property order puts answer first. Other valid orders still get a
    # final answer, but no speculative preview of unrelated JSON fields.
    match = re.match(r'^\s*\{\s*"answer"\s*:\s*"', text)
    if not match:
        return ''
    start = match.end()
    i = start
    while i < len(text):
        char = text[i]
        if char == '"':
            break
        if char == '\\':
            if i + 1 >= len(text):
                break
            size = 6 if text[i + 1] == 'u' else 2
            if i + size > len(text):
                break
            i += size
        else:
            i += 1
    try:
        value = json.loads('"' + text[start:i] + '"')
        # A split UTF-16 surrogate pair is withheld until both halves arrive.
        if value and 0xD800 <= ord(value[-1]) < 0xDC00:
            value = value[:-1]
        value.encode('utf-8')
    except (ValueError, UnicodeError):
        return ''
    return value if len(value.encode('utf-8')) <= ANSWER_LIMIT else ''


def safe_preview(text):
    # Withhold incomplete delimiters as well as complete opaque values. This is
    # deliberately conservative for [[wiki links]] in an unfinished response.
    text = re.sub(r'\[\[.*?(?:\]\]|$)', '[보호된 값]', text, flags=re.S)
    return text[:-1] if text.endswith('[') else text
