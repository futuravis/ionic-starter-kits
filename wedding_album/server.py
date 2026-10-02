"""
Local web UI server.

Serves a single-page interface for configuring a run, reviewing the people
found, giving them relationship roles, and writing the album. Standard library
only: no Flask, no FastAPI, nothing added to requirements.

    python3 -m wedding_album serve

Security, stated plainly: there is no authentication. The server binds to
127.0.0.1 so only this machine can reach it, and that loopback binding is the
entire security boundary. Do not change HOST to 0.0.0.0 on a shared network -
it would expose your photo collection, and the image endpoints would let anyone
on that network read files from the collection directory.

Path traversal is guarded separately: image requests are resolved and must land
inside the collection being worked on, so a crafted URL cannot walk out to
arbitrary files on disk.
"""

from __future__ import annotations

import json
import os
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np

from . import (
    __version__, batch, config, faces, labels, organizer, people, pipeline,
    flat, quality, scanner, sections, selection,
)


HOST = "127.0.0.1"
PORT = 8766                      # 8765 belongs to the older picker
UI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.html")

THUMB_DIR_NAME = "thumbs"
THUMB_WIDTH = 320


# ============================================================================
# SHARED STATE
# ============================================================================

class Run:
    """
    The current analysis, held in memory so labeling and re-sectioning are
    instant rather than re-running detection.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.source: str = config.DEFAULT_SOURCE_DIR
        self.results: List = []
        self.people: List[people.Person] = []
        self.scores: Dict[str, float] = {}
        self.phashes: Dict[str, str] = {}
        self.assignments: List[sections.Assignment] = []
        self.groups: List[selection.ShotGroup] = []
        self.boxes: Dict[str, List[tuple]] = {}
        self.analyzed = False
        self.overrides: Dict[str, object] = {}
        self.picks_wanted = config.DEFAULT_PICKS_PER_SECTION
        self.flat = config.FLAT_ALBUM
        self.top_per_group = config.PICKS_PER_GROUP
        self.albums: List = []

        # Batch mode: the whole wedding, one album per shoot folder.
        self.mode = "batch"
        self.shoots: List[batch.ShootResult] = []
        self.percent = config.DEFAULT_PICKS_PERCENT
        self.drop_duplicates = config.DROP_DUPLICATES
        self.raw_shoots: List[tuple] = []
        self.times: Dict[str, object] = {}

        # progress
        self.running = False
        self.stage = ""
        self.current = 0
        self.total = 0
        self.error = ""
        self.message = ""

    # -- progress ------------------------------------------------------
    def begin(self, stage: str) -> None:
        with self.lock:
            self.running = True
            self.stage = stage
            self.current = 0
            self.total = 0
            self.error = ""
            self.message = ""

    def step(self, current: int, total: int) -> None:
        with self.lock:
            self.current = current
            self.total = total

    def stage_is(self, stage: str) -> None:
        with self.lock:
            self.stage = stage

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
                "analyzed": self.analyzed,
            }


RUN = Run()


# ============================================================================
# WORK
# ============================================================================

def _do_analyze(source: str, overrides: dict, limit: int, workers: int,
                picks_wanted: int = config.DEFAULT_PICKS_PER_SECTION) -> None:
    """Full analysis, run on a background thread."""
    try:
        previous = config.apply_overrides(overrides)
        try:
            RUN.stage_is("scanning")
            images = pipeline.load_images(source, limit)
            if not images:
                RUN.finish(error=f"No images found under {source}")
                return

            RUN.stage_is("detecting faces")
            results, from_cache = pipeline.analyze_faces(
                source, images, workers=workers,
                progress=RUN.step, overrides=overrides,
            )

            RUN.stage_is("grouping people")
            found = people.cluster(results)
            people.carry_labels(found, source)
            people.save(found, source)

            RUN.stage_is("scoring quality")
            metrics = pipeline.analyze_quality(
                images, workers=workers, progress=RUN.step, overrides=overrides,
            )
            scores = quality.score_collection(metrics)
            phashes = {m.filepath: m.phash for m in metrics if m.ok}

            RUN.stage_is("assigning sections")
            assigned = sections.assign_all(results, found, scores)
            sections.mark_duplicates(assigned, phashes)

            RUN.stage_is("grouping shots")
            groups = selection.group_shots(assigned, phashes, times=RUN.times)
            selection.choose(groups, per_section=picks_wanted)

            boxes: Dict[str, List[tuple]] = {
                r.filepath: [f.box for f in r.faces] for r in results
            }

            with RUN.lock:
                RUN.source = source
                RUN.results = results
                RUN.people = found
                RUN.scores = scores
                RUN.phashes = phashes
                RUN.assignments = assigned
                RUN.groups = groups
                RUN.boxes = boxes
                RUN.analyzed = True
                RUN.overrides = dict(overrides)
                RUN.picks_wanted = picks_wanted

            RUN.finish(
                message=f"Analyzed {len(images)} photos "
                        f"({from_cache} from cache): {len(found)} people, "
                        f"{len(groups)} shot groups."
            )
        finally:
            config.apply_overrides(previous)

    except Exception as exc:  # noqa: BLE001 - surfaced to the browser
        RUN.finish(error=f"{type(exc).__name__}: {exc}")
        traceback.print_exc()


def _do_batch_analyze(root: str, overrides: dict, workers: int, percent: float,
                      picks: int, drop_duplicates: bool, limit: int) -> None:
    """
    Analyze the whole wedding: identity once, selection per shoot.

    Writes nothing. The browser gets a per-shoot breakdown to review before any
    file is copied.
    """
    try:
        previous = config.apply_overrides(overrides)
        try:
            def progress(stage: str, done: int, total: int) -> None:
                RUN.stage_is(stage)
                RUN.step(done, total)

            raw_shoots, results, found, scores, phashes, times = batch.analyze_collection(
                root, workers=workers, overrides=overrides,
                progress=progress, limit_per_shoot=limit,
            )
            if not results:
                RUN.finish(error=f"No photos found under {root}")
                return

            RUN.stage_is("grouping and picking per shoot")
            shoot_results = batch.prepare_shoots(
                raw_shoots, results, found, scores, phashes,
                percent=percent, per_section=picks,
                drop_duplicates=drop_duplicates, times=times,
            )

            boxes = {r.filepath: [f.box for f in r.faces] for r in results}

            with RUN.lock:
                RUN.source = root
                RUN.mode = "batch"
                RUN.results = results
                RUN.people = found
                RUN.scores = scores
                RUN.phashes = phashes
                RUN.boxes = boxes
                RUN.shoots = shoot_results
                RUN.raw_shoots = raw_shoots
                RUN.analyzed = True
                RUN.overrides = dict(overrides)
                RUN.percent = percent
                RUN.picks_wanted = picks
                RUN.times = times
                RUN.drop_duplicates = drop_duplicates
                # Flatten for the people panel and the shared payloads.
                RUN.assignments = [a for s in shoot_results for a in s.assignments]
                RUN.groups = [g for s in shoot_results for g in s.groups]

            if RUN.flat:
                by_shoot = {sr.folder: sr.assignments for sr in shoot_results}
                with RUN.lock:
                    RUN.albums = flat.build(
                        root, by_shoot, phashes, times=times,
                        picks_per_group=RUN.top_per_group,
                    )

            picked = sum(s.pick_count for s in shoot_results)
            dropped = sum(s.duplicates_dropped for s in shoot_results)
            RUN.finish(
                message=f"{len(shoot_results)} shoot(s), {len(results)} photos: "
                        f"dropped {dropped} duplicate(s), picked {picked}. "
                        f"{len(found)} people found."
            )
        finally:
            config.apply_overrides(previous)

    except Exception as exc:  # noqa: BLE001
        RUN.finish(error=f"{type(exc).__name__}: {exc}")
        traceback.print_exc()


def _do_batch_write(opts: dict) -> None:
    """Write every shoot's album, in place."""
    try:
        with RUN.lock:
            shoot_results = list(RUN.shoots)
            overrides = dict(RUN.overrides)

        if not shoot_results:
            RUN.finish(error="Analyze first.")
            return

        previous = config.apply_overrides(overrides)
        try:
            def progress(stage: str, done: int, total: int) -> None:
                RUN.stage_is(stage)
                RUN.step(done, total)

            if RUN.flat:
                RUN.stage_is("writing the album")
                name = (opts.get("output_name") or "").strip() or \
                    organizer.target_version_name(
                        source, clean=bool(opts.get("clean", False)))
                written, errors, album_dir = flat.write(
                    source, RUN.albums, name,
                    clean=bool(opts.get("clean", False)))
                RUN.finish(
                    message=f"Wrote {written} file(s) to "
                            f"{os.path.basename(album_dir)}"
                            + (f", {len(errors)} problem(s)" if errors else "")
                            + ". Your folders were not changed."
                )
                return

            batch.write_shoots(
                shoot_results,
                clean=bool(opts.get("clean", False)),
                progress=progress,
                times=RUN.times,
            )

            copied = sum(s.copied for s in shoot_results)
            failed = [s for s in shoot_results if s.errors]
            note = (f"Copied {copied} file(s) into "
                    f"{len([s for s in shoot_results if s.copied])} album(s)")
            if failed:
                note += f", {len(failed)} shoot(s) had problems"
            RUN.finish(message=note + ". Your folders were not changed.")
        finally:
            config.apply_overrides(previous)

    except Exception as exc:  # noqa: BLE001
        RUN.finish(error=f"{type(exc).__name__}: {exc}")
        traceback.print_exc()


