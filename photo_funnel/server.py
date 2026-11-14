"""
HTTP server for Photo Funnel. Standard library only.

Its own session, deliberately: photo_studio and photo_shortlist share one
in-memory batch, which meant running both at once had them treading on each
other. This one owns its state outright.

Whether photos arrive by browser upload or by opening a folder on disk, they
are copied into a temporary working directory first; straightening and manual
rotation rewrite those copies only.

The one thing that touches an original is Delete, and only when the user asks
for it: the original is moved, never removed, into a Deleted/ folder inside
the folder that was opened (see _delete).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import cv2

from wedding_album import config, people as album_people, pipeline, scanner
from photo_shortlist import metrics as measurement
from photo_shortlist import vertical
from photo_shortlist.web import parse_multipart

from . import __version__, engine, export


HOST = "127.0.0.1"
PORT = 8770
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

MAX_FILES = 300
MAX_UPLOAD_BYTES = 1024 * 1024 * 1024
# Deleted originals are moved here, inside the folder that was opened.
DELETED_DIR = "Deleted"
# Export folders written by this app: Filter_v1, Filter_v2, ...
_EXPORT_DIR_RE = re.compile(rf"^{re.escape(export.PREFIX.lower())}_v\d+$")
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
        # photo name -> the original file on disk it was copied from. Uploads
        # have no entry: the browser does not say where a file came from.
        self.origins: Dict[str, str] = {}
        self.source_dir: Optional[str] = None
        self.mixed_sources = False
        self.angles: Dict[str, int] = {}     # name -> degrees applied so far
        self.straightened = False
        self.cache = engine.Cache()
        self.params: Dict[str, object] = dict(engine.DEFAULTS)
        self.picks: List[str] = []
        # Duplicate-group "swap" overrides: photo name -> chosen keeper name.
        # Every member of a group is mapped to the chosen frame so the choice
        # survives even if the group's membership shifts between runs.
        self.dup_keepers: Dict[str, str] = {}
        self.result: Optional[engine.FunnelResult] = None
        self.last_export: Optional[dict] = None
        # who is in which photo: filled by _identify()
        self.persons: List[dict] = []
        self.identities: Dict[str, List[str]] = {}
        self.faces_of: Dict[str, Dict[str, tuple]] = {}   # photo -> pid -> box
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

    def clear_batch(self) -> None:
        """
        Drop the current photos but keep the job state and the parameters.

        Unlike reset(), this is safe to call from inside a running job: it
        does not replace the lock or the progress fields the browser is
        polling, and the user's slider settings carry over to the new folder.
        """
        with self.lock:
            old = self.workdir
            self.workdir = ""
            self.files = []
            self.origins = {}
            self.source_dir = None
            self.mixed_sources = False
            self.angles = {}
            self.straightened = False
            self.cache = engine.Cache()
            self.picks = []
            self.dup_keepers = {}
            self.result = None
            self.last_export = None
            self.persons, self.identities, self.faces_of = [], {}, {}
            self.params["people_sel"] = []
        if old and os.path.isdir(old):
            shutil.rmtree(old, ignore_errors=True)

    def by_name(self, name: str) -> Optional[str]:
        with self.lock:
            for p in self.files:
                if os.path.basename(p) == name:
                    return p
        return None

    def add_file(self, name: str, writer, origin: Optional[str] = None) -> Optional[str]:
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
            if origin:
                self.origins[os.path.basename(target)] = os.path.abspath(origin)
            self.result = None
            self.straightened = False
        return target

    def forget(self, name: str) -> Optional[str]:
        """
        Take one photo out of the batch and everything that refers to it.

        Returns the working copy's path (still on disk; the caller removes
        it), or None if the photo is not in the batch.
        """
        with self.lock:
            path = self.by_name(name)
            if path is None:
                return None
            self.files = [p for p in self.files if p != path]
            self.cache.records.pop(path, None)
            self.origins.pop(name, None)
            self.angles.pop(name, None)
            self.picks = [n for n in self.picks if n != name]
            self.dup_keepers = {k: v for k, v in self.dup_keepers.items()
                                if k != name and v != name}
            for pid in self.identities.pop(name, []):
                for person in self.persons:
                    if person.get("id") == pid and person.get("photos", 0) > 0:
                        person["photos"] -= 1
            self.faces_of.pop(name, None)
            # A person whose face thumbnail came from this photo gets another.
            for person in self.persons:
                face = person.get("face") or {}
                if face.get("p") != name:
                    continue
                other = next(((n, boxes[person["id"]]) for n, boxes in self.faces_of.items()
                              if person.get("id") in boxes), None)
                person["face"] = ({"p": other[0], "box": list(other[1])} if other
                                  else {"p": None, "box": None})
            self.result = None
            return path

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

# ============================================================================
# FOLDER BROWSER
# ============================================================================

# Browsing is confined to the user's home and mounted drives. The server only
# listens on 127.0.0.1, but there is still no reason to let a page walk /etc.
BROWSE_ROOTS = [os.path.expanduser("~"), "/Volumes"]

# Folders under home that never hold a photographer's shoot and are slow or
# protected to list.
_BROWSE_HIDE = {"library", "applications", "node_modules", ".trash"}

# Counting photos inside a subtree stops here, so a huge folder cannot stall
# the listing. Counts at the cap are shown as "N+".
_COUNT_CAP = 5000


def _allowed(path: str) -> bool:
    real = os.path.realpath(path)
    return any(real == r or real.startswith(r.rstrip(os.sep) + os.sep)
               for r in map(os.path.realpath, BROWSE_ROOTS))


def _is_image(name: str) -> bool:
    return (not name.startswith(".")
            and os.path.splitext(name)[1].lower() in config.IMAGE_EXTENSIONS)


def _skip_dir(name: str) -> bool:
    """
    Folders never scanned for photos: wedding_album's own skip list, plus this
    app's Deleted/ folder and its Filter_vN exports. Without the last two,
    analysing the same shoot again would bring back what was deleted and
    count every exported photo twice.
    """
    lowered = name.lower()
    return (scanner._excluded(name) or lowered == DELETED_DIR.lower()
            or bool(_EXPORT_DIR_RE.match(lowered)))


def _visible_dir(entry: os.DirEntry) -> bool:
    name = entry.name
    return (not name.startswith(".") and name.lower() not in _BROWSE_HIDE
            and not _skip_dir(name) and entry.is_dir(follow_symlinks=False))


def _count_tree(path: str) -> Tuple[int, bool]:
    """Photos in `path` and below, using the same skip rules as import."""
    total = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not d.startswith(".") and not _skip_dir(d)]
        total += sum(1 for f in files if _is_image(f))
        if total >= _COUNT_CAP:
            return total, True
    return total, False


def _browse(path: str) -> dict:
    """One folder: its subfolders with photo counts, and its own photos."""
    path = os.path.abspath(os.path.expanduser(path or "~"))
    if not os.path.isdir(path):
        raise FileNotFoundError(f"Folder not found: {export.pretty(path)}")
    if not _allowed(path):
        raise PermissionError("Browsing is limited to your home folder and external drives.")

    try:
        entries = sorted(os.scandir(path), key=lambda e: e.name.lower())
    except PermissionError:
        raise PermissionError(f"macOS did not allow reading {export.pretty(path)}.")

    here = [e.name for e in entries if e.is_file() and _is_image(e.name)]
    folders = []
    for e in entries:
        try:
            if not _visible_dir(e):
                continue
            n, capped = _count_tree(e.path)
            subs = sum(1 for c in os.scandir(e.path) if _visible_dir(c))
        except (PermissionError, OSError):
            continue
        folders.append({"name": e.name, "path": e.path, "photos": n,
                        "capped": capped, "folders": subs})

    total, capped = _count_tree(path)
    parent = os.path.dirname(path)
    shortcuts = [{"label": label, "path": p} for label, p in (
        ("Home", os.path.expanduser("~")),
        ("Desktop", os.path.expanduser("~/Desktop")),
        ("Pictures", os.path.expanduser("~/Pictures")),
        ("Drives", "/Volumes"),
    ) if os.path.isdir(p)]

    return {
        "path": path, "pretty": export.pretty(path),
        "parent": parent if parent != path and _allowed(parent) else None,
        "here": len(here), "sample": here[:6],
        "total": total, "total_capped": capped,
        "folders": folders, "shortcuts": shortcuts,
        "limit": MAX_FILES,
    }


def _native_pick(start: str) -> dict:
    """
    Show the macOS Choose Folder dialog and return the chosen path.

    This works only because the server runs on the same Mac as the browser;
    a page cannot open a native folder dialog that reveals a real path. The
    script calls `activate` so the dialog comes to the front rather than
    hiding behind the browser, and it needs no automation permission because
    it does not script any other application.
    """
    import subprocess
    import sys

    if sys.platform != "darwin":
        return {"error": "The Finder folder dialog is only available on macOS."}

    start = os.path.expanduser(start or "~/Desktop")
    if not os.path.isdir(start):
        start = os.path.expanduser("~")
    script = (
        'activate\n'
        'POSIX path of (choose folder with prompt "Choose the folder of photos to analyse" '
        'default location (POSIX file "%s"))' % start.replace('"', '\\"')
    )
    try:
        proc = subprocess.run(["osascript", "-e", script], capture_output=True,
                              text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return {"cancelled": True}
    if proc.returncode != 0:
        # -128 is "User canceled"; anything else is a real failure.
        if "-128" in proc.stderr or "canceled" in proc.stderr.lower():
            return {"cancelled": True}
        return {"error": proc.stderr.strip() or "The folder dialog could not be shown."}
    path = proc.stdout.strip().rstrip("/") or "/"
    if not _allowed(path):
        return {"error": "Choose a folder inside your home folder or on an external drive."}
    return {"path": path, "pretty": export.pretty(path)}


def _list_images(folder: str, recursive: bool) -> List[str]:
    if recursive:
        found = []
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if not _skip_dir(d)]
            found += [os.path.join(root, f) for f in files if _is_image(f)]
        return sorted(found)
    return sorted(os.path.join(folder, n) for n in os.listdir(folder)
                  if _is_image(n) and os.path.isfile(os.path.join(folder, n)))


def _job_import(folder: str, recursive: bool = True) -> None:
    images = _list_images(folder, recursive)
    room = MAX_FILES - len(S.files)
    taken = images[:max(0, room)]
    for i, src in enumerate(taken, 1):
        S.add_file(os.path.basename(src), lambda dst, s=src: shutil.copy2(s, dst), origin=src)
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


def _job_analyse_folder(folder: str, recursive: bool) -> None:
    """
    The whole first half of the funnel in one go: find the photos in a folder,
    copy them in, stand them upright, measure them, and run every step.

    This replaces the current batch. Choosing a folder means "this is the
    shoot", so mixing it with whatever was loaded before would be a surprise.
    """
    S.clear_batch()
    pretty = export.pretty(folder)

    S.step("finding photos", 0, 0)
    images = _list_images(folder, recursive)
    if not images:
        where = " or its subfolders" if recursive else ""
        hint = "" if recursive else " Try including subfolders."
        S.finish(error=f"No photos found in {pretty}{where}.{hint}")
        return

    taken = images[:MAX_FILES]
    for i, src in enumerate(taken, 1):
        S.add_file(os.path.basename(src), lambda dst, s=src: shutil.copy2(s, dst), origin=src)
        S.step("copying photos", i, len(taken))
    with S.lock:
        S.source_dir = os.path.abspath(folder)
        paths = list(S.files)
        margin = float(S.params["straighten_margin"])

    workers = pipeline.default_workers()
    turned = engine.straighten(paths, margin, workers=workers, progress=S.step)
    with S.lock:
        for r in turned:
            if r.changed:
                S.angles[r.name] = (S.angles.get(r.name, 0) + r.angle) % 360
        S.straightened = True

    S.cache.scan(paths, workers=workers, progress=S.step)
    _identify(paths)
    S.step("running the funnel", 0, 0)
    result = _recompute()

    rotated = sum(1 for r in turned if r.changed)
    note = f"Analysed {len(taken)} photo(s) from {pretty}: {rotated} rotated upright"
    if result is not None:
        note += f", {len(result.selects)} selected, best is {result.best}"
    if len(images) > len(taken):
        note += f". Only the first {MAX_FILES} of {len(images)} were loaded"
    S.finish(message=note + ".")


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
    if measured or not S.persons:
        _identify(paths)
    _recompute()
    S.finish(message=f"Measured {measured} photo(s)." if measured else "Up to date.")


def _job_export(base: str, name: str, include: dict, step: int = export.FINAL_STEP) -> None:
    with S.lock:
        result, picks, workdir = S.result, list(S.picks), S.workdir
    if result is None:
        S.finish(error="Analyse the photos first.")
        return
    try:
        summary = export.write(result, picks, base, name, include, workdir,
                               progress=S.step, step=step)
    except export.ExportError as exc:
        S.finish(error=str(exc))
        return
    with S.lock:
        S.last_export = summary
    note = f"Saved {summary['photos']} photo(s) to {summary['pretty']}"
    if summary["errors"]:
        note += f" with {len(summary['errors'])} problem(s)"
    S.finish(message=note + ".")


def _identify(paths: List[str]) -> None:
    """
    Recognise who is in each photo, so the user can choose a person.

    Uses wedding_album's SFace embeddings and clustering unchanged. Results are
    cached inside the working directory by file version, so after the first
    run this costs nothing, and a manually rotated photo is re-read.

    A failure here is not fatal: the funnel works without identities, the
    person chooser just stays empty and says why.
    """
    workers = pipeline.default_workers()
    try:
        results, _ = pipeline.analyze_faces(
            S.ensure_workdir(), paths, workers=workers,
            progress=lambda d, t: S.step("recognising people", d, t))
        found = album_people.cluster(results)
    except Exception as exc:  # noqa: BLE001 - reported in the UI instead
        traceback.print_exc()
        with S.lock:
            S.persons = [{"error": f"{type(exc).__name__}: {exc}"}]
            S.identities, S.faces_of = {}, {}
        return

    boxes = {r.filepath: [f.box for f in r.faces] for r in results}
    identities: Dict[str, List[str]] = {}
    faces_of: Dict[str, Dict[str, tuple]] = {}
    persons: List[dict] = []
    for i, person in enumerate(found, 1):
        for ref in person.refs:
            name = os.path.basename(ref.filepath)
            ids = identities.setdefault(name, [])
            if person.id not in ids:
                ids.append(person.id)
            bx = boxes.get(ref.filepath, [])
            if ref.face_index < len(bx):
                faces_of.setdefault(name, {})[person.id] = bx[ref.face_index]
        best = person.best_ref
        bx = boxes.get(best.filepath, [])
        persons.append({
            "id": person.id, "label": f"Person {i}",
            "photos": person.photo_count,
            "face": {"p": os.path.basename(best.filepath),
                     "box": list(bx[best.face_index]) if best.face_index < len(bx) else None},
        })
    with S.lock:
        S.persons, S.identities, S.faces_of = persons, identities, faces_of
        known = {p["id"] for p in persons}
        S.params["people_sel"] = [x for x in S.params.get("people_sel", []) if x in known]


def _recompute() -> Optional[engine.FunnelResult]:
    with S.lock:
        records = S.cache.ordered(S.files)
        if not records or S.cache.stale(S.files):
            S.result = None
            return None
        S.result = engine.run_funnel(records, S.params, S.cache.faces_probed,
                                     identities=S.identities,
                                     dup_keepers=S.dup_keepers)
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
        "people": st.people,
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
        "persons": _persons_payload(r),
    }


def _persons_payload(r: engine.FunnelResult) -> List[dict]:
    """People found, with how many of their photos are still in at step 10."""
    with S.lock:
        persons = [dict(p) for p in S.persons]
    for p in persons:
        if "id" in p:
            p["in_funnel"] = sum(1 for st in r.states.values()
                                 if p["id"] in st.people
                                 and (st.removed_by is None or st.removed_by >= 9))
    return persons


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
        "params": dict(S.params),
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


def _face_crop(path: str, box: str, size: int = 160) -> Optional[bytes]:
    """A square crop around a face box given in detection-space coordinates."""
    try:
        x, y, w, h = (int(float(v)) for v in box.split(","))
    except (ValueError, AttributeError):
        return None
    img = cv2.imread(path, cv2.IMREAD_COLOR)
    if img is None:
        return None
    # Boxes were measured on a copy capped at FACE_DETECT_MAX_DIM.
    longest = max(img.shape[:2])
    if longest > config.FACE_DETECT_MAX_DIM:
        s = config.FACE_DETECT_MAX_DIM / longest
        img = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    cx, cy, side = x + w / 2, y + h / 2, max(w, h) * 1.9
    ih, iw = img.shape[:2]
    x0, y0 = int(max(0, cx - side / 2)), int(max(0, cy - side / 2))
    x1, y1 = int(min(iw, cx + side / 2)), int(min(ih, cy + side / 2))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    crop = cv2.resize(img[y0:y1, x0:x1], (size, size), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", crop, [int(cv2.IMWRITE_JPEG_QUALITY), 86])
    return buf.tobytes() if ok else None


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


def _deleted_target(origin: str, source: Optional[str]) -> str:
    """
    Where a deleted original goes: <opened folder>/Deleted/<its path inside it>.

    Keeping the subfolder path means two IMG_0001.jpg from different
    subfolders cannot collide, and shows where each one came from. A photo
    from outside the opened folder goes to a Deleted/ beside it instead. An
    existing file is never replaced: the name gets _2, _3, ...
    """
    folder, rel = os.path.dirname(origin), os.path.basename(origin)
    if source:
        real_src, real_org = os.path.realpath(source), os.path.realpath(origin)
        if real_org.startswith(real_src.rstrip(os.sep) + os.sep):
            folder, rel = source, os.path.relpath(real_org, real_src)
    target = os.path.join(folder, DELETED_DIR, rel)
    stem, ext = os.path.splitext(target)
    n = 2
    while os.path.exists(target):
        target = f"{stem}_{n}{ext}"
        n += 1
    return target


def _delete(name: str) -> Tuple[str, str]:
    """
    Delete one photo: move its original into Deleted/ and drop it from the
    batch. Returns (message, error); on error nothing has changed.

    The original is moved, not removed, so it can be put back by hand. If the
    move fails the photo stays in the batch, so a failed delete never looks
    like a successful one.
    """
    with S.lock:
        path = S.by_name(name)
        if path is None:
            return "", "no such photo"
        origin, source = S.origins.get(name), S.source_dir

        if origin and os.path.isfile(origin):
            if not _allowed(origin):
                return "", "That photo is outside your home folder and external drives."
            target = _deleted_target(origin, source)
            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.move(origin, target)
            except OSError as exc:
                return "", (f"Could not move {name} to {DELETED_DIR}/: "
                            f"{exc.strerror or exc}. Nothing was changed.")
            message = f"Moved {name} to {export.pretty(target)}"
        elif origin:
            message = (f"Removed {name} from the batch. Its original was no longer at "
                       f"{export.pretty(origin)}, so nothing was moved")
        else:
            message = (f"Removed {name} from the batch. It was uploaded, so there is no "
                       f"original folder to move it into")

        S.forget(name)
        thumbs = os.path.join(S.workdir, ".thumbs")
        for leftover in [path] + ([os.path.join(thumbs, t) for t in os.listdir(thumbs)
                                   if t.startswith(name + ".")]
                                  if os.path.isdir(thumbs) else []):
            try:
                os.remove(leftover)
            except OSError:
                pass
        _recompute()
    return message + ".", ""


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
        if route == "/api/browse":
            try:
                return self._json(_browse(q.get("path", ["~"])[0]))
            except (FileNotFoundError, PermissionError) as exc:
                return self._error(str(exc))
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
        if route == "/api/face":
            path = _safe(q.get("p", [""])[0])
            if path is None:
                return self._error("not found", 404)
            data = _face_crop(path, q.get("box", [""])[0])
            if data is None:
                return self._error("unreadable", 404)
            return self._send(data, "image/jpeg", cache="max-age=86400")
        if route == "/api/ranking.csv":
            with S.lock:
                r, picks = S.result, list(S.picks)
            if r is None:
                return self._error("nothing to export yet")
            return self._send(export.ranking_csv(r, picks).encode("utf-8"), "text/csv; charset=utf-8",
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
            if not _allowed(folder):
                return self._error("Choose a folder inside your home folder or on an external drive.")
            return self._start("copying photos", _job_import, folder,
                               bool(body.get("recursive", True)))

        if route == "/api/pick-folder":
            result = _native_pick(str(body.get("start", "")))
            if "error" in result:
                return self._error(result["error"])
            return self._json(result)

        if route == "/api/analyse-folder":
            folder = os.path.abspath(os.path.expanduser(str(body.get("path", "")).strip()))
            if not os.path.isdir(folder):
                return self._error(f"Folder not found: {export.pretty(folder)}")
            if not _allowed(folder):
                return self._error("Choose a folder inside your home folder or on an external drive.")
            return self._start("finding photos", _job_analyse_folder, folder,
                               bool(body.get("recursive", True)))

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
                # Saved anyway, so "best photos" can be set before choosing a folder.
                with S.lock:
                    saved = dict(S.params)
                return self._json({"ready": False, "params": saved,
                                   "scan_needed": bool(S.files)})
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

        if route == "/api/swap":
            # Choose which frame survives a duplicate group. `name` is the
            # frame the user wants to keep; the group it belongs to is found
            # from the current result's pairs. Passing `auto: true` clears any
            # override for that group and restores the score-based keeper.
            name = str(body.get("name", ""))
            auto = bool(body.get("auto", False))
            with S.lock:
                if S.result is None or name not in S.result.states:
                    return self._error("no such photo")
                # Build the group (keeper + its removed frames) that contains
                # the named photo, from the pairs the engine reported.
                group: Optional[List[str]] = None
                for pr in S.result.pairs:
                    members = [pr["keeper"]] + [r["name"] for r in pr["removed"]]
                    if name in members:
                        group = members
                        break
                if group is None:
                    return self._error(
                        "This photo is not part of a duplicate group.")
                if auto:
                    for member in group:
                        S.dup_keepers.pop(member, None)
                else:
                    for member in group:
                        S.dup_keepers[member] = name
            _recompute()
            return self._json(_funnel_payload())

        if route == "/api/export":
            if S.result is None:
                return self._error("Analyse the photos first.")
            base = str(body.get("path", ""))
            name = str(body.get("name", ""))
            include = body.get("include") or {}
            step = export.clamp_step(body.get("step", export.FINAL_STEP))
            # Validate up front so a bad path is a message, not a failed job.
            try:
                export.check_target(export.resolve_base(base), name, S.workdir)
            except export.ExportError as exc:
                return self._error(str(exc), suggestion=exc.suggestion)
            return self._start("saving", _job_export, base, name, include, step)

        if route == "/api/delete":
            # This one moves a file on disk, so insist on a JSON request. A
            # page on another site can only send a JSON content type after a
            # CORS preflight, which this server never answers, so it cannot
            # reach this route by submitting a form at 127.0.0.1.
            if "application/json" not in (self.headers.get("Content-Type") or "").lower():
                return self._error("expected application/json", 415)
            if S.progress()["running"]:
                return self._error("Still working.", 409)
            message, err = _delete(str(body.get("name", "")))
            if err:
                return self._error(err)
            return self._json({"message": message, "files": _files_payload(),
                               "funnel": _funnel_payload()})

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
        files = parse_multipart(self.rfile.read(length), ctype)
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
