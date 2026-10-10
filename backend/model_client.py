"""Send only an audited prompt to the independently isolated model worker."""
import os
import socket
import stat
import time

from model_input import PreparedPrompt
from model_protocol import ModelError, PROTOCOL_VERSION, REQUEST_LIMIT, RESPONSE_LIMIT
from model_protocol import encode_frame, peer_uid, receive_frame
from model_protocol import ERROR_MESSAGES


def invoke(config, prompt, purpose, checkpoint, deadline, progress=None):
    checkpoint()
    if not isinstance(prompt, PreparedPrompt):
        raise ModelError('검증되지 않은 모델 입력을 차단했습니다.')
    prompt.validate()
    request = {'version': PROTOCOL_VERSION, 'provider': config['provider'], 'purpose': purpose,
               'prompt': str(prompt), 'timeout': min(600.0, max(0.01, deadline - time.monotonic()))}
    payload = encode_frame(request, REQUEST_LIMIT)
    try:
        info = os.lstat(config['socket'])
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != config['worker_uid']:
            raise ModelError('Unexpected model socket owner')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(0.25)
            connection.connect(config['socket'])
            if peer_uid(connection) != config['worker_uid']:
                raise ModelError('Unexpected model worker identity')
            try:
                # Check again immediately before transmission; never serialize the vault.
                checkpoint()
                prompt.validate()
                offset = 0
                while offset < len(payload):
                    checkpoint()
                    try:
                        sent = connection.send(payload[offset:offset + 65536])
                    except socket.timeout:
                        continue
                    if not sent:
                        raise ModelError('Model connection closed')
                    offset += sent
                previews = 0
                while True:
                    response = receive_frame(connection, RESPONSE_LIMIT, checkpoint)
                    if set(response) != {'preview'}:
                        break
                    previews += 1
                    if (purpose != 'answer' or type(response['preview']) is not str
                            or len(response['preview'].encode()) > 64000 or previews > 7500):
                        raise ModelError('Invalid model preview')
                    if progress:
                        progress(response['preview'])
                checkpoint()
            except BaseException:
                # EOF cancels the remote process. Allow its process group to stop before
                # the web job releases its admission slot; never retry the model request.
                try:
                    connection.shutdown(socket.SHUT_WR)
                    until = time.monotonic() + 3
                    def cleanup_deadline():
                        if time.monotonic() > until:
                            raise TimeoutError()
                    while 'preview' in receive_frame(connection, RESPONSE_LIMIT, cleanup_deadline):
                        pass
                except Exception:
                    pass
                raise
        if response.get('ok') is False and set(response) == {'ok', 'error'}:
            code = response['error']
            if type(code) is not str or code not in ERROR_MESSAGES:
                code = 'model_unavailable'
            raise ModelError(ERROR_MESSAGES[code], code=code)
        if response.get('ok') is not True or set(response) != {'ok', 'result'}:
            raise ModelError('Model request failed')
        return response['result']
    except ModelError as exc:
        raise ModelError(ERROR_MESSAGES[exc.code], code=exc.code) from exc
    except (InterruptedError, TimeoutError):
        raise
    except OSError as exc:
        raise ModelError(ERROR_MESSAGES['model_unavailable']) from exc