def _albums_payload() -> List[dict]:
    """The flat album, one entry per phase, as the UI shows it."""
    with RUN.lock:
        albums = list(RUN.albums)
        root = RUN.source

    out = []
    for album in albums:
        out.append({
            "event": album.event,
            "phase": album.phase,
            "label": album.label,
            "directory": album.directory,
            "photos": album.photo_count,
            "groups": len(album.groups),
            "top": [
                {
                    "photo": os.path.relpath(a.filepath, root),
                    "name": os.path.basename(a.filepath),
                    "section": flat.SECTION_TAG.get(a.section, ""),
                    "score": round(a.quality * 100, 1),
                    "reason": a.reason,
                }
                for a in album.top
            ],
            "review_count": len(album.review),
            "others_count": len(album.others),
        })
    return out


def _shoots_payload() -> List[dict]:
    with RUN.lock:
        shoot_results = list(RUN.shoots)
        root = RUN.source

    out = []
    for shoot in shoot_results:
        picks = []
        for section, chosen in shoot.picks.items():
            for a in chosen:
                picks.append({
                    "photo": os.path.relpath(a.filepath, root),
                    "name": os.path.basename(a.filepath),
                    "section": section.value,
                    "score": round(a.quality * 100, 1),
                    "rank": a.pick_rank,
                    "reason": a.reason,
                })
        picks.sort(key=lambda p: (p["section"], p["rank"]))

        out.append({
            "folder": os.path.relpath(shoot.folder, root),
            "name": shoot.name,
            "photos": shoot.photo_count,
            "duplicates": shoot.duplicates_dropped,
            "kept": shoot.kept_count,
            "groups": len(shoot.groups),
            "pick_count": shoot.pick_count,
            "sections": shoot.section_counts,
            "album": os.path.basename(shoot.album_dir) if shoot.album_dir else "",
            "copied": shoot.copied,
            "errors": shoot.errors[:3],
            "picks": picks,
        })
    return out


