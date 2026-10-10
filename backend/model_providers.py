"""Provider adapters and bounded child execution; never import the notebook server."""
from contextlib import contextmanager
import json
import ctypes
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import tempfile
import time

from model_protocol import ModelError, RESPONSE_LIMIT
from model_schemas import SCHEMAS, validate_result

AGY_DENY = [f'{action}(*)' for action in (
    'read_file', 'write_file', 'read_url', 'execute_url', 'command', 'unsandboxed', 'mcp')]
CAPTURE_LIMIT = 4 * RESPONSE_LIMIT
CREDENTIALS = {'codex': ('auth.json',),
               'agy': ('.gemini/antigravity-cli/antigravity-oauth-token',
                       '.gemini/antigravity-cli/installation_id')}


def enable_subreaper():
    # Detached helper processes must be reparented here instead of escaping to PID 1.
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0):  # PR_SET_CHILD_SUBREAPER
        raise OSError('Cannot contain model descendants')


def child_pids():
    children = set()
    for path in Path('/proc/self/task').glob('*/children'):
        try:
            children.update(int(value) for value in path.read_text().split())
        except FileNotFoundError:
            pass
    return children


def reap_helpers(previous):
    # The worker admits one invocation at a time. Preserve any pre-existing children.
    for _ in range(100):
        children = child_pids() - previous
        if not children:
            return
        for pid in children:
            try:
                handle = os.pidfd_open(pid)
                try:
                    signal.pidfd_send_signal(handle, signal.SIGKILL)
                finally:
                    os.close(handle)
                os.waitpid(pid, os.WNOHANG)
            except (ProcessLookupError, ChildProcessError):
                pass
        time.sleep(0.01)
    raise ModelError('Model helper cleanup failed')


def codex_command(binary, model, effort, schema, output):
    return [binary, 'exec', '--ignore-user-config', '--ignore-rules',
            '--ephemeral', '--skip-git-repo-check', '--sandbox', 'read-only',
            '--disable', 'shell_tool', '--disable', 'apps', '--disable', 'multi_agent',
            '--disable', 'view_image', '--disable', 'browser_use', '--disable', 'computer_use',
            '--disable', 'code_mode_host', '--disable', 'skill_search', '--disable', 'daemon_auto_start',
            '--enable', 'skip_host_skill_discovery', '-c', 'web_search="disabled"',
            '-c', f'model_reasoning_effort={json.dumps(effort)}', '-m', model,
            '--output-schema', str(schema), '--output-last-message', str(output), '--color', 'never', '-']


def read_regular(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ModelError('Invalid model file')
        result = bytearray()
        while len(result) <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - len(result)))
            if not chunk:
                return bytes(result)
            result.extend(chunk)
        raise ModelError('Model file is too large')
    finally:
        os.close(fd)


def atomic_private_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.credential-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def agy_settings():
    return {'permissions': {'deny': AGY_DENY, 'allow': [], 'ask': []},
            'trustedWorkspaces': []}


def sync_credentials(provider, persistent, home):
    for name in CREDENTIALS[provider]:
        source = home / name
        if source.exists() or source.is_symlink():
            data = read_regular(source, 65536)
            destination = persistent / name
            if not destination.exists() or read_regular(destination, 65536) != data:
                atomic_private_write(destination, data)


@contextmanager
def profile(provider, persistent, scratch):
    """Carry only login material across jobs; settings/history/plugins are never copied.

    Refreshes are copied back atomically. Conversation databases, artifacts, logs,
    and caches live under the temporary HOME and are removed even on cancellation.
    """
    home = scratch / 'home'
    home.mkdir(mode=0o700)
    original = {}
    for name in CREDENTIALS[provider]:
        source = persistent / name
        if source.exists() or source.is_symlink():
            original[name] = read_regular(source, 65536)
            atomic_private_write(home / name, original[name])
    if provider == 'agy':
        settings = home / '.gemini/antigravity-cli/settings.json'
        atomic_private_write(settings, json.dumps(agy_settings()).encode())
    environment = {'HOME': str(home), 'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8',
                   'TMPDIR': str(scratch), 'XDG_CONFIG_HOME': str(home / '.config'),
                   'XDG_CACHE_HOME': str(home / '.cache'), 'XDG_DATA_HOME': str(home / '.local/share')}
    if provider == 'codex':
        environment['CODEX_HOME'] = str(home)
    try:
        yield environment
    finally:
        # Only known credential basenames can be persisted, with no symlink following.
        for name in CREDENTIALS[provider]:
            source = home / name
            if source.exists() or source.is_symlink():
                data = read_regular(source, 65536)
                if data != original.get(name):
                    atomic_private_write(persistent / name, data)


