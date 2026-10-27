"""
HTTP server for Photo Funnel. Standard library only.

Its own session, deliberately: photo_studio and photo_shortlist share one
in-memory batch, which meant running both at once had them treading on each
other. This one owns its state outright.

The originals are never touched. Whether photos arrive by browser upload or
by opening a folder on disk, they are copied into a temporary working
directory first; straightening and manual rotation rewrite those copies only.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Set
from urllib.parse import parse_qs, urlparse

import cv2

from wedding_album import config, pipeline, scanner
from photo_shortlist import metrics as measurement
from photo_shortlist import vertical
from photo_shortlist.app import _parse_multipart

from . import __version__, engine, export


HOST = "127.0.0.1"
PORT = 8770
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

MAX_FILES = 300
MAX_UPLOAD_BYTES = 1024 * 1024 * 1024
ROTATIONS = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180,
             270: cv2.ROTATE_90_COUNTERCLOCKWISE}

CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8", ".svg": "image/svg+xml"}


# ============================================================================
# SESSION
# ============================================================================

class Session:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.workdir = ""
        self.files: List[str] = []           # working copies, in order
        self.source_dir: Optional[str] = None
        self.mixed_sources = False
        self.angles: Dict[str, int] = {}     # name -> degrees applied so far
        self.straightened = False
        self.cache = engine.Cache()
        self.params: Dict[str, object] = dict(engine.DEFAULTS)
        self.picks: List[str] = []
        self.result: Optional[engine.FunnelResult] = None
        self.last_export: Optional[dict] = None
        # background job
        self.running = False
        self.stage = ""
        self.current = 0
        self.total = 0
        self.error = ""
        self.message = ""

    # -- storage -----------------------------------------------------------
    def ensure_workdir(self) -> str:
        with self.lock:
            if not self.workdir or not os.path.isdir(self.workdir):
                self.workdir = tempfile.mkdtemp(prefix="photo_funnel_")
            return self.workdir

    def reset(self) -> None:
        with self.lock:
            old = self.workdir
            self.__init__()
        if old and os.path.isdir(old):
            shutil.rmtree(old, ignore_errors=True)

    def by_name(self, name: str) -> Optional[str]:
        with self.lock:
            for p in self.files:
                if os.path.basename(p) == name:
                    return p
        return None

    def add_file(self, name: str, writer) -> Optional[str]:
        """Place one file in the working dir under a unique name."""
        directory = self.ensure_workdir()
        stem, ext = os.path.splitext(os.path.basename(name.replace("\\", "/")))
        target = os.path.join(directory, stem + ext)
        n = 2
        while os.path.exists(target):
            target = os.path.join(directory, f"{stem}_{n}{ext}")
            n += 1
        writer(target)
        with self.lock:
            self.files.append(target)
            self.result = None
            self.straightened = False
        return target

    # -- job ---------------------------------------------------------------
    def begin(self, stage: str) -> bool:
        with self.lock:
            if self.running:
                return False
            self.running, self.stage = True, stage
            self.current = self.total = 0
            self.error = self.message = ""
            return True

    def step(self, stage: str, current: int, total: int) -> None:
        with self.lock:
            self.stage, self.current, self.total = stage, current, total

    def finish(self, message: str = "", error: str = "") -> None:
        with self.lock:
            self.running, self.stage = False, ""
            self.message, self.error = message, error

    def progress(self) -> dict:
        with self.lock:
            return {"running": self.running, "stage": self.stage,
                    "current": self.current, "total": self.total,
                    "error": self.error, "message": self.message}


S = Session()


def _background(fn, *args) -> None:
    def runner():
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001 - surfaced to the browser
            traceback.print_exc()
            S.finish(error=f"{type(exc).__name__}: {exc}")
    threading.Thread(target=runner, daemon=True).start()


# ============================================================================
# JOBS
# ============================================================================

def _job_import(folder: str) -> None:
    images = scanner.find_images(folder)
    room = MAX_FILES - len(S.files)
    taken = images[:max(0, room)]
    for i, src in enumerate(taken, 1):
        S.add_file(os.path.basename(src), lambda dst, s=src: shutil.copy2(s, dst))
        S.step("copying photos", i, len(taken))
    with S.lock:
        if S.source_dir is None and not S.mixed_sources and len(S.files) == len(taken):
            S.source_dir = os.path.abspath(folder)
        else:
            S.mixed_sources, S.source_dir = True, None
    note = f"Added {len(taken)} photo(s) from {export.pretty(folder)}."
    if len(images) > len(taken):
        note += f" {len(images) - len(taken)} skipped: the batch limit is {MAX_FILES}."
    S.finish(message=note)


def _job_straighten(margin: float) -> None:
    with S.lock:
        paths = list(S.files)
    results = engine.straighten(paths, margin, workers=pipeline.default_workers(),
                                progress=S.step)
    with S.lock:
        for r in results:
            if r.changed:
                S.angles[r.name] = (S.angles.get(r.name, 0) + r.angle) % 360
        S.straightened = True
        S.result = None
    turned = sum(1 for r in results if r.changed)
    S.finish(message=f"Rotated {turned} of {len(results)} photo(s) upright.")


def _job_scan() -> None:
    with S.lock:
        paths = list(S.files)
    measured = S.cache.scan(paths, workers=pipeline.default_workers(), progress=S.step)
    _recompute()
    S.finish(message=f"Measured {measured} photo(s)." if measured else "Up to date.")


def _job_export(base: str, name: str, include: dict) -> None:
    with S.lock:
        result, picks, workdir = S.result, list(S.picks), S.workdir
    if result is None:
        S.finish(error="Analyse the photos first.")
        return
    try:
        summary = export.write(result, picks, base, name, include, workdir, progress=S.step)
    except export.ExportError as exc:
        S.finish(error=str(exc))
        return
    with S.lock:
        S.last_export = summary
    note = f"Saved {summary['photos']} photo(s) to {summary['pretty']}"
    if summary["errors"]:
        note += f" with {len(summary['errors'])} problem(s)"
    S.finish(message=note + ".")


def _recompute() -> Optional[engine.FunnelResult]:
    with S.lock:
        records = S.cache.ordered(S.files)
        if not records or S.cache.stale(S.files):
            S.result = None
            return None
        S.result = engine.run_funnel(records, S.params, S.cache.faces_probed)
        return S.result


# ============================================================================
# PAYLOADS
# ============================================================================

def _version(path: str) -> str:
    mtime, size = engine.file_stamp(path)
    return f"{mtime:x}{size:x}"


def _files_payload() -> List[dict]:
    with S.lock:
        files = list(S.files)
        angles = dict(S.angles)
    out = []
    for p in files:
        name = os.path.basename(p)
        rec = S.cache.records.get(p)
        m = rec.metrics if rec else None
        out.append({
            "name": name, "v": _version(p), "angle": angles.get(name, 0),
            "dims": f"{m.width}x{m.height}" if m and m.ok else "",
        })
    return out


def _photo(st: engine.PhotoState) -> dict:
    m = st.record.metrics
    return {
        "name": st.name, "v": _version(st.record.path),
        "score": round(st.score * 100, 1), "quality": round(st.quality * 100, 1),
        "rank": st.rank, "faces": st.faces, "band": st.band,
        "shape": st.orientation, "dims": f"{m.width}x{m.height}",
        "removed_by": st.removed_by, "reason": st.reason, "also": st.also,
        "group": st.group, "pick_rank": st.pick_rank, "majors": st.majors,
        "faults": [f.payload() for f in st.faults],
        "breakdown": st.breakdown,
    }


def _funnel_payload() -> dict:
    with S.lock:
        r, picks = S.result, list(S.picks)
    if r is None:
        return {"ready": False}
    return {
        "ready": True,
        "params": r.params,
        "faces_probed": r.faces_probed,
        "total": r.total,
        "steps": [{
            "n": s.n, "key": s.key, "name": s.name, "before": s.before,
            "after": s.after, "removed": s.removed, "hits": s.hits,
            "available": s.available, "note": s.note,
        } for s in r.steps],
        "photos": [_photo(st) for st in sorted(r.states.values(), key=lambda s: s.rank)],
        "selects": r.selects, "best": r.best, "pairs": r.pairs,
        "bands": r.bands, "moments": r.moments, "failures": r.failures,
        "picks": picks,
    }


def _state_payload() -> dict:
    with S.lock:
        source, mixed = S.source_dir, S.mixed_sources
        picks = list(S.picks)
        stale = len(S.cache.stale(S.files)) if S.files else 0
        straightened = S.straightened
        last = S.last_export
    return {
        "version": __version__,
        "files": _files_payload(),
        "source_dir": source, "source_pretty": export.pretty(source) if source else "",
        "mixed_sources": mixed,
        "straightened": straightened,
        "scan_needed": stale > 0,
        "stale": stale,
        "picks": picks,
        "defaults": engine.DEFAULTS,
        "limits": engine.LIMITS,
        "faces_available": measurement.faces_available(),
        "max_files": MAX_FILES,
        "extensions": sorted(config.IMAGE_EXTENSIONS),
        "home_desktop": export.pretty(os.path.expanduser("~/Desktop")),
        "last_export": last,
        "progress": S.progress(),
    }


def _destinations_payload() -> dict:
    with S.lock:
        source = S.source_dir
        n = len(S.result.selects) if S.result else 6
    out = []
    for d in export.destinations(source):
        entry = dict(d)
        entry["pretty"] = export.pretty(d["path"]) if d["path"] else ""
        entry["names"] = export.name_suggestions(d["path"], source, n) if d["path"] else []
        entry["existing"] = (sorted(x for x in os.listdir(d["path"])
                                    if os.path.isdir(os.path.join(d["path"], x)))
                             if d["path"] and os.path.isdir(d["path"]) else [])
        out.append(entry)
    return {"destinations": out, "event": export.event_name(source)}


# ============================================================================
# IMAGES
# ============================================================================

def _safe(name: str) -> Optional[str]:
    """Resolve a requested file and refuse anything outside the working dir."""
    with S.lock:
        root = S.workdir
    if not root or not name:
        return None
    root = os.path.realpath(root)
    path = os.path.realpath(os.path.join(root, os.path.basename(name)))
    if not path.startswith(root + os.sep) or not os.path.isfile(path):
        return None
    return path


def _thumbnail(path: str, width: int) -> Optional[bytes]:
    """JPEG at `width`, cached on disk and invalidated by the file's version."""
    cache_dir = os.path.join(S.ensure_workdir(), ".thumbs")
    os.makedirs(cache_dir, exist_ok=True)
    key = os.path.join(cache_dir, f"{os.path.basename(path)}.{_version(path)}.{width}.jpg")
    if os.path.isfile(key):
        with open(key, "rb") as fh:
            return fh.read()

    img = cv2.imread(path, cv2.IMREAD_REDUCED_COLOR_2 if width <= 1200 else cv2.IMREAD_COLOR)
    if img is None:
        img = cv2.imread(path, cv2.IMREAD_COLOR)
    if img is None:
        return None
    h, w = img.shape[:2]
    if max(h, w) > width:
        s = width / max(h, w)
        img = cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))),
                         interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 84])
    if not ok:
        return None
    data = buf.tobytes()
    try:
        with open(key, "wb") as fh:
            fh.write(data)
    except OSError:
        pass
    return data