def _do_organize(opts: dict) -> None:
    """Copy the album, on a background thread."""
    try:
        with RUN.lock:
            source = RUN.source
            assigned = list(RUN.assignments)
            overrides = dict(RUN.overrides)

        if not assigned:
            RUN.finish(error="Nothing analyzed yet.")
            return

        previous = config.apply_overrides(overrides)
        try:
            output_name = (opts.get("output_name") or "").strip() \
                or organizer.next_version_name(source)

            RUN.stage_is("choosing picks")
            with RUN.lock:
                groups = list(RUN.groups)
            picks = selection.choose(
                groups,
                per_section=int(opts.get("picks", config.DEFAULT_PICKS_PER_SECTION)),
                skip_duplicates=bool(opts.get("skip_duplicates", False)),
            )

            RUN.stage_is("planning")
            clean = bool(opts.get("clean", False))
            try:
                clean_path = organizer.check_output(
                    source, clean=clean, output_name=output_name)
            except organizer.OutputExists as exc:
                RUN.finish(error=str(exc))
                return

            plan = organizer.build_plan(
                assigned, source, picks=picks, output_name=output_name,
                times=RUN.times,
            )

            RUN.stage_is("copying")
            RUN.step(0, len(plan.copies))
            done, errors = organizer.execute(plan, clean_path=clean_path)
            RUN.step(done, len(plan.copies))

            RUN.stage_is("writing report")
            report = organizer.write_report(assigned, plan, source)

            note = (f"Copied {done} file(s) to {os.path.basename(plan.album_dir)}: "
                    f"{len(plan.picks)} picks, {len(plan.runners_up)} in "
                    f"{config.SECOND_OPTION_DIR_NAME}")
            if errors:
                note += f", {len(errors)} error(s)"
            RUN.finish(message=f"{note}. Report: {os.path.basename(report)}")
        finally:
            config.apply_overrides(previous)

    except Exception as exc:  # noqa: BLE001
        RUN.finish(error=f"{type(exc).__name__}: {exc}")
        traceback.print_exc()


