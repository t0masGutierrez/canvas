"""Exercise Chrome sign-in with a cookie database and a fixed Canvas reply."""
import contextlib
import io
import json
import os
from pathlib import Path
import plistlib
import sqlite3
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import canvas as c
from test_agent import Request, Response


class ChromeLoginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.apps = self.root / 'Applications' / 'Chrome Apps.localized'
        self.app = self.write_app('Canvas', 'https://school.test/')
        self.state = self.root / 'state.json'
        self.state.write_text('{"cookies": [], "origins": []}')
        self.original_state = self.state.read_bytes()
        self.db = self.root / 'Chrome' / 'Default' / 'Cookies'
        self.db.parent.mkdir(parents=True)
        with contextlib.closing(sqlite3.connect(self.db)) as db, db:
            db.execute('create table meta (key text, value text)')
            db.execute("insert into meta values ('version', '24')")
            db.execute('''create table cookies (
                host_key text, name text, value text, encrypted_value blob,
                path text, expires_utc integer, is_secure integer,
                is_httponly integer, samesite integer)''')
            db.execute("insert into cookies values (?, ?, ?, ?, '/', 0, 1, 1, 1)",
                       ('school.test', 'canvas_session', 'expired-test-session', b''))
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({
            'base_url': 'https://school.test',
            'chrome_user_data_dir': str(self.root / 'Chrome'),
            'chrome_login_timeout_seconds': 30,
        }))
        self.opened = []
        self.requests = []
        self.sign_in_on_open = False
        self.open_returncode = 0
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self.stack.enter_context(patch.dict(os.environ,
            CANVAS_CHROME_BRIDGE_DIR=str(self.root / 'bridge')))
        self.stack.enter_context(patch.object(c, 'STATE_FILE', self.state))
        self.stack.enter_context(patch.object(c, 'CONFIG_FILE', self.config))
        self.stack.enter_context(patch.object(c, 'CHROME_APP_DIRS', (self.apps,), create=True))
        self.stack.enter_context(patch.object(c, 'chrome_cookie_decryption_key', return_value=None))
        self.stack.enter_context(patch.object(c.subprocess, 'run', side_effect=self.run_process))
        self.stack.enter_context(patch.object(c, 'sync_playwright', return_value=self))
        self.stack.enter_context(patch.object(c.time, 'monotonic', side_effect=iter(range(0, 300, 30))))
        self.stack.enter_context(patch.object(c.time, 'sleep'))
        self.request = self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def new_context(self, **kwargs):
        self.assertEqual(kwargs['base_url'], 'https://school.test')
        state = json.loads(Path(kwargs['storage_state']).read_text())
        signed_in = any(cookie['name'] == 'canvas_session' and
                        cookie['value'] == 'valid-test-session'
                        for cookie in state['cookies'])
        req = Request({'/api/v1/users/self/profile':
                       Response({'id': 42} if signed_in else {}, 200 if signed_in else 401)})
        self.requests.append(req)
        return req

    def sign_in(self):
        with contextlib.closing(sqlite3.connect(self.db)) as db, db:
            db.execute("update cookies set value='valid-test-session'")

    def write_app(self, name, url, bundle_id='com.google.Chrome.app.' + 'a' * 32):
        app = self.apps / f'{name}.app'
        info = app / 'Contents' / 'Info.plist'
        info.parent.mkdir(parents=True, exist_ok=True)
        info.write_bytes(plistlib.dumps({
            'CFBundleIdentifier': bundle_id,
            'CrAppModeShortcutURL': url,
        }))
        return app

    def run_process(self, args, **kwargs):
        self.assertEqual(args, ['open', '-a', str(self.app), 'https://school.test'])
        self.opened.append(args[-1])
        if self.sign_in_on_open and self.open_returncode == 0:
            self.sign_in()
        return subprocess.CompletedProcess(args, self.open_returncode)

    def invoke(self, *options):
        output, error, code = io.StringIO(), io.StringIO(), 0
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            try:
                c.main(['auth', 'login', '--json', *options])
            except SystemExit as exc:
                code = exc.code
        return code, json.loads(output.getvalue()), error.getvalue()

    def test_valid_chrome_session_is_checked_and_saved_without_opening_browser(self):
        self.sign_in()
        code, result, status = self.invoke()
        self.assertEqual(status, "Authenticating...\n")
        self.assertEqual(code, 0)
        self.assertEqual(result, {'authenticated': True, 'user_id': 42})
        self.assertEqual(self.opened, [])
        self.assertEqual([call[0] for req in self.requests for call in req.calls],
                         ['/api/v1/users/self/profile'])
        self.assertEqual(json.loads(self.state.read_text())['cookies'][0]['value'],
                         'valid-test-session')
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)

    def test_expired_session_opens_canvas_url_in_the_installed_app(self):
        self.sign_in_on_open = True
        code, result, status = self.invoke()
        self.assertEqual(status, "Authenticating...\n")
        self.assertEqual(code, 0)
        self.assertTrue(result['authenticated'])
        self.assertEqual(self.opened, ['https://school.test'])
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(json.loads(self.state.read_text())['cookies'][0]['value'],
                         'valid-test-session')

    def test_login_timeout_keeps_old_state_and_reports_sign_in_required(self):
        code, result, _ = self.invoke()
        self.assertEqual(code, 3)
        self.assertEqual(result['errors'][0]['code'], 'AUTH_REQUIRED')
        self.assertEqual(self.state.read_bytes(), self.original_state)
        self.assertFalse(self.state.with_name('state.json.tmp').exists())

    def test_failed_app_launch_reports_sign_in_required_without_polling(self):
        self.open_returncode = 1
        code, result, _ = self.invoke()
        self.assertEqual(code, 3)
        self.assertEqual(result['errors'][0]['code'], 'AUTH_REQUIRED')
        self.assertEqual(self.opened, ['https://school.test'])
        self.assertEqual(len(self.requests), 1)

    def test_missing_canvas_app_reports_sign_in_required_without_opening_browser(self):
        self.app.rename(self.app.with_suffix('.removed'))
        code, result, _ = self.invoke()
        self.assertEqual(code, 3)
        self.assertEqual(result['errors'][0]['code'], 'AUTH_REQUIRED')
        self.assertIn('Canvas app', result['errors'][0]['message'])
        self.assertEqual(self.opened, [])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.state.read_bytes(), self.original_state)

    def test_another_website_or_browser_is_not_used_for_canvas_sign_in(self):
        for url, bundle_id in [
            ('https://other.test/', 'com.google.Chrome.app.' + 'a' * 32),
            ('https://school.test.evil.test/', 'com.google.Chrome.app.' + 'a' * 32),
            ('http://school.test/', 'com.google.Chrome.app.' + 'a' * 32),
            ('https://school.test:444/', 'com.google.Chrome.app.' + 'a' * 32),
            ('https://school.test/', 'com.apple.Safari.app.' + 'a' * 32),
        ]:
            with self.subTest(url=url, bundle_id=bundle_id):
                self.write_app('Canvas', url, bundle_id)
                code, result, _ = self.invoke()
                self.assertEqual(code, 3)
                self.assertEqual(result['errors'][0]['code'], 'AUTH_REQUIRED')
                self.assertEqual(self.opened, [])

    def test_app_discovery_skips_unreadable_or_invalid_app_records(self):
        bad = self.write_app('A broken app', 'https://school.test/')
        (bad / 'Contents/Info.plist').write_text('invalid plist')
        for name, value in [('B list', []), ('C invalid url', {'CrAppModeShortcutURL': 1}),
                            ('D missing file', None)]:
            app = self.write_app(name, 'https://school.test/')
            info = app / 'Contents/Info.plist'
            if value is None:
                info.unlink()
            else:
                info.write_bytes(plistlib.dumps(value))
        self.sign_in_on_open = True
        code, result, status = self.invoke()
        self.assertEqual(status, "Authenticating...\n")
        self.assertEqual(code, 0)
        self.assertTrue(result['authenticated'])
        self.assertEqual(self.opened, ['https://school.test'])

    def test_no_input_login_does_not_read_chrome_or_open_a_browser(self):
        code, result, _ = self.invoke('--no-input')
        self.assertEqual(code, 2)
        self.assertEqual(result['errors'][0]['code'], 'INVALID_USAGE')
        self.assertEqual(self.requests, [])
        self.assertEqual(self.opened, [])

    def test_live_chrome_session_takes_priority_over_an_old_cookie_file(self):
        directory = self.root / 'bridge'
        directory.mkdir(mode=0o700)
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(directory / 'test.sock'))
        server.listen()
        server.settimeout(2)
        seen = []

        def reply():
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                size = struct.unpack('=I', conn.recv(4))[0]
                seen.append(json.loads(conn.recv(size)))
                data = json.dumps({'cookies': [{
                    'name': 'canvas_session', 'value': 'valid-test-session',
                    'domain': 'school.test', 'path': '/', 'expires': -1,
                    'secure': True, 'httpOnly': True, 'sameSite': 'Lax',
                }]}).encode()
                conn.sendall(struct.pack('=I', len(data)) + data)

        thread = threading.Thread(target=reply, daemon=True)
        thread.start()
        try:
            with patch.dict(os.environ, CANVAS_CHROME_BRIDGE_DIR=str(directory)):
                code, result, _ = self.invoke()
        finally:
            server.close()
            thread.join(3)
        self.assertEqual(code, 0)
        self.assertTrue(result['authenticated'])
        self.assertEqual(seen, [{'url': 'https://school.test/'}])
        self.assertEqual(self.opened, [])
        self.assertEqual(json.loads(self.state.read_text())['cookies'][0]['value'],
                         'valid-test-session')


if __name__ == '__main__':
    unittest.main()
