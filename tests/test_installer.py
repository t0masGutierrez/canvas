import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
class InstallerTests(unittest.TestCase):
    def test_update_without_readme_removes_old_installed_readme(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source, destination = root / 'new', root / 'installed'
            (source / 'src').mkdir(parents=True)
            (source / 'pyproject.toml').write_text('[project]\nname = "example"\n')
            destination.mkdir()
            (destination / 'README.md').write_text('Old instructions')
            (destination / 'state.json').write_text('keep saved sign-in')
            script = (ROOT / 'install.sh').read_text().rsplit('\nmain "$@"', 1)[0]
            script += '\nrefresh_source "$1" "$2"\n'
            result = subprocess.run(['bash', '-c', script, 'test', str(source), str(destination)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((destination / 'README.md').exists())
            self.assertEqual((destination / 'state.json').read_text(), 'keep saved sign-in')

    def test_launcher_replaces_symlink_without_writing_to_its_target(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = root / 'old-source.sh'
            target.write_text('preserve source')
            launcher = root / 'canvas'
            launcher.symlink_to(target)
            script = (ROOT / 'install.sh').read_text().rsplit('\nmain "$@"', 1)[0]
            script += '\nwrite_launcher "/example/bin/canvas" "$1"\n'
            result = subprocess.run(['bash', '-c', script, 'test', str(launcher)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(target.read_text(), 'preserve source')
            self.assertFalse(launcher.is_symlink())

    def test_archive_path_remains_available_for_cleanup(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=root/'source';(source/'src').mkdir(parents=True)
            archive=root/'source.tar.gz'
            with tarfile.open(archive,'w:gz') as tar:tar.add(source,arcname='canvas')
            script=(ROOT/'install.sh').read_text().rsplit('\nmain "$@"',1)[0]
            script+='\ntmpdir=""\ndownload_source\ntest -n "$tmpdir"\ntest -d "$downloaded_source/src"\n'
            env=dict(os.environ,TMPDIR=str(root),CANVAS_ARCHIVE_URL=archive.as_uri())
            result=subprocess.run(['bash','-c',script],env=env,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)

    def test_launcher_uses_selected_install_path_and_preserves_arguments(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);target=root/'.venv/bin/canvas';target.parent.mkdir(parents=True)
            target.write_text('#!/bin/sh\nprintf "<%s>\\n" "$@"\n');target.chmod(0o755)
            result=subprocess.run(['bash',str(ROOT/'src/canvas_cli.sh'),'one','two words'],env=dict(os.environ,CANVAS_INSTALL_DIR=str(root)),capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(result.stdout,'<one>\n<two words>\n')

if __name__ == "__main__": unittest.main()