# ============================================================================
# SERIALIZATION
# ============================================================================

def _people_payload() -> List[dict]:
    with RUN.lock:
        found = list(RUN.people)
        source = RUN.source

    out = []
    for person in found:
        refs = sorted(person.refs, key=lambda r: r.clarity, reverse=True)[:10]
        faces_out = []
        for ref in refs:
            boxes = RUN.boxes.get(ref.filepath, [])
            if ref.face_index >= len(boxes):
                continue
            x, y, w, h = boxes[ref.face_index]
            faces_out.append({
                "photo": os.path.relpath(ref.filepath, source),
                "box": f"{x},{y},{w},{h}",
            })

        out.append({
            "id": person.id,
            "name": person.name,
            "role": person.role,
            "note": person.note,
            "photo_count": person.photo_count,
            "face_count": person.face_count,
            "cohesion": round(person.cohesion(), 3),
            "labeled": person.is_labeled,
            "faces": faces_out,
        })
    return out


def _sections_payload() -> List[dict]:
    with RUN.lock:
        assigned = list(RUN.assignments)
        source = RUN.source

    grouped: Dict[str, List[sections.Assignment]] = {}
    for a in assigned:
        grouped.setdefault(a.section.value, []).append(a)

    def photo(a: sections.Assignment) -> dict:
        return {
            "photo": os.path.relpath(a.filepath, source),
            "name": os.path.basename(a.filepath),
            "quality": round(a.quality, 4),
            "score": round(a.quality * 100, 1),
            "reason": a.reason,
            "people": a.person_names,
            "faces": a.face_count,
            "duplicate": a.is_duplicate,
            "duplicate_of": os.path.basename(a.duplicate_of) if a.duplicate_of else "",
            "shot_group": a.shot_group,
            "pick": a.is_pick,
            "pick_rank": a.pick_rank,
            "pick_reason": a.pick_reason,
        }

    out = []
    for section in config.SECTION_ORDER:
        members = grouped.get(section.value)
        if not members:
            continue

        # Grouped presentation: one card per shot group, mirroring the album.
        by_group: Dict[int, List[sections.Assignment]] = {}
        for a in members:
            by_group.setdefault(a.shot_group, []).append(a)

        shot_groups = []
        for index in sorted(by_group):
            batch = sorted(by_group[index], key=lambda a: -a.quality)
            shot_groups.append({
                "index": index,
                "size": len(batch),
                "photos": [photo(a) for a in batch],
            })

        picks = sorted((a for a in members if a.is_pick),
                       key=lambda a: a.pick_rank)

        out.append({
            "section": section.value,
            "count": len(members),
            "group_count": len(shot_groups),
            "pick_count": len(picks),
            "picks": [photo(a) for a in picks],
            "groups": shot_groups,
        })
    return out


