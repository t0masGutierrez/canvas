"""Read current Canvas cookies through the optional Chrome helper."""
import math
import os
from pathlib import Path
import socket
import stat
from urllib.parse import urlsplit

from .protocol import read_message, write_message


def bridge_directory():
    return Path(os.environ.get('CANVAS_CHROME_BRIDGE_DIR',
                               Path.home() / '.config/canvas-cli/chrome'))


def canvas_state(cookies, base_url):
    """Accept only cookies that Chrome may send to this Canvas website."""
    host = urlsplit(base_url).hostname
    if not isinstance(cookies, list) or not host:
        return None
    accepted = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        domain = cookie.get('domain', '')
        if not isinstance(domain, str) or not (
                domain == host or (domain.startswith('.') and
                (domain[1:] == host or host.endswith(domain)))):
            continue
        if not all(isinstance(cookie.get(key), str) and cookie[key]
                   for key in ('name', 'value', 'path')):
            continue
        expires = cookie.get('expires', -1)
        if not isinstance(expires, (int, float)) or not math.isfinite(expires):
            continue
        accepted.append({
            'name': cookie['name'], 'value': cookie['value'], 'domain': domain,
            'path': cookie['path'], 'expires': expires,
            'secure': bool(cookie.get('secure')), 'httpOnly': bool(cookie.get('httpOnly')),
            'sameSite': cookie.get('sameSite') if cookie.get('sameSite') in
                        ('None', 'Lax', 'Strict') else 'Lax',
        })
    return {'cookies': accepted, 'origins': []} if accepted else None


def live_states(base_url):
    """Ask each connected Chrome profile, without opening a browser."""
    for path in sorted(bridge_directory().glob('*.sock')):
        try:
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                continue
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(5)
                client.connect(str(path))
                with client.makefile('rwb') as stream:
                    write_message(stream, {'url': base_url.rstrip('/') + '/'})
                    reply = read_message(stream)
            state = canvas_state(reply.get('cookies'), base_url)
            if state:
                yield state
        except (OSError, EOFError, ValueError):
            continue
