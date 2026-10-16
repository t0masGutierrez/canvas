"""Small, bounded JSON messages for Chrome and local sockets."""
import json
import struct

MAX_MESSAGE_BYTES = 1024 * 1024


def read_exact(stream, size):
    parts = []
    while size:
        data = stream.read(size)
        if not data:
            raise EOFError
        parts.append(data)
        size -= len(data)
    return b''.join(parts)


def read_message(stream):
    size = struct.unpack('=I', read_exact(stream, 4))[0]
    if not 0 < size <= MAX_MESSAGE_BYTES:
        raise ValueError('Invalid message size')
    message = json.loads(read_exact(stream, size))
    if not isinstance(message, dict):
        raise ValueError('Expected a JSON object')
    return message


def write_message(stream, message):
    data = json.dumps(message, allow_nan=False, separators=(',', ':')).encode()
    if len(data) > MAX_MESSAGE_BYTES:
        raise ValueError('Message too large')
    stream.write(struct.pack('=I', len(data)) + data)
    stream.flush()