def _stats_payload() -> dict:
    with RUN.lock:
        assigned = list(RUN.assignments)
        found = list(RUN.people)

    with RUN.lock:
        groups = list(RUN.groups)

    unlabeled = sum(1 for p in found if not p.is_labeled)
    real = [p for p in found if p.is_person]
    picks = sum(1 for a in assigned if a.is_pick)

    return {
        "total_photos": len(assigned),
        "people": len(found),
        "real_people": len(real),
        "unlabeled": unlabeled,
        "duplicates": sum(1 for a in assigned if a.is_duplicate),
        "sections_used": len({a.section.value for a in assigned}),
        "shot_groups": len(groups),
        "picks": picks,
        "reduced": (round(100 * (1 - picks / len(assigned))) if assigned else 0),
    }


def _state_payload() -> dict:
    snap = config.snapshot()
    return {
        "version": __version__,
        "source": RUN.source,
        "analyzed": RUN.analyzed,
        "models_present": faces.models_present(),
        "roles": [r.value for r in config.ASSIGNABLE_ROLES],
        "section_order": [s.value for s in config.SECTION_ORDER],
        "priority": [s.value for s in config.SECTION_PRIORITY],
        "suggested_name": organizer.next_version_name(RUN.source),
        "existing_versions": organizer.existing_versions(
            RUN.source, config.OUTPUT_DIR_PREFIX),
        "default_picks": config.DEFAULT_PICKS_PER_SECTION,
        "default_percent": config.DEFAULT_PICKS_PERCENT,
        "default_top": config.PICKS_PER_GROUP,
        "flat_album": config.FLAT_ALBUM,
        "bins": [config.TOP_DIR_NAME, config.REVIEW_DIR_NAME,
                 config.OTHERS_DIR_NAME],
        "second_option": config.SECOND_OPTION_DIR_NAME,
        "mode": RUN.mode,
        "config": {
            k: (v if not isinstance(v, dict) else {kk: round(vv, 4) for kk, vv in v.items()})
            for k, v in snap.items()
        },
        "workers": pipeline.default_workers(),
    }


# ============================================================================
# IMAGE SERVING
# ============================================================================

def _safe_path(relative: str) -> Optional[str]:
    """
    Resolve a browser-supplied path against the collection and refuse anything
    that escapes it.

    Without this, /api/thumb?p=../../../../etc/passwd would happily serve files
    from anywhere the process can read.
    """
    with RUN.lock:
        source = RUN.source

    root = os.path.realpath(source)
    candidate = os.path.realpath(os.path.join(root, relative))

    if candidate != root and not candidate.startswith(root + os.sep):
        return None
    if not os.path.isfile(candidate):
        return None
    return candidate


