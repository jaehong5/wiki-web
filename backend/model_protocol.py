"""Small, bounded local protocol. No paths, credentials, or CLI flags cross it."""
import json
import socket
import struct

REQUEST_LIMIT = 2 * 1024 * 1024
RESPONSE_LIMIT = 1024 * 1024
PROTOCOL_VERSION = 2

ERROR_MESSAGES = {
    'model_unavailable': 'AI 응답을 받지 못했습니다. 실행 서비스 상태를 확인해주세요.',
    'busy': 'AI가 다른 요청을 처리 중입니다. 잠시 후 다시 질문해주세요.',
    'timeout': 'AI 처리 시간이 초과됐습니다. 질문 범위를 좁혀 다시 시도해주세요.',
    'cancelled': 'AI 요청이 중단됐습니다.',
    'provider_exit': 'AI 실행기가 오류로 종료됐습니다. 계정 상태와 사용 한도를 확인해주세요.',
    'invalid_output': 'AI 응답이 필요한 형식을 충족하지 못했습니다. 다시 질문해주세요.',
    'tool_attempt': 'AI가 허용되지 않은 도구를 사용하려 해 답변을 중단했습니다.',
    'output_limit': 'AI 응답이 너무 큽니다. 질문 범위를 좁혀주세요.',
    'profile_unavailable': 'AI 실행 설정이나 로그인 파일을 읽지 못했습니다.',
}


class ModelError(ValueError):
    def __init__(self, message, *, code='model_unavailable'):
        super().__init__(message)
        self.code = code if type(code) is str and code in ERROR_MESSAGES else 'model_unavailable'


def failure_code(error):
    if isinstance(error, ModelError):
        return error.code
    if isinstance(error, TimeoutError):
        return 'timeout'
    if isinstance(error, InterruptedError):
        return 'cancelled'
    if isinstance(error, (ValueError, UnicodeError, RecursionError)):
        return 'invalid_output'
    if isinstance(error, OSError):
        return 'profile_unavailable'
    return 'model_unavailable'


def peer_uid(connection):
    return struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]


def encode_frame(value, limit):
    try:
        data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    except (ValueError, UnicodeError, TypeError, RecursionError) as exc:
        raise ModelError('Invalid model message') from exc
    if len(data) > limit:
        raise ModelError('Model message is too large')
    return struct.pack('!I', len(data)) + data


def receive_frame(connection, limit, checkpoint=lambda: None):
    def receive(size):
        result = bytearray()
        while len(result) < size:
            checkpoint()
            try:
                part = connection.recv(min(65536, size - len(result)))
            except socket.timeout:
                continue
            if not part:
                raise ModelError('Model connection closed')
            result.extend(part)
        return bytes(result)
    size = struct.unpack('!I', receive(4))[0]
    if not 0 < size <= limit:
        raise ModelError('Model message is too large')
    try:
        value = json.loads(receive(size).decode('utf-8'))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ModelError('Invalid model message') from exc
    if type(value) is not dict:
        raise ModelError('Invalid model message')
    return value
