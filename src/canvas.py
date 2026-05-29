#!/usr/local/bin/python3
"""
Read-only Canvas LMS CLI.
"""
import argparse
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse

from playwright.sync_api import sync_playwright

BASE_URL = ""
CANVAS_REPO_ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = CANVAS_REPO_ROOT / "state.json"
CONFIG_FILE = Path.home() / ".config/canvas-cli/config.json"
CANVAS_FILES_ROOT = CANVAS_REPO_ROOT / "files"
CANVAS_HOST = ""
CANVAS_CLI_AUTH_KEY = "canvas_cli_auth"
CHROME_USER_DATA_DIR = Path.home() / "Library/Application Support/Google/Chrome"
CHROME_COOKIE_EPOCH_OFFSET = 11644473600
CHROME_APP_NAME = "Google Chrome"


def normalize_canvas_base_url(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        raise SystemExit("Canvas institution URL is required. Run canvas setup https://school.instructure.com")
    if "://" not in value:
        value = "https://" + value
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SystemExit(f"Invalid Canvas institution URL: {value}")
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def set_canvas_base_url(value: str) -> str:
    global BASE_URL, CANVAS_HOST
    BASE_URL = normalize_canvas_base_url(value)
    CANVAS_HOST = urlparse(BASE_URL).netloc
    return BASE_URL


if os.environ.get("CANVAS_BASE_URL"):
    set_canvas_base_url(os.environ["CANVAS_BASE_URL"])

PREFERRED_KEYS = [
    "id",
    "name",
    "course_code",
    "title",
    "display_name",
    "filename",
    "due_at",
    "unlock_at",
    "lock_at",
    "points_possible",
    "published",
    "workflow_state",
    "state",
    "items_count",
    "size",
    "updated_at",
    "created_at",
    "html_url",
    "url",
]

KNOWN_COLUMNS = [
    (re.compile(r"/api/v1/courses/?(?:\?|$)"), ["id", "name", "course_code", "workflow_state"]),
    (re.compile(r"/assignments(?:\?|$)"), ["id", "name", "due_at", "points_possible", "published", "html_url"]),
    (re.compile(r"/files(?:\?|$)"), ["id", "display_name", "filename", "size", "updated_at", "url"]),
    (re.compile(r"/modules(?:\?|$)"), ["id", "name", "state", "items_count"]),
    (re.compile(r"/pages(?:\?|$)"), ["page_id", "title", "updated_at", "html_url"]),
    (re.compile(r"/discussion_topics(?:\?|$)"), ["id", "title", "posted_at", "published", "html_url"]),
    (re.compile(r"/users(?:\?|$)"), ["name"]),
]

COMMANDS = ["setup", "home", "courses", "announcements", "assignments", "grades", "people", "pages", "files", "syllabus", "modules"]
COMMAND_METAVAR = "{" + ", ".join(COMMANDS) + "}"
SHORTCUT_COMMANDS = set(COMMANDS)
INVALID_USAGE_MESSAGE = "Invalid usage, please follow the format: canvas {cmd} course [optional filter]"
HELP_TEXT = """usage: canvas [-h, --help] [--json] [--date | --name]
             {setup, home, courses, announcements, assignments, grades, people, pages, files, syllabus, modules}

Canvas LMS CLI

options:
  -h, --help  Show this help message
  --json      Print pretty JSON instead of the default readable table
  --date      Assignments filter: sort by descending due dates
  --name      Assignments filter: sort names alphabetically

Commands:
    setup               Save your Canvas institution URL
    home                Open the Canvas dashboard or a course home page
    courses             List active courses
    announcements       List announcements
    assignments         List assignments
    grades              List grades
    people              List active people in a course
    pages               List pages
    files               Mirror course file links locally
    syllabus            Open course syllabus page
    modules             List modules

Examples:
    canvas setup https://school.instructure.com
    canvas courses
    canvas courses "PHY 382N"
    canvas assignments phy382n
    canvas grades phy
    canvas modules 382n
    canvas files "PHY 382N"
    canvas home phy382n
    canvas pages phy
    canvas syllabus 382n
    canvas people "PHY 382N"
    canvas announcements phy382n
"""


def parse_args(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if any(arg in ("-h", "--help") for arg in argv):
        sys.stdout.write(HELP_TEXT)
        raise SystemExit(0)

    parser = argparse.ArgumentParser(
        prog="canvas",
        usage=f"canvas [-h, --help] [--json] [--date | --name]\n             {COMMAND_METAVAR}",
        description="Canvas LMS CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("endpoint", nargs="?", metavar=COMMAND_METAVAR, help=argparse.SUPPRESS)
    parser.add_argument("filters", nargs="*", help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", help="Print pretty JSON instead of the default readable table")

    sort_group = parser.add_mutually_exclusive_group()
    sort_group.add_argument("--date", action="store_true", help="Assignments shortcut only: sort by due date descending")
    sort_group.add_argument("--name", action="store_true", help="Assignments shortcut only: sort names alphabetically")
    if hasattr(parser, "parse_intermixed_args"):
        return parser.parse_intermixed_args(argv)
    return parser.parse_args(argv)


def is_raw_endpoint_candidate(endpoint: str) -> bool:
    return (
        endpoint.startswith("/")
        or endpoint.startswith("http://")
        or endpoint.startswith("https://")
        or "/" in endpoint
        or "?" in endpoint
    )


def resolve_shortcut_command(endpoint: str, filters: list[str] | None = None) -> tuple[str, list[str]]:
    filters = list(filters or [])
    command = endpoint.strip().lower()
    if command in SHORTCUT_COMMANDS:
        return command, filters
    if any(token.strip().lower() in SHORTCUT_COMMANDS for token in filters):
        raise SystemExit(INVALID_USAGE_MESSAGE)
    if not is_raw_endpoint_candidate(endpoint.strip()):
        raise SystemExit(INVALID_USAGE_MESSAGE)
    return command, filters


def normalize_endpoint(endpoint: str) -> str:
    if endpoint.startswith("http://") or endpoint.startswith("https://"):
        parsed = urlparse(endpoint)
        if parsed.netloc != CANVAS_HOST:
            raise SystemExit(f"Refusing non-configured Canvas host: {parsed.netloc}")
        return parsed.path + (("?" + parsed.query) if parsed.query else "")
    if not endpoint.startswith("/"):
        endpoint = "/" + endpoint
    return endpoint


def next_url_from_link(link: str | None) -> str | None:
    if not link:
        return None
    for part in link.split(","):
        m = re.match(r'\s*<([^>]+)>;\s*rel="([^"]+)"', part)
        if m and m.group(2) == "next":
            url = m.group(1)
            parsed = urlparse(url)
            if parsed.netloc and parsed.netloc != CANVAS_HOST:
                raise SystemExit(f"Refusing pagination to non-configured Canvas host: {parsed.netloc}")
            return parsed.path + (("?" + parsed.query) if parsed.query else "")
    return None


def is_scalar(value) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def titleize(key: str) -> str:
    special = {
        "id": "ID",
        "url": "URL",
        "html_url": "HTML URL",
        "api_url": "API URL",
        "login_id": "Login ID",
        "sis_course_id": "SIS Course ID",
        "page_id": "Page ID",
    }
    if key in special:
        return special[key]
    return key.replace("_", " ").title()


def stringify(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return ", ".join(stringify(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value).replace("\n", " ").strip()


def truncate(text: str, width: int) -> str:
    text = stringify(text)
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return text[: width - 3] + "..."


def table_lines(headers, rows, max_width=52, separator_before: set[int] | None = None) -> tuple[str, str, list[str]]:
    if not headers:
        return "", "", []
    string_rows = [[stringify(cell) for cell in row] for row in rows]
    widths = []
    for i, header in enumerate(headers):
        longest = len(stringify(header))
        for row in string_rows:
            longest = max(longest, len(row[i]))
        widths.append(min(longest, max_width))

    def fmt(row):
        return " | ".join(truncate(cell, widths[i]).ljust(widths[i]) for i, cell in enumerate(row))

    sep = "-+-".join("-" * w for w in widths)
    header = fmt(headers)
    body = []
    separator_before = separator_before or set()
    for index, row in enumerate(string_rows):
        if index in separator_before:
            body.append(sep)
        body.append(fmt(row))
    return header, sep, body


def render_table(headers, rows, max_width=52, separator_before: set[int] | None = None, repeat_header_every: int | None = None) -> str:
    del repeat_header_every
    header, sep, body = table_lines(headers, rows, max_width=max_width, separator_before=separator_before)
    if not header:
        return ""
    return "\n".join([header, sep, *body])


def config_int(config: dict | None, key: str, env_key: str, default: int) -> int:
    config = config or {}
    value = os.environ.get(env_key, config.get(key, default))
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def output_max_width(text: str) -> int:
    lines = str(text).splitlines() or [""]
    return max(len(line.rstrip()) for line in lines)


def terminal_resize_target(text: str, config: dict | None = None) -> tuple[int, int]:
    default_cols = config_int(config, "terminal_default_columns", "CANVAS_DEFAULT_COLS", 80)
    default_rows = config_int(config, "terminal_default_rows", "CANVAS_DEFAULT_ROWS", 24)
    width = output_max_width(text)
    return (width if width > default_cols else default_cols, default_rows)


def maybe_resize_terminal_for_output(text: str, config: dict | None = None) -> None:
    if os.environ.get("CANVAS_AUTO_RESIZE", "1").lower() in {"0", "false", "no", "off"}:
        return
    if not sys.stdout.isatty():
        return
    cols, rows = terminal_resize_target(text, config)
    sys.stdout.write(f"\x1b[8;{rows};{cols}t")
    sys.stdout.flush()


def print_human(text: str, config: dict | None = None) -> None:
    maybe_resize_terminal_for_output(text, config)
    print(text)


def endpoint_columns(endpoint: str, rows: list[dict]) -> list[str]:
    for pattern, keys in KNOWN_COLUMNS:
        if pattern.search(endpoint):
            cols = [key for key in keys if any(row.get(key) not in (None, "") for row in rows)]
            if cols:
                return cols

    cols = [key for key in PREFERRED_KEYS if any(row.get(key) not in (None, "") for row in rows)]
    if len(cols) >= 2:
        return cols[:6]

    for row in rows:
        for key, value in row.items():
            if key not in cols and is_scalar(value):
                cols.append(key)
            if len(cols) >= 6:
                return cols
    return cols


def render_list(rows, endpoint: str) -> str:
    if not rows:
        return "0 rows"
    if all(isinstance(row, dict) for row in rows):
        cols = endpoint_columns(endpoint, rows)
        if cols:
            headers = [titleize(col) for col in cols]
            table_rows = [[row.get(col) for col in cols] for row in rows]
            return f"{len(rows)} rows\n\n" + render_table(headers, table_rows)
    return json.dumps(rows, indent=2, ensure_ascii=False)


def render_dict(data: dict) -> str:
    rows = []
    for key in PREFERRED_KEYS:
        if key in data and is_scalar(data[key]):
            rows.append([key, data[key]])
    for key, value in data.items():
        if key not in {row[0] for row in rows} and is_scalar(value):
            rows.append([key, value])
    if not rows:
        return json.dumps(data, indent=2, ensure_ascii=False)
    return render_table(["Field", "Value"], rows, max_width=80)


def render_human(data, endpoint: str = "") -> str:
    if isinstance(data, list):
        return render_list(data, endpoint)
    if isinstance(data, dict):
        return render_dict(data)
    return json.dumps(data, indent=2, ensure_ascii=False)


def format_score(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return stringify(value)


def load_config(path: Path = CONFIG_FILE) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_config(config: dict, path: Path = CONFIG_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


def canvas_base_url_from_state_file(state_file: Path = STATE_FILE) -> str | None:
    try:
        data = json.loads(state_file.read_text())
    except Exception:
        return None
    cookies = data.get("cookies") if isinstance(data, dict) else None
    if not isinstance(cookies, list):
        return None
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        domain = str(cookie.get("domain") or "").strip().lstrip(".")
        name = str(cookie.get("name") or "")
        if domain and name == "canvas_session":
            return normalize_canvas_base_url(f"https://{domain}")
    return None


def configured_canvas_base_url(config: dict | None = None, state_file: Path = STATE_FILE) -> str:
    config = config or {}
    value = os.environ.get("CANVAS_BASE_URL") or config.get("base_url") or BASE_URL
    if value:
        return normalize_canvas_base_url(value)
    state_url = canvas_base_url_from_state_file(state_file)
    if state_url:
        return state_url
    raise SystemExit("Canvas institution is not configured, run: canvas setup https://school.instructure.com")


def apply_canvas_base_url(config: dict | None = None, state_file: Path = STATE_FILE) -> str:
    return set_canvas_base_url(configured_canvas_base_url(config, state_file=state_file))


def setup_canvas_institution(filters: list[str] | None, config: dict | None = None, config_file: Path = CONFIG_FILE) -> None:
    filters = list(filters or [])
    if len(filters) > 1:
        raise SystemExit("Usage: canvas setup https://school.instructure.com")
    if filters:
        raw_url = filters[0]
    else:
        print("Canvas institution URL: ", end="", flush=True)
        raw_url = sys.stdin.readline().strip()
    base_url = normalize_canvas_base_url(raw_url)
    next_config = dict(config or {})
    next_config["base_url"] = base_url
    save_config(next_config, config_file)
    set_canvas_base_url(base_url)
    print(f"Saved Canvas institution: {base_url}")


def hidden_course_patterns(config: dict | None = None) -> list[str]:
    config = config or {}
    values = config.get("hidden_courses") or []
    if not isinstance(values, list):
        return []
    return [str(value).strip().lower() for value in values if str(value).strip()]


def course_identity_text(course: dict) -> str:
    return " ".join(stringify(course.get(key)) for key in ("id", "course_code", "name", "original_name")).lower()


def course_is_hidden(course: dict, config: dict | None = None) -> bool:
    text = course_identity_text(course)
    return any(pattern in text for pattern in hidden_course_patterns(config))


def filter_hidden_courses(courses: list[dict], config: dict | None = None) -> list[dict]:
    if not config:
        return courses
    return [course for course in courses if not (isinstance(course, dict) and course_is_hidden(course, config))]


GRADE_ROW_KEYS = ["course", "name", "grade_fraction", "grade_percent"]


def as_number(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def format_percent(score: float | None, points: float | None = 100.0) -> str:
    if score is None or points is None:
        return "-"
    if points == 0:
        return "0%" if score == 0 else "-"
    return f"{format_score((score / points) * 100)}%"


def format_fraction(score: float | None, points: float | None) -> str:
    if score is None or points is None:
        return "-"
    return f"{format_score(score)}/{format_score(points)}"


def singular_group_name(name) -> str:
    text = stringify(name or "Assignments") or "Assignment"
    lower = text.lower()
    if lower.endswith("quizzes"):
        return text[:-3]
    if lower.endswith("ies"):
        return text[:-3] + "y"
    if lower.endswith(("sses", "ches", "shes", "xes", "zes")):
        return text[:-2]
    if lower.endswith("s") and not lower.endswith("ss"):
        return text[:-1]
    return text


def assignment_score(assignment: dict) -> float | None:
    submission = assignment.get("submission") or {}
    if isinstance(submission, dict):
        score = as_number(submission.get("score"))
        if score is not None:
            return score
    return as_number(assignment.get("score"))


def grade_rows(assignment_groups: list[dict], enrollment: dict | None = None, course: dict | None = None) -> list[dict]:
    course_value = course_code(course)
    assignment_rows = []
    group_rows = []
    total_score = 0.0
    total_points = 0.0
    for group in assignment_groups:
        if not isinstance(group, dict):
            continue
        group_score = 0.0
        group_points = 0.0
        assignments = group.get("assignments") or []
        for assignment in assignments:
            if not isinstance(assignment, dict):
                continue
            score = assignment_score(assignment)
            points = as_number(assignment.get("points_possible"))
            assignment_rows.append({
                "course": course_value,
                "name": assignment.get("name") or "",
                "grade_fraction": format_fraction(score, points),
                "grade_percent": format_percent(score, points),
            })
            if score is not None and points is not None and points > 0:
                group_score += score
                group_points += points
        if assignments:
            group_rows.append({
                "course": course_value,
                "name": singular_group_name(group.get('name')),
                "grade_fraction": format_fraction(group_score if group_points else None, group_points if group_points else None),
                "grade_percent": format_percent(group_score if group_points else None, group_points if group_points else None),
            })
            total_score += group_score
            total_points += group_points

    if assignment_rows and group_rows:
        group_rows[0]["__separator_before"] = True
    rows = assignment_rows + group_rows
    grades = (enrollment or {}).get("grades") or {}
    current_score = as_number(grades.get("current_score"))
    if current_score is not None:
        rows.append({"course": course_value, "name": "Total", "grade_fraction": f"{format_score(current_score)}/100", "grade_percent": f"{format_score(current_score)}%"})
    elif total_points > 0:
        rows.append({"course": course_value, "name": "Total", "grade_fraction": format_fraction(total_score, total_points), "grade_percent": format_percent(total_score, total_points)})
    return rows


def render_grade_rows(rows: list[dict]) -> str:
    headers = ["Course", "Name", "Grade (/)", "Grade (%)"]
    table_rows = [[row.get(key, "") for key in GRADE_ROW_KEYS] for row in rows]
    separator_before = {index for index, row in enumerate(rows) if row.get("__separator_before")}
    return f"{len(rows)} grades\n\n" + render_table(headers, table_rows, max_width=64, separator_before=separator_before)


def strip_private_keys(data):
    if isinstance(data, list):
        return [strip_private_keys(item) for item in data]
    if isinstance(data, dict):
        return {key: strip_private_keys(value) for key, value in data.items() if not str(key).startswith("__")}
    return data


def grade_json_rows(sections: list[dict]):
    rows = []
    for section in sections:
        rows.extend(section.get("rows") or [])
    cleaned = strip_private_keys(rows)
    if not isinstance(cleaned, list):
        return []
    return [row for row in cleaned if isinstance(row, dict)]


def render_grade_sections(sections: list[dict]) -> str:
    rows = grade_json_rows(sections)
    return render_grade_rows(rows) if rows else "0 grades"


COURSE_ROW_KEYS = ["course", "section", "id", "instructor"]


def section_number_from_text(value: str | None) -> str:
    if not value:
        return ""
    match = re.search(r"\((\d{5})\)", value)
    if match:
        return match.group(1)
    match = re.search(r"\b(\d{5})\b", value)
    return match.group(1) if match else ""


def enrolled_section(course: dict) -> dict:
    sections = course.get("sections") or []
    if not isinstance(sections, list) or not sections:
        return {}
    for section in sections:
        if isinstance(section, dict) and section.get("enrollment_role") == "StudentEnrollment":
            return section
    return sections[0] if isinstance(sections[0], dict) else {}


def course_section_number(course: dict) -> str:
    section = enrolled_section(course)
    candidates = [
        section.get("name") if section else "",
        course.get("name"),
        course.get("original_name"),
    ]
    for candidate in candidates:
        number = section_number_from_text(candidate)
        if number:
            return number
    return ""


TA_MARKERS = ("teaching assistant", "teacher assistant", "ta", "assistant instructor")


def teacher_name(teacher: dict) -> str:
    return teacher.get("display_name") or teacher.get("name") or ""


def teacher_looks_like_ta(teacher: dict) -> bool:
    fields = [
        teacher.get("role"),
        teacher.get("role_name"),
        teacher.get("type"),
        teacher.get("enrollment_type"),
        teacher.get("enrollment_role"),
        teacher_name(teacher),
    ]
    haystack = " ".join(str(value).lower() for value in fields if value)
    return any(re.search(rf"(?<![a-z]){re.escape(marker)}(?![a-z])", haystack) for marker in TA_MARKERS)


def course_instructors(course: dict) -> str:
    teachers = course.get("teachers") or []
    if isinstance(teachers, list):
        for teacher in teachers:
            if isinstance(teacher, dict):
                name = teacher_name(teacher)
                if name and not teacher_looks_like_ta(teacher):
                    return name
    return ""


def course_rows(courses: list[dict]) -> list[dict]:
    rows = []
    for course in courses:
        if not isinstance(course, dict):
            continue
        course_id = course.get("id")
        rows.append({
            "course": course_code(course),
            "id": course_id,
            "section": course_section_number(course),
            "instructor": course_instructors(course),
        })
    return rows


def render_courses(rows: list[dict]) -> str:
    headers = ["Course", "Section", "ID", "Instructor"]
    table_rows = [[row.get(key, "") for key in COURSE_ROW_KEYS] for row in rows]
    return f"{len(rows)} courses\n\n" + render_table(headers, table_rows, max_width=36)


def strip_html(value: str | None) -> str:
    if not value:
        return ""
    text = re.sub(r"(?i)<\s*br\s*/?\s*>", " ", value)
    text = re.sub(r"(?i)</\s*p\s*>", " ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return " ".join(text.split())


def split_canvas_datetime(value: str | None) -> tuple[str, str]:
    if not value:
        return "", ""
    if "T" not in value:
        return value, ""
    date, time = value.split("T", 1)
    time = time.replace("Z", "")[:5]
    return date, time


def announcement_author(announcement: dict) -> str:
    author = announcement.get("author") or {}
    if isinstance(author, dict):
        return author.get("display_name") or author.get("name") or announcement.get("user_name") or ""
    return announcement.get("user_name") or ""


ANNOUNCEMENT_ROW_KEYS = ["course", "author", "title", "message", "date", "time"]


def announcement_context_codes(announcement: dict) -> list[str]:
    codes = []
    for key in ("context_code", "context_codes"):
        value = announcement.get(key)
        if isinstance(value, list):
            codes.extend(str(item) for item in value if item)
        elif value:
            codes.append(str(value))
    context_id = announcement.get("context_id")
    context_type = str(announcement.get("context_type") or "").lower()
    if context_id and (not context_type or context_type == "course"):
        codes.append(f"course_{context_id}")
    return codes


def announcement_rows(announcements: list[dict], courses: list[dict] | None = None) -> list[dict]:
    courses = courses or []
    course_by_context = {f"course_{course.get('id')}": course_code(course) for course in courses if isinstance(course, dict) and course.get("id")}
    fallback_course = course_code(courses[0]) if len(courses) == 1 else ""
    rows = []
    for announcement in announcements:
        if not isinstance(announcement, dict):
            continue
        posted_at = announcement.get("posted_at") or announcement.get("created_at") or announcement.get("delayed_post_at")
        date, time = split_canvas_datetime(posted_at)
        course_value = ""
        for context_code in announcement_context_codes(announcement):
            course_value = course_by_context.get(context_code, "")
            if course_value:
                break
        rows.append({
            "course": course_value or fallback_course,
            "author": announcement_author(announcement),
            "title": announcement.get("title") or "",
            "message": strip_html(announcement.get("message")),
            "date": date,
            "time": time,
        })
    return rows


def render_announcements(rows: list[dict]) -> str:
    headers = ["Course", "Author", "Title", "Message", "Date", "Time"]
    table_rows = [[row.get(key, "") for key in ANNOUNCEMENT_ROW_KEYS] for row in rows]
    return f"{len(rows)} announcements\n\n" + render_table(headers, table_rows, max_width=64)


PEOPLE_ROW_KEYS = ["course", "name"]


def person_name(user: dict) -> str:
    return user.get("display_name") or user.get("name") or user.get("short_name") or user.get("sortable_name") or ""


def course_code(course: dict | None) -> str:
    if not isinstance(course, dict):
        return ""
    return stringify(course.get("course_code") or course.get("code") or course.get("id") or "")


def people_rows(users: list[dict], course: dict | None = None) -> list[dict]:
    rows = []
    seen = set()
    course = course or {}
    course_value = course_code(course)
    for user in users:
        if not isinstance(user, dict):
            continue
        name = person_name(user).strip()
        key = (course_value.lower(), name.lower())
        if not name or key in seen:
            continue
        seen.add(key)
        rows.append({"course": course_value, "name": name})
    rows.sort(key=lambda row: ((row.get("course") or "").lower(), (row.get("name") or "").lower()))
    return rows


def render_people(rows: list[dict]) -> str:
    headers = ["Course", "Name"]
    table_rows = [[row.get(key, "") for key in PEOPLE_ROW_KEYS] for row in rows]
    return f"{len(rows)} people\n\n" + render_table(headers, table_rows, max_width=64)


def sanitize_path_component(value, fallback: str = "untitled") -> str:
    text = stringify(value).strip()
    text = re.sub(r"[\\/:\0]", "-", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip(".")
    return text or fallback


def local_course_files_root(course: dict | None) -> Path:
    course_name = sanitize_path_component(course_code(course) or (course or {}).get("id") or "course")
    return CANVAS_FILES_ROOT / course_name


def missing_course_message(command: str) -> str:
    return f'Please specify course, e.g. canvas {command} "RHE 306"'


def require_course_filter(command: str, filters: list[str] | None = None) -> None:
    if not filters:
        raise SystemExit(missing_course_message(command))


def require_files_course_filter(filters: list[str] | None = None) -> None:
    require_course_filter("files", filters)


def render_local_files_listing(path: Path) -> str:
    result = subprocess.run(["/bin/ls", "-Cp", str(path)], text=True, capture_output=True, check=False)
    if result.returncode == 0:
        return result.stdout.rstrip("\n")
    return ""


def canvas_file_name(file_info: dict) -> str:
    return sanitize_path_component(file_info.get("display_name") or file_info.get("filename") or file_info.get("name") or "file")


def canvas_folder_name(folder: dict) -> str:
    return sanitize_path_component(folder.get("name") or folder.get("full_name") or folder.get("id") or "folder")


def uniquify_path(parent: Path, name: str, used_names: set[str]) -> Path:
    path = parent / name
    stem = path.stem
    suffix = path.suffix
    counter = 2
    key = path.name.lower()
    while key in used_names:
        candidate = parent / f"{stem} ({counter}){suffix}"
        key = candidate.name.lower()
        path = candidate
        counter += 1
    used_names.add(key)
    return path


def backup_existing_path(path: Path, mirror_root: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_root = Path.home() / "Downloads" / "canvas-files-backups" / timestamp / mirror_root.name
    rel = path.relative_to(mirror_root)
    backup_path = backup_root / rel
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    counter = 2
    original = backup_path
    while backup_path.exists():
        backup_path = original.with_name(f"{original.stem} ({counter}){original.suffix}")
        counter += 1
    path.rename(backup_path)
    return backup_path


def write_webloc_link(url: str, target: Path) -> None:
    link_dir = target.parent / ".canvas-links"
    link_dir.mkdir(parents=True, exist_ok=True)
    webloc = link_dir / f"{target.name}.webloc"
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict><key>URL</key><string>'
        + html.escape(url, quote=True)
        + '</string></dict></plist>\n'
    )
    webloc.write_text(body)
    if target.exists() or target.is_symlink():
        target.unlink()
    target.symlink_to(webloc)


def target_webloc_matches_url(target: Path, url: str) -> bool:
    if not target.is_symlink():
        return False
    try:
        body = target.resolve().read_text()
    except OSError:
        return False
    escaped_url = html.escape(url, quote=True)
    return escaped_url in body or url in html.unescape(body)


def canvas_file_preview_url(file_info: dict, course_id, folder_path: str = "") -> str:
    file_id = file_info.get("id") or file_info.get("uuid")
    if not course_id or not file_id:
        return file_info.get("html_url") or file_info.get("url") or ""
    folder_path = (folder_path or "").strip("/")
    if folder_path:
        encoded_folder = quote(folder_path, safe="/")
        return f"{BASE_URL}/courses/{course_id}/files/folder/{encoded_folder}?preview={file_id}"
    return f"{BASE_URL}/courses/{course_id}/files?preview={file_id}"


def write_canvas_file(req, file_info: dict, target: Path, mirror_root: Path, course_id=None, folder_path: str = "") -> str:
    url = canvas_file_preview_url(file_info, course_id, folder_path)
    if not url:
        return "skipped"
    if target_webloc_matches_url(target, url):
        return "unchanged"
    if target.exists() or target.is_symlink():
        backup_existing_path(target, mirror_root)
    write_webloc_link(url, target)
    return "linked"


def download_canvas_folder_tree(req, folder: dict, local_root: Path, course_id=None) -> dict:
    local_root.mkdir(parents=True, exist_ok=True)
    result = {"root": str(local_root), "folders": 0, "files": 0, "downloaded": 0, "linked": 0, "unchanged": 0, "skipped": 0}

    def sync_folder(folder_id, local_dir: Path, folder_path: str = ""):
        child_folders = fetch_paginated(req, f"/api/v1/folders/{folder_id}/folders?per_page=100")
        files = fetch_paginated(req, f"/api/v1/folders/{folder_id}/files?per_page=100&sort=name&order=asc")
        used_names: set[str] = set()
        for child in sorted([item for item in child_folders if isinstance(item, dict)], key=lambda item: canvas_folder_name(item).lower()):
            child_name = canvas_folder_name(child)
            child_path = uniquify_path(local_dir, child_name, used_names)
            if child_path.exists() and not child_path.is_dir():
                backup_existing_path(child_path, local_root)
            child_path.mkdir(parents=True, exist_ok=True)
            result["folders"] += 1
            child_folder_path = "/".join(part for part in [folder_path, child_name] if part)
            sync_folder(child.get("id"), child_path, child_folder_path)
        for file_info in sorted([item for item in files if isinstance(item, dict)], key=lambda item: canvas_file_name(item).lower()):
            target = uniquify_path(local_dir, canvas_file_name(file_info), used_names)
            status = write_canvas_file(req, file_info, target, local_root, course_id=course_id, folder_path=folder_path)
            result["files"] += 1
            if status in result:
                result[status] += 1

    folder_id = folder.get("id") if isinstance(folder, dict) else None
    if not folder_id:
        raise SystemExit("Selected Canvas course has no files root folder")
    sync_folder(folder_id, local_root)
    return result


def fetch_course_root_folder(req, course_id):
    resp = req.get(f"/api/v1/courses/{course_id}/folders/root")
    ensure_ok(resp)
    return resp.json()


def render_files_sync_result(result: dict) -> str:
    root = result.get("root", "")
    listing = render_local_files_listing(Path(root)) if root else ""
    return f"{root}\n\n{listing}".rstrip()


MODULE_ROW_KEYS = ["course", "module", "items", "state"]


def module_position(module: dict) -> float:
    value = as_number(module.get("position"))
    return value if value is not None else 10**9


def module_state(module: dict) -> str:
    if "published" in module:
        return "published" if module.get("published") else "unpublished"
    return stringify(module.get("state") or module.get("workflow_state") or "")


def module_rows(modules: list[dict], course: dict | None = None) -> list[dict]:
    rows = []
    course_value = course_code(course)
    for module in sorted([item for item in modules if isinstance(item, dict)], key=lambda item: (module_position(item), stringify(item.get("name")).lower())):
        name = stringify(module.get("name")).strip()
        if not name:
            continue
        rows.append({
            "course": course_value,
            "module": name,
            "items": module.get("items_count") if module.get("items_count") is not None else "",
            "state": module_state(module),
        })
    return rows


def render_modules(rows: list[dict]) -> str:
    headers = ["Course", "Module", "Items", "State"]
    table_rows = [[row.get(key, "") for key in MODULE_ROW_KEYS] for row in rows]
    return f"{len(rows)} modules\n\n" + render_table(headers, table_rows, max_width=64)


def page_title(page: dict) -> str:
    return page.get("title") or page.get("url") or page.get("html_url") or ""


def page_rows(pages: list[dict], course: dict | None = None) -> list[dict]:
    rows = []
    seen = set()
    course_value = course_code(course)
    for page in pages:
        if not isinstance(page, dict):
            continue
        title = page_title(page).strip()
        key = (course_value.lower(), title.lower())
        if not title or key in seen:
            continue
        seen.add(key)
        rows.append({"course": course_value, "title": title})
    rows.sort(key=lambda row: ((row.get("course") or "").lower(), (row.get("title") or "").lower()))
    return rows


def render_pages(rows: list[dict]) -> str:
    headers = ["Course", "Title"]
    table_rows = [[row.get("course", ""), row.get("title", "")] for row in rows]
    return f"{len(rows)} pages\n\n" + render_table(headers, table_rows, max_width=64)


ASSIGNMENT_ROW_KEYS = ["course", "name", "due_date", "submitted_date", "due_time", "submitted_time"]


def split_canvas_datetime_local(value: str | None) -> tuple[str, str]:
    if not value:
        return "", ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        local = dt.astimezone()
        return local.date().isoformat(), local.strftime("%H:%M")
    except Exception:
        return split_canvas_datetime(value)


def assignment_submitted(assignment: dict) -> bool:
    submission = assignment.get("submission") or {}
    if not isinstance(submission, dict):
        return False
    workflow_state = str(submission.get("workflow_state") or "").lower()
    return bool(submission.get("submitted_at") or workflow_state in {"submitted", "graded", "pending_review"})


def assignment_submission_datetime(assignment: dict) -> tuple[str, str]:
    submission = assignment.get("submission") or {}
    if not isinstance(submission, dict) or not assignment_submitted(assignment):
        return "-", "-"
    submitted_at = submission.get("submitted_at")
    if not submitted_at:
        return "-", "-"
    date, time = split_canvas_datetime_local(submitted_at)
    return (date or "-", time or "-")


def assignment_rows(assignments: list[dict], course: dict | None = None) -> list[dict]:
    rows = []
    course_value = course_code(course)
    for assignment in assignments:
        if not isinstance(assignment, dict):
            continue
        due_date, due_time = split_canvas_datetime_local(assignment.get("due_at"))
        submitted_date, submitted_time = assignment_submission_datetime(assignment)
        rows.append({
            "course": course_value,
            "name": assignment.get("name") or "",
            "due_date": due_date,
            "due_time": due_time,
            "submitted_date": submitted_date,
            "submitted_time": submitted_time,
        })
    rows.sort(key=lambda row: ((row.get("course") or "").lower(), row.get("due_date") or "9999-99-99", row.get("due_time") or "99:99", row.get("name") or ""))
    return rows


def render_assignments(rows: list[dict]) -> str:
    headers = ["Course", "Name", "Due Date", "Submitted Date", "Due Time", "Submitted Time"]
    table_rows = [[row.get(key, "") for key in ASSIGNMENT_ROW_KEYS] for row in rows]
    return f"{len(rows)} assignments\n\n" + render_table(headers, table_rows, max_width=64)


def _assignment_datetime_sort_value(row: dict) -> datetime | None:
    date = (row.get("due_date") or "").strip()
    if not date:
        return None
    time = (row.get("due_time") or "00:00").strip() or "00:00"
    try:
        return datetime.fromisoformat(f"{date}T{time}")
    except Exception:
        return None


def sort_assignment_rows(rows: list[dict], sort_by: str | None = None) -> list[dict]:
    if sort_by == "date":
        def date_key(row: dict):
            value = _assignment_datetime_sort_value(row)
            if value is None:
                return (1, 0.0, (row.get("course") or "").lower(), (row.get("name") or "").lower())
            return (0, -value.timestamp(), (row.get("course") or "").lower(), (row.get("name") or "").lower())

        return sorted(rows, key=date_key)
    if sort_by == "name":
        return sorted(rows, key=lambda row: ((row.get("course") or "").lower(), (row.get("name") or "").lower(), row.get("due_date") or "", row.get("due_time") or ""))
    return rows


def course_search_text(course: dict) -> str:
    return " ".join(stringify(course.get(key)) for key in ("id", "course_code")).lower()


def compact_course_search_text(text: str) -> str:
    return re.sub(r"\s+", "", stringify(text).lower())


def course_filter_query(filters: list[str] | None = None) -> str:
    filters = filters or []
    if not filters:
        return ""
    if len(filters) > 1:
        raise SystemExit('Course filters with spaces must be quoted, e.g. canvas <cmd> "RHE 306"')
    return filters[0].strip()


def select_courses(courses: list[dict], filters: list[str] | None = None) -> list[dict]:
    query = course_filter_query(filters).lower()
    if not query:
        return courses
    selected = []
    compact_query = compact_course_search_text(query)
    matches = []
    for course in courses:
        text = course_search_text(course)
        if query in text or (compact_query and compact_query in compact_course_search_text(text)):
            matches.append(course)
    selected.extend(matches)
    deduped = []
    seen = set()
    for course in selected:
        course_id = course.get("id")
        if course_id not in seen:
            deduped.append(course)
            seen.add(course_id)
    if not deduped:
        choices = ", ".join(f"{c.get('id')}:{c.get('course_code') or c.get('name')}" for c in courses)
        raise SystemExit(f"No active course matched filter: {' '.join(filters or [])}\nAvailable: {choices}")
    return deduped


def select_single_course(courses: list[dict], filters: list[str] | None = None, command: str = "<cmd>") -> dict:
    query = course_filter_query(filters)
    if not query:
        raise SystemExit(missing_course_message(command))
    matches = select_courses(courses, [query])
    if len(matches) > 1:
        choices = ", ".join(stringify(c.get("course_code") or c.get("code") or c.get("name") or "Unknown course") for c in matches)
        raise SystemExit(f"Course filter matched multiple active courses: {choices}")
    return matches[0]


def require_pages_course_filter(filters: list[str] | None = None) -> None:
    require_course_filter("pages", filters)


def dashboard_home_url() -> str:
    return BASE_URL


def course_home_url(course: dict) -> str:
    course_id = course.get("id") if isinstance(course, dict) else None
    if not course_id:
        raise SystemExit("Selected course has no Canvas ID")
    return f"{BASE_URL}/courses/{course_id}"


def course_syllabus_url(course: dict) -> str:
    course_id = course.get("id") if isinstance(course, dict) else None
    if not course_id:
        raise SystemExit("Selected course has no Canvas ID")
    return f"{BASE_URL}/courses/{course_id}/assignments/syllabus"


def open_url(url: str) -> None:
    subprocess.run(["open", url], check=True)


def announcement_endpoint(courses: list[dict]) -> str:
    now = datetime.now(timezone.utc)
    params = []
    for course in courses:
        course_id = course.get("id")
        if course_id:
            params.append(("context_codes[]", f"course_{course_id}"))
    params.extend([
        ("start_date", (now - timedelta(days=365)).date().isoformat()),
        ("end_date", (now + timedelta(days=365)).date().isoformat()),
        ("per_page", "100"),
    ])
    return "/api/v1/announcements?" + urlencode(params)


def parse_response_json(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def canvas_error_messages(resp) -> list[str]:
    data = parse_response_json(resp.text())
    messages = []
    if isinstance(data, dict):
        status = data.get("status")
        if status:
            messages.append(str(status))
        errors = data.get("errors") or []
        if isinstance(errors, list):
            for error in errors:
                if isinstance(error, dict) and error.get("message"):
                    messages.append(str(error.get("message")))
    return messages



def canvas_access_token_from_state_file(state_file: Path) -> str | None:
    try:
        data = json.loads(state_file.read_text())
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    auth = data.get(CANVAS_CLI_AUTH_KEY)
    if not isinstance(auth, dict) or auth.get("type") != "bearer_token":
        return None
    token = auth.get("access_token")
    if not token:
        return None
    return str(token)


def canvas_request_context_kwargs(state_file: Path) -> dict:
    token = canvas_access_token_from_state_file(state_file)
    if token:
        return {
            "base_url": BASE_URL,
            "extra_http_headers": {"Authorization": f"Bearer {token}"},
        }
    return {"base_url": BASE_URL, "storage_state": str(state_file)}


def verify_canvas_state_file(playwright, state_file: Path) -> dict:
    req = playwright.request.new_context(**canvas_request_context_kwargs(state_file))
    interrupted = False
    try:
        resp = req.get("/api/v1/users/self/profile", timeout=10000)
        if resp.status != 200:
            preview = ""
            try:
                preview = resp.text()[:200].replace("\n", " ")
            except Exception:
                pass
            raise SystemExit(f"Imported Canvas cookies did not authenticate (HTTP {resp.status}). Copy a fresh Canvas request after logging in. {preview}".strip())
        data = resp.json()
        if not isinstance(data, dict) or not data.get("id"):
            raise SystemExit("Imported Canvas cookies reached Canvas but did not return a valid profile.")
        return data
    except KeyboardInterrupt:
        interrupted = True
        raise SystemExit(AUTH_CANCELLED_MESSAGE)
    finally:
        if hasattr(req, "dispose"):
            try:
                req.dispose()
            except KeyboardInterrupt:
                if not interrupted:
                    raise SystemExit(AUTH_CANCELLED_MESSAGE)
            except Exception:
                pass


def import_canvas_storage_state(state: dict, playwright, state_file: Path = STATE_FILE) -> dict:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = state_file.with_name(state_file.name + ".tmp")
    tmp_file.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    os.chmod(tmp_file, 0o600)
    try:
        profile = verify_canvas_state_file(playwright, tmp_file)
        os.replace(tmp_file, state_file)
        os.chmod(state_file, 0o600)
        return profile
    finally:
        try:
            if tmp_file.exists():
                tmp_file.unlink()
        except OSError:
            pass


def chrome_profile_names(config: dict | None = None) -> list[str]:
    value = configured_profile_value(config, "CANVAS_CHROME_PROFILES", "chrome_profiles", ["Default"])
    return normalize_profile_names(value, ["Default"])


def configured_profile_value(config: dict | None, env_key: str, config_key: str, default):
    config = config or {}
    env_profiles = os.environ.get(env_key)
    return env_profiles if env_profiles is not None else config.get(config_key, default)


def normalize_profile_names(value, default: list[str]) -> list[str]:
    if isinstance(value, str):
        profiles = [part.strip() for part in value.split(",")]
    elif isinstance(value, list):
        profiles = [stringify(part).strip() for part in value]
    else:
        profiles = default
    return [profile for profile in profiles if profile]


def chrome_user_data_dir(config: dict | None = None) -> Path:
    config = config or {}
    value = os.environ.get("CANVAS_CHROME_USER_DATA_DIR") or config.get("chrome_user_data_dir")
    return Path(value).expanduser() if value else CHROME_USER_DATA_DIR


def profile_cookie_db_paths(base: Path, profiles: list[str]) -> list[Path]:
    paths = []
    for profile in profiles:
        profile_dir = base / profile
        for relative in ("Cookies", "Network/Cookies"):
            path = profile_dir / relative
            if path.exists() and path not in paths:
                paths.append(path)
    return paths


def chrome_cookie_db_paths(config: dict | None = None) -> list[Path]:
    direct = os.environ.get("CANVAS_CHROME_COOKIE_DB") or (config or {}).get("chrome_cookie_db")
    if direct:
        return [Path(direct).expanduser()]
    return profile_cookie_db_paths(chrome_user_data_dir(config), chrome_profile_names(config))


def safe_storage_password(service: str) -> str | None:
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-w", "-s", service],
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except Exception:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.rstrip("\n")


def chrome_safe_storage_password() -> str | None:
    return safe_storage_password("Chrome Safe Storage")


def chrome_cookie_decryption_key() -> bytes | None:
    password = chrome_safe_storage_password()
    if not password:
        return None
    return hashlib.pbkdf2_hmac("sha1", password.encode("utf-8"), b"saltysalt", 1003, 16)


def decrypt_chrome_cookie_value(host_key: str, value: str, encrypted_value: bytes, key: bytes | None, meta_version: int = 0) -> str:
    if value:
        return value
    if not encrypted_value or key is None:
        return ""
    encrypted = bytes(encrypted_value)
    if encrypted.startswith((b"v10", b"v11")):
        encrypted = encrypted[3:]
    iv = b" " * 16
    try:
        result = subprocess.run(
            ["openssl", "enc", "-aes-128-cbc", "-d", "-K", key.hex(), "-iv", iv.hex(), "-nopad"],
            input=encrypted,
            capture_output=True,
            check=False,
            timeout=5,
        )
    except Exception:
        return ""
    if result.returncode != 0 or not result.stdout:
        return ""
    plaintext = result.stdout
    pad = plaintext[-1]
    if 1 <= pad <= 16 and plaintext.endswith(bytes([pad]) * pad):
        plaintext = plaintext[:-pad]
    host_hash = hashlib.sha256(host_key.encode("utf-8")).digest()
    if meta_version >= 24 and plaintext.startswith(host_hash):
        plaintext = plaintext[len(host_hash):]
    try:
        return plaintext.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def chrome_time_to_unix(expires_utc: int | None) -> int:
    try:
        value = int(expires_utc or 0)
    except Exception:
        return -1
    if value <= 0:
        return -1
    return max(0, value // 1_000_000 - CHROME_COOKIE_EPOCH_OFFSET)


def chrome_samesite(value) -> str:
    try:
        code = int(value)
    except Exception:
        code = -1
    return {0: "None", 1: "Lax", 2: "Strict"}.get(code, "Lax")


def copy_sqlite_db_for_read(source: Path, destination: Path) -> None:
    shutil.copy2(source, destination)
    for suffix in ("-wal", "-shm"):
        sidecar = source.with_name(source.name + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, destination.with_name(destination.name + suffix))


def read_canvas_cookies_from_chrome_db(cookie_db: Path, key: bytes | None = None) -> list[dict]:
    if not cookie_db.exists():
        return []
    cookies = []
    with tempfile.TemporaryDirectory(prefix="canvas-chrome-cookies-") as tmpdir:
        tmp_db = Path(tmpdir) / "Cookies"
        copy_sqlite_db_for_read(cookie_db, tmp_db)
        con = sqlite3.connect(str(tmp_db))
        try:
            meta_row = con.execute("select value from meta where key='version'").fetchone()
            try:
                meta_version = int(meta_row[0]) if meta_row else 0
            except Exception:
                meta_version = 0
            rows = con.execute(
                """
                select host_key, name, value, encrypted_value, path, expires_utc,
                       is_secure, is_httponly, samesite
                from cookies
                where host_key = ? or host_key = ? or host_key like ?
                """,
                (CANVAS_HOST, f".{CANVAS_HOST}", f"%.{CANVAS_HOST}"),
            ).fetchall()
        finally:
            con.close()
    now = int(time.time())
    for host_key, name, value, encrypted_value, path, expires_utc, is_secure, is_httponly, samesite in rows:
        cookie_value = decrypt_chrome_cookie_value(host_key, value or "", encrypted_value, key, meta_version=meta_version)
        if not name or not cookie_value:
            continue
        expires = chrome_time_to_unix(expires_utc)
        if expires != -1 and expires <= now:
            continue
        cookies.append({
            "name": name,
            "value": cookie_value,
            "domain": host_key,
            "path": path or "/",
            "expires": expires,
            "httpOnly": bool(is_httponly),
            "secure": bool(is_secure),
            "sameSite": chrome_samesite(samesite),
        })
    return cookies


def storage_state_from_chrome(config: dict | None = None) -> dict | None:
    cookie_dbs = chrome_cookie_db_paths(config)
    key = chrome_cookie_decryption_key()
    return storage_state_from_cookie_dbs(cookie_dbs, [key] if key else [])


def storage_state_from_cookie_dbs(cookie_dbs: list[Path], keys: list[bytes]) -> dict | None:
    if not cookie_dbs:
        return None
    cookies = []
    seen = set()
    key_candidates = [None] + [key for key in keys if key is not None]
    for cookie_db in cookie_dbs:
        db_cookies = []
        for key in key_candidates:
            db_cookies = read_canvas_cookies_from_chrome_db(cookie_db, key=key)
            if db_cookies:
                break
        for cookie in db_cookies:
            identity = (cookie.get("domain"), cookie.get("path"), cookie.get("name"))
            if identity in seen:
                continue
            seen.add(identity)
            cookies.append(cookie)
    if not cookies:
        return None
    return {"cookies": cookies, "origins": []}


def import_canvas_state_from_chrome(playwright, state_file: Path = STATE_FILE, config: dict | None = None) -> dict | None:
    state = storage_state_from_chrome(config)
    if not state:
        return None
    return import_canvas_storage_state(state, playwright, state_file=state_file)


class CanvasRequestSession:
    def __init__(self, playwright, state_file: Path = STATE_FILE, config: dict | None = None):
        self.playwright = playwright
        self.state_file = state_file
        self.config = config or {}
        self.req = self.new_context()
        self.tried_auth_refresh = False

    def new_context(self):
        return self.playwright.request.new_context(**canvas_request_context_kwargs(self.state_file))

    def recreate_context(self) -> None:
        if hasattr(self.req, "dispose"):
            try:
                self.req.dispose()
            except Exception:
                pass
        self.req = self.new_context()

    def refresh_auth_from_browser_session(self) -> bool:
        forget_canvas_state_file(self.state_file)
        _, profile = import_canvas_state_from_browser_session(self.playwright, state_file=self.state_file, config=self.config)
        if profile is None:
            return False
        self.recreate_context()
        return True

    def wait_for_authentication(self) -> bool:
        forget_canvas_state_file(self.state_file)
        _, profile = wait_for_canvas_authentication(self.playwright, state_file=self.state_file, config=self.config)
        if profile is None:
            return False
        self.recreate_context()
        return True

    def get(self, endpoint: str):
        resp = self.req.get(endpoint)
        if resp.status != 401 or self.tried_auth_refresh:
            return resp
        self.tried_auth_refresh = True
        if self.refresh_auth_from_browser_session():
            return self.req.get(endpoint)
        if self.wait_for_authentication():
            return self.req.get(endpoint)
        request_canvas_authentication(state_file=self.state_file, config=self.config)


AUTH_REQUIRED_MESSAGE = "Authenticating..."
AUTH_CANCELLED_MESSAGE = "Authentication cancelled."
MANUAL_STATE_HELP = "Open Canvas in Google Chrome and log in, then rerun this command."


def config_bool(config: dict | None, key: str, env_key: str, default: bool = False) -> bool:
    config = config or {}
    value = os.environ.get(env_key, config.get(key, default))
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def canvas_auto_chrome_import_enabled(config: dict | None = None) -> bool:
    return config_bool(config, "auto_import_chrome_cookies", "CANVAS_AUTO_CHROME_IMPORT", True)


def canvas_auto_open_chrome_enabled(config: dict | None = None) -> bool:
    return config_bool(config, "auto_open_chrome_for_login", "CANVAS_AUTO_OPEN_CHROME", True)


def canvas_chrome_login_timeout_seconds(config: dict | None = None) -> int:
    value = os.environ.get("CANVAS_CHROME_LOGIN_TIMEOUT_SECONDS") or (config or {}).get("chrome_login_timeout_seconds", 120)
    try:
        seconds = int(value)
    except Exception:
        return 120
    return max(30, seconds)


def canvas_auth_poll_interval_seconds(config: dict | None = None) -> float:
    value = os.environ.get("CANVAS_AUTH_POLL_INTERVAL_SECONDS") or (config or {}).get("auth_poll_interval_seconds", 0.25)
    try:
        seconds = float(value)
    except Exception:
        return 0.25
    return min(max(seconds, 0.1), 5.0)


def import_canvas_state_from_chrome_if_available(playwright, state_file: Path = STATE_FILE, config: dict | None = None) -> dict | None:
    if not canvas_auto_chrome_import_enabled(config):
        return None
    try:
        return import_canvas_state_from_chrome(playwright, state_file=state_file, config=config)
    except SystemExit:
        return None


def run_osascript(script: str) -> str | None:
    try:
        result = subprocess.run(
            ["osascript"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def import_canvas_state_from_browser_session(playwright, state_file: Path = STATE_FILE, config: dict | None = None) -> tuple[str | None, dict | None]:
    profile = import_canvas_state_from_chrome_if_available(playwright, state_file=state_file, config=config)
    if profile is not None:
        return "Chrome", profile
    return None, None


def focus_existing_chrome_canvas_tab() -> bool:
    script = f"""
tell application {json.dumps(CHROME_APP_NAME)}
    repeat with browserWindow in windows
        set tabIndex to 1
        repeat with browserTab in tabs of browserWindow
            if URL of browserTab starts with {json.dumps(BASE_URL)} then
                set active tab index of browserWindow to tabIndex
                set index of browserWindow to 1
                activate
                return "found"
            end if
            set tabIndex to tabIndex + 1
        end repeat
    end repeat
end tell
""".strip()
    return run_osascript(script) == "found"


def open_canvas_in_chrome(config: dict | None = None) -> None:
    del config
    if focus_existing_chrome_canvas_tab():
        return
    args = ["open", "-a", CHROME_APP_NAME, BASE_URL]
    subprocess.run(args, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def forget_canvas_state_file(state_file: Path = STATE_FILE) -> None:
    try:
        state_file.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def request_canvas_authentication(state_file: Path | None = None, config: dict | None = None) -> None:
    if state_file is not None:
        forget_canvas_state_file(state_file)
    open_canvas_in_chrome(config=config)
    raise SystemExit(AUTH_REQUIRED_MESSAGE)


def wait_for_canvas_authentication(playwright, state_file: Path = STATE_FILE, config: dict | None = None) -> tuple[str | None, dict | None]:
    if not canvas_auto_open_chrome_enabled(config):
        raise SystemExit(AUTH_REQUIRED_MESSAGE)
    print(AUTH_REQUIRED_MESSAGE, file=sys.stderr, flush=True)
    open_canvas_in_chrome(config=config)
    deadline = time.monotonic() + canvas_chrome_login_timeout_seconds(config)
    poll_interval = canvas_auth_poll_interval_seconds(config)
    while True:
        source, profile = import_canvas_state_from_browser_session(playwright, state_file=state_file, config=config)
        if profile is not None:
            return source, profile
        if time.monotonic() >= deadline:
            raise SystemExit(AUTH_REQUIRED_MESSAGE)
        time.sleep(poll_interval)


def ensure_ok(resp):
    if resp.status == 401:
        request_canvas_authentication()
    if resp.status == 403:
        messages = canvas_error_messages(resp)
        detail = "; ".join(messages)
        lower_detail = detail.lower()
        if "user not authorized" in lower_detail or "unauthorized" in lower_detail:
            raise SystemExit("Canvas access denied (403); this course/resource is not available to your account.")
        raise SystemExit(f"Canvas auth failed (403); {MANUAL_STATE_HELP}")
    if resp.status < 200 or resp.status >= 300:
        raise SystemExit(f"HTTP {resp.status}: {resp.text()[:500]}")


def fetch_paginated(req, endpoint: str):
    out = []
    url = endpoint
    while url:
        resp = req.get(url)
        ensure_ok(resp)
        data = resp.json()
        if not isinstance(data, list):
            raise SystemExit("Paginated Canvas endpoint expected each page to be a JSON array")
        out.extend(data)
        url = next_url_from_link(resp.headers.get("link"))
    return out



def main(argv=None):
    try:
        return _main(argv)
    except KeyboardInterrupt:
        raise SystemExit(AUTH_CANCELLED_MESSAGE)


def _main(argv=None):
    args = parse_args(argv)
    if args.endpoint is None:
        sys.stdout.write(HELP_TEXT)
        return

    config = load_config(CONFIG_FILE)
    command, filters = resolve_shortcut_command(args.endpoint, args.filters)

    if command == "setup":
        setup_canvas_institution(filters, config, config_file=CONFIG_FILE)
        return

    apply_canvas_base_url(config, state_file=STATE_FILE)

    if not STATE_FILE.exists():
        with sync_playwright() as p:
            _, profile = import_canvas_state_from_browser_session(p, state_file=STATE_FILE, config=config)
            if profile is None:
                _, profile = wait_for_canvas_authentication(p, state_file=STATE_FILE, config=config)
        if profile is None:
            request_canvas_authentication(config=config)

    endpoint = None if command in SHORTCUT_COMMANDS else normalize_endpoint(args.endpoint)
    with sync_playwright() as p:
        req = CanvasRequestSession(p, state_file=STATE_FILE, config=config)
        if command == "courses":
            courses = fetch_paginated(req, "/api/v1/courses?enrollment_state=active&include[]=sections&include[]=teachers&per_page=100")
            courses = filter_hidden_courses(courses, config)
            if filters:
                courses = select_courses(courses, filters)
            rows = course_rows(courses)
            if args.json:
                print(json.dumps(rows, indent=2, ensure_ascii=False))
            else:
                print_human(render_courses(rows), config)
            return

        if command == "home":
            if filters:
                courses = fetch_paginated(req, "/api/v1/courses?enrollment_state=active&per_page=100")
                url = course_home_url(select_single_course(courses, filters, command="home"))
            else:
                url = dashboard_home_url()
            if args.json:
                print(json.dumps({"url": url}, indent=2, ensure_ascii=False))
            else:
                open_url(url)
                print(f"Opened {url}")
            return

        if command == "syllabus":
            courses = fetch_paginated(req, "/api/v1/courses?enrollment_state=active&per_page=100")
            courses = filter_hidden_courses(courses, config)
            url = course_syllabus_url(select_single_course(courses, filters, command="syllabus"))
            if args.json:
                print(json.dumps({"url": url}, indent=2, ensure_ascii=False))
            else:
                open_url(url)
                print(f"Opened {url}")
            return

        if command == "modules":
            courses = fetch_paginated(req, "/api/v1/courses?enrollment_state=active&per_page=100")
            courses = filter_hidden_courses(courses, config)
            if filters:
                courses = select_courses(courses, filters)
            rows = []
            for course in courses:
                course_id = course.get("id")
                if not course_id:
                    continue
                modules = fetch_paginated(req, f"/api/v1/courses/{course_id}/modules?per_page=100")
                rows.extend(module_rows(modules, course))
            if args.json:
                print(json.dumps(rows, indent=2, ensure_ascii=False))
            else:
                print_human(render_modules(rows), config)
            return

        if command == "assignments":
            courses = fetch_paginated(req, "/api/v1/courses?enrollment_state=active&per_page=100")
            courses = filter_hidden_courses(courses, config)
            if filters:
                courses = select_courses(courses, filters)
            rows = []
            for course in courses:
                course_id = course.get("id")
                if not course_id:
                    continue
                assignments = fetch_paginated(req, f"/api/v1/courses/{course_id}/assignments?include[]=submission&per_page=100")
                rows.extend(assignment_rows(assignments, course))
            sort_by = "date" if args.date else "name" if args.name else None
            rows = sort_assignment_rows(rows, sort_by=sort_by)
            if args.json:
                print(json.dumps(rows, indent=2, ensure_ascii=False))
            else:
                print_human(render_assignments(rows), config)
            return

        if command == "grades":
            courses = fetch_paginated(req, "/api/v1/courses?enrollment_state=active&per_page=100")
            courses = filter_hidden_courses(courses, config)
            if filters:
                courses = select_courses(courses, filters)
            enrollments = fetch_paginated(req, "/api/v1/users/self/enrollments?type[]=StudentEnrollment&state[]=active&per_page=100")
            enrollment_by_course = {enrollment.get("course_id"): enrollment for enrollment in enrollments if isinstance(enrollment, dict)}
            sections = []
            for course in courses:
                course_id = course.get("id")
                if not course_id:
                    continue
                groups = fetch_paginated(req, f"/api/v1/courses/{course_id}/assignment_groups?include[]=assignments&include[]=submission&per_page=100")
                rows = grade_rows(groups, enrollment_by_course.get(course_id), course)
                sections.append({"course": course, "rows": rows})
            if args.json:
                print(json.dumps(grade_json_rows(sections), indent=2, ensure_ascii=False))
            else:
                print_human(render_grade_sections(sections), config)
            return

        if command == "announcements":
            courses = fetch_paginated(req, "/api/v1/courses?enrollment_state=active&per_page=100")
            selected_courses = select_courses(courses, filters)
            announcements = fetch_paginated(req, announcement_endpoint(selected_courses))
            announcements.sort(key=lambda item: item.get("posted_at") or item.get("created_at") or "", reverse=True)
            rows = announcement_rows(announcements, selected_courses)
            if args.json:
                print(json.dumps(rows, indent=2, ensure_ascii=False))
            else:
                print_human(render_announcements(rows), config)
            return

        if command == "people":
            courses = fetch_paginated(req, "/api/v1/courses?enrollment_state=active&per_page=100")
            courses = filter_hidden_courses(courses, config)
            selected_courses = select_courses(courses, filters)
            rows = []
            for course in selected_courses:
                course_id = course.get("id")
                if not course_id:
                    continue
                users = fetch_paginated(req, f"/api/v1/courses/{course_id}/users?enrollment_state[]=active&per_page=100")
                rows.extend(people_rows(users, course))
            rows.sort(key=lambda row: ((row.get("course") or "").lower(), (row.get("name") or "").lower()))
            if args.json:
                print(json.dumps(rows, indent=2, ensure_ascii=False))
            else:
                print_human(render_people(rows), config)
            return

        if command == "files":
            require_files_course_filter(filters)
            courses = fetch_paginated(req, "/api/v1/courses?enrollment_state=active&per_page=100")
            courses = filter_hidden_courses(courses, config)
            course = select_single_course(courses, filters, command="files")
            course_id = course.get("id")
            if not course_id:
                raise SystemExit("Selected course has no Canvas ID")
            local_root = local_course_files_root(course)
            root_folder = fetch_course_root_folder(req, course_id)
            result = download_canvas_folder_tree(req, root_folder, local_root, course_id=course_id)
            if args.json:
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                print_human(render_files_sync_result(result), config)
            return

        if command == "pages":
            require_pages_course_filter(filters)
            courses = fetch_paginated(req, "/api/v1/courses?enrollment_state=active&per_page=100")
            courses = filter_hidden_courses(courses, config)
            selected_courses = select_courses(courses, filters)
            rows = []
            for course in selected_courses:
                course_id = course.get("id")
                if not course_id:
                    continue
                pages = fetch_paginated(req, f"/api/v1/courses/{course_id}/pages?per_page=100")
                rows.extend(page_rows(pages, course))
            rows.sort(key=lambda row: ((row.get("course") or "").lower(), (row.get("title") or "").lower()))
            if args.json:
                print(json.dumps(strip_private_keys(rows), indent=2, ensure_ascii=False))
            else:
                print_human(render_pages(rows), config)
            return

        resp = req.get(endpoint)
        text = resp.text()
        ensure_ok(resp)
        data = parse_response_json(text)
        if data is None:
            print(text)
        elif args.json:
            print(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            print_human(render_human(data, endpoint), config)


if __name__ == "__main__":
    main()