def _thumb_dir() -> str:
    with RUN.lock:
        source = RUN.source
    path = os.path.join(source, config.STATE_DIR_NAME, THUMB_DIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def _thumbnail(path: str, width: int) -> Optional[bytes]:
    """
    Return a JPEG thumbnail, cached on disk.

    Decoding a 6000x4000 original per grid tile makes the UI unusable, so the
    first request pays for it and the rest are file reads.
    """
    key = f"{os.path.basename(path)}_{width}.jpg"
    cached = os.path.join(_thumb_dir(), key)

    try:
        if os.path.isfile(cached) and os.path.getmtime(cached) >= os.path.getmtime(path):
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


def _face_crop(path: str, box: str, size: int = 200) -> Optional[bytes]:
    """Crop a padded square around a face box given in detect-space coords."""
    try:
        x, y, w, h = (int(float(v)) for v in box.split(","))
    except (ValueError, AttributeError):
        return None

    img = cv2.imread(path, cv2.IMREAD_COLOR)
    if img is None:
        return None

    # Boxes were measured after downscaling to FACE_DETECT_MAX_DIM; rebuild
    # that same space so the coordinates line up.
    ih, iw = img.shape[:2]
    longest = max(ih, iw)
    if longest > config.FACE_DETECT_MAX_DIM:
        scale = config.FACE_DETECT_MAX_DIM / longest
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

    margin = 0.45
    cx, cy = x + w / 2.0, y + h / 2.0
    side = max(w, h) * (1.0 + 2.0 * margin)

    ih, iw = img.shape[:2]
    x0, y0 = int(max(0, cx - side / 2)), int(max(0, cy - side / 2))
    x1, y1 = int(min(iw, cx + side / 2)), int(min(ih, cy + side / 2))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None

    patch = cv2.resize(img[y0:y1, x0:x1], (size, size), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", patch, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    return buf.tobytes() if ok else None


# ============================================================================
# HTTP
# ============================================================================

class Handler(BaseHTTPRequestHandler):
    server_version = f"WeddingAlbum/{__version__}"

    def log_message(self, *args) -> None:
        pass                              # keep the console for pipeline output

    # -- helpers -------------------------------------------------------
    def _send(self, body: bytes, ctype: str, code: int = 200,
              cache: Optional[str] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if cache:
            self.send_header("Cache-Control", cache)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass                          # browser navigated away mid-image

    def _json(self, obj, code: int = 200) -> None:
        self._send(json.dumps(obj).encode("utf-8"), "application/json", code)

    def _error(self, message: str, code: int = 400) -> None:
        self._json({"error": message}, code)

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            return json.loads(self.rfile.read(length) or b"{}")
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
            self._json(RUN.progress())
            return

        if route == "/api/result":
            if not RUN.analyzed:
                self._json({"analyzed": False})
                return
            payload = {
                "analyzed": True,
                "mode": RUN.mode,
                "stats": _stats_payload(),
                "people": _people_payload(),
            }
            if RUN.mode == "batch":
                payload["shoots"] = _shoots_payload()
                payload["albums"] = _albums_payload()
                payload["flat"] = RUN.flat
            else:
                payload["sections"] = _sections_payload()
            self._json(payload)
            return

        if route == "/api/shoots":
            # Cheap listing before any analysis, so the shape of the collection
            # is visible up front.
            try:
                found = scanner.find_shoots(
                    os.path.expanduser(query.get("path", [RUN.source])[0]))
            except (NotADirectoryError, FileNotFoundError) as exc:
                self._error(str(exc), 400)
                return
            root = os.path.expanduser(query.get("path", [RUN.source])[0])
            self._json({
                "root": root,
                "count": len(found),
                "total_photos": sum(len(p) for _, p in found),
                "shoots": [
                    {"folder": os.path.relpath(f, root), "photos": len(p)}
                    for f, p in found
                ],
            })
            return

        if route == "/api/folders":
            self._json(self._folders(query.get("path", [""])[0]))
            return

        if route == "/api/thumb":
            rel = query.get("p", [""])[0]
            width = min(1200, max(80, int(query.get("w", [THUMB_WIDTH])[0])))
            path = _safe_path(rel)
            if not path:
                self._error("not found", 404)
                return
            data = _thumbnail(path, width)
            if data is None:
                self._error("unreadable", 404)
                return
            self._send(data, "image/jpeg", cache="max-age=3600")
            return

        if route == "/api/face":
            rel = query.get("p", [""])[0]
            box = query.get("box", [""])[0]
            path = _safe_path(rel)
            if not path:
                self._error("not found", 404)
                return
            data = _face_crop(path, box)
            if data is None:
                self._error("unreadable", 404)
                return
            self._send(data, "image/jpeg", cache="max-age=3600")
            return

        self._error("no such route", 404)

    # -- POST ----------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        payload = self._body()

        if route == "/api/analyze":
            if RUN.running:
                self._error("a run is already in progress", 409)
                return
            if not faces.models_present():
                self._error("face models missing; run download-models first", 400)
                return

            source = os.path.expanduser(payload.get("source") or RUN.source)
            if not os.path.isdir(source):
                self._error(f"not a folder: {source}", 400)
                return

            try:
                overrides = self._overrides(payload.get("config") or {})
            except config.ConfigError as exc:
                self._error(str(exc), 400)
                return

            with RUN.lock:
                RUN.source = source

            RUN.begin("starting")
            threading.Thread(
                target=_do_analyze,
                args=(source, overrides,
                      int(payload.get("limit", 0) or 0),
                      int(payload.get("workers", 0) or pipeline.default_workers()),
                      int(payload.get("picks", config.DEFAULT_PICKS_PER_SECTION))),
                daemon=True,
            ).start()
            self._json({"started": True})
            return

        if route == "/api/batch":
            if RUN.running:
                self._error("a run is already in progress", 409)
                return
            if not faces.models_present():
                self._error("face models missing; run download-models first", 400)
                return

            root = os.path.expanduser(payload.get("source") or RUN.source)
            if not os.path.isdir(root):
                self._error(f"not a folder: {root}", 400)
                return

            try:
                overrides = self._overrides(payload.get("config") or {})
            except config.ConfigError as exc:
                self._error(str(exc), 400)
                return

            with RUN.lock:
                RUN.source = root

            with RUN.lock:
                RUN.flat = bool(payload.get("flat", config.FLAT_ALBUM))
                RUN.top_per_group = int(
                    payload.get("top", config.PICKS_PER_GROUP))

            RUN.begin("starting")
            threading.Thread(
                target=_do_batch_analyze,
                args=(root, overrides,
                      int(payload.get("workers", 0) or pipeline.default_workers()),
                      float(payload.get("percent", config.DEFAULT_PICKS_PERCENT)),
                      int(payload.get("picks", 0) or 0),
                      bool(payload.get("drop_duplicates", True)),
                      int(payload.get("limit", 0) or 0)),
                daemon=True,
            ).start()
            self._json({"started": True})
            return

        if route == "/api/batch_write":
            if RUN.running:
                self._error("a run is already in progress", 409)
                return
            if not RUN.shoots:
                self._error("analyze first", 400)
                return

            unlabeled = [p for p in RUN.people if not p.is_labeled]
            if unlabeled and not payload.get("allow_unlabeled"):
                self._error(
                    f"{len(unlabeled)} of {len(RUN.people)} people are still "
                    f"unidentified. Label the main ones, or tick "
                    f"'write anyway'.",
                    400,
                )
                return

            RUN.begin("starting")
            threading.Thread(target=_do_batch_write, args=(payload,),
                             daemon=True).start()
            self._json({"started": True})
            return

        if route == "/api/label":
            if not RUN.analyzed:
                self._error("analyze first", 400)
                return
            try:
                specs = [
                    labels.Assignment(
                        person_id=a["id"],
                        role=a["role"],
                        name=a.get("name", ""),
                        note=a.get("note", ""),
                    )
                    for a in payload.get("assignments", [])
                ]
                if not specs:
                    self._error("no assignments given", 400)
                    return
                labels.apply(RUN.source, specs, force=bool(payload.get("force")))
            except labels.LabelError as exc:
                self._error(str(exc), 400)
                return
            except KeyError as exc:
                self._error(f"missing field {exc}", 400)
                return

            self._resection()
            out = {
                "ok": True,
                "mode": RUN.mode,
                "stats": _stats_payload(),
                "people": _people_payload(),
            }
            if RUN.mode == "batch":
                out["shoots"] = _shoots_payload()
            else:
                out["sections"] = _sections_payload()
            self._json(out)
            return

        if route == "/api/organize":
            if RUN.running:
                self._error("a run is already in progress", 409)
                return
            if not RUN.analyzed:
                self._error("analyze first", 400)
                return

            unlabeled = [p for p in RUN.people if not p.is_labeled]
            if unlabeled and not payload.get("allow_unlabeled"):
                self._error(
                    f"{len(unlabeled)} person(s) still unlabeled "
                    f"({', '.join(p.id for p in unlabeled)}). "
                    f"Label them, or tick 'organize anyway'.",
                    400,
                )
                return

            RUN.begin("starting")
            threading.Thread(target=_do_organize, args=(payload,), daemon=True).start()
            self._json({"started": True})
            return

        self._error("no such route", 404)

    # -- internals -----------------------------------------------------
    def _overrides(self, raw: dict) -> dict:
        """Validate UI config against config.OVERRIDABLE before using it."""
        out: Dict[str, object] = {}
        for key, value in raw.items():
            if key not in config.OVERRIDABLE:
                raise config.ConfigError(f"{key} is not overridable")
            out[key] = value
        # Bounce it through apply/restore purely to surface bad values now,
        # while a request is still around to receive the error.
        previous = config.apply_overrides(out)
        config.apply_overrides(previous)
        return out

    def _resection(self) -> None:
        """
        Re-derive sections after labels change.

        Reuses the cached faces, clusters and quality scores, so this is
        effectively instant: relabeling never re-runs detection.
        """
        with RUN.lock:
            results = RUN.results
            found = RUN.people
            scores = dict(RUN.scores)
            phashes = dict(RUN.phashes)
            source = RUN.source
            mode = RUN.mode
            raw_shoots = list(RUN.raw_shoots)

        people.carry_labels(found, source)

        if mode == "batch":
            # Redo every shoot. Identity changed, so which section a photo
            # belongs to may have changed in any of them.
            shoot_results = batch.prepare_shoots(
                raw_shoots, results, found, scores, phashes,
                percent=RUN.percent, per_section=RUN.picks_wanted,
                drop_duplicates=RUN.drop_duplicates, times=RUN.times,
            )
            with RUN.lock:
                RUN.shoots = shoot_results
                RUN.assignments = [a for s in shoot_results for a in s.assignments]
                RUN.groups = [g for s in shoot_results for g in s.groups]
            return

        assigned = sections.assign_all(results, found, scores)
        sections.mark_duplicates(assigned, phashes)

        # Shot groups live inside a section, so moving photos between sections
        # invalidates them and they have to be rebuilt along with the picks.
        groups = selection.group_shots(assigned, phashes, times=RUN.times)
        selection.choose(groups, per_section=RUN.picks_wanted)

        with RUN.lock:
            RUN.assignments = assigned
            RUN.groups = groups

    def _folders(self, path: str) -> dict:
        """List subfolders, so the UI can offer a simple folder picker."""
        base = os.path.expanduser(path or "~/Desktop")
        if not os.path.isdir(base):
            return {"path": base, "error": "not a folder", "folders": []}

        try:
            entries = sorted(
                e for e in os.listdir(base)
                if not e.startswith(".") and os.path.isdir(os.path.join(base, e))
            )
        except OSError as exc:
            return {"path": base, "error": str(exc), "folders": []}

        images = len(scanner.find_images(base)) if os.path.isdir(base) else 0
        return {
            "path": os.path.realpath(base),
            "parent": os.path.dirname(os.path.realpath(base)),
            "folders": entries,
            "image_count": images,
        }


# ============================================================================
# ENTRY POINT
# ============================================================================

def serve(host: str = HOST, port: int = PORT, source: Optional[str] = None,
          open_browser: bool = True) -> int:
    if source:
        RUN.source = os.path.expanduser(source)

    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"refusing to bind {host}: this server has no authentication "
              f"and must stay on loopback")
        return 2

    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}"

    print("=" * 68)
    print(f"  WEDDING ALBUM {__version__} - web UI")
    print("=" * 68)
    print(f"  {url}")
    print(f"  collection: {RUN.source}")
    if not faces.models_present():
        print("  WARNING: face models missing, run download-models first")
    print("  loopback only, no authentication. Ctrl-C to stop.")
    print()

    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
