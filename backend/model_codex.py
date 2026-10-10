"""One isolated stdio app-server, with a fresh ephemeral thread per invocation."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import select
import subprocess
import tempfile
import time

from model_protocol import ModelError, REQUEST_LIMIT, RESPONSE_LIMIT
from model_providers import profile, stop_process, child_pids, reap_helpers, sync_credentials
from model_schemas import SCHEMAS, validate_result
from model_stream import answer_prefix

DISABLED = ('shell_tool', 'apps', 'multi_agent', 'view_image', 'browser_use',
            'computer_use', 'code_mode_host', 'skill_search', 'daemon_auto_start')


class CodexSession:
    """Called only while the worker's single invocation slot is held.

    No requests are retried. Any failure destroys the process and scratch HOME;
    a subsequent independent request may start a new process. Periodic recycling
    bounds app-server's in-memory thread grace period and temporary artifacts.
    """
    MAX_CALLS = 32
    MAX_AGE = 1800
    IDLE_TTL = 300

    def __init__(self):
        self.proc = None
        self.stack = None
        self.calls = 0
        self.last_used = 0
        self.sequence = 0

    def close(self):
        proc, self.proc = self.proc, None
        try:
            if proc:
                try:
                    stop_process(proc)
                finally:
                    for stream in (proc.stdin, proc.stdout):
                        stream.close()
                    reap_helpers(self.previous_children)
        finally:
            self.buffer = bytearray()
            if self.stack:
                stack, self.stack = self.stack, None
                stack.close()

    def expire(self):
        if self.proc and time.monotonic() - self.last_used >= self.IDLE_TTL:
            self.close()

    def start(self, settings, checkpoint):
        now = time.monotonic()
        if self.proc and (self.proc.poll() is not None or self.settings != settings
                          or self.calls >= self.MAX_CALLS or now - self.started >= self.MAX_AGE):
            self.close()
        if self.proc:
            return True
        self.settings = dict(settings)
        self.stack = ExitStack()
        scratch = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='wiki-codex-')))
        self.workspace = scratch / 'workspace'
        self.workspace.mkdir(mode=0o700)
        self.environment = self.stack.enter_context(profile('codex', Path(settings['state_dir']), scratch))
        args = [settings['binary'], 'app-server', '--listen', 'stdio://']
        for feature in DISABLED:
            args += ['--disable', feature]
        args += ['--enable', 'skip_host_skill_discovery', '-c', 'web_search="disabled"',
                 '-c', 'project_doc_max_bytes=0', '-c', 'history.persistence="none"',
                 '-c', 'model_provider="openai"']
        self.previous_children = child_pids()
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, cwd=self.workspace, env=self.environment,
                                     start_new_session=True, umask=0o077)
        os.set_blocking(self.proc.stdin.fileno(), False)
        os.set_blocking(self.proc.stdout.fileno(), False)
        self.buffer = bytearray()
        self.calls = 0
        self.started = self.last_used = now
        self.rpc('initialize', {'clientInfo': {'name': 'wiki_web', 'version': '1.0.0'}}, checkpoint)
        self.send({'method': 'initialized'}, checkpoint)
        return False

    def send(self, value, checkpoint):
        data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode() + b'\n'
        if len(data) > REQUEST_LIMIT + 65536:
            raise ModelError('Oversized app-server input')
        offset = 0
        while offset < len(data):
            checkpoint()
            if self.proc.poll() is not None:
                raise ModelError('App-server exited', code='provider_exit')
            if select.select([], [self.proc.stdin], [], .05)[1]:
                try:
                    offset += os.write(self.proc.stdin.fileno(), data[offset:offset + 65536])
                except BrokenPipeError as exc:
                    raise ModelError('App-server exited', code='provider_exit') from exc

    def read(self, checkpoint):
        checkpoint()
        while b'\n' not in self.buffer:
            checkpoint()
            if not select.select([self.proc.stdout], [], [], .05)[0]:
                continue
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                raise ModelError('App-server exited', code='provider_exit')
            self.buffer.extend(chunk)
            if len(self.buffer) > 4 * RESPONSE_LIMIT:
                raise ModelError('Oversized app-server event', code='output_limit')
        line, _, rest = self.buffer.partition(b'\n')
        self.buffer = bytearray(rest)
        self.received += len(line)
        if self.received > 16 * RESPONSE_LIMIT:
            raise ModelError('Oversized app-server response', code='output_limit')
        event = json.loads(line)
        if type(event) is not dict:
            raise ValueError('Invalid app-server event')
        if 'method' in event and 'id' in event:
            # Never grant an approval, tool call, or credentials request.
            raise ModelError('Unexpected app-server request', code='tool_attempt')
        if event.get('method') in ('item/started', 'item/completed'):
            kind = event.get('params', {}).get('item', {}).get('type')
            if kind not in {'userMessage', 'agentMessage', 'reasoning'}:
                raise ModelError('Unexpected model tool', code='tool_attempt')
        if event.get('method') == 'error':
            raise ModelError('Model stream failed', code='provider_exit')
        return event

    def rpc(self, method, params, checkpoint, on_event=lambda event: None):
        self.sequence += 1
        ident = self.sequence
        self.send({'id': ident, 'method': method, 'params': params}, checkpoint)
        while True:
            event = self.read(checkpoint)
            if 'id' in event:
                if event['id'] != ident or type(event.get('result')) is not dict or 'error' in event:
                    raise ModelError('App-server request failed', code='provider_exit')
                return event['result']
            on_event(event)

    def invoke(self, settings, prompt, purpose, checkpoint, progress):
        started = time.monotonic()
        self.received = 0
        try:
            reused = self.start(settings, checkpoint)
            startup_ms = round((time.monotonic() - started) * 1000)
            prepared = self.rpc('thread/start', {
                'model': settings['model'], 'cwd': str(self.workspace), 'ephemeral': True,
                'approvalPolicy': 'never', 'sandbox': 'read-only',
                'config': {'model_reasoning_effort': settings['effort']}}, checkpoint)
            if (prepared.get('model') != settings['model']
                    or prepared.get('reasoningEffort') != settings['effort']
                    or prepared.get('approvalPolicy') != 'never'
                    or prepared.get('sandbox', {}).get('type') != 'readOnly'
                    or prepared.get('instructionSources')):
                raise ModelError('Unexpected thread settings')
            thread = prepared['thread']
            if thread.get('ephemeral') is not True or type(thread.get('id')) is not str:
                raise ModelError('Non-ephemeral thread refused')
            thread_id = thread['id']
            state = {'turn': None, 'item': None, 'text': '', 'final': None, 'done': False,
                     'last_preview': '', 'last_emit': 0, 'first_delta_ms': None}

            def event_received(event):
                method, params = event.get('method'), event.get('params', {})
                if params.get('threadId') != thread_id:
                    return
                if method == 'turn/started':
                    state['turn'] = params['turn']['id']
                if method in ('item/agentMessage/delta', 'item/completed', 'turn/completed'):
                    turn_id = params.get('turnId', params.get('turn', {}).get('id'))
                    if state['turn'] is not None and turn_id != state['turn']:
                        raise ValueError('Unexpected turn')
                if method == 'item/agentMessage/delta':
                    delta, item = params['delta'], params['itemId']
                    if type(delta) is not str or (state['item'] is not None and item != state['item']):
                        raise ValueError('Unexpected answer item')
                    state['item'] = item
                    state['text'] += delta
                    if len(state['text'].encode()) > RESPONSE_LIMIT:
                        raise ModelError('Oversized answer', code='output_limit')
                    if state['first_delta_ms'] is None:
                        state['first_delta_ms'] = round((time.monotonic() - started) * 1000)
                    if purpose == 'answer' and time.monotonic() - state['last_emit'] >= .08:
                        preview = answer_prefix(state['text'])
                        if preview and preview != state['last_preview']:
                            progress(preview)
                            state.update(last_preview=preview, last_emit=time.monotonic())
                elif method == 'item/completed' and params['item']['type'] == 'agentMessage':
                    item = params['item']
                    if state['final'] is not None or (state['item'] and item['id'] != state['item']):
                        raise ValueError('Multiple answer items')
                    state['final'] = item['text']
                elif method == 'turn/completed':
                    if params['turn']['status'] != 'completed':
                        raise ModelError('Model turn failed', code='provider_exit')
                    state['done'] = True

            response = self.rpc('turn/start', {'threadId': thread_id,
                'input': [{'type': 'text', 'text': prompt}], 'effort': settings['effort'],
                'outputSchema': SCHEMAS[purpose]}, checkpoint, event_received)
            if state['turn'] is not None and state['turn'] != response['turn']['id']:
                raise ValueError('Unexpected turn identity')
            state['turn'] = response['turn']['id']
            while not state['done']:
                event_received(self.read(checkpoint))
            if type(state['final']) is not str or len(state['final'].encode()) > RESPONSE_LIMIT:
                raise ValueError('Missing structured answer')
            result = validate_result(purpose, json.loads(state['final']))
            self.rpc('thread/unsubscribe', {'threadId': thread_id}, checkpoint)
            sync_credentials('codex', Path(settings['state_dir']), Path(self.environment['HOME']))
            self.calls += 1
            self.last_used = time.monotonic()
            self.metrics = {'reused': reused, 'startup_ms': startup_ms,
                            'first_delta_ms': state['first_delta_ms']}
            return result
        except BaseException:
            self.close()
            raise
