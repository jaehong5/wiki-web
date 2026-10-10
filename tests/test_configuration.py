"""Portable configuration and builds cannot depend on an operator's machine."""
import importlib.util
import json
import os
import pwd
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from settings import load_config
from model_worker import load_worker_config

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('build_frontend', ROOT / 'scripts/build_frontend.py')
frontend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(frontend)


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = tempfile.TemporaryDirectory(prefix='wiki-config-test-')
        self.addCleanup(self.fixture.cleanup)
        self.directory = Path(self.fixture.name)
        self.config = json.loads((ROOT / 'config.example.json').read_text())

    def write_config(self, config):
        path = self.directory / 'settings.json'
        path.write_text(json.dumps(config))
        return path

    def test_external_config_overrides_default_and_reaches_auth_boundary(self):
        config = dict(self.config, origin='https://other.example.com/',
                      issuer='https://other-team.cloudflareaccess.com/')
        path = self.write_config(config)
        with patch.dict(os.environ, {'WIKI_CONFIG': str(path)}):
            loaded = load_config(self.directory / 'absent.json')
        self.assertEqual(loaded['origin'], 'https://other.example.com')
        self.assertEqual(loaded['issuer'], 'https://other-team.cloudflareaccess.com')
        spec = importlib.util.spec_from_file_location('configured_app', ROOT / 'backend/server.py')
        app = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {'WIKI_CONFIG': str(path)}):
            spec.loader.exec_module(app)
        self.assertEqual(app.ORIGIN, 'https://other.example.com')
        self.assertEqual(app.CONFIG['audience'], config['audience'])

    def test_missing_auth_and_invalid_origins_fail_before_serving(self):
        for field, value in [('origin', ''), ('audience', ''), ('issuer', ''),
                             ('origin', 'http://wiki.example.com'),
                             ('issuer', 'https://user:secret@example.com'),
                             ('origin', 'https://example.com/path'),
                             ('origin', 'https://example.com?query=1'),
                             ('origin', 'https://example.com:invalid'),
                             ('notes_root', './notes'), ('port', True)]:
            with self.subTest(field=field, value=value):
                path = self.write_config(dict(self.config, **{field: value}))
                with patch.dict(os.environ, {'WIKI_CONFIG': str(path)}):
                    with self.assertRaises(ValueError):
                        load_config(path)

    def test_worker_mode_needs_no_cli_in_web_and_rejects_request_like_settings(self):
        config={key:value for key,value in self.config.items() if key not in ('codex','model')}
        ai={'provider':'agy','socket':str(self.directory/'worker.sock'),
            'worker_user':pwd.getpwuid(os.getuid()).pw_name}
        path=self.write_config(dict(config,ai=ai))
        with patch.dict(os.environ,{'WIKI_CONFIG':str(path)}):
            self.assertEqual(load_config(path)['ai']['worker_uid'],os.getuid())
        for changed in [{'provider':'unknown'},{'socket':'relative.sock'},
                        {'worker_user':'synthetic-missing-user'},{'binary':'/usr/bin/false'}]:
            with self.subTest(changed=changed):
                path=self.write_config(dict(config,ai=dict(ai,**changed)))
                with patch.dict(os.environ,{'WIKI_CONFIG':str(path)}), self.assertRaises(ValueError):
                    load_config(path)

    def test_worker_configuration_is_operator_owned_and_fixed_shape(self):
        config={'socket':str(self.directory/'worker.sock'),
                'allowed_user':pwd.getpwuid(os.getuid()).pw_name,
                'providers':{'agy':{'binary':'/usr/bin/false','state_dir':str(self.directory),
                                    'model':'synthetic','effort':'medium'}}}
        self.assertEqual(load_worker_config(self.write_config(config))['allowed_uid'],os.getuid())
        for changed in [{'binary':'relative'},{'model':''},{'effort':'unbounded'},
                        {'env':{'TOKEN':'synthetic'}}]:
            altered=dict(config,providers={'agy':dict(config['providers']['agy'],**changed)})
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                load_worker_config(self.write_config(altered))

    def test_frontend_build_uses_configurable_urls_and_omits_private_config(self):
        output = frontend.build('https://demo.example.org', 'https://notes.example.org',
                                self.directory / 'public')
        landing = (output / 'index.html').read_text()
        self.assertIn('https://demo.example.org/assets/branding/og-image.png', landing)
        self.assertNotIn('https://wiki.example.com', landing)
        self.assertIn('https://notes.example.org/', (output / 'app/index.html').read_text())
        self.assertFalse((output / 'config.json').exists())
        self.assertTrue((output / 'assets/branding/og-image.png').is_file())

    def test_optional_notes_link_removed_and_existing_output_not_overwritten(self):
        output = frontend.build('https://demo.example.org', None, self.directory / 'public')
        self.assertNotIn('https://notes.example.com', (output / 'app/index.html').read_text())
        marker = output / 'keep.txt'
        marker.write_text('keep this file')
        with self.assertRaises(ValueError):
            frontend.build('https://demo.example.org', None, output)
        self.assertEqual(marker.read_text(), 'keep this file')

    def test_frontend_rejects_injectable_urls_before_creating_output(self):
        for url in ['javascript:alert(1)', 'https://example.com/\" onclick=\"x',
                    'https://user:secret@example.com', 'https://example.com/?token=example']:
            with self.subTest(url=url):
                output = self.directory / 'invalid'
                with self.assertRaises(ValueError):
                    frontend.build('https://demo.example.org', url, output)
                self.assertFalse(output.exists())