def stop_process(proc):
    if proc is None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass
    # Kill remaining descendants even when the original process exited first.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait(timeout=2)


def execute(args, payload, environment, directory, checkpoint, capture):
    """Drain stdout while writing stdin; bound memory and cancellation latency."""
    proc = None
    previous_children = child_pids()
    output = bytearray()
    try:
        proc = subprocess.Popen(args, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, env=environment, cwd=directory,
                                start_new_session=True, umask=0o077)
        with selectors.DefaultSelector() as selector:
            os.set_blocking(proc.stdin.fileno(), False)
            selector.register(proc.stdin, selectors.EVENT_WRITE, 'input')
            if capture:
                os.set_blocking(proc.stdout.fileno(), False)
                selector.register(proc.stdout, selectors.EVENT_READ, 'output')
            offset = 0
            while selector.get_map() or proc.poll() is None:
                checkpoint()
                for key, events in selector.select(0.05):
                    if key.data == 'input':
                        try:
                            offset += os.write(key.fd, payload[offset:offset + 65536])
                        except BrokenPipeError:
                            offset = len(payload)
                        if offset == len(payload):
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                    else:
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                        else:
                            if len(output) + len(chunk) > CAPTURE_LIMIT:
                                raise ModelError('Model output is too large', code='output_limit')
                            output.extend(chunk)
                if not selector.get_map() and proc.poll() is None:
                    time.sleep(0.05)
            checkpoint()
            if proc.returncode != 0:
                raise ModelError('Model execution failed', code='provider_exit')
        return bytes(output)
    finally:
        try:
            try:
                stop_process(proc)
            finally:
                reap_helpers(previous_children)
        finally:
            if proc:
                for stream in (proc.stdin, proc.stdout):
                    if stream:
                        stream.close()


def decode_agy(data):
    result = None
    for line in data.splitlines():
        event = json.loads(line)
        if type(event) is not dict:
            raise ModelError('Invalid AGY event', code='invalid_output')
        if event.get('event') == 'step_update':
            step = event.get('step_update', {})
            if step.get('step_type') == 'tool':
                raise ModelError('Unexpected model tool operation', code='tool_attempt')
        if event.get('event') == 'result':
            if result is not None:
                raise ModelError('Multiple model results', code='invalid_output')
            result = event.get('result')
    if (type(result) is not dict or result.get('status') != 'SUCCESS'
            or type(result.get('structured_output')) is not dict):
        raise ModelError('AGY did not produce a structured answer', code='invalid_output')
    return result['structured_output']


def run_provider(provider, settings, prompt, purpose, checkpoint):
    checkpoint()
    with tempfile.TemporaryDirectory(prefix='wiki-model-') as temporary:
        scratch = Path(temporary)
        workspace = scratch / 'workspace'
        workspace.mkdir(mode=0o700)
        schema = scratch / 'schema.json'
        schema.write_text(json.dumps(SCHEMAS[purpose]))
        output = scratch / 'answer.json'
        with profile(provider, Path(settings['state_dir']), scratch) as environment:
            if provider == 'codex':
                args = codex_command(settings['binary'], settings['model'], settings['effort'], schema, output)
                execute(args, prompt.encode('utf-8'), environment, workspace, checkpoint, False)
                result = json.loads(read_regular(output, RESPONSE_LIMIT))
            elif provider == 'agy':
                args = [settings['binary'], '--input-format', 'stream-json', '--output-format', 'stream-json',
                        '--json-schema', str(schema), '--model', settings['model'], '--effort', settings['effort'],
                        '--disable-slash-commands', '--mode', 'plan', '--log-file', '/dev/null',
                        '--print-timeout', '10m']
                payload = json.dumps({'event': 'user', 'message': {'content': prompt}}, ensure_ascii=False).encode() + b'\n'
                result = decode_agy(execute(args, payload, environment, workspace, checkpoint, True))
            else:
                raise ModelError('Unknown model provider')
            checkpoint()
            return validate_result(purpose, result)
