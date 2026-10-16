"""Read-only Canvas request handling. Credentials stay on the configured origin."""
import json
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit

from playwright.sync_api import Error as PlaywrightError


class CanvasError(Exception):
    def __init__(self, code, message, *, status=None, endpoint=None, retryable=False, data=None):
        super().__init__(message)
        self.code, self.status, self.endpoint = code, status, endpoint
        self.retryable, self.data = retryable, data

    def as_dict(self):
        result = {"code": self.code, "message": str(self), "retryable": self.retryable}
        if self.status is not None:
            result["status"] = self.status
        if self.endpoint:
            result["path"] = urlsplit(self.endpoint).path
        return result


def origin(url):
    parsed = urlsplit(url)
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise CanvasError("INVALID_URL", "The URL has an invalid port.") from exc
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), port


def api_path(endpoint, base_url):
    """Accept only API paths on the exact configured origin, including redirects."""
    endpoint = str(endpoint).strip()
    if not endpoint or re.search(r"[\x00-\x20\x7f\\]", endpoint) or endpoint.startswith("//"):
        raise CanvasError("INVALID_URL", "Use a Canvas API path or a full Canvas URL.")
    parsed = urlsplit(endpoint)
    if parsed.username is not None or parsed.password is not None:
        raise CanvasError("INVALID_URL", "URLs must not contain sign-in information.")
    if parsed.scheme or parsed.netloc:
        if parsed.scheme not in {"https", "http"} or origin(endpoint) != origin(base_url):
            raise CanvasError("HOST_NOT_ALLOWED", "The request must use the configured Canvas host.")
    path = parsed.path if parsed.path.startswith("/") else "/" + parsed.path
    decoded = unquote(path)
    if (not path.startswith("/api/v1/") or "\\" in decoded
            or any(part in {".", ".."} for part in decoded.split("/"))
            or re.search(r"[\x00-\x1f\x7f]", decoded)):
        raise CanvasError("INVALID_URL", "Use an /api/v1/ path for API reads.")
    # Reading a conversation must not mark its messages as read.
    query = parsed.query
    if re.fullmatch(r"/api/v1/conversations/\d+(?:\.json)?/?", decoded):
        params = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True) if k != "auto_mark_as_read"]
        params.append(("auto_mark_as_read", "false"))
        query = urlencode(params)
    return path + ("?" + query if query else "")


def next_link(header):
    for match in re.finditer(r"<([^>]+)>\s*((?:;[^,]*)?)", header or ""):
        rel = re.search(r';\s*rel\s*=\s*(?:"([^"]+)"|([^;\s,]+))', match.group(2), re.I)
        if rel and "next" in (rel.group(1) or rel.group(2)).split():
            return match.group(1)
    return None


def retry_delay(headers, attempt):
    value = headers.get("retry-after", "")
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            seconds = 2 ** attempt
    return min(30.0, max(0.0, seconds))


def request_get(req, base_url, endpoint, *, timeout=30000, retries=2):
    path = api_path(endpoint, base_url)
    seen = set()
    for _ in range(11):
        if path in seen:
            raise CanvasError("REDIRECT_LOOP", "Canvas returned a repeated redirect.", endpoint=path)
        seen.add(path)
        for attempt in range(retries + 1):
            try:
                resp = req.get(path, timeout=timeout, max_redirects=0)
            except PlaywrightError as exc:
                if attempt < retries:
                    time.sleep(min(2 ** attempt, 30))
                    continue
                raise CanvasError("NETWORK_ERROR", "The Canvas request failed or timed out.", endpoint=path, retryable=True) from exc
            if resp.status not in {429, 500, 502, 503, 504} or attempt == retries:
                break
            delay = retry_delay(resp.headers, attempt)
            resp.dispose()
            time.sleep(delay)
        if resp.status not in {301, 302, 303, 307, 308}:
            return resp
        location = resp.headers.get("location")
        resp.dispose()
        if not location:
            raise CanvasError("INVALID_RESPONSE", "Canvas returned a redirect without a destination.")
        path = api_path(urljoin(base_url + path, location), base_url)
    raise CanvasError("REDIRECT_LIMIT", "Canvas returned too many redirects.")


def ensure_response(resp, endpoint=None):
    if 200 <= resp.status < 300:
        return
    status = resp.status
    if status == 401:
        code, message = "AUTH_REQUIRED", "Sign in with canvas auth login, then repeat the command."
    elif status == 403:
        code, message = "ACCESS_DENIED", "Canvas access denied (403), this resource is not available to your account."
    elif status == 404:
        code, message = "NOT_FOUND", "Canvas could not find this resource, or its list is disabled."
    elif status == 429:
        code, message = "RATE_LIMITED", "Canvas limited the request rate. Try again later."
    else:
        code, message = "HTTP_ERROR", f"Canvas returned HTTP {status}."
    raise CanvasError(code, message, status=status, endpoint=endpoint, retryable=status in {429, 500, 502, 503, 504})


def fetch_json(req, endpoint, base_url, *, expect_list=False, max_pages=1000):
    """Read a JSON object, or every page of a JSON list. Never silently truncate."""
    path = api_path(endpoint, base_url)
    output, seen = [], set()
    try:
        while path:
            if path in seen or len(seen) >= max_pages:
                raise CanvasError("PAGINATION_LIMIT", "Canvas repeated a page or exceeded the page limit.", endpoint=path)
            seen.add(path)
            resp = req.get(path)
            try:
                ensure_response(resp, path)
                try:
                    data = resp.json()
                except (ValueError, json.JSONDecodeError) as exc:
                    raise CanvasError("INVALID_RESPONSE", "Canvas did not return valid JSON.", endpoint=path) from exc
                headers = {key.lower(): value for key, value in resp.headers.items()}
                following = next_link(headers.get("link"))
            finally:
                if hasattr(resp, "dispose"):
                    resp.dispose()
            if not isinstance(data, list):
                if expect_list or output or following:
                    raise CanvasError("INVALID_RESPONSE", "This Canvas list did not return a JSON array.", endpoint=path)
                return data
            output.extend(data)
            path = api_path(urljoin(base_url + path, following), base_url) if following else None
        return output
    except CanvasError as exc:
        if output:
            exc.data = output
        raise
