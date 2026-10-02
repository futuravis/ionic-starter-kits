"""
The web server for Photo Studio.

Thin on purpose. The analysis comes from `photo_shortlist.shortlist`, and the
upload, thumbnail and export plumbing is reused from `photo_shortlist.app`
rather than written again - including the session object, so the two front ends
share one batch.

What is genuinely new here is the static file serving (this is a real site with
separate CSS and JS, not one HTML blob) and a per-photo detail route the other
UI has no use for.
"""

from __future__ import annotations

import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Optional
from urllib.parse import parse_qs, urlparse

from wedding_album import config, pipeline

from photo_shortlist import metrics as measurement, shortlist
from photo_shortlist.app import (
    MAX_FILES, MAX_UPLOAD_BYTES, MAX_WANT, MIN_WANT, SESSION,
    _parse_multipart, _photo_payload, _picks_zip, _ranking_csv,
    _result_payload, _safe_path, _store_uploads, _thumbnail,
)

from . import __version__


HOST = "127.0.0.1"
PORT = 8769                  # 8766 album, 8767 album in use, 8768 shortlist

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".json": "application/json",
}

DEFAULT_WANT = 6
GALLERY_THUMB = 420


# ============================================================================
# WORK
# ============================================================================

def _analyse(want: int, skip_duplicates: bool, workers: int,
             use_faces: bool) -> None:
    """Run the analysis on a background thread."""
    try:
        with SESSION.lock:
            paths = list(SESSION.uploads)

        if not paths:
            SESSION.finish(error="Add some photos first.")
            return

        result = shortlist.run(
            paths, want=want, workers=workers, progress=SESSION.step,
            skip_duplicates=skip_duplicates, use_faces=use_faces,
        )

        with SESSION.lock:
            SESSION.result = result
            SESSION.want = want
            SESSION.skip_duplicates = skip_duplicates

        if not result.ranked:
            SESSION.finish(error="No readable photos in this batch.")
            return

        SESSION.finish(message=result.summary())

    except Exception as exc:  # noqa: BLE001 - surfaced to the browser
        import traceback
        SESSION.finish(error=f"{type(exc).__name__}: {exc}")
        traceback.print_exc()


# ============================================================================
# PAYLOADS
# ============================================================================

def _settings_payload() -> dict:
    with SESSION.lock:
        count = len(SESSION.uploads)
        names = [os.path.basename(p) for p in SESSION.uploads]

    return {
        "version": __version__,
        "uploaded": count,
        "names": names,
        "default_want": DEFAULT_WANT,
        "min_want": MIN_WANT,
        "max_want": MAX_WANT,
        "max_files": MAX_FILES,
        "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
        "extensions": sorted(config.IMAGE_EXTENSIONS),
        "workers": pipeline.default_workers(),
        "faces_available": measurement.faces_available(),
        "ready": SESSION.result is not None,
    }


def _detail_payload(name: str) -> Optional[dict]:
    """
    One photo in full, with the other frames from its bundle.

    This is what the lightbox needs: enough to explain a single decision
    without the client holding the whole result set in memory.
    """
    with SESSION.lock:
        result = SESSION.result

    if result is None:
        return None

    target = next((c for c in result.ranked
                   if os.path.basename(c.filepath) == name), None)
    if target is None:
        return None

    siblings = []
    for group in result.groups:
        if group.index != target.group:
            continue
        siblings = [_photo_payload(c) for c in group.members
                    if os.path.basename(c.filepath) != name]
        break

    payload = _photo_payload(target)
    payload["siblings"] = siblings
    payload["total"] = result.total
    return payload


# ============================================================================
# HTTP
# ============================================================================

