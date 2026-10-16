"""Checks for complete command information and ordinary terminal output."""
import contextlib
import io
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import canvas


class TerminalOutput(io.StringIO):
    def isatty(self):
        return True


class CommandOutputTests(unittest.TestCase):
    def test_capabilities_shows_commands_formats_content_and_limits(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            canvas.main(['capabilities'])
        text = output.getvalue()
        for detail in ['courses', 'assignments', 'quizzes', 'inbox',
                       '--json', '--agent', 'submission', 'External services',
                       'sign-in required']:
            with self.subTest(detail=detail):
                self.assertIn(detail, text)

    def test_interactive_output_does_not_resize_the_terminal(self):
        output = TerminalOutput()
        with patch.dict(os.environ, CANVAS_AUTO_RESIZE='1', CANVAS_DEFAULT_COLS='120'), \
                contextlib.redirect_stdout(output):
            canvas.main(['capabilities'])
        self.assertTrue(output.getvalue().strip())
        self.assertNotIn('\x1b[8;', output.getvalue())


if __name__ == '__main__':
    unittest.main()
