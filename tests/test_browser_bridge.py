"""Check live Chrome messages without a browser or real account."""
import io
import json
import os
from pathlib import Path
import select
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / 'src'
sys.path.insert(0, str(SOURCE))
from canvas_browser import canvas_state, live_states
from canvas_browser.__main__ import prepare
from canvas_browser.protocol import read_message, write_message


COOKIE = {'name': 'canvas_session', 'value': 'test-session',
          'domain': 'school.test', 'path': '/', 'expires': -1,
          'secure': True, 'httpOnly': True, 'sameSite': 'Lax'}


class MessageTests(unittest.TestCase):
    def test_partial_reads_preserve_a_complete_message(self):
        class SmallReads(io.BytesIO):
            def read(self, size=-1):
                return super().read(min(size, 1))
        self.assertEqual(read_message(SmallReads(b'\x07\x00\x00\x00{"a":1}')), {'a': 1})

    def test_large_declared_message_is_rejected_before_reading_its_body(self):
        with self.assertRaises(ValueError):
            read_message(io.BytesIO(struct.pack('=I', 1048577)))

    def test_message_length_counts_bytes_for_non_ascii_text(self):
        stream = io.BytesIO()
        write_message(stream, {'value': 'ñ'})
        header, body = stream.getvalue()[:4], stream.getvalue()[4:]
        self.assertEqual(struct.unpack('=I', header)[0], len(body))
        self.assertEqual(json.loads(body), {'value': 'ñ'})

    def test_cookie_from_another_website_is_not_imported(self):
        self.assertIsNone(canvas_state([dict(COOKIE, domain='other.test')], 'https://school.test'))
        self.assertIsNone(canvas_state([dict(COOKIE, domain='.evil-school.test')], 'https://school.test'))

    def test_only_matching_cookies_are_kept(self):
        state = canvas_state([COOKIE, dict(COOKIE, domain='other.test')], 'https://school.test')
        self.assertEqual(state, {'cookies': [COOKIE], 'origins': []})

    def test_invalid_cookie_data_is_rejected(self):
        for cookies in (None, {}, [None], [dict(COOKIE, domain=None)],
                        [dict(COOKIE, expires=float('nan'))], [dict(COOKIE, value=None)]):
            with self.subTest(cookies=cookies):
                self.assertIsNone(canvas_state(cookies, 'https://school.test'))


class HostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / 'sockets'
        self.env = dict(os.environ, PYTHONPATH=str(SOURCE),
                        CANVAS_CHROME_BRIDGE_DIR=str(self.directory))
        self.host = subprocess.Popen([
            sys.executable, '-m', 'canvas_browser', 'host',
            '--base-url', 'https://school.test', '--extension-id', 'a' * 32,
            'chrome-extension://' + 'a' * 32 + '/',
        ], env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.stop_host)
        self.assertEqual(self.receive(), {'type': 'ready'})

    def stop_host(self):
        if self.host.poll() is None:
            self.host.stdin.close()
            try:
                self.host.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.host.kill()
                self.host.wait()
        for stream in (self.host.stdin, self.host.stdout, self.host.stderr):
            stream.close()

    def receive(self):
        self.assertTrue(select.select([self.host.stdout], [], [], 3)[0], 'Host did not reply')
        header = self.host.stdout.read(4)
        self.assertEqual(len(header), 4)
        size = struct.unpack('=I', header)[0]
        return json.loads(self.host.stdout.read(size))

    def test_cli_gets_the_current_chrome_reply_through_a_private_socket(self):
        result = []
        with patch.dict(os.environ, CANVAS_CHROME_BRIDGE_DIR=str(self.directory)):
            reader = threading.Thread(target=lambda: result.extend(live_states('https://school.test')))
            reader.start()
            request = self.receive()
            self.assertEqual(request['url'], 'https://school.test/')
            self.assertEqual(request['type'], 'cookies')
            response = json.dumps({'id': request['id'], 'cookies': [COOKIE]}).encode()
            self.host.stdin.write(struct.pack('=I', len(response)) + response)
            self.host.stdin.flush()
            reader.join(6)
        self.assertFalse(reader.is_alive())
        self.assertEqual(result, [{'cookies': [COOKIE], 'origins': []}])
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(next(self.directory.glob('*.sock')).stat().st_mode & 0o777, 0o600)

    def test_other_websites_are_rejected_before_asking_chrome(self):
        with patch.dict(os.environ, CANVAS_CHROME_BRIDGE_DIR=str(self.directory)):
            self.assertEqual(list(live_states('https://other.test')), [])
        self.assertFalse(select.select([self.host.stdout], [], [], 0.05)[0])

    def test_closing_chrome_removes_the_socket(self):
        self.host.stdin.close()
        self.assertEqual(self.host.wait(timeout=3), 0)
        self.assertEqual(list(self.directory.glob('*.sock')), [])

    def test_another_extension_cannot_start_the_host(self):
        result = subprocess.run([
            sys.executable, '-m', 'canvas_browser', 'host', '--base-url', 'https://school.test',
            '--extension-id', 'a' * 32, 'chrome-extension://' + 'b' * 32 + '/',
        ], env=self.env, capture_output=True, timeout=3)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, b'')
        self.assertEqual(len(list(self.directory.glob('*.sock'))), 1)


class SetupTests(unittest.TestCase):
    def test_preparation_limits_access_and_keeps_the_extension_id_on_update(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'helper with spaces'
            first = prepare('https://school.test', path)
            second = prepare('https://school.test', path)
            self.assertEqual(first['extension_id'], second['extension_id'])
            manifest = json.loads((Path(first['extension']) / 'manifest.json').read_text())
            self.assertEqual(manifest['host_permissions'], ['https://school.test/*'])
            self.assertNotIn('<all_urls>', manifest['host_permissions'])
            native = json.loads(Path(first['host_manifest']).read_text())
            self.assertEqual(native['allowed_origins'],
                             ['chrome-extension://' + first['extension_id'] + '/'])
            # Preparation does not register the helper with Chrome.
            self.assertEqual(set(item.name for item in path.iterdir()),
                             {'extension', 'canvas-chrome-host', 'com.canvas.cli.json'})


if __name__ == '__main__':
    unittest.main()
