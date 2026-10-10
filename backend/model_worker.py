"""Single-flight model broker on a private Unix socket, under a separate OS user."""
import argparse
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import pwd
import select
import signal
import socket
import socketserver
import stat
import threading
import time

from model_protocol import ModelError, PROTOCOL_VERSION, REQUEST_LIMIT, RESPONSE_LIMIT
from model_protocol import encode_frame, peer_uid, receive_frame
from model_protocol import failure_code
from model_providers import enable_subreaper, run_provider
from model_schemas import SCHEMAS
from model_codex import CodexSession

LOG = logging.getLogger('wiki-model')


def load_worker_config(path):
    config = json.loads(Path(path).read_text())
    if type(config) is not dict or set(config) != {'socket', 'allowed_user', 'providers'}:
        raise ValueError('Invalid model worker configuration')
    if type(config['socket']) is not str or not Path(config['socket']).is_absolute():
        raise ValueError('Model socket must be absolute')
    config['allowed_uid'] = pwd.getpwnam(config['allowed_user']).pw_uid
    providers = config['providers']
    if type(providers) is not dict or not providers or not set(providers) <= {'codex', 'agy'}:
        raise ValueError('Invalid model providers')
    for settings in providers.values():
        if type(settings) is not dict or set(settings) != {'binary', 'model', 'effort', 'state_dir'}:
            raise ValueError('Invalid model provider settings')
        if not all(type(settings[key]) is str and settings[key].strip() for key in settings):
            raise ValueError('Model provider settings must be nonempty strings')
        if not Path(settings['binary']).is_absolute() or not Path(settings['state_dir']).is_absolute():
            raise ValueError('Model provider paths must be absolute')
        if settings['effort'] not in {'low', 'medium', 'high', 'xhigh', 'max'}:
            raise ValueError('Invalid model effort')
    return config


def validate_request(request, providers):
    if (set(request) != {'version', 'provider', 'purpose', 'prompt', 'timeout'}
            or type(request['version']) is not int or request['version'] != PROTOCOL_VERSION
            or type(request['provider']) is not str or request['provider'] not in providers
            or type(request['purpose']) is not str or request['purpose'] not in SCHEMAS
            or type(request['prompt']) is not str or not request['prompt']
            or type(request['timeout']) not in (int, float) or not math.isfinite(request['timeout'])
            or not 0 < request['timeout'] <= 600):
        raise ModelError('Invalid model request')


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        connection = self.request
        connection.settimeout(0.2)
        if peer_uid(connection) != self.server.config['allowed_uid']:
            return
        acquired = False
        locked = False
        started = time.monotonic()
        provider = purpose = None
        code = None
        metrics = {}
        try:
            # Bound incomplete requests as well as model execution.
            deadline = time.monotonic() + 5
            def checkpoint():
                if self.server.stopping.is_set():
                    raise InterruptedError('Worker stopping')
                if time.monotonic() >= deadline:
                    raise TimeoutError('Model deadline')
            request = receive_frame(connection, REQUEST_LIMIT, checkpoint)
            validate_request(request, self.server.config['providers'])
            provider, purpose = request['provider'], request['purpose']
            acquired = self.server.slot.acquire(False)
            if not acquired:
                raise ModelError('Worker busy', code='busy')
            try:
                fcntl.flock(self.server.activity, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except BlockingIOError:
                raise ModelError('Worker maintenance', code='busy') from None
            deadline = time.monotonic() + request['timeout']
            def running_checkpoint():
                checkpoint()
                if self.server.stopping.is_set():
                    raise InterruptedError('Worker stopping')
                if select.select([connection], [], [], 0)[0]:
                    # A closed write side or additional data cancels this one-shot request.
                    raise InterruptedError('Caller disconnected')
            def progress(text):
                running_checkpoint()
                connection.sendall(encode_frame({'preview': text}, RESPONSE_LIMIT))
            settings = self.server.config['providers'][provider]
            if provider == 'codex':
                result = self.server.codex.invoke(settings, request['prompt'], purpose, running_checkpoint, progress)
                metrics = self.server.codex.metrics
            else:
                result = run_provider(provider, settings, request['prompt'], purpose, running_checkpoint)
            response = {'ok': True, 'result': result}
        except Exception as error:
            # Never send exception text, process output, prompts, or login diagnostics back.
            code = failure_code(error)
            response = {'ok': False, 'error': code}
        finally:
            if locked:
                fcntl.flock(self.server.activity, fcntl.LOCK_UN)
            if acquired:
                self.server.slot.release()
        # Fixed enums and elapsed time only: no prompts, note names, output or exception text.
        if provider is not None:
            LOG.info(json.dumps({'event': 'wiki_model_request', 'provider': provider, 'purpose': purpose,
                                 'status': 'failed' if code else 'completed', 'code': code,
                                 'elapsed_ms': round((time.monotonic() - started) * 1000), **metrics}))
        try:
            connection.sendall(encode_frame(response, RESPONSE_LIMIT))
        except (OSError, ModelError):
            pass


class ModelServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    request_queue_size = 4

    def __init__(self, config):
        enable_subreaper()
        self.config = config
        self.slot = threading.BoundedSemaphore(1)
        self.connections = threading.BoundedSemaphore(4)
        self.codex = CodexSession()
        self.stopping = threading.Event()
        # The directory is private to the worker/group. Never unlink other file types.
        path = Path(config['socket'])
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError('Unexpected existing model socket')
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(str(path))
            except ConnectionRefusedError:
                path.unlink()
            else:
                raise ValueError('Model socket is already in use')
            finally:
                probe.close()
        super().__init__(str(path), Handler)
        self.activity = os.open(path.with_name('activity.lock'),
                                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o640)
        os.chmod(path, 0o660)

    def verify_request(self, request, address):
        try:
            return peer_uid(request) == self.config['allowed_uid']
        except OSError:
            return False

    def process_request(self, request, address):
        # Slow/incomplete callers cannot create an unbounded number of threads.
        if not self.connections.acquire(False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.connections.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.connections.release()

    def handle_error(self, request, address):
        # socketserver's default traceback logger could reveal request-related data.
        pass

    def service_actions(self):
        if self.slot.acquire(False):
            try:
                self.codex.expire()
            finally:
                self.slot.release()

    def server_close(self):
        self.stopping.set()
        super().server_close()
        with self.slot:
            self.codex.close()
        os.close(self.activity)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    config = load_worker_config(args.config)
    with ModelServer(config) as server:
        def stop(signum, frame):
            server.stopping.set()
            raise SystemExit(0)
        signal.signal(signal.SIGTERM, stop)
        server.serve_forever(poll_interval=0.2)


if __name__ == '__main__':
    main()
