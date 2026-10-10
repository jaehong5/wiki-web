"""Real Unix-socket and child-process tests, using fictional notes and fake CLIs."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import pwd
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

import model_client
from model_input import prepare_prompt
from model_protocol import ModelError, REQUEST_LIMIT, RESPONSE_LIMIT, encode_frame, receive_frame
from model_providers import AGY_DENY
from model_worker import ModelServer
from privacy import PrivacyVault
from test_support import load_test_app

app = load_test_app()


FAKE = r'''#!/usr/bin/python3
import json, os, signal, subprocess, sys, time
from pathlib import Path
if 'app-server' in sys.argv:
    sys.path.insert(0, FIXTURE_DIRECTORY)
    from fake_app_server import serve
    serve(CAPTURE)
    sys.exit(0)
args = sys.argv[1:]
agy = '--input-format' in args
model = args[args.index('--model' if agy else '-m') + 1]
data = sys.stdin.read()
prompt = json.loads(data)['message']['content'] if agy else data
schema = json.loads(Path(args[args.index('--json-schema' if agy else '--output-schema') + 1]).read_text())
home = Path(os.environ['HOME'])
settings = json.loads((home/'.gemini/antigravity-cli/settings.json').read_text()) if agy else {}
record = {'argv':args,'prompt':prompt,'cwd':str(Path.cwd()),'home':str(home),
          'settings':settings,'pid':os.getpid(), 'inherited_secret':os.environ.get('SYNTHETIC_PARENT_SECRET')}
with Path(CAPTURE).open('a') as stream:
    stream.write(json.dumps(record)+'\n')
(home/'history.jsonl').write_text('SYNTHETIC TRANSCRIPT')
token = home/('.gemini/antigravity-cli/antigravity-oauth-token' if agy else 'auth.json')
token.parent.mkdir(parents=True,exist_ok=True)
token.write_text('SYNTHETIC REFRESHED LOGIN')
if model == 'wait':
    signal.signal(signal.SIGTERM,signal.SIG_IGN)
    while True: time.sleep(0.1)
if model == 'nonzero':
    print('SYNTHETIC SECRET DIAGNOSTIC',file=sys.stderr)
    sys.exit(7)
if model == 'flood':
    sys.stdout.write('x'*(5*1024*1024)); sys.exit(0)
if model == 'detached':
    child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],
                           stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                           start_new_session=True)
    Path(CAPTURE+'.helper').write_text(str(child.pid))
value = {'paths':['doc_0001'], 'catalog_only':False} if 'paths' in schema['properties'] else {'answer':'synthetic answer','sources':[{'path':'doc_0001','line':1}]}
if model == 'evidence':
    body = json.loads(prompt.split('\n', 1)[1])
    if 'catalog' in body:
        value = {'paths':[body['catalog'][0]['path']], 'catalog_only':False}
    else:
        note = body['evidence']['notes'][0]
        value = {'answer':'\n'.join(line.split(': ',1)[1] for line in note['lines']),
                 'sources':[{'path':note['path'],'line':1}]}
if model == 'malformed': value = {'answer':[], 'sources':[]}
if agy:
    print(json.dumps({'event':'init','init':{'tools':['read_file']}}))
    if model == 'tool': print(json.dumps({'event':'step_update','step_update':{'step_type':'tool','tool_name':'read_file'}}))
    envelope = {'event':'result','result':{'status':'SUCCESS','structured_output':value}}
    if model == 'unstructured': envelope['result'] = {'status':'SUCCESS','response':'not a structured answer'}
    print(json.dumps(envelope))
else:
    Path(args[args.index('--output-last-message')+1]).write_text(json.dumps(value))
'''


class WorkerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='wiki-provider-tests-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.capture = self.root / 'capture.jsonl'
        self.cli = self.root / 'fake-cli'
        self.cli.write_text(FAKE.replace('CAPTURE', repr(str(self.capture)))
                            .replace('FIXTURE_DIRECTORY', repr(str(Path(__file__).parent))))
        self.cli.chmod(0o700)
        providers = {}
        for name in ('codex','agy'):
            state = self.root / name
            state.mkdir()
            (state/'history.jsonl').write_text('SYNTHETIC PREVIOUS HISTORY')
            providers[name] = {'binary':str(self.cli), 'model':'normal','effort':'medium','state_dir':str(state)}
        self.config = {'socket':str(self.root/'worker.sock'),'allowed_uid':os.getuid(),'providers':providers}
        self.server = ModelServer(self.config)
        self.thread = threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':0.01},daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, provider='codex', purpose='answer', prompt=None, checkpoint=lambda:None, seconds=5, progress=None):
        if prompt is None:
            prompt = prepare_prompt(PrivacyVault(),'Synthetic instructions',
                                    {'question':'synthetic question','history':[],'evidence':{'notes':[]}})
        return model_client.invoke({'provider':provider,'socket':self.config['socket'],'worker_uid':os.getuid()},
                                   prompt,purpose,checkpoint,time.monotonic()+seconds,progress=progress)

    def captures(self):
        return [json.loads(line) for line in self.capture.read_text().splitlines()]

    def test_both_adapters_preserve_prepared_input_and_return_same_contract(self):
        secret='SyntheticProviderSecret9!'
        vault=PrivacyVault()
        vault.learn_value(secret)
        prompt=prepare_prompt(vault,'Synthetic instructions',
            {'question':vault.sanitize('Password: '+secret),'history':[],'evidence':{'notes':[]}})
        os.environ['SYNTHETIC_PARENT_SECRET']='must not inherit'
        self.addCleanup(os.environ.pop,'SYNTHETIC_PARENT_SECRET',None)
        for provider in ('codex','agy'):
            with self.subTest(provider=provider):
                result=self.request(provider,prompt=prompt)
                self.assertEqual(result['answer'],'synthetic answer')
                record=self.captures()[-1]
                self.assertEqual(record['prompt'],prompt)
                self.assertNotIn(secret,record['prompt'])
                self.assertNotIn(prompt,record['argv'])
                self.assertIsNone(record['inherited_secret'])
                if provider == 'codex':
                    self.assertTrue(Path(record['home']).exists())
                    self.server.codex.close()
                self.assertFalse(Path(record['home']).exists())
                self.assertFalse(Path(record['cwd']).exists())
                if provider=='agy':
                    self.assertEqual(record['settings']['permissions']['deny'],AGY_DENY)
                    self.assertIn('--disable-slash-commands',record['argv'])
                state=self.root/provider
                self.assertEqual((state/'history.jsonl').read_text(),'SYNTHETIC PREVIOUS HISTORY')
                token=state/('.gemini/antigravity-cli/antigravity-oauth-token' if provider=='agy' else 'auth.json')
                self.assertEqual(token.read_text(),'SYNTHETIC REFRESHED LOGIN')
                self.assertEqual(token.stat().st_mode & 0o777,0o600)

    def test_both_adapters_support_document_selection(self):
        for provider in ('codex','agy'):
            self.assertEqual(self.request(provider,'select'),{'paths':['doc_0001'],'catalog_only':False})

    def test_full_web_job_masks_both_calls_and_restores_values_only_in_web(self):
        notes = self.root/'notes'
        notes.mkdir()
        secret = 'SyntheticLocalOnlySecret9!'
        (notes/'Garden.md').write_text('# Garden\nPassword: '+secret+'\n')
        for provider in ('codex','agy'):
            with self.subTest(provider=provider):
                self.config['providers'][provider]['model']='evidence'
                job={'id':uuid.uuid4().hex,'owner':'synthetic-owner','status':'running',
                     'created':time.monotonic()}
                ai={'provider':provider,'socket':self.config['socket'],'worker_uid':os.getuid()}
                self.assertTrue(app.LIMIT.acquire(False))
                previews = []
                original_invoke = model_client.invoke
                def tracked_invoke(*args, **kwargs):
                    callback = kwargs.get('progress')
                    if callback:
                        def preview(text):
                            callback(text)
                            previews.append(app.public_job(job))
                        kwargs['progress'] = preview
                    return original_invoke(*args, **kwargs)
                with patch.object(app,'ROOT',notes), patch.dict(app.CONFIG,{'ai':ai}), \
                        patch.object(model_client, 'invoke', tracked_invoke):
                    app.run_job(job,'Show the garden password',[])
                self.assertEqual(job['status'],'completed',job.get('error'))
                self.assertIn(secret,job['answer'])
                self.assertNotIn(secret,job['display_answer'])
                self.assertEqual(job['sources'],[{'path':'Garden.md','line':1}])
                self.assertNotIn(secret,self.capture.read_text())
                self.assertEqual(len(self.captures()[-2:]),2)
                if provider == 'codex':
                    self.assertTrue(previews)
                    for data in previews:
                        self.assertEqual(data['status'], 'running')
                        self.assertNotIn('sources', data)
                        self.assertNotIn('protected_values', data)
                        self.assertNotIn(secret, json.dumps(data))
                        self.assertNotIn('PRIVATE_', data['preview'])
                    self.assertEqual(len({r['pid'] for r in self.captures()[-2:]}), 1)
                    self.assertEqual(len({r['thread_id'] for r in self.captures()[-2:]}), 2)
                    self.server.codex.close()
                for record in self.captures()[-2:]:
                    self.assertNotIn(secret,record['prompt'])
                    self.assertFalse(Path(record['home']).exists())
                self.assertTrue(app.LIMIT.acquire(False))
                app.LIMIT.release()

    def test_slow_connections_are_bounded_and_release_admission(self):
        connections=[]
        try:
            for _ in range(4):
                connection=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
                connection.connect(self.config['socket'])
                connections.append(connection)
            end=time.monotonic()+1
            while self.server.connections._value and time.monotonic()<end:
                time.sleep(0.01)
            self.assertEqual(self.server.connections._value,0)
            with self.assertRaises(ModelError): self.request()
            self.assertFalse(self.capture.exists())
        finally:
            for connection in connections: connection.close()
        end=time.monotonic()+1
        while self.server.connections._value!=4 and time.monotonic()<end:
            time.sleep(0.01)
        self.assertEqual(self.request()['answer'],'synthetic answer')

    def test_raw_or_newly_sensitive_input_never_reaches_worker(self):
        vault=PrivacyVault()
        prompt=prepare_prompt(vault,'Synthetic instructions',
            {'question':'SyntheticNewSecret9!','history':[],'evidence':{'notes':[]}})
        vault.learn_value('SyntheticNewSecret9!')
        for value in ['raw text',prompt]:
            with self.assertRaises(ValueError): self.request(prompt=value)
        self.assertFalse(self.capture.exists())

    def test_peer_identity_mismatch_blocks_execution(self):
        self.config['allowed_uid']=os.getuid()+10000
        with self.assertRaises(ModelError): self.request()
        self.assertFalse(self.capture.exists())

    def test_rejects_caller_supplied_command_and_unknown_provider_without_fallback(self):
        for changed in [{'binary':'/usr/bin/false'},{'provider':'unknown'}]:
            request={'version':1,'provider':'codex','purpose':'answer','prompt':'synthetic','timeout':2,**changed}
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
                connection.settimeout(1)
                connection.connect(self.config['socket'])
                connection.sendall(encode_frame(request,REQUEST_LIMIT))
                response=receive_frame(connection,RESPONSE_LIMIT)
            self.assertIs(response['ok'],False)
        self.assertFalse(self.capture.exists())

    def test_invalid_provider_output_fails_closed_without_diagnostics_or_fallback(self):
        for provider,mode,code in [('codex','nonzero','provider_exit'),('codex','malformed','invalid_output'),
                              ('agy','nonzero','provider_exit'),('agy','malformed','invalid_output'),
                              ('agy','unstructured','invalid_output'),('agy','tool','tool_attempt'),
                              ('agy','flood','output_limit')]:
            with self.subTest(provider=provider,mode=mode):
                self.config['providers'][provider]['model']=mode
                before=len(self.captures()) if self.capture.exists() else 0
                with self.assertLogs('wiki-model',level='INFO') as log, self.assertRaises(ModelError) as error:
                    self.request(provider)
                self.assertEqual(error.exception.code,code)
                self.assertNotIn('SYNTHETIC SECRET',str(error.exception))
                self.assertEqual(len(log.records),1)
                logged=log.records[0].getMessage()
                event=json.loads(logged)
                self.assertEqual(set(event),{'event','provider','purpose','status','code','elapsed_ms'})
                self.assertEqual(event['code'],code)
                self.assertIsInstance(event['elapsed_ms'],int)
                self.assertNotIn('synthetic question',logged)
                self.assertNotIn('SYNTHETIC SECRET',logged)
                self.assertEqual(len(self.captures()),before+1)
                self.assertFalse(Path(self.captures()[-1]['home']).exists())

    def test_cancellation_kills_stubborn_child_and_cleans_profile_before_return(self):
        self.config['providers']['agy']['model']='wait'
        class Cancelled(Exception): pass
        def cancel_when_started():
            if self.capture.exists(): raise Cancelled()
        started=time.monotonic()
        with self.assertRaises(Cancelled): self.request('agy',checkpoint=cancel_when_started)
        self.assertLess(time.monotonic()-started,3)
        record=self.captures()[-1]
        with self.assertRaises(ProcessLookupError): os.kill(record['pid'],0)
        self.assertFalse(Path(record['home']).exists())
        self.config['providers']['agy']['model']='normal'
        self.assertEqual(self.request('agy')['answer'],'synthetic answer')

    def test_worker_deadline_kills_child_even_if_client_does_not_cancel(self):
        self.config['providers']['codex']['model']='wait'
        with self.assertRaises(ModelError): self.request(seconds=0.25)
        record=self.captures()[-1]
        with self.assertRaises(ProcessLookupError): os.kill(record['pid'],0)
        self.assertFalse(Path(record['home']).exists())

    def test_detached_helper_cannot_outlive_the_invocation(self):
        self.config['providers']['agy']['model']='detached'
        self.request('agy')
        pid=int(Path(str(self.capture)+'.helper').read_text())
        with self.assertRaises(ProcessLookupError): os.kill(pid,0)

    def test_codex_reuses_process_but_never_conversation_and_streams_before_result(self):
        self.config['providers']['codex']['model'] = 'stream'
        previews = []
        result = self.request(progress=lambda text: previews.append(text))
        self.assertTrue(previews)
        self.assertNotEqual(previews[0], result['answer'])
        self.request()
        records = self.captures()
        self.assertEqual(records[0]['pid'], records[1]['pid'])
        self.assertNotEqual(records[0]['thread_id'], records[1]['thread_id'])
        self.assertTrue(all(r['thread']['ephemeral'] for r in records))
        self.assertTrue(self.server.codex.metrics['reused'])
        self.server.codex.last_used = time.monotonic() - 301
        self.server.service_actions()
        self.assertIsNone(self.server.codex.proc)
        self.assertFalse(Path(records[-1]['home']).exists())

    def test_codex_cancellation_discards_process_then_next_request_starts_clean(self):
        self.config['providers']['codex']['model'] = 'wait'
        def cancel():
            if self.capture.exists(): raise InterruptedError()
        with self.assertRaises(InterruptedError): self.request(checkpoint=cancel)
        self.assertIsNone(self.server.codex.proc)
        self.config['providers']['codex']['model'] = 'normal'
        self.assertEqual(self.request()['answer'], 'synthetic answer')

    def test_codex_tool_request_is_rejected_without_approval_or_replay(self):
        self.config['providers']['codex']['model'] = 'tool'
        with self.assertRaises(ModelError) as error: self.request()
        self.assertEqual(error.exception.code, 'tool_attempt')
        self.assertEqual(len(self.captures()), 1)
        self.assertIsNone(self.server.codex.proc)

    def test_worker_sigterm_removes_idle_runtime_and_retains_refreshed_login(self):
        config = {'socket': str(self.root / 'process.sock'), 'allowed_user': pwd.getpwuid(os.getuid()).pw_name,
                  'providers': self.config['providers']}
        path = self.root / 'worker.json'; path.write_text(json.dumps(config))
        worker = subprocess.Popen([sys.executable, '-B', str(Path(__file__).parents[1] / 'backend/model_worker.py'),
                                   '--config', str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            until = time.monotonic() + 3
            while not Path(config['socket']).exists() and time.monotonic() < until:
                time.sleep(.02)
            prompt = prepare_prompt(PrivacyVault(), 'Synthetic instructions',
                                   {'question': 'synthetic', 'history': [], 'evidence': {'notes': []}})
            model_client.invoke({'provider': 'codex', 'socket': config['socket'], 'worker_uid': os.getuid()},
                                prompt, 'answer', lambda: None, time.monotonic() + 5)
            record = self.captures()[-1]
            self.assertTrue(Path(record['home']).exists())
            worker.terminate(); worker.wait(timeout=3)
            self.assertEqual(worker.returncode, 0)
            self.assertFalse(Path(record['home']).exists())
            with self.assertRaises(ProcessLookupError): os.kill(record['pid'], 0)
            self.assertEqual((self.root / 'codex/auth.json').read_text(), 'SYNTHETIC REFRESHED LOGIN')
        finally:
            if worker.poll() is None:
                worker.kill(); worker.wait(timeout=3)

    def test_oversized_frame_is_rejected_before_body_or_execution(self):
        import struct
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
            connection.settimeout(1)
            connection.connect(self.config['socket'])
            connection.sendall(struct.pack('!I',REQUEST_LIMIT+1))
            self.assertFalse(receive_frame(connection,RESPONSE_LIMIT)['ok'])
        self.assertFalse(self.capture.exists())


if __name__=='__main__': unittest.main()
