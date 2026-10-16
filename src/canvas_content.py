"""Canvas content reads, link discovery, and file downloads."""
import hashlib
import http.client
import os
import re
import ssl
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

import certifi

from canvas_api import CanvasError, api_path, fetch_json, origin


class ContentHTML(HTMLParser):
    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.base_url, self.parts, self.links, self.hidden = base_url, [], [], 0
        self.seen_links = set()

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag in {"script", "style"}:
            self.hidden += 1
        if self.hidden:
            return
        if tag in {"p", "div", "br", "tr", "h1", "h2", "h3", "h4", "li", "pre"}:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")
        if tag in {"td", "th"}:
            self.parts.append("\t")
        if tag == "img":
            label = attrs.get("data-equation-content") or attrs.get("alt")
            if label:
                self.parts.append(" " + label + " ")
        target = attrs.get("href") if tag == "a" else attrs.get("src") if tag in {"img", "iframe", "video", "audio", "source"} else None
        if target:
            url = urljoin(self.base_url, target)
            if urlsplit(url).scheme in {"http", "https"}:
                link = {"url": url, "type": tag}
                if attrs.get("data-api-endpoint"):
                    link["api_url"] = urljoin(self.base_url, attrs["data-api-endpoint"])
                if attrs.get("alt"):
                    link["label"] = attrs["alt"]
                key = tuple(sorted(link.items()))
                if key not in self.seen_links:
                    self.seen_links.add(key)
                    self.links.append(link)

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden and tag in {"p", "div", "tr", "h1", "h2", "h3", "h4", "li", "pre"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)

    def result(self):
        text = "\n".join(line.strip() for line in "".join(self.parts).splitlines())
        return re.sub(r"\n{3,}", "\n\n", text).strip(), self.links


def html_content(value, base_url):
    parser = ContentHTML(base_url)
    parser.feed(value or "")
    return parser.result()


