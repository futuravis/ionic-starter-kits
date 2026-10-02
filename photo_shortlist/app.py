"""
The web application: an upload page and a small JSON API over shortlist.py.

Standard library only, plus the OpenCV already required by wedding_album. No
Flask, no FastAPI, nothing added to requirements.

    python3 -m photo_shortlist

Security, stated plainly: there is no authentication. HOST is 127.0.0.1 and
that loopback binding is the entire security boundary - do not change it to
0.0.0.0 on a shared network, because the image routes would then let anyone
reachable read files from the session directory. Uploaded paths are resolved
and must land inside the session directory, so a crafted URL cannot walk out to
arbitrary files, and upload filenames are reduced to a basename for the same
reason.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import threading
import traceback
import webbrowser
import zipfile
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import cv2

from wedding_album import config, pipeline

from . import __version__, metrics as measurement, shortlist


HOST = "127.0.0.1"
PORT = 8767                      # 8765 is the old picker, 8766 the album UI
UI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.html")

THUMB_WIDTH = 360
THUMB_DIR = ".thumbs"

# Upload guards. A wedding JPEG is a few MB; these bounds are generous for the
# 20-50 photo batch this app is for and still refuse a runaway request.
MAX_UPLOAD_BYTES = 512 * 1024 * 1024
MAX_FILES = 300

DEFAULT_WANT = 5
MIN_WANT = 1
MAX_WANT = 20


# ============================================================================
# SESSION STATE
# ============================================================================

class Session:
    """
    One batch of uploads and its shortlist, held in memory.

    Single-session by design. This is a local tool for one person reviewing one
    set of photos, and pretending otherwise would mean user accounts, storage
    limits and cleanup policies for no benefit.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.directory: str = ""
        self.uploads: List[str] = []
        self.result: Optional[shortlist.Result] = None
        self.want: int = DEFAULT_WANT
        self.skip_duplicates: bool = True

        # What the last straighten pass did, one entry per uploaded file.
        self.straightened: List = []

        self.running = False
        self.stage = ""
        self.current = 0
        self.total = 0
        self.error = ""
        self.message = ""

    # -- storage -------------------------------------------------------
    def ensure_dir(self) -> str:
        with self.lock:
            if not self.directory or not os.path.isdir(self.directory):
                self.directory = tempfile.mkdtemp(prefix="photo_shortlist_")
            return self.directory

    def clear(self) -> None:
        """Drop the batch and delete its files."""
        with self.lock:
            old = self.directory
            self.directory = ""
            self.uploads = []
            self.result = None
            self.straightened = []
            self.message = ""
            self.error = ""
        if old and os.path.isdir(old):
            shutil.rmtree(old, ignore_errors=True)

    # -- progress ------------------------------------------------------
    def begin(self, stage: str) -> None:
        with self.lock:
            self.running = True
            self.stage = stage
            self.current = 0
            self.total = 0
            self.error = ""
            self.message = ""

    def step(self, stage: str, current: int, total: int) -> None:
        with self.lock:
            self.stage = stage
            self.current = current
            self.total = total

    def finish(self, message: str = "", error: str = "") -> None:
        with self.lock:
            self.running = False
            self.stage = ""
            self.message = message
            self.error = error

    def progress(self) -> dict:
        with self.lock:
            return {
                "running": self.running,
                "stage": self.stage,
                "current": self.current,
                "total": self.total,
                "error": self.error,
                "message": self.message,
                "uploaded": len(self.uploads),
                "ready": self.result is not None,
            }


SESSION = Session()


# ============================================================================
# WORK
# ============================================================================

