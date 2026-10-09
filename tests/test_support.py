"""Isolated application imports for tests; never read production configuration."""
import atexit
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
from unittest.mock import patch


def load_test_app():
    fixture = tempfile.TemporaryDirectory(prefix='wiki-test-app-')
    atexit.register(fixture.cleanup)
    root = Path(fixture.name)
    source = Path(__file__).resolve().parents[1] / 'backend'
    for name in ('server.py', 'privacy.py', 'privacy_analysis.py', 'model_input.py', 'settings.py'):
        shutil.copy2(source / name, root / name)
    (root / 'notes').mkdir()
    (root / 'config.json').write_text(json.dumps({
        'notes_root': str(root / 'notes'), 'issuer': 'https://synthetic.invalid',
        'origin': 'https://wiki.example.com',
        'audience': 'synthetic-wiki', 'codex': '/usr/bin/false',
        'model': 'synthetic-model', 'port': 0,
    }))
    spec = importlib.util.spec_from_file_location('wiki_test_app', root / 'server.py')
    app = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, {'WIKI_CONFIG': str(root / 'config.json')}):
        spec.loader.exec_module(app)
    return app