class CanvasContent:
    def __init__(self, req, base_url):
        self.req, self.base_url, self.errors = req, base_url, []

    def get(self, path, *, list_result=False):
        return fetch_json(self.req, path, self.base_url, expect_list=list_result)

    def attempt(self, path, default, *, list_result=False):
        try:
            return self.get(path, list_result=list_result)
        except CanvasError as exc:
            self.errors.append(exc.as_dict())
            return exc.data if exc.data is not None else default

    def modules(self, course_id):
        modules = self.attempt(f"/api/v1/courses/{course_id}/modules?per_page=100", [], list_result=True)
        for module in modules:
            module["items"] = self.attempt(f"/api/v1/courses/{course_id}/modules/{module['id']}/items?per_page=100", [], list_result=True)
        return modules

    def linked_pages(self, course_id, modules):
        pages = {}
        for module in modules:
            for item in module.get("items", []):
                slug = item.get("page_url")
                if item.get("type") == "Page" and slug:
                    pages.setdefault(slug, {"url": slug, "title": item.get("title", slug), "course_id": course_id,
                                           "html_url": f"{self.base_url}/courses/{course_id}/pages/{quote(slug, safe='')}"})
        return pages

    def pages(self, course_id):
        pages = []
        try:
            return self.get(f"/api/v1/courses/{course_id}/pages?per_page=100", list_result=True)
        except CanvasError as exc:
            if exc.status not in {403, 404}:
                raise
            self.errors.append(exc.as_dict())
            pages = exc.data or []
        modules = self.modules(course_id)
        known = {page.get("url") for page in pages}
        for slug in self.linked_pages(course_id, modules):
            if slug in known:
                continue
            page = self.attempt(f"/api/v1/courses/{course_id}/pages/{quote(slug, safe='')}", None)
            if page is not None:
                pages.append(page)
        return pages

    def outline(self, course_id):
        course = self.get(f"/api/v1/courses/{course_id}?include[]=syllabus_body")
        tabs = self.attempt(f"/api/v1/courses/{course_id}/tabs?per_page=100", [], list_result=True)
        modules = self.modules(course_id)
        pages = self.attempt(f"/api/v1/courses/{course_id}/pages?per_page=100", [], list_result=True)
        linked = self.linked_pages(course_id, modules)
        for page in pages:
            linked[page.get("url") or str(page.get("page_id"))] = page
        files = self.attempt(f"/api/v1/courses/{course_id}/files?per_page=100", [], list_result=True)
        known_files = {str(item.get("id")) for item in files}
        for module in modules:
            for item in module.get("items", []):
                if item.get("type") == "File" and str(item.get("content_id")) not in known_files:
                    files.append({"id": item.get("content_id"), "display_name": item.get("title"), "html_url": item.get("html_url")})
                    known_files.add(str(item.get("content_id")))
        return {"course": course, "tabs": tabs, "modules": modules, "pages": list(linked.values()), "files": files,
                "source_url": f"{self.base_url}/courses/{course_id}"}

    def read(self, value):
        if re.search(r"[\x00-\x20\x7f\\]", value) or value.startswith("//"):
            raise CanvasError("INVALID_URL", "Use a Canvas URL or a Canvas page path.")
        url = urljoin(self.base_url + "/", value)
        parsed = urlsplit(url)
        if parsed.username is not None or parsed.password is not None or origin(url) != origin(self.base_url):
            raise CanvasError("EXTERNAL_RESOURCE", "This content uses another service. Open its source URL with that service.", data={"url": url})
        path = parsed.path.rstrip("/")
        if path.startswith("/api/v1/"):
            data = self.get(api_path(url, self.base_url))
            return self.record("api", data, url)
        course = re.fullmatch(r"/courses/(\d+)(?:/(.*))?", path)
        if course:
            cid, tail = course.group(1), course.group(2) or ""
            root = f"/api/v1/courses/{cid}"
            if tail in {"", "assignments/syllabus"}:
                data = self.get(root + "?include[]=syllabus_body")
                result = self.record("course", data, url)
                if not tail and data.get("default_view") == "wiki":
                    front = self.attempt(root + "/front_page", None)
                    if front:
                        result["front_page"] = self.record("page", front, front.get("html_url") or url)
                return result
            page = re.fullmatch(r"pages/(.+)", tail)
            if page:
                return self.record("page", self.get(root + "/pages/" + quote(unquote(page.group(1)), safe='')), url)
            assignment = re.fullmatch(r"assignments/(\d+)(?:/submissions/(self|\d+))?", tail)
            if assignment:
                aid = assignment.group(1)
                data = self.get(root + f"/assignments/{aid}?include[]=submission")
                result = self.record("assignment", data, url)
                result["submission"] = self.attempt(root + f"/assignments/{aid}/submissions/{assignment.group(2) or 'self'}?include[]=submission_comments&include[]=rubric_assessment&include[]=submission_history", None)
                return result
            item = re.fullmatch(r"(discussion_topics|quizzes|modules)/(\d+)", tail)
            if item:
                kind, iid = item.groups()
                data = self.get(root + f"/{kind}/{iid}")
                result = self.record(kind, data, url)
                if kind == "discussion_topics":
                    result["view"] = self.attempt(root + f"/{kind}/{iid}/view", None)
                if kind == "modules":
                    result["items"] = self.attempt(root + f"/modules/{iid}/items?per_page=100", [], list_result=True)
                return result
            file_match = re.fullmatch(r"files/(\d+)(?:/(?:download|preview))?", tail)
            file_id = file_match.group(1) if file_match else parse_qs(parsed.query).get("preview", [None])[0] if tail.startswith("files") else None
            if file_id and str(file_id).isdigit():
                return self.record("file", self.get(f"/api/v1/files/{file_id}"), url)
            if tail.startswith("external_tools/"):
                raise CanvasError("EXTERNAL_RESOURCE", "This Canvas link opens an external tool. Use its browser page or service connection.", data={"url": url})
        file_match = re.fullmatch(r"/files/(\d+)(?:/(?:download|preview))?", path)
        if file_match:
            return self.record("file", self.get(f"/api/v1/files/{file_match.group(1)}"), url)
        inbox = re.fullmatch(r"/conversations/(\d+)", path)
        if inbox:
            return self.record("conversation", self.get(f"/api/v1/conversations/{inbox.group(1)}?auto_mark_as_read=false"), url)
        raise CanvasError("UNSUPPORTED_URL", "This Canvas URL is not supported by read. Use course outline or an API path.", data={"url": url})

    def record(self, kind, data, source_url):
        body = ""
        if isinstance(data, dict):
            body = next((data.get(key) for key in ("body", "description", "message", "syllabus_body") if isinstance(data.get(key), str) and data.get(key)), "")
        text, links = html_content(body, source_url)
        return {"type": kind, "source_url": source_url, "data": data, "text": text, "links": links}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def download_url(url, base_url):
    parsed = urlsplit(url)
    if parsed.username is not None or parsed.password is not None or re.search(r"[\x00-\x20\x7f\\]", url):
        raise CanvasError("INVALID_URL", "The file download URL is invalid.")
    if not parsed.hostname or (parsed.scheme != "https" and not (parsed.scheme == "http" and origin(url) == origin(base_url))):
        raise CanvasError("INVALID_URL", "File downloads require HTTPS.")
    return url


