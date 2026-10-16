"""Prepare the optional Chrome helper, or run its private local connection."""
import argparse
import base64
import hashlib
from importlib.resources import files
import json
from pathlib import Path
import shlex
import subprocess
import sys
from urllib.parse import urlsplit

from .host import serve


def prepare(base_url, directory):
    parsed = urlsplit(base_url)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError('Use your Canvas website address')
    base_url = f'{parsed.scheme}://{parsed.netloc}'
    directory = Path(directory).resolve()
    extension = directory / 'extension'
    extension.mkdir(parents=True, exist_ok=True)
    manifest_path = extension / 'manifest.json'
    if manifest_path.exists():
        key = json.loads(manifest_path.read_text())['key']
    else:
        # Only the public key is kept, to give this installation a stable ID.
        private = subprocess.run(['openssl', 'genrsa', '2048'], capture_output=True,
                                 check=True, timeout=15).stdout
        public = subprocess.run(['openssl', 'rsa', '-pubout', '-outform', 'DER'],
                                input=private, capture_output=True, check=True, timeout=15).stdout
        key = base64.b64encode(public).decode()
    extension_id = hashlib.sha256(base64.b64decode(key)).hexdigest()[:32].translate(
        str.maketrans('0123456789abcdef', 'abcdefghijklmnop'))
    manifest = {
        'manifest_version': 3, 'name': 'Canvas sign-in helper', 'version': '1.0.0',
        'description': 'Let your Canvas terminal command check your current Chrome sign-in.',
        'key': key, 'permissions': ['cookies', 'nativeMessaging', 'alarms'],
        'host_permissions': [base_url + '/*'],
        'background': {'service_worker': 'background.js', 'type': 'module'},
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
    (extension / 'background.js').write_text(
        files('canvas_browser').joinpath('extension/background.js').read_text())
    launcher = directory / 'canvas-chrome-host'
    launcher.write_text('#!/bin/sh\nexec ' + shlex.join([
        sys.executable, '-m', 'canvas_browser', 'host', '--base-url', base_url,
        '--extension-id', extension_id]) + ' "$@"\n')
    launcher.chmod(0o700)
    host_manifest = directory / 'com.canvas.cli.json'
    host_manifest.write_text(json.dumps({
        'name': 'com.canvas.cli', 'description': 'Canvas terminal sign-in',
        'path': str(launcher), 'type': 'stdio',
        'allowed_origins': [f'chrome-extension://{extension_id}/'],
    }, indent=2) + '\n')
    return {'extension': str(extension), 'host_manifest': str(host_manifest),
            'extension_id': extension_id, 'canvas_url': base_url}


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='command', required=True)
    setup = commands.add_parser('prepare')
    setup.add_argument('--base-url', required=True)
    setup.add_argument('--directory', type=Path, required=True)
    host = commands.add_parser('host')
    host.add_argument('--base-url', required=True)
    host.add_argument('--extension-id', required=True)
    host.add_argument('caller_origin')
    args = parser.parse_args()
    if args.command == 'prepare':
        print(json.dumps(prepare(args.base_url, args.directory), indent=2))
    else:
        raise SystemExit(serve(args.base_url, args.extension_id, args.caller_origin))


if __name__ == '__main__':
    main()
