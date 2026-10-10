"""Load installation settings without bundling personal data in the source tree."""
import json
import os
import pwd
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
    fields = ('origin', 'issuer', 'audience', 'notes_root')
    if 'ai' not in config:
        fields += ('codex', 'model')  # Compatibility for existing Codex-only installations.
    for field in fields:
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
    if 'ai' in config:
        ai = config['ai']
        if (type(ai) is not dict or set(ai) != {'provider', 'socket', 'worker_user'}
                or ai['provider'] not in ('codex', 'agy')
                or type(ai['socket']) is not str or not Path(ai['socket']).is_absolute()
                or type(ai['worker_user']) is not str or not ai['worker_user']):
            raise ValueError('Wiki ai requires provider, absolute socket, and worker_user.')
        try:
            ai['worker_uid'] = pwd.getpwnam(ai['worker_user']).pw_uid
        except KeyError as exc:
            raise ValueError('Create the dedicated model worker account first.') from exc
    return config