def _do_shortlist(want: int, skip_duplicates: bool, workers: int,
                  use_faces: bool = True, drop_worst: int = 0) -> None:
    """Run the analysis on a background thread."""
    try:
        with SESSION.lock:
            paths = list(SESSION.uploads)

        if not paths:
            SESSION.finish(error="Add some photos first.")
            return

        result = shortlist.run(
            paths, want=want, workers=workers,
            progress=SESSION.step, skip_duplicates=skip_duplicates,
            use_faces=use_faces, drop_worst=drop_worst,
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
        SESSION.finish(error=f"{type(exc).__name__}: {exc}")
        traceback.print_exc()


# ============================================================================
# SERIALIZATION
# ============================================================================

def _photo_payload(candidate: shortlist.Candidate) -> dict:
    return {
        "photo": os.path.basename(candidate.filepath),
        "name": candidate.name,
        "score": candidate.percent,
        "quality": candidate.quality_percent,
        "penalty": candidate.penalty_percent,
        "rank": candidate.rank,
        "group": candidate.group,
        "group_size": candidate.group_size,
        "pick": candidate.is_pick,
        "pick_rank": candidate.pick_rank,
        "reason": candidate.reason,
        "duplicate": candidate.is_duplicate,
        "duplicate_of": (os.path.basename(candidate.duplicate_of)
                         if candidate.duplicate_of else ""),
        "breakdown": candidate.breakdown,
        "headline": candidate.headline,
        "usable": candidate.is_usable,
        "faces": candidate.face_count,
        "face_ratio": round(candidate.face_ratio, 3),
        "issues": [
            {
                "code": issue.code,
                "label": issue.label,
                "severity": issue.severity,
                "detail": issue.detail,
            }
            for issue in candidate.issues
        ],
        "dimensions": (f"{candidate.width}x{candidate.height}"
                       if candidate.width else ""),
        "orientation": candidate.orientation,
        "aspect": round(candidate.aspect, 3),
    }


def _shape_tally() -> Dict[str, int]:
    """How many photos are portrait, landscape or square."""
    with SESSION.lock:
        result = SESSION.result

    if result is None:
        return {}

    counts: Dict[str, int] = {}
    for candidate in result.ranked:
        counts[candidate.orientation] = counts.get(candidate.orientation, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def _result_payload() -> dict:
    with SESSION.lock:
        result = SESSION.result
        want = SESSION.want

    if result is None:
        return {"ready": False}

    best = result.best

    return {
        "ready": True,
        "want": want,
        "summary": result.summary(),
        "faces_probed": result.faces_probed,
        "stats": {
            "analyzed": result.total,
            "shots": len(result.groups),
            "picked": len(result.picks),
            "usable": result.usable_count,
            "flawed": result.total - result.usable_count,
            "duplicates": sum(1 for c in result.ranked if c.is_duplicate),
            "unreadable": len(result.failures),
        },
        "issue_tally": result.issue_tally,
        # How the batch breaks down by shape, so a mix of portrait and
        # landscape is visible before every tile gets cropped to one shape.
        "shape_tally": _shape_tally(),
        # Frames set aside as weakest. Listed so the removal is visible and
        # checkable rather than silent - they are still on disk.
        "dropped": [_photo_payload(c) for c in result.dropped],
        "considered": result.considered,
        "best": _photo_payload(best) if best else None,
        "picks": [_photo_payload(c) for c in result.picks],
        # The whole batch, best to worst. This is the 1-to-N ordering.
        "ranked": [_photo_payload(c) for c in result.ranked],
        "groups": [
            {
                "index": g.index,
                "size": g.size,
                "photos": [_photo_payload(c) for c in g.members],
            }
            for g in result.groups
        ],
        "failures": [
            {"name": os.path.basename(m.filepath), "error": m.error}
            for m in result.failures
        ],
    }


def _state_payload() -> dict:
    with SESSION.lock:
        count = len(SESSION.uploads)
        names = [os.path.basename(p) for p in SESSION.uploads]

    return {
        "version": __version__,
        "uploaded": count,
        "names": names[:MAX_FILES],
        "default_want": DEFAULT_WANT,
        "min_want": MIN_WANT,
        "max_want": MAX_WANT,
        "max_files": MAX_FILES,
        "extensions": sorted(config.IMAGE_EXTENSIONS),
        "workers": pipeline.default_workers(),
        "ready": SESSION.result is not None,
        # Whether face-aware faults can be reported at all. The ONNX model is
        # not in version control, so a fresh clone has it missing until
        # `python3 -m wedding_album download-models` has been run.
        "faces_available": measurement.faces_available(),
    }


# ============================================================================
# IMAGE SERVING
# ============================================================================

def _safe_path(relative: str) -> Optional[str]:
    """
    Resolve a browser-supplied name against the session directory and refuse
    anything that escapes it.

    Without this, /api/thumb?p=../../../../etc/passwd would happily serve files
    from anywhere this process can read.
    """
    with SESSION.lock:
        root = SESSION.directory

    if not root or not relative:
        return None

    root = os.path.realpath(root)
    candidate = os.path.realpath(os.path.join(root, relative))

    if candidate != root and not candidate.startswith(root + os.sep):
        return None
    if not os.path.isfile(candidate):
        return None
    return candidate


def _thumb_dir() -> str:
    path = os.path.join(SESSION.ensure_dir(), THUMB_DIR)
    os.makedirs(path, exist_ok=True)
    return path


def _thumbnail(path: str, width: int) -> Optional[bytes]:
    """
    Return a JPEG thumbnail, cached on disk.

    Decoding a 6000x4000 original for every grid tile makes the page unusable,
    so the first request pays for it and the rest are plain file reads.
    """
    key = f"{os.path.basename(path)}_{width}.jpg"
    cached = os.path.join(_thumb_dir(), key)

    try:
        if (os.path.isfile(cached)
                and os.path.getmtime(cached) >= os.path.getmtime(path)):
            with open(cached, "rb") as fh:
                return fh.read()
    except OSError:
        pass

    img = cv2.imread(path, cv2.IMREAD_REDUCED_COLOR_4)
    if img is None:
        img = cv2.imread(path, cv2.IMREAD_COLOR)
    if img is None:
        return None

    h, w = img.shape[:2]
    if w > width:
        img = cv2.resize(img, (width, int(round(h * width / w))),
                         interpolation=cv2.INTER_AREA)

    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
    if not ok:
        return None

    data = buf.tobytes()
    try:
        with open(cached, "wb") as fh:
            fh.write(data)
    except OSError:
        pass
    return data


def _picks_zip() -> Optional[bytes]:
    """
    Bundle the picks into a zip, named so the ranking survives the download.

    Built in memory: a shortlist is a handful of photos, so there is no reason
    to write a temp file and then clean it up.
    """
    with SESSION.lock:
        result = SESSION.result

    if result is None or not result.picks:
        return None

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        lines = ["Photo Shortlist - selected photos", ""]

        for candidate in result.picks:
            if not os.path.isfile(candidate.filepath):
                continue
            # Numbered by shortlist position, so the ordering survives the
            # download and the files sort correctly in any folder.
            arcname = f"{candidate.pick_rank:02d}_{candidate.name}"
            archive.write(candidate.filepath, arcname)

            lines.append(f"{candidate.pick_rank}. {arcname}")
            lines.append(f"      score {candidate.percent}  "
                         f"(overall rank {candidate.rank} of {result.total})")
            lines.append(f"      {candidate.reason}")
            for issue in candidate.issues:
                lines.append(f"      - {issue.severity}: {issue.label} "
                             f"({issue.detail})")
            lines.append("")

        # JPEGs are already compressed, so the zip is for bundling only. The
        # notes explain the choices once the photos leave this app, and the
        # full ranking goes along so nothing is lost by downloading only the
        # shortlist.
        archive.writestr("why_these_photos.txt", "\n".join(lines) + "\n")
        archive.writestr("full_ranking.csv", _ranking_csv() or "")

    return buffer.getvalue()


def _ranking_csv() -> Optional[str]:
    """
    The complete ranking as CSV, best to worst.

    Exists so the ordering can go into a spreadsheet and be handed to someone
    who is never going to open this application.
    """
    with SESSION.lock:
        result = SESSION.result

    if result is None or not result.ranked:
        return None

    def escape(value: str) -> str:
        if any(ch in value for ch in ',"\n'):
            return '"' + value.replace('"', '""') + '"'
        return value

    rows = ["rank,photo,score,shortlisted,shot_group,near_duplicate,"
            "faces,major_faults,all_faults"]

    for c in result.ranked:
        major = "; ".join(i.label for i in c.issues if i.is_major)
        every = "; ".join(f"{i.label} ({i.severity})" for i in c.issues)
        rows.append(",".join([
            str(c.rank),
            escape(c.name),
            str(c.percent),
            "yes" if c.is_pick else "",
            str(c.group),
            "yes" if c.is_duplicate else "",
            str(c.face_count) if c.face_count >= 0 else "not checked",
            escape(major),
            escape(every),
        ]))

    return "\n".join(rows) + "\n"


# ============================================================================
# UPLOADS
# ============================================================================

def _parse_multipart(body: bytes, content_type: str) -> List[Tuple[str, bytes]]:
    """
    Pull (filename, data) pairs out of a multipart/form-data body.

    Uses the stdlib email parser rather than `cgi`, which is deprecated and
    removed in Python 3.13. The body is handed over with a synthesized header
    block so the parser sees a well-formed MIME document.
    """
    header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n"
    message = BytesParser(policy=HTTP).parsebytes(header.encode("latin-1") + body)

    if not message.is_multipart():
        return []

    out: List[Tuple[str, bytes]] = []
    for part in message.iter_parts():
        filename = part.get_filename()
        if not filename:
            continue
        payload = part.get_payload(decode=True)
        if payload:
            out.append((filename, payload))
    return out


def _store_uploads(files: List[Tuple[str, bytes]]) -> Tuple[int, List[str]]:
    """
    Write accepted uploads into the session directory.

    Returns (stored, skipped messages). Filenames are reduced to a basename and
    de-duplicated, so neither a path in the filename nor two photos called
    IMG_1234.jpg can overwrite anything.
    """
    directory = SESSION.ensure_dir()
    stored = 0
    skipped: List[str] = []

    for filename, data in files:
        name = os.path.basename(filename.replace("\\", "/")).strip()
        if not name or name.startswith("."):
            skipped.append(f"{filename or '(unnamed)'}: unusable filename")
            continue

        extension = os.path.splitext(name)[1].lower()
        if extension not in config.IMAGE_EXTENSIONS:
            skipped.append(f"{name}: not a supported image type")
            continue

        with SESSION.lock:
            if len(SESSION.uploads) + stored >= MAX_FILES:
                skipped.append(f"{name}: batch limit of {MAX_FILES} reached")
                break

        stem, extension = os.path.splitext(name)
        target = os.path.join(directory, name)
        counter = 2
        while os.path.exists(target):
            target = os.path.join(directory, f"{stem}_{counter}{extension}")
            counter += 1

        try:
            with open(target, "wb") as fh:
                fh.write(data)
        except OSError as exc:
            skipped.append(f"{name}: could not be saved ({exc.strerror})")
            continue

        with SESSION.lock:
            SESSION.uploads.append(target)
        stored += 1

    if stored:
        # A new batch invalidates the old shortlist; keeping it on screen next
        # to different photos is how someone downloads the wrong set. The
        # straighten report goes with it - it described the previous files.
        with SESSION.lock:
            SESSION.result = None
            SESSION.straightened = []

    return stored, skipped


# ============================================================================
# HTTP
# ============================================================================

class Handler(BaseHTTPRequestHandler):
    server_version = f"PhotoShortlist/{__version__}"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        pass                              # keep the console for app output

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
            pass                          # browser navigated away mid-image

    def _json(self, obj, code: int = 200) -> None:
        self._send(json.dumps(obj).encode("utf-8"), "application/json", code)

    def _error(self, message: str, code: int = 400) -> None:
        self._json({"error": message}, code)

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return b""
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _json_body(self) -> dict:
        try:
            raw = self._read_body()
            return json.loads(raw or b"{}")
        except (ValueError, TypeError):
            return {}

    # -- GET -----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)

        if route in ("/", "/index.html"):
            try:
                with open(UI_PATH, "rb") as fh:
                    self._send(fh.read(), "text/html; charset=utf-8")
            except OSError:
                self._error("ui.html not found", 500)
            return

        if route == "/api/state":
            self._json(_state_payload())
            return

        if route == "/api/progress":
            self._json(SESSION.progress())
            return

        if route == "/api/result":
            self._json(_result_payload())
            return

        if route == "/api/thumb":
            path = _safe_path(query.get("p", [""])[0])
            if not path:
                self._error("not found", 404)
                return
            try:
                width = int(query.get("w", [THUMB_WIDTH])[0])
            except (TypeError, ValueError):
                width = THUMB_WIDTH
            data = _thumbnail(path, min(1600, max(80, width)))
            if data is None:
                self._error("unreadable", 404)
                return
            self._send(data, "image/jpeg", cache="max-age=3600")
            return

        if route == "/api/full":
            path = _safe_path(query.get("p", [""])[0])
            if not path:
                self._error("not found", 404)
                return
            data = _thumbnail(path, 1600)
            if data is None:
                self._error("unreadable", 404)
                return
            self._send(data, "image/jpeg", cache="max-age=3600")
            return

        if route == "/api/download":
            data = _picks_zip()
            if data is None:
                self._error("nothing to download yet", 400)
                return
            self._send(data, "application/zip",
                       disposition='attachment; filename="shortlist.zip"')
            return

        if route == "/api/ranking.csv":
            csv = _ranking_csv()
            if csv is None:
                self._error("nothing to export yet", 400)
                return
            self._send(csv.encode("utf-8"), "text/csv; charset=utf-8",
                       disposition='attachment; filename="full_ranking.csv"')
            return

        self._error("no such route", 404)

    # -- POST ----------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path).path

        if route == "/api/upload":
            self._upload()
            return

        if route == "/api/shortlist":
            self._shortlist()
            return

        if route == "/api/reset":
            if SESSION.progress()["running"]:
                self._error("Still working; wait for it to finish.", 409)
                return
            SESSION.clear()
            self._json({"ok": True, "uploaded": 0})
            return

        self._error("no such route", 404)

    # -- handlers ------------------------------------------------------
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
                f"upload too large: {length // (1024 * 1024)} MB, "
                f"limit is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB", 413)
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

        self._json({
            "stored": stored,
            "skipped": skipped,
            "uploaded": total,
            "names": [os.path.basename(p) for p in SESSION.uploads],
        })

    def _shortlist(self) -> None:
        if SESSION.progress()["running"]:
            self._error("Already working.", 409)
            return

        body = self._json_body()

        try:
            want = int(body.get("want", DEFAULT_WANT))
        except (TypeError, ValueError):
            self._error("'want' must be a number")
            return
        want = max(MIN_WANT, min(MAX_WANT, want))

        try:
            workers = int(body.get("workers", pipeline.default_workers()))
        except (TypeError, ValueError):
            workers = pipeline.default_workers()
        workers = max(1, min(16, workers))

        skip_duplicates = bool(body.get("skip_duplicates", True))
        use_faces = bool(body.get("use_faces", True))

        try:
            drop_worst = int(body.get("drop_worst", 0))
        except (TypeError, ValueError):
            drop_worst = 0
        drop_worst = max(0, drop_worst)

        with SESSION.lock:
            if not SESSION.uploads:
                self._error("Add some photos first.")
                return

        SESSION.begin("starting")
        threading.Thread(
            target=_do_shortlist,
            args=(want, skip_duplicates, workers, use_faces, drop_worst),
            daemon=True,
        ).start()

        self._json({"started": True, "want": want, "use_faces": use_faces,
                    "drop_worst": drop_worst})


# ============================================================================
# RUN
# ============================================================================

def serve(port: int = PORT, open_browser: bool = True) -> int:
    """Start the server and block until interrupted."""
    address = f"http://{HOST}:{port}/"

    try:
        httpd = ThreadingHTTPServer((HOST, port), Handler)
    except OSError as exc:
        print(f"Could not start on {address}: {exc}")
        print("Another copy may already be running. Try --port with a free port.")
        return 1

    print(f"Photo Shortlist {__version__}")
    print(f"Serving {address}")
    print("Drop 20-50 photos in the page, pick how many you want, shortlist.")
    print("Press Ctrl+C to stop.\n")

    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(address)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        httpd.server_close()
        SESSION.clear()                  # uploads are temporary by design

    return 0
