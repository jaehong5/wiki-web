"""Load installation settings without bundling personal data in the source tree."""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit


def load_config(default_path):
    """WIKI_CONFIG points to a private JSON file outside the checkout."""
    path = Path(os.environ.get('WIKI_CONFIG', str(default_path))).expanduser()
    try:
        config = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError('Set WIKI_CONFIG to a readable JSON configuration file.') from exc
    if not isinstance(config, dict):
        raise ValueError('Wiki configuration must be a JSON object.')
    for field in ('origin', 'issuer', 'audience', 'notes_root', 'codex', 'model'):
        if not isinstance(config.get(field), str) or not config[field].strip():
            raise ValueError(f'Wiki configuration requires a nonempty {field}.')
    for field in ('origin', 'issuer'):
        value = config[field].rstrip('/')
        parsed = urlsplit(value)
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError(f'Wiki {field} must be an HTTPS origin.') from exc
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.path or parsed.query or parsed.fragment):
            raise ValueError(f'Wiki {field} must be an HTTPS origin.')
        config[field] = value
    if not Path(config['notes_root']).is_absolute():
        raise ValueError('Wiki notes_root must be an absolute path outside the public web root.')
    port = config.get('port')
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError('Wiki port must be an integer between 0 and 65535.')
    return config
