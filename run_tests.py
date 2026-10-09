#!/usr/bin/env python3
"""Run isolated tests with synthetic notes and no external AI requests."""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import unittest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', action='store_true', help='also run Playwright scenarios')
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    sys.path[:0] = [str(root / 'backend'), str(root / 'tests')]
    suite = unittest.TestLoader().discover(str(root / 'tests'), pattern='test_*.py')
    result = unittest.TextTestRunner(verbosity=2 if args.verbose else 1).run(suite)
    if not result.wasSuccessful():
        return 1
    if args.browser:
        if not shutil.which('node'):
            parser.error('Install Node.js and run npm ci before using --browser.')
        return subprocess.run(['node', str(root / 'tests/test_browser.cjs')], cwd=root).returncode
    return 0


if __name__ == '__main__':
    sys.exit(main())