def _rotate(name: str, angle: int) -> str:
    """Quarter-turn one working copy by hand. Returns an error string or ''."""
    path = _safe(name)
    if path is None:
        return "no such photo"
    if angle not in ROTATIONS:
        return "angle must be 90, 180 or 270"
    img = cv2.imread(path, cv2.IMREAD_COLOR)
    if img is None:
        return "unreadable"
    if not cv2.imwrite(path, cv2.rotate(img, ROTATIONS[angle]),
                       [int(cv2.IMWRITE_JPEG_QUALITY), vertical.STRAIGHTEN_QUALITY]):
        return "could not write"
    with S.lock:
        S.angles[name] = (S.angles.get(name, 0) + angle) % 360
        S.result = None            # the file changed; measurements are stale
    return ""


# ============================================================================
# HTTP
# ============================================================================

class Handler(BaseHTTPRequestHandler):
    server_version = f"PhotoFunnel/{__version__}"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        pass

    def _send(self, body: bytes, ctype: str, code: int = 200, cache: str = "no-store",
              disposition: Optional[str] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
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

    def _error(self, message: str, code: int = 400, **extra) -> None:
        self._json({"error": message, **extra}, code)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}
        except (ValueError, TypeError):
            return {}

    def _static(self, rel: str) -> None:
        root = os.path.realpath(STATIC_DIR)
        path = os.path.realpath(os.path.join(root, rel))
        if not path.startswith(root + os.sep) or not os.path.isfile(path):
            self._error("not found", 404)
            return
        with open(path, "rb") as fh:
            self._send(fh.read(), CONTENT_TYPES.get(os.path.splitext(path)[1], "application/octet-stream"))

    def _start(self, stage: str, fn, *args) -> None:
        if not S.begin(stage):
            self._error("Already working - wait for the current task to finish.", 409)
            return
        _background(fn, *args)
        self._json({"started": True})

    # -- GET ---------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        q = parse_qs(url.query)
        route = url.path

        if route in ("/", "/index.html"):
            return self._static("index.html")
        if route.startswith("/static/"):
            return self._static(route[len("/static/"):])
        if route == "/api/state":
            return self._json(_state_payload())
        if route == "/api/progress":
            return self._json(S.progress())
        if route == "/api/funnel":
            return self._json(_funnel_payload())
        if route == "/api/destinations":
            return self._json(_destinations_payload())
        if route in ("/api/thumb", "/api/photo"):
            path = _safe(q.get("p", [""])[0])
            if path is None:
                return self._error("not found", 404)
            default = 420 if route == "/api/thumb" else 2000
            try:
                width = int(q.get("w", [default])[0])
            except ValueError:
                width = default
            data = _thumbnail(path, max(80, min(2400, width)))
            if data is None:
                return self._error("unreadable", 404)
            # Versioned URLs are immutable: a rotated file gets a new ?v=.
            return self._send(data, "image/jpeg", cache="max-age=86400")
        if route == "/api/ranking.csv":
            with S.lock:
                r, picks = S.result, list(S.picks)
            if r is None:
                return self._error("nothing to export yet")
            return self._send(export._csv(r, picks).encode("utf-8"), "text/csv; charset=utf-8",
                              disposition='attachment; filename="ranking.csv"')
        self._error("no such route", 404)

    # -- POST --------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path).path

        if route == "/api/upload":
            return self._upload()

        body = self._body()

        if route == "/api/import":
            folder = os.path.abspath(os.path.expanduser(str(body.get("path", "")).strip()))
            if not os.path.isdir(folder):
                return self._error(f"Folder not found: {export.pretty(folder)}")
            if os.path.realpath(folder).startswith(os.path.realpath(S.ensure_workdir())):
                return self._error("That is the working directory.")
            return self._start("copying photos", _job_import, folder)

        if route == "/api/reset":
            if S.progress()["running"]:
                return self._error("Still working.", 409)
            S.reset()
            return self._json({"ok": True})

        if route == "/api/straighten":
            if not S.files:
                return self._error("Add some photos first.")
            margin = engine.normalize({"straighten_margin": body.get("margin")})["straighten_margin"]
            with S.lock:
                S.params["straighten_margin"] = margin
            return self._start("standing photos upright", _job_straighten, margin)

        if route == "/api/rotate":
            if S.progress()["running"]:
                return self._error("Still working.", 409)
            try:
                angle = int(body.get("angle", 90)) % 360
            except (TypeError, ValueError):
                return self._error("bad angle")
            err = _rotate(str(body.get("name", "")), angle)
            return self._error(err) if err else self._json({"ok": True, "files": _files_payload()})

        if route == "/api/scan":
            if not S.files:
                return self._error("Add some photos first.")
            return self._start("measuring photos", _job_scan)

        if route == "/api/params":
            with S.lock:
                S.params = engine.normalize({**S.params, **(body.get("params") or {})})
                if body.get("reset"):
                    S.params = dict(engine.DEFAULTS)
            if _recompute() is None:
                return self._error("Photos need measuring first.", 409, scan_needed=True)
            return self._json(_funnel_payload())

        if route == "/api/pick":
            name = str(body.get("name", ""))
            with S.lock:
                known = S.result is not None and name in S.result.states
                if not known:
                    return self._error("no such photo")
                if body.get("on", True):
                    if name not in S.picks:
                        S.picks.append(name)
                else:
                    S.picks = [n for n in S.picks if n != name]
                picks = list(S.picks)
            return self._json({"picks": picks})

        if route == "/api/export":
            if S.result is None:
                return self._error("Analyse the photos first.")
            base = str(body.get("path", ""))
            name = str(body.get("name", ""))
            include = body.get("include") or {}
            # Validate up front so a bad path is a message, not a failed job.
            try:
                export.check_target(export.resolve_base(base), name, S.workdir)
            except export.ExportError as exc:
                return self._error(str(exc), suggestion=exc.suggestion)
            return self._start("saving", _job_export, base, name, include)

        self._error("no such route", 404)

    def _upload(self) -> None:
        ctype = self.headers.get("Content-Type") or ""
        if "multipart/form-data" not in ctype.lower():
            return self._error("expected multipart/form-data")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._error("missing Content-Length")
        if length <= 0 or length > MAX_UPLOAD_BYTES:
            return self._error("upload empty or too large", 413)
        files = _parse_multipart(self.rfile.read(length), ctype)
        stored, skipped = 0, []
        for fname, data in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in config.IMAGE_EXTENSIONS:
                skipped.append(f"{fname}: not a supported image")
                continue
            if len(S.files) >= MAX_FILES:
                skipped.append(f"{fname}: batch limit of {MAX_FILES} reached")
                continue

            def write(dst, payload=data):
                with open(dst, "wb") as fh:
                    fh.write(payload)
            S.add_file(fname, write)
            stored += 1
        with S.lock:
            if stored:
                S.mixed_sources = S.mixed_sources or S.source_dir is not None
                S.source_dir = None
        self._json({"stored": stored, "skipped": skipped, "files": _files_payload()})


def serve(port: int = PORT, open_browser: bool = True) -> int:
    address = f"http://{HOST}:{port}/"
    try:
        httpd = ThreadingHTTPServer((HOST, port), Handler)
    except OSError as exc:
        print(f"Could not start on {address}: {exc}")
        return 1
    print(f"Photo Funnel {__version__}\nServing {address}\nPress Ctrl+C to stop.\n")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(address)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        httpd.server_close()
        S.reset()
    return 0
