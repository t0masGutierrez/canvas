"""Connect the local CLI to one Chrome extension, with no network listener."""
import os
import queue
import socket
import sys
import threading
import uuid

from . import bridge_directory
from .protocol import read_message, write_message


def serve(base_url, extension_id, caller_origin, incoming=None, outgoing=None):
    if caller_origin.rstrip('/') != f'chrome-extension://{extension_id}':
        return 2
    incoming = incoming or sys.stdin.buffer
    outgoing = outgoing or sys.stdout.buffer
    directory = bridge_directory()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_uid != os.getuid():
        return 2
    directory.chmod(0o700)
    path = directory / f'{os.getpid()}-{uuid.uuid4().hex[:8]}.sock'
    allowed_url = base_url.rstrip('/') + '/'
    stopped = threading.Event()
    pending, pending_lock, output_lock = {}, threading.Lock(), threading.Lock()

    def receive():
        try:
            while not stopped.is_set():
                message = read_message(incoming)
                with pending_lock:
                    target = pending.get(message.get('id'))
                if target is not None:
                    try:
                        target.put_nowait(message)
                    except queue.Full:
                        pass
        except (EOFError, OSError, ValueError, TypeError):
            pass
        finally:
            stopped.set()

    def handle(client):
        request_id = uuid.uuid4().hex
        try:
            with client:
                client.settimeout(5)
                with client.makefile('rwb') as stream:
                    request = read_message(stream)
                    if request.get('url') != allowed_url:
                        write_message(stream, {'error': 'Website is not allowed'})
                        return
                    replies = queue.Queue(maxsize=1)
                    with pending_lock:
                        pending[request_id] = replies
                    with output_lock:
                        write_message(outgoing, {'id': request_id, 'type': 'cookies',
                                                 'url': allowed_url})
                    try:
                        result = replies.get(timeout=3)
                    except queue.Empty:
                        result = {'error': 'Chrome did not reply'}
                    write_message(stream, {'cookies': result.get('cookies', [])})
        except (EOFError, OSError, ValueError):
            pass
        finally:
            with pending_lock:
                pending.pop(request_id, None)

    try:
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(path))
            path.chmod(0o600)
            server.listen(8)
            server.settimeout(0.25)
            write_message(outgoing, {'type': 'ready'})
            threading.Thread(target=receive, daemon=True).start()
            while not stopped.is_set():
                try:
                    client, _ = server.accept()
                except socket.timeout:
                    continue
                threading.Thread(target=handle, args=(client,), daemon=True).start()
    finally:
        path.unlink(missing_ok=True)
    return 0