def download_ssl_context():
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=certifi.where())
    return context


def download_file(info, output, base_url, *, overwrite=False, max_bytes=100 * 1024 * 1024, credential_headers=None):
    """Use Canvas sign-in only on its origin; strip credentials on external hops."""
    target = Path(output).expanduser().absolute()
    if target.is_dir():
        name = re.sub(r"[\\/\x00-\x1f]", "-", info.get("display_name") or info.get("filename") or str(info.get("id", "file"))).strip(".") or "file"
        target /= name
    if (target.exists() or target.is_symlink()) and not overwrite:
        raise CanvasError("FILE_EXISTS", "The output file exists. Use --overwrite to replace it.")
    if max_bytes < 1:
        raise CanvasError("INVALID_USAGE", "The file size limit must be positive.")
    if info.get("size") is not None and int(info["size"]) > max_bytes:
        raise CanvasError("FILE_TOO_LARGE", "The file exceeds --max-bytes.")
    if info.get("locked_for_user"):
        raise CanvasError("ACCESS_DENIED", "This file is locked for your account.")
    if not info.get("url"):
        raise CanvasError("INVALID_RESPONSE", "Canvas did not provide a download URL.")
    url = download_url(urljoin(base_url, info["url"]), base_url)
    opener = build_opener(NoRedirect(), HTTPSHandler(context=download_ssl_context()))
    response, temporary = None, None
    try:
        for _ in range(11):
            try:
                headers = {"Accept-Encoding": "identity"}
                if credential_headers is not None and origin(url) == origin(base_url):
                    headers.update(credential_headers(url))
                response = opener.open(Request(url, headers=headers), timeout=30)
                break
            except HTTPError as exc:
                if exc.code not in {301, 302, 303, 307, 308} or not exc.headers.get("Location"):
                    status = exc.code; exc.close()
                    raise CanvasError("DOWNLOAD_FAILED", f"The file server returned HTTP {status}.", status=status) from exc
                following = exc.headers["Location"]; exc.close()
                url = download_url(urljoin(url, following), base_url)
        if response is None:
            raise CanvasError("REDIRECT_LIMIT", "The file server returned too many redirects.")
        expected_type = (info.get("content-type") or "").split(";")[0].lower()
        received_type = (response.headers.get("Content-Type") or "").split(";")[0].lower()
        if received_type == "text/html" and expected_type and expected_type != "text/html":
            raise CanvasError("DOWNLOAD_FAILED", "The file server returned an HTML page instead of the requested file.")
        length = response.headers.get("Content-Length")
        length = int(length) if length and length.isdigit() else None
        if length is not None and length > max_bytes:
            raise CanvasError("FILE_TOO_LARGE", "The download exceeds --max-bytes.")
        target.parent.mkdir(parents=True, exist_ok=True)
        digest, size = hashlib.sha256(), 0
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".canvas-download-", delete=False) as stream:
            temporary = Path(stream.name)
            while True:
                chunk = response.read(min(65536, max_bytes - size + 1))
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise CanvasError("FILE_TOO_LARGE", "The download exceeds --max-bytes.")
                digest.update(chunk); stream.write(chunk)
            stream.flush(); os.fsync(stream.fileno())
        if (length is not None and size != length) or (info.get("size") is not None and size != int(info["size"])):
            raise CanvasError("INCOMPLETE_DOWNLOAD", "The downloaded size does not match the file size.")
        if overwrite:
            os.replace(temporary, target)
        else:
            try:
                os.link(temporary, target)
            except FileExistsError as exc:
                raise CanvasError("FILE_EXISTS", "The output file was created by another process.") from exc
        return {"id": info.get("id"), "path": str(target), "size": size, "sha256": digest.hexdigest(),
                "content_type": info.get("content-type") or response.headers.get("Content-Type"),
                "source_url": info.get("html_url") or f"{base_url}/files/{info.get('id')}"}
    except (URLError, TimeoutError, http.client.HTTPException, OSError) as exc:
        raise CanvasError("DOWNLOAD_FAILED", "The file download failed. No incomplete file was saved.", retryable=True) from exc
    finally:
        if response is not None:
            response.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)
