import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CANVAS_HELP_FIXTURE = ROOT / "tests/fixtures/canvas.txt"
EXPECTED_AUTH_REQUIRED_MESSAGE = "Authenticating..."
EXPECTED_AUTH_CANCELLED_MESSAGE = "Authentication cancelled."
EXPECTED_INVALID_USAGE_MESSAGE = "Invalid usage, please follow the format: canvas {cmd} course [optional filter]"
INSTALL_SCRIPT = ROOT / "install.sh"
sys.path.insert(0, str(ROOT / "src"))

import canvas

TEST_CANVAS_BASE_URL = "https://school.instructure.com"


class TtyStringIO(io.StringIO):
    def isatty(self):
        return True


class FakeResponse:
    def __init__(self, data, status=200):
        self._data = data
        self.status = status
        self.headers = {}

    def json(self):
        return self._data

    def text(self):
        return ""


class FakeRequest:
    def get(self, endpoint):
        if endpoint.startswith("/api/v1/courses?"):
            return FakeResponse([
                {"id": 1, "course_code": "M 408D", "name": "M 408D — Differential and Integral Calculus"}
            ])
        if endpoint.startswith("/api/v1/users/self/enrollments"):
            return FakeResponse([
                {"course_id": 1, "grades": {"current_score": 95}}
            ])
        if endpoint.startswith("/api/v1/courses/1/assignment_groups"):
            return FakeResponse([
                {
                    "name": "Homework",
                    "assignments": [
                        {"name": "HW 1", "points_possible": 10, "submission": {"score": 9}}
                    ],
                }
            ])
        if endpoint.startswith("/api/v1/courses/1/modules"):
            return FakeResponse([
                {"id": 10, "name": "Week 1: Limits", "position": 2, "items_count": 5, "state": "active"},
                {"id": 9, "name": "Start Here", "position": 1, "items_count": 3, "workflow_state": "active"},
            ])
        if endpoint.startswith("/api/v1/courses/1/pages"):
            return FakeResponse([
                {"title": "Start Here", "html_url": "https://school.instructure.com/courses/1/pages/start-here"},
                {"title": "Syllabus", "html_url": "https://school.instructure.com/courses/1/pages/syllabus"},
            ])
        if endpoint.startswith("/api/v1/courses/1/folders/root"):
            return FakeResponse({"id": "root"})
        if endpoint.startswith("/api/v1/folders/root/folders"):
            return FakeResponse([
                {"id": "week-1", "name": "Week 1"},
            ])
        if endpoint.startswith("/api/v1/folders/root/files"):
            return FakeResponse([
                {"id": 101, "display_name": "Syllabus.pdf"},
            ])
        if endpoint.startswith("/api/v1/folders/week-1/folders"):
            return FakeResponse([])
        if endpoint.startswith("/api/v1/folders/week-1/files"):
            return FakeResponse([
                {"id": 102, "display_name": "Lecture 1.pdf"},
            ])
        raise AssertionError(f"unexpected endpoint: {endpoint}")


class FakePlaywright:
    def __enter__(self):
        self.request = types.SimpleNamespace(new_context=lambda **kwargs: FakeRequest())
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeUnauthorizedRequest:
    def get(self, endpoint):
        return FakeResponse({"status": "unauthenticated"}, status=401)


class FakeCurlRetryRequest:
    def __init__(self, fail_first: bool):
        self.fail_first = fail_first

    def get(self, endpoint):
        if self.fail_first:
            return FakeResponse({"status": "unauthenticated"}, status=401)
        if endpoint.startswith("/api/v1/courses?"):
            return FakeResponse([
                {"id": 1, "course_code": "M 408D", "name": "M 408D — Differential and Integral Calculus"}
            ])
        raise AssertionError(f"unexpected endpoint after cURL cookie refresh: {endpoint}")


class FakeCurlRetryPlaywright:
    def __init__(self):
        self.context_count = 0

    def __enter__(self):
        self.request = types.SimpleNamespace(new_context=self.new_context)
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def new_context(self, **kwargs):
        fail_first = self.context_count == 0
        self.context_count += 1
        return FakeCurlRetryRequest(fail_first=fail_first)


class FakeUnauthorizedPlaywright:
    def __init__(self):
        self.context_count = 0

    def __enter__(self):
        self.request = types.SimpleNamespace(new_context=self.new_context)
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def new_context(self, **kwargs):
        self.context_count += 1
        return FakeUnauthorizedRequest()


    def wait_for_load_state(self, *args, **kwargs):
        return None


class FakeImportRequest:
    def __init__(self):
        self.disposed = False

    def get(self, endpoint, **kwargs):
        if endpoint == "/api/v1/users/self/profile":
            return FakeResponse({"id": 7, "name": "Tomas"})
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    def dispose(self):
        self.disposed = True


class FakeImportPlaywright:
    def __init__(self):
        self.context_kwargs = []
        self.requests = []

    @property
    def request(self):
        return types.SimpleNamespace(new_context=self.new_context)

    def new_context(self, **kwargs):
        self.context_kwargs.append(kwargs)
        req = FakeImportRequest()
        self.requests.append(req)
        return req


class FakeInterruptingImportRequest:
    def __init__(self):
        self.disposed = False

    def get(self, endpoint, **kwargs):
        raise KeyboardInterrupt

    def dispose(self):
        self.disposed = True
        raise KeyboardInterrupt


