"""Synthetic app-server transport fixture; no network, real notes, or accounts."""
import json
import os
from pathlib import Path
import signal
import sys
import time


def serve(capture):
    def emit(value):
        print(json.dumps(value), flush=True)
    threads = {}
    for line in sys.stdin:
        request = json.loads(line)
        method, params = request['method'], request.get('params', {})
        def reply(value):
            emit({'id': request['id'], 'result': value})
        if method == 'initialize':
            reply({})
        elif method == 'thread/start':
            ident = 'thread-' + str(len(threads) + 1)
            threads[ident] = params
            reply({'thread': {'id': ident, 'ephemeral': params['ephemeral']},
                   'model': params['model'], 'reasoningEffort': params['config']['model_reasoning_effort'],
                   'approvalPolicy': params['approvalPolicy'], 'sandbox': {'type': 'readOnly'}})
        elif method == 'thread/unsubscribe':
            reply({'status': 'unsubscribed'})
        elif method == 'turn/start':
            thread = threads[params['threadId']]
            model = thread['model']
            prompt = params['input'][0]['text']
            home = Path(os.environ['HOME'])
            with Path(capture).open('a') as f:
                f.write(json.dumps({'argv': sys.argv[1:], 'prompt': prompt,
                    'cwd': str(Path.cwd()), 'home': str(home), 'pid': os.getpid(),
                    'inherited_secret': os.environ.get('SYNTHETIC_PARENT_SECRET'),
                    'thread': thread, 'thread_id': params['threadId']}) + '\n')
            (home / 'auth.json').write_text('SYNTHETIC REFRESHED LOGIN')
            if model == 'wait':
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                while True: time.sleep(.1)
            if model == 'nonzero':
                sys.exit(7)
            if model == 'flood':
                sys.stdout.write('x' * (5 * 1024 * 1024)); sys.stdout.flush(); continue
            if model == 'tool':
                emit({'id': 'approval', 'method': 'item/commandExecution/requestApproval', 'params': {}})
                continue
            value = ({'paths': ['doc_0001'], 'catalog_only': False}
                     if 'paths' in params['outputSchema']['properties'] else
                     {'answer': 'synthetic answer', 'sources': [{'path': 'doc_0001', 'line': 1}]})
            if model == 'evidence':
                body = json.loads(prompt.split('\n', 1)[1])
                if 'catalog' in body:
                    value = {'paths': [body['catalog'][0]['path']], 'catalog_only': False}
                else:
                    note = body['evidence']['notes'][0]
                    value = {'answer': '\n'.join(line.split(': ', 1)[1] for line in note['lines']),
                             'sources': [{'path': note['path'], 'line': 1}]}
            if model == 'malformed':
                value = {'answer': [], 'sources': []}
            common = {'threadId': params['threadId'], 'turnId': 'turn-1'}
            emit({'method': 'turn/started', 'params': {'threadId': params['threadId'], 'turn': {'id': 'turn-1'}}})
            # Deliberately notify before acknowledging turn/start.
            text = json.dumps(value, ensure_ascii=False)
            emit({'method': 'item/agentMessage/delta', 'params': {**common, 'itemId': 'answer', 'delta': text[:24]}})
            reply({'turn': {'id': 'turn-1'}})
            if model == 'stream':
                time.sleep(.2)
            emit({'method': 'item/agentMessage/delta', 'params': {**common, 'itemId': 'answer', 'delta': text[24:]}})
            emit({'method': 'item/completed', 'params': {**common, 'item': {'id': 'answer', 'type': 'agentMessage', 'text': text}}})
            emit({'method': 'turn/completed', 'params': {'threadId': params['threadId'], 'turn': {'id': 'turn-1', 'status': 'completed'}}})