class Handler(BaseHTTPRequestHandler):
    server_version = f"PhotoStudio/{__version__}"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        pass

    # -- helpers -------------------------------------------------------
    def _send(self, body: bytes, ctype: str, code: int = 200,
              cache: Optional[str] = None,
              disposition: Optional[str] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if cache:
            self.send_header("Cache-Control", cache)
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(json.dumps(obj).encode("utf-8"), "application/json", code)

    def _error(self, message: str, code: int = 400) -> None:
        self._json({"error": message}, code)

    def _static(self, relative: str) -> None:
        """
        Serve a file from static/, refusing anything that escapes it.

        Same reasoning as the image routes: a URL must never be able to walk
        out of the directory it is supposed to address.
        """
        root = os.path.realpath(STATIC_DIR)
        target = os.path.realpath(os.path.join(root, relative))

        if target != root and not target.startswith(root + os.sep):
            self._error("not found", 404)
            return
        if not os.path.isfile(target):
            self._error("not found", 404)
            return

        extension = os.path.splitext(target)[1].lower()
        try:
            with open(target, "rb") as fh:
                self._send(
                    fh.read(),
                    CONTENT_TYPES.get(extension, "application/octet-stream"),
                    # Explicitly uncached. Without this the browser caches the
                    # CSS and JS heuristically, so editing a stylesheet appears
                    # to do nothing and the next person debugs the wrong file.
                    # These are local reads of a few KB; there is nothing to
                    # save by caching them.
                    cache="no-store, must-revalidate",
                )
        except OSError:
            self._error("could not read file", 500)

    # -- GET -----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)

        if route in ("/", "/index.html"):
            self._static("index.html")
            return

        if route.startswith("/static/"):
            self._static(route[len("/static/"):])
            return

        if route == "/api/settings":
            self._json(_settings_payload())
            return

        if route == "/api/progress":
            self._json(SESSION.progress())
            return

        if route == "/api/result":
            self._json(_result_payload())
            return

        if route == "/api/detail":
            payload = _detail_payload(query.get("p", [""])[0])
            if payload is None:
                self._error("no such photo", 404)
                return
            self._json(payload)
            return

        if route == "/api/thumb":
            self._image(query, GALLERY_THUMB, 1600)
            return

        if route == "/api/photo":
            self._image(query, 1600, 2400)
            return

        if route == "/api/export.zip":
            data = _picks_zip()
            if data is None:
                self._error("nothing to export yet", 400)
                return
            self._send(data, "application/zip",
                       disposition='attachment; filename="selects.zip"')
            return

        if route == "/api/export.csv":
            csv = _ranking_csv()
            if csv is None:
                self._error("nothing to export yet", 400)
                return
            self._send(csv.encode("utf-8"), "text/csv; charset=utf-8",
                       disposition='attachment; filename="ranking.csv"')
            return

        self._error("no such route", 404)

    def _image(self, query: Dict, default_width: int, cap: int) -> None:
        path = _safe_path(query.get("p", [""])[0])
        if not path:
            self._error("not found", 404)
            return
        try:
            width = int(query.get("w", [default_width])[0])
        except (TypeError, ValueError):
            width = default_width
        data = _thumbnail(path, min(cap, max(80, width)))
        if data is None:
            self._error("unreadable", 404)
            return
        self._send(data, "image/jpeg", cache="max-age=3600")

    # -- POST ----------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path).path

        if route == "/api/upload":
            self._upload()
            return

        if route == "/api/analyse":
            self._start()
            return

        if route == "/api/reset":
            if SESSION.progress()["running"]:
                self._error("Still working; wait for it to finish.", 409)
                return
            SESSION.clear()
            self._json({"ok": True, "uploaded": 0})
            return

        self._error("no such route", 404)

    def _upload(self) -> None:
        content_type = self.headers.get("Content-Type") or ""
        if "multipart/form-data" not in content_type.lower():
            self._error("expected multipart/form-data")
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self._error("missing Content-Length")
            return

        if length <= 0:
            self._error("empty upload")
            return
        if length > MAX_UPLOAD_BYTES:
            self._error(
                f"upload too large: {length // (1024 * 1024)} MB, limit is "
                f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB", 413)
            return

        body = self.rfile.read(length)

        try:
            files = _parse_multipart(body, content_type)
        except Exception as exc:  # noqa: BLE001
            self._error(f"could not read the upload: {exc}")
            return

        if not files:
            self._error("no files in the upload")
            return

        stored, skipped = _store_uploads(files)
        with SESSION.lock:
            total = len(SESSION.uploads)
            names = [os.path.basename(p) for p in SESSION.uploads]

        self._json({"stored": stored, "skipped": skipped,
                    "uploaded": total, "names": names})

    def _start(self) -> None:
        if SESSION.progress()["running"]:
            self._error("Already working.", 409)
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except (ValueError, TypeError):
            body = {}

        try:
            want = int(body.get("want", DEFAULT_WANT))
        except (TypeError, ValueError):
            self._error("'want' must be a number")
            return
        want = max(MIN_WANT, min(MAX_WANT, want))

        workers = pipeline.default_workers()
        skip_duplicates = bool(body.get("skip_duplicates", True))
        use_faces = bool(body.get("use_faces", True))

        with SESSION.lock:
            if not SESSION.uploads:
                self._error("Add some photos first.")
                return

        SESSION.begin("starting")
        threading.Thread(
            target=_analyse,
            args=(want, skip_duplicates, workers, use_faces),
            daemon=True,
        ).start()

        self._json({"started": True, "want": want})


# ============================================================================
# RUN
# ============================================================================

def serve(port: int = PORT, open_browser: bool = True) -> int:
    """Start the site and block until interrupted."""
    address = f"http://{HOST}:{port}/"

    if not os.path.isdir(STATIC_DIR):
        print(f"static directory missing: {STATIC_DIR}")
        return 1

    try:
        httpd = ThreadingHTTPServer((HOST, port), Handler)
    except OSError as exc:
        print(f"Could not start on {address}: {exc}")
        print("Another copy may already be running. Try --port with a free port.")
        return 1

    print(f"Photo Studio {__version__}")
    print(f"Serving {address}")
    print("Add photos, analyse, then work through the shoot.")
    print("Press Ctrl+C to stop.\n")

    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(address)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        httpd.server_close()
        SESSION.clear()

    return 0