class FakeInterruptingImportPlaywright:
    def __init__(self):
        self.request_context = FakeInterruptingImportRequest()

    @property
    def request(self):
        return types.SimpleNamespace(new_context=lambda **kwargs: self.request_context)


class CanvasGradesOutputTests(unittest.TestCase):
    def setUp(self):
        canvas.set_canvas_base_url(TEST_CANVAS_BASE_URL)

    def tearDown(self):
        canvas.set_canvas_base_url(TEST_CANVAS_BASE_URL)

    def test_install_script_has_valid_bash_syntax(self):
        self.assertTrue(INSTALL_SCRIPT.exists())
        body = INSTALL_SCRIPT.read_text()
        self.assertIn("set -euo pipefail", body)
        self.assertIn("CANVAS_INSTALL_DIR", body)
        self.assertIn("CANVAS_BIN_DIR", body)
        result = subprocess.run(["bash", "-n", str(INSTALL_SCRIPT)], text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_install_script_installs_from_local_source_into_temp_dirs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            env = {
                **os.environ,
                "CANVAS_SOURCE_DIR": str(ROOT),
                "CANVAS_INSTALL_DIR": str(tmp / "app"),
                "CANVAS_BIN_DIR": str(tmp / "bin"),
                "CANVAS_SKIP_CHROME_CHECK": "1",
                "CANVAS_SKIP_PATH_SETUP": "1",
            }
            result = subprocess.run(["bash", str(INSTALL_SCRIPT)], text=True, capture_output=True, env=env, check=False, timeout=120)

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertTrue((tmp / "app/source/src/canvas.py").exists())
            self.assertTrue((tmp / "app/.venv/bin/canvas").exists())
            launcher = tmp / "bin/canvas"
            self.assertTrue(launcher.exists())

            help_result = subprocess.run([str(launcher), "--help"], text=True, capture_output=True, check=False, timeout=30)
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            self.assertIn("Canvas LMS CLI", help_result.stdout)

    def run_canvas_main(self, argv, stdin=None, stdout=None, stderr=None, extra_patches=()):
        stdout = stdout or io.StringIO()
        stderr = stderr or io.StringIO()
        stdin = stdin or io.StringIO()
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            config_file = Path(tmpdir) / "config.json"
            state_file.write_text("{}")
            with patch.object(canvas, "STATE_FILE", state_file), \
                 patch.object(canvas, "CONFIG_FILE", config_file), \
                 patch.object(canvas, "sync_playwright", lambda: FakePlaywright()), \
                 patch.dict(os.environ, {"CANVAS_AUTO_RESIZE": "0", "CANVAS_AUTO_OPEN_CHROME": "0"}), \
                 patch.object(sys, "stdin", stdin), \
                 patch.object(sys, "stdout", stdout), \
                 contextlib.redirect_stderr(stderr), \
                 contextlib.ExitStack() as stack:
                for manager in extra_patches:
                    stack.enter_context(manager)
                canvas.main(argv)
        return stdout.getvalue(), stderr.getvalue()

    def test_help_output_matches_canvas_txt_fixture(self):
        expected_help = CANVAS_HELP_FIXTURE.read_text()
        stdout = io.StringIO()
        with patch.object(sys, "stdout", stdout):
            with self.assertRaises(SystemExit) as exc:
                canvas.parse_args(["--help"])

        self.assertEqual(exc.exception.code, 0)
        self.assertEqual(expected_help, canvas.HELP_TEXT)
        self.assertEqual(expected_help, stdout.getvalue())

    def test_bare_canvas_command_prints_canvas_txt_fixture(self):
        expected_help = CANVAS_HELP_FIXTURE.read_text()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.object(sys, "stdout", stdout), contextlib.redirect_stderr(stderr):
            canvas.main([])

        self.assertEqual(expected_help, stdout.getvalue())
        self.assertEqual("", stderr.getvalue())

    def test_default_local_paths_are_repo_local_and_gitignored(self):
        self.assertEqual(canvas.STATE_FILE, ROOT / "state.json")
        self.assertEqual(canvas.CANVAS_FILES_ROOT, ROOT / "files")

    def test_setup_command_writes_canvas_institution_to_config(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.json"
            with patch.object(canvas, "CONFIG_FILE", config_file), \
                 patch.object(sys, "stdout", stdout), \
                 contextlib.redirect_stderr(stderr):
                canvas.main(["setup", "school.instructure.com"])
            saved = json.loads(config_file.read_text())
            mode = oct(config_file.stat().st_mode & 0o777)

        self.assertEqual(saved["base_url"], "https://school.instructure.com")
        self.assertEqual(mode, "0o600")
        self.assertEqual(stdout.getvalue(), "Saved Canvas institution: https://school.instructure.com\n")
        self.assertEqual(stderr.getvalue(), "")

    def test_setup_command_can_prompt_for_canvas_institution(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        stdin = io.StringIO("https://canvas.example.edu/dashboard\n")
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.json"
            with patch.object(canvas, "CONFIG_FILE", config_file), \
                 patch.object(sys, "stdin", stdin), \
                 patch.object(sys, "stdout", stdout), \
                 contextlib.redirect_stderr(stderr):
                canvas.main(["setup"])
            saved = json.loads(config_file.read_text())

        self.assertEqual(saved["base_url"], "https://canvas.example.edu")
        self.assertEqual(stdout.getvalue(), "Canvas institution URL: Saved Canvas institution: https://canvas.example.edu\n")
        self.assertEqual(stderr.getvalue(), "")

    def test_saved_canvas_institution_is_used_for_canvas_urls(self):
        opened_urls = []
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.json"
            config_file.write_text(json.dumps({"base_url": "https://canvas.example.edu"}))
            stdout, stderr = self.run_canvas_main(
                ["syllabus", "M 408D"],
                extra_patches=(
                    patch.object(canvas, "CONFIG_FILE", config_file),
                    patch.object(canvas, "open_url", opened_urls.append),
                ),
            )

        self.assertEqual(opened_urls, ["https://canvas.example.edu/courses/1/assignments/syllabus"])
        self.assertEqual(stdout, "Opened https://canvas.example.edu/courses/1/assignments/syllabus\n")
        self.assertEqual(stderr, "")

    def test_missing_course_messages_start_with_capital_please(self):
        self.assertEqual(canvas.missing_course_message("pages"), 'Please specify course, e.g. canvas pages "RHE 306"')
        self.assertEqual(canvas.missing_course_message("files"), 'Please specify course, e.g. canvas files "RHE 306"')

    def test_files_command_syncs_files_and_prints_local_listing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            files_root = Path(tmpdir) / "files"
            stdout, stderr = self.run_canvas_main(
                ["files", "M 408D"],
                extra_patches=(patch.object(canvas, "CANVAS_FILES_ROOT", files_root),),
            )

            course_root = files_root / "M 408D"
            self.assertEqual(stderr, "")
            self.assertTrue((course_root / "Syllabus.pdf").is_symlink())
            self.assertTrue((course_root / "Week 1" / "Lecture 1.pdf").is_symlink())
            self.assertTrue(stdout.startswith(str(course_root) + "\n\n"), stdout)
            self.assertIn("Syllabus.pdf", stdout)
            self.assertIn("Week 1/", stdout)

    def test_files_command_json_prints_machine_readable_sync_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            files_root = Path(tmpdir) / "files"
            stdout, stderr = self.run_canvas_main(
                ["files", "M 408D", "--json"],
                extra_patches=(patch.object(canvas, "CANVAS_FILES_ROOT", files_root),),
            )

        self.assertEqual(stderr, "")
        data = json.loads(stdout)
        self.assertEqual(data["root"], str(files_root / "M 408D"))
        self.assertEqual(data["folders"], 1)
        self.assertEqual(data["files"], 2)
        self.assertEqual(data["linked"], 2)
        self.assertNotIn("Syllabus.pdf", stdout)

    def test_syllabus_command_opens_selected_course_syllabus_page(self):
        opened_urls = []
        stdout, stderr = self.run_canvas_main(
            ["syllabus", "M 408D"],
            extra_patches=(patch.object(canvas, "open_url", opened_urls.append),),
        )

        self.assertEqual(opened_urls, ["https://school.instructure.com/courses/1/assignments/syllabus"])
        self.assertEqual(stdout, "Opened https://school.instructure.com/courses/1/assignments/syllabus\n")
        self.assertEqual(stderr, "")

    def test_syllabus_command_accepts_course_code_without_spaces(self):
        opened_urls = []
        stdout, stderr = self.run_canvas_main(
            ["syllabus", "m408d"],
            extra_patches=(patch.object(canvas, "open_url", opened_urls.append),),
        )

        self.assertEqual(opened_urls, ["https://school.instructure.com/courses/1/assignments/syllabus"])
        self.assertEqual(stdout, "Opened https://school.instructure.com/courses/1/assignments/syllabus\n")
        self.assertEqual(stderr, "")

    def test_modules_command_prints_course_module_overview_table(self):
        stdout, stderr = self.run_canvas_main(["modules", "M 408D"])

        self.assertEqual(stderr, "")
        self.assertIn("2 modules\n\nCourse | Module", stdout)
        self.assertIn("M 408D", stdout)
        self.assertLess(stdout.index("Start Here"), stdout.index("Week 1: Limits"))
        self.assertIn("Items", stdout)
        self.assertIn("State", stdout)
        self.assertIn("3", stdout)
        self.assertIn("active", stdout)

    def test_courses_command_filters_table_when_course_filter_provided(self):
        courses = [
            {"id": 1, "course_code": "M 408D", "name": "M 408D — Differential and Integral Calculus"},
            {"id": 2, "course_code": "M 374M", "name": "M 374M — Mathematical Modeling"},
        ]
        stdout, stderr = self.run_canvas_main(
            ["courses", "m374m"],
            extra_patches=(patch.object(canvas, "fetch_paginated", return_value=courses),),
        )

        self.assertEqual(stderr, "")
        self.assertIn("1 courses\n\nCourse | Section | ID | Instructor", stdout)
        self.assertIn("M 374M", stdout)
        self.assertNotIn("M 408D", stdout)

    def test_ambiguous_course_filter_message_lists_course_codes_only(self):
        courses = [
            {"id": 1441244, "course_code": "M 374M", "name": "M 374M — Mathematical Modeling"},
            {"id": 1441052, "course_code": "M 362K", "name": "M 362K — Probability"},
            {"id": 1441152, "course_code": "M 365C", "name": "M 365C — Real Analysis"},
        ]

        with self.assertRaises(SystemExit) as exc:
            canvas.select_single_course(courses, ["m"], command="files")

        self.assertEqual(str(exc.exception), "Course filter matched multiple active courses: M 374M, M 362K, M 365C")

    def test_assignments_command_accepts_unquoted_course_code_suffix(self):
        courses = [
            {"id": 1, "course_code": "M 408D", "name": "M 408D — Differential and Integral Calculus"},
            {"id": 2, "course_code": "M 374M", "name": "M 374M — Mathematical Modeling"},
        ]

        def fake_fetch_paginated(req, endpoint):
            if endpoint.startswith("/api/v1/courses?"):
                return courses
            if endpoint.startswith("/api/v1/courses/2/assignments?"):
                return [{"name": "Modeling Project", "due_at": "2026-06-01T15:00:00Z", "submission": {}}]
            if endpoint.startswith("/api/v1/courses/1/assignments?"):
                raise AssertionError("assignments command should only fetch the matched course")
            raise AssertionError(f"unexpected endpoint: {endpoint}")

        stdout, stderr = self.run_canvas_main(
            ["assignments", "374m"],
            extra_patches=(patch.object(canvas, "fetch_paginated", side_effect=fake_fetch_paginated),),
        )

        self.assertEqual(stderr, "")
        self.assertIn("1 assignments\n\nCourse | Name", stdout)
        self.assertIn("M 374M", stdout)
        self.assertIn("Modeling Project", stdout)
        self.assertNotIn("M 408D", stdout)

    def test_assignments_command_misspelling_rejects_invalid_usage(self):
        with self.assertRaises(SystemExit) as exc:
            self.run_canvas_main(["assigments", "374m", "--name"])

        self.assertEqual(str(exc.exception), EXPECTED_INVALID_USAGE_MESSAGE)

    def test_shortcut_rejects_course_filter_before_command(self):
        stdout_command_first, stderr_command_first = self.run_canvas_main(["pages", "m408d"])

        with self.assertRaises(SystemExit) as exc:
            self.run_canvas_main(["m408d", "pages"])

        self.assertEqual(stderr_command_first, "")
        self.assertEqual(str(exc.exception), EXPECTED_INVALID_USAGE_MESSAGE)
        self.assertIn("2 pages\n\nCourse | Title", stdout_command_first)
        self.assertIn("M 408D", stdout_command_first)

    def test_shortcut_resolution_rejects_filter_before_command(self):
        with self.assertRaises(SystemExit) as exc:
            canvas.resolve_shortcut_command("m374m", ["files"])

        self.assertEqual(str(exc.exception), EXPECTED_INVALID_USAGE_MESSAGE)

    def test_bare_course_filter_without_command_rejects_invalid_usage(self):
        with self.assertRaises(SystemExit) as exc:
            self.run_canvas_main(["m374m"])

        self.assertEqual(str(exc.exception), EXPECTED_INVALID_USAGE_MESSAGE)

    def test_page_rows_ignore_page_urls(self):
        rows = canvas.page_rows([
            {"title": "Start Here", "html_url": "https://school.instructure.com/courses/1/pages/start-here"}
        ], {"course_code": "M 408D"})

        self.assertEqual(rows, [{
            "course": "M 408D",
            "title": "Start Here",
        }])

    def test_render_pages_outputs_plain_titles_without_terminal_hyperlinks(self):
        url = "https://school.instructure.com/courses/1/pages/start-here"
        out = canvas.render_pages([{"course": "M 408D", "title": "Start Here", "__url": url}])

        self.assertIn("Start Here", out)
        self.assertNotIn("\x1b]8;;", out)
        self.assertNotIn("\x1b[4m", out)
        self.assertNotIn(url, out)

    def test_pages_command_never_emits_terminal_hyperlinks_even_when_forced(self):
        stdout = TtyStringIO()

        with patch.dict(os.environ, {"CANVAS_HYPERLINKS": "always", "TERM_PROGRAM": "iTerm.app"}, clear=False):
            out, err = self.run_canvas_main(["pages", "M 408D"], stdout=stdout)

        self.assertEqual(err, "")
        self.assertIn("2 pages\n\nCourse | Title", out)
        self.assertIn("Start Here", out)
        self.assertNotIn("\x1b]8;;", out)
        self.assertNotIn("\x1b[4m", out)
        self.assertNotIn("https://school.instructure.com/courses/1/pages/start-here", out)

    def test_terminal_hyperlink_helpers_are_removed(self):
        removed = [
            "terminal_supports_osc8",
            "terminal_hyperlinks_enabled",
            "terminal_hyperlink",
            "page_url",
        ]
        for name in removed:
            self.assertFalse(hasattr(canvas, name), f"{name} should be removed")

    def test_help_output_omits_browser_cookie_auth_flow(self):
        self.assertNotIn("instructure.com cookies from the browser", canvas.HELP_TEXT)
        self.assertNotIn("open Canvas in the browser", canvas.HELP_TEXT)
        removed_auth_text = [
            "auth import-curl",
            "Copy as cURL",
            "pbpaste",
            "clipboard",
            "Duo",
            "Bitwarden",
            "CANVAS_AUTO_REAUTH",
        ]
        for text in removed_auth_text:
            self.assertNotIn(text, canvas.HELP_TEXT)

    def test_removed_auth_surfaces_are_not_present(self):
        removed = [
            "COMMAND_ALIASES",
            "canonical_command",
            "handle_auth_command",
            "storage_state_from_curl",
            "import_canvas_state_from_curl",
            "refresh_canvas_login_state",
            "bitwarden_password",
            "extract_duo_verification_code",
            "read_clipboard_text",
            "import_canvas_state_from_clipboard_if_available",
            "atlas_user_data_dir",
            "atlas_profile_names",
            "atlas_cookie_db_paths",
            "atlas_cookie_decryption_keys",
            "storage_state_from_atlas",
            "import_canvas_state_from_atlas",
            "import_canvas_state_from_atlas_if_available",
            "default_http_browser_bundle_id",
            "canvas_access_token_creation_javascript",
            "browser_token_apple_script",
            "default_http_browser_app_name",
            "browser_bundle_id_for_app_name",
            "canvas_access_token_from_browser_app",
            "canvas_access_token_from_default_browser",
            "import_canvas_token_from_browser_app_if_available",
            "import_canvas_token_from_default_browser_if_available",
            "browser_token_import_apps",
            "canvas_login_browser_app",
            "focus_existing_canvas_tab",
            "import_canvas_access_token",
            "mac_app_exists",
            "import_canvas_state_automatically",
            "automatic_import_candidate_available",
            "wait_for_chrome_login_import",
            "automatic_import_message",
            "REAUTHENTICATION_MESSAGE",
        ]
        for name in removed:
            self.assertFalse(hasattr(canvas, name), f"{name} should be removed")

    def test_unused_course_helpers_are_not_present(self):
        removed = [
            "DEFAULT_CANVAS_FILE_MAX_MB",
            "PAGE_ROW_KEYS",
            "canvas_file_download_max_bytes",
            "course_label",
            "extract_meeting_info_from_text",
            "extract_text_from_pdf_bytes",
            "fetch_course_meeting_info",
            "fetch_syllabus_file_meeting_info",
            "merge_meeting_info",
            "normalize_location",
            "normalize_meeting_days",
            "normalize_meeting_time",
            "render_people_sections",
            "response_json_or_none",
        ]
        for name in removed:
            self.assertFalse(hasattr(canvas, name), f"{name} should be removed")

    def test_open_canvas_login_uses_google_chrome_when_cookie_import_is_enabled(self):
        calls = []

        def fake_run(args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0)

        with patch.object(canvas, "focus_existing_chrome_canvas_tab", return_value=False), \
             patch.object(canvas.subprocess, "run", side_effect=fake_run):
            canvas.open_canvas_in_chrome(config={})

        self.assertEqual(calls, [(["open", "-a", "Google Chrome", canvas.BASE_URL], {
            "check": False,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        })])

    def test_open_canvas_login_reuses_existing_chrome_tab(self):
        calls = []

        def fake_run(args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0)

        with patch.object(canvas, "focus_existing_chrome_canvas_tab", return_value=True), \
             patch.object(canvas.subprocess, "run", side_effect=fake_run):
            canvas.open_canvas_in_chrome(config={})

        self.assertEqual(calls, [])

    def test_401_opens_canvas_login_and_reports_auth_required(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        fake_playwright = FakeUnauthorizedPlaywright()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            state_file.write_text(json.dumps({"cookies": [], "origins": []}))
            with patch.object(canvas, "STATE_FILE", state_file), \
                 patch.object(canvas, "sync_playwright", lambda: fake_playwright), \
                 patch.object(canvas, "import_canvas_state_from_browser_session", return_value=(None, None)), \
                 patch.object(canvas, "open_canvas_in_chrome") as open_browser, \
                 patch.object(canvas, "canvas_chrome_login_timeout_seconds", return_value=0), \
                 patch.dict(os.environ, {"CANVAS_AUTO_RESIZE": "0"}), \
                 patch.object(sys, "stdout", stdout), \
                 contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as exc:
                    canvas.main(["courses"])

            state_exists = state_file.exists()

        self.assertEqual(str(exc.exception), EXPECTED_AUTH_REQUIRED_MESSAGE)
        open_browser.assert_called_once()
        self.assertEqual(fake_playwright.context_count, 1)
        self.assertFalse(state_exists)
        self.assertEqual("", stdout.getvalue())
        self.assertEqual("Authenticating...\n", stderr.getvalue())

    def test_401_chrome_import_failure_reports_chrome_only_help(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        fake_playwright = FakeCurlRetryPlaywright()
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            state_file.write_text(json.dumps({"cookies": [], "origins": []}))
            with patch.object(canvas, "STATE_FILE", state_file), \
                 patch.object(canvas, "sync_playwright", lambda: fake_playwright), \
                 patch.object(canvas, "import_canvas_state_from_chrome_if_available", return_value=None), \
                 patch.object(canvas, "import_canvas_state_from_browser_session", return_value=(None, None)), \
                 patch.object(canvas, "open_canvas_in_chrome") as open_browser, \
                 patch.object(canvas, "canvas_chrome_login_timeout_seconds", return_value=0), \
                 patch.dict(os.environ, {"CANVAS_AUTO_RESIZE": "0"}, clear=False), \
                 patch.object(sys, "stdout", stdout), \
                 contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as exc:
                    canvas.main(["courses"])

        message = str(exc.exception)
        self.assertEqual(message, EXPECTED_AUTH_REQUIRED_MESSAGE)
        open_browser.assert_called_once()
        self.assertNotIn("Copy as cURL", message)
        self.assertNotIn("auth import-curl", message)
        self.assertNotIn("Duo", message)
        self.assertEqual(fake_playwright.context_count, 1)
        self.assertEqual("Authenticating...\n", stderr.getvalue())
        self.assertEqual("", stdout.getvalue())

    def test_storage_state_from_chrome_reads_canvas_cookies_from_profile_db(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cookie_db = Path(tmpdir) / "Cookies"
            con = sqlite3.connect(cookie_db)
            try:
                con.execute("create table meta(key text primary key, value text)")
                con.execute("insert into meta(key, value) values('version', '24')")
                con.execute("""
                    create table cookies(
                        host_key text, name text, value text, encrypted_value blob, path text,
                        expires_utc integer, is_secure integer, is_httponly integer, samesite integer
                    )
                """)
                future_chrome_time = (int(time.time()) + canvas.CHROME_COOKIE_EPOCH_OFFSET + 3600) * 1_000_000
                con.execute(
                    "insert into cookies values(?,?,?,?,?,?,?,?,?)",
                    (canvas.CANVAS_HOST, "canvas_session", "abc123", b"", "/", future_chrome_time, 1, 1, 1),
                )
                con.execute(
                    "insert into cookies values(?,?,?,?,?,?,?,?,?)",
                    ("example.com", "other", "ignore", b"", "/", future_chrome_time, 1, 1, 1),
                )
                con.commit()
            finally:
                con.close()

            with patch.object(canvas, "chrome_cookie_db_paths", return_value=[cookie_db]), \
                 patch.object(canvas, "chrome_cookie_decryption_key", return_value=None):
                state = canvas.storage_state_from_chrome({})

        self.assertIsNotNone(state)
        cookies = {cookie["name"]: cookie for cookie in state["cookies"]}
        self.assertEqual(cookies["canvas_session"]["value"], "abc123")
        self.assertEqual(cookies["canvas_session"]["domain"], canvas.CANVAS_HOST)
        self.assertEqual(cookies["canvas_session"]["sameSite"], "Lax")
        self.assertEqual(state["origins"], [])
        self.assertNotIn("other", cookies)

    def test_storage_state_from_chrome_includes_uncheckpointed_wal_cookies(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cookie_db = Path(tmpdir) / "Cookies"
            con = sqlite3.connect(cookie_db)
            try:
                con.execute("pragma journal_mode=wal")
                con.execute("create table meta(key text primary key, value text)")
                con.execute("insert into meta(key, value) values('version', '24')")
                con.execute("""
                    create table cookies(
                        host_key text, name text, value text, encrypted_value blob, path text,
                        expires_utc integer, is_secure integer, is_httponly integer, samesite integer
                    )
                """)
                con.commit()
                con.execute("pragma wal_checkpoint(truncate)")
                future_chrome_time = (int(time.time()) + canvas.CHROME_COOKIE_EPOCH_OFFSET + 3600) * 1_000_000
                con.execute(
                    "insert into cookies values(?,?,?,?,?,?,?,?,?)",
                    (canvas.CANVAS_HOST, "canvas_session", "wal-cookie", b"", "/", future_chrome_time, 1, 1, 1),
                )
                con.commit()
                self.assertTrue(cookie_db.with_name("Cookies-wal").exists())

                with patch.object(canvas, "chrome_cookie_db_paths", return_value=[cookie_db]), \
                     patch.object(canvas, "chrome_cookie_decryption_key", return_value=None):
                    state = canvas.storage_state_from_chrome({})
            finally:
                con.close()

        cookies = {cookie["name"]: cookie for cookie in state["cookies"]}
        self.assertEqual(cookies["canvas_session"]["value"], "wal-cookie")

    def test_chrome_login_timeout_defaults_to_120_seconds(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(canvas.canvas_chrome_login_timeout_seconds({}), 120)
            self.assertEqual(canvas.canvas_chrome_login_timeout_seconds({"chrome_login_timeout_seconds": "oops"}), 120)

    def test_auth_poll_interval_defaults_to_quarter_second(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(canvas.canvas_auth_poll_interval_seconds({}), 0.25)
            self.assertEqual(canvas.canvas_auth_poll_interval_seconds({"auth_poll_interval_seconds": "oops"}), 0.25)
            self.assertEqual(canvas.canvas_auth_poll_interval_seconds({"auth_poll_interval_seconds": 0.01}), 0.1)

    def test_wait_for_canvas_authentication_opens_chrome_and_exits(self):
        stderr = io.StringIO()

        with patch.dict(os.environ, {}, clear=True), \
             patch.object(canvas, "open_canvas_in_chrome") as open_browser, \
             patch.object(canvas, "import_canvas_state_from_browser_session", return_value=(None, None)), \
             patch.object(canvas, "canvas_chrome_login_timeout_seconds", return_value=0), \
             contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as exc:
                canvas.wait_for_canvas_authentication(FakePlaywright(), config={})

        self.assertEqual(str(exc.exception), EXPECTED_AUTH_REQUIRED_MESSAGE)
        open_browser.assert_called_once()
        self.assertEqual(stderr.getvalue(), "Authenticating...\n")

    def test_wait_for_canvas_authentication_polls_until_timeout(self):
        stderr = io.StringIO()
        import_calls = []
        sleeps = []

        with patch.dict(os.environ, {}, clear=True), \
             patch.object(canvas, "import_canvas_state_from_browser_session", side_effect=lambda *args, **kwargs: import_calls.append((args, kwargs)) or (None, None)), \
             patch.object(canvas, "open_canvas_in_chrome"), \
             patch.object(canvas, "canvas_chrome_login_timeout_seconds", return_value=0), \
             patch.object(canvas.time, "sleep", side_effect=sleeps.append), \
             contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                canvas.wait_for_canvas_authentication(FakePlaywright(), config={})

        self.assertEqual(str(raised.exception), EXPECTED_AUTH_REQUIRED_MESSAGE)
        self.assertEqual(len(import_calls), 1)
        self.assertEqual(sleeps, [])
        self.assertEqual(stderr.getvalue(), "Authenticating...\n")
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_wait_for_canvas_authentication_does_not_launch_test_browser(self):
        stderr = io.StringIO()
        launch_calls = []
        fake_playwright = types.SimpleNamespace(
            chromium=types.SimpleNamespace(
                launch=lambda **kwargs: launch_calls.append(kwargs) or (_ for _ in ()).throw(AssertionError("test browser should not open"))
            )
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            with patch.dict(os.environ, {}, clear=True), \
                 patch.object(canvas, "open_canvas_in_chrome") as open_browser, \
                 patch.object(canvas, "import_canvas_state_from_browser_session", return_value=(None, None)), \
                 patch.object(canvas, "canvas_chrome_login_timeout_seconds", return_value=0), \
                 contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as exc:
                    canvas.wait_for_canvas_authentication(fake_playwright, state_file=state_file, config={})

        self.assertEqual(str(exc.exception), EXPECTED_AUTH_REQUIRED_MESSAGE)
        self.assertEqual(launch_calls, [])
        open_browser.assert_called_once()
        self.assertEqual(stderr.getvalue(), "Authenticating...\n")

    def test_401_refreshes_from_existing_browser_session_before_opening_login(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        fake_playwright = FakeCurlRetryPlaywright()
        import_calls = []

        def fake_import(playwright, state_file=canvas.STATE_FILE, config=None):
            import_calls.append((playwright, state_file, config))
            state_file.write_text(json.dumps({"cookies": [{"name": "canvas_session", "value": "secret"}], "origins": []}))
            os.chmod(state_file, 0o600)
            return "browser token", {"id": 7, "name": "Tomas"}

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            state_file.write_text(json.dumps({"cookies": [], "origins": []}))
            with patch.object(canvas, "STATE_FILE", state_file), \
                 patch.object(canvas, "sync_playwright", lambda: fake_playwright), \
                 patch.object(canvas, "import_canvas_state_from_browser_session", side_effect=fake_import), \
                 patch.object(canvas, "open_canvas_in_chrome") as open_browser, \
                 patch.dict(os.environ, {"CANVAS_AUTO_RESIZE": "0"}), \
                 patch.object(sys, "stdout", stdout), \
                 contextlib.redirect_stderr(stderr):
                canvas.main(["courses"])

        open_browser.assert_not_called()
        self.assertEqual(fake_playwright.context_count, 2)
        self.assertEqual(len(import_calls), 1)
        self.assertIn("1 courses", stdout.getvalue())
        self.assertEqual("", stderr.getvalue())

    def test_401_waits_for_interactive_cookie_import(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        fake_playwright = FakeCurlRetryPlaywright()
        wait_calls = []

        def fake_wait(playwright, state_file=canvas.STATE_FILE, config=None):
            wait_calls.append((playwright, state_file, config))
            state_file.write_text(json.dumps({"cookies": [{"name": "canvas_session", "value": "secret"}], "origins": []}))
            os.chmod(state_file, 0o600)
            return "browser token", {"id": 7, "name": "Tomas"}

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            state_file.write_text(json.dumps({"cookies": [], "origins": []}))
            with patch.object(canvas, "STATE_FILE", state_file), \
                 patch.object(canvas, "sync_playwright", lambda: fake_playwright), \
                 patch.object(canvas, "import_canvas_state_from_browser_session", return_value=(None, None)), \
                 patch.object(canvas, "wait_for_canvas_authentication", side_effect=fake_wait), \
                 patch.object(canvas, "open_canvas_in_chrome") as open_browser, \
                 patch.dict(os.environ, {"CANVAS_AUTO_RESIZE": "0"}), \
                 patch.object(sys, "stdout", stdout), \
                 contextlib.redirect_stderr(stderr):
                canvas.main(["courses"])

        open_browser.assert_not_called()
        self.assertEqual(fake_playwright.context_count, 2)
        self.assertEqual(len(wait_calls), 1)
        self.assertIn("1 courses", stdout.getvalue())
        self.assertEqual("", stderr.getvalue())
        self.assertNotIn("secret", stdout.getvalue() + stderr.getvalue())

    def test_import_canvas_state_from_chrome_verifies_before_replacing_state_file(self):
        fake_playwright = FakeImportPlaywright()
        state = {"cookies": [{"name": "canvas_session", "value": "secret", "domain": canvas.CANVAS_HOST, "path": "/"}], "origins": []}
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            with patch.object(canvas, "storage_state_from_chrome", return_value=state):
                profile = canvas.import_canvas_state_from_chrome(fake_playwright, state_file=state_file)
            saved = json.loads(state_file.read_text())
            mode = oct(state_file.stat().st_mode & 0o777)

        self.assertEqual(profile["name"], "Tomas")
        self.assertEqual(saved["cookies"][0]["name"], "canvas_session")
        self.assertEqual(mode, "0o600")
        self.assertTrue(fake_playwright.context_kwargs[0]["storage_state"].endswith("state.json.tmp"))
        self.assertTrue(fake_playwright.requests[0].disposed)

    def test_verifying_canvas_state_handles_keyboard_interrupt_during_cleanup(self):
        fake_playwright = FakeInterruptingImportPlaywright()
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            state_file.write_text(json.dumps({"cookies": [], "origins": []}))

            with self.assertRaises(SystemExit) as exc:
                canvas.verify_canvas_state_file(fake_playwright, state_file)

        self.assertEqual(str(exc.exception), EXPECTED_AUTH_CANCELLED_MESSAGE)
        self.assertTrue(fake_playwright.request_context.disposed)

    def test_browser_session_import_uses_chrome_only(self):
        fake_playwright = FakeImportPlaywright()
        profile = {"id": 7, "name": "Tomas"}

        with patch.object(canvas, "import_canvas_state_from_chrome_if_available", return_value=profile) as import_chrome:
            source, imported_profile = canvas.import_canvas_state_from_browser_session(fake_playwright, config={})

        self.assertEqual(source, "Chrome")
        self.assertEqual(imported_profile, profile)
        import_chrome.assert_called_once_with(fake_playwright, state_file=canvas.STATE_FILE, config={})

    def test_token_state_uses_bearer_authorization_header(self):
        fake_playwright = FakeImportPlaywright()
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            state_file.write_text(json.dumps({
                "canvas_cli_auth": {
                    "type": "bearer_token",
                    "access_token": "canvas-token",
                }
            }))

            canvas.CanvasRequestSession(fake_playwright, state_file=state_file)

        self.assertEqual(fake_playwright.context_kwargs[0], {
            "base_url": canvas.BASE_URL,
            "extra_http_headers": {"Authorization": "Bearer canvas-token"},
        })

    def test_missing_state_imports_chrome_state_before_opening_login(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        def fake_import_chrome(playwright, state_file=canvas.STATE_FILE, config=None):
            state_file.write_text(json.dumps({
                "cookies": [{"name": "canvas_session", "value": "secret"}],
                "origins": [],
            }))
            return {"id": 7, "name": "Tomas"}

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            with patch.object(canvas, "STATE_FILE", state_file), \
                 patch.object(canvas, "sync_playwright", lambda: FakePlaywright()), \
                 patch.object(canvas, "import_canvas_state_from_chrome_if_available", side_effect=fake_import_chrome), \
                 patch.object(canvas, "open_canvas_in_chrome") as open_browser, \
                 patch.dict(os.environ, {"CANVAS_AUTO_RESIZE": "0"}), \
                 patch.object(sys, "stdout", stdout), \
                 contextlib.redirect_stderr(stderr):
                canvas.main(["courses"])

        open_browser.assert_not_called()
        self.assertIn("1 courses", stdout.getvalue())
        self.assertEqual("", stderr.getvalue())

    def test_missing_state_opens_canvas_login_and_reports_auth_required(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            with patch.object(canvas, "STATE_FILE", state_file), \
                 patch.object(canvas, "sync_playwright", lambda: FakePlaywright()), \
                 patch.object(canvas, "import_canvas_state_from_browser_session", return_value=(None, None)), \
                 patch.object(canvas, "open_canvas_in_chrome") as open_browser, \
                 patch.object(canvas, "canvas_chrome_login_timeout_seconds", return_value=0), \
                 patch.dict(os.environ, {"CANVAS_AUTO_RESIZE": "0"}), \
                 patch.object(sys, "stdout", stdout), \
                 contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as exc:
                    canvas.main(["courses"])

        message = str(exc.exception)
        self.assertEqual(message, EXPECTED_AUTH_REQUIRED_MESSAGE)
        open_browser.assert_called_once()
        self.assertEqual("", stdout.getvalue())
        self.assertEqual("Authenticating...\n", stderr.getvalue())

    def test_keyboard_interrupt_during_authentication_exits_without_traceback(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        fake_playwright = FakeCurlRetryPlaywright()
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            state_file.write_text(json.dumps({"cookies": [], "origins": []}))
            with patch.object(canvas, "STATE_FILE", state_file), \
                 patch.object(canvas, "sync_playwright", lambda: fake_playwright), \
                 patch.object(canvas, "import_canvas_state_from_browser_session", return_value=(None, None)), \
                 patch.object(canvas, "wait_for_canvas_authentication", side_effect=KeyboardInterrupt), \
                 patch.dict(os.environ, {"CANVAS_AUTO_RESIZE": "0"}), \
                 patch.object(sys, "stdout", stdout), \
                 contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as exc:
                    canvas.main(["courses"])

        self.assertEqual(str(exc.exception), EXPECTED_AUTH_CANCELLED_MESSAGE)
        self.assertEqual("", stdout.getvalue())

    def test_grades_command_prints_plain_table_without_tui_even_on_tty(self):
        fake_curses = types.SimpleNamespace(
            wrapper=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("curses TUI should not run"))
        )
        stdout = TtyStringIO()
        stderr = io.StringIO()
        stdin = TtyStringIO()
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            state_file.write_text("{}")
            with patch.object(canvas, "STATE_FILE", state_file), \
                 patch.object(canvas, "sync_playwright", lambda: FakePlaywright()), \
                 patch.dict(os.environ, {"CANVAS_AUTO_RESIZE": "0", "CANVAS_AUTO_OPEN_CHROME": "0"}), \
                 patch.dict(sys.modules, {"curses": fake_curses}), \
                 patch.object(sys, "stdin", stdin), \
                 patch.object(sys, "stdout", stdout), \
                 contextlib.redirect_stderr(stderr):
                canvas.main(["grades"])

        out = stdout.getvalue()
        err = stderr.getvalue()
        self.assertEqual(err, "")
        self.assertTrue(out.startswith("3 grades\n\nCourse | Name"), out)
        self.assertIn("M 408D", out)
        self.assertNotIn("Pinned table view unavailable", err)
        self.assertNotIn("M 408D — Differential and Integral Calculus\n\n", out)


if __name__ == "__main__":
    unittest.main()
