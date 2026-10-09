#!/usr/bin/env python3
"""Render installation URLs into a new static output directory."""
import argparse
import html
import json
import os
from pathlib import Path
import shutil
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]


def https_url(value, *, origin=False):
    if not isinstance(value, str) or any(char.isspace() or char in '\"<>`\\' for char in value):
        raise ValueError('Use an HTTPS URL without whitespace or markup.')
    parsed = urlsplit(value)
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError('Invalid HTTPS URL port.') from exc
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or (origin and parsed.path not in ('', '/'))):
        raise ValueError('Use an HTTPS origin or notes URL without credentials, query, or fragment.')
    return value.rstrip('/')


def build(origin, notes_url, output):
    origin = https_url(origin, origin=True)
    notes_url = https_url(notes_url) if notes_url else None
    source = ROOT / 'frontend/public'
    output = Path(output).resolve()
    if output == ROOT or output in ROOT.parents or output.is_relative_to(source):
        raise ValueError('The output must be a separate, new directory.')
    if output.exists():
        raise ValueError('Output already exists; choose a new --output directory.')
    shutil.copytree(source, output)
    landing = output / 'index.html'
    landing.write_text(landing.read_text().replace('https://wiki.example.com', html.escape(origin, quote=True)))
    application = output / 'app/index.html'
    content = application.read_text()
    link = '<a href="https://notes.example.com/" target="_blank" rel="noopener noreferrer">SilverBullet 열기 ↗</a>'
    if notes_url:
        content = content.replace('https://notes.example.com/', html.escape(notes_url + '/', quote=True))
    else:
        content = content.replace(link, '')
    application.write_text(content)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=os.environ.get('WIKI_CONFIG'), help='external private JSON settings')
    parser.add_argument('--origin', help='public HTTPS origin; overrides the config value')
    parser.add_argument('--notes-url', help='optional SilverBullet HTTPS URL')
    parser.add_argument('--output', type=Path, default=ROOT / 'dist/public', help='new output directory')
    args = parser.parse_args()
    try:
        config = json.loads(Path(args.config).expanduser().read_text()) if args.config else {}
        if not isinstance(config, dict):
            raise ValueError('Configuration must be a JSON object.')
        origin = args.origin or config.get('origin')
        if not origin:
            raise ValueError('Provide --origin or --config with an origin value.')
        output = build(origin, args.notes_url or config.get('notes_url'), args.output)
    except (OSError, ValueError) as exc:
        parser.exit(1, f'Build failed: {exc}\n')
    print(f'Static site built at {output}')


if __name__ == '__main__':
    main()
