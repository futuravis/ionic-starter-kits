"""
Writing the result somewhere the user chose.

This is the only module that writes outside the session's working directory,
so it is careful about three things:

* **Nothing is ever overwritten.** If the target folder exists the export
  refuses and offers the next free version name instead. That is the same
  promise wedding_album makes with BestImages_v1, _v2, ...
* **Names are sanitised.** A folder name cannot contain a path separator, so a
  typed "../../x" cannot place files anywhere but the chosen destination.
* **The engine's selects and the user's own picks never merge.** Selects are
  numbered 01_..NN_ in score order; manual picks go to Manually_Selected/ with
  their original names, because they were never ranked against anything.

Files are copied from the working directory, which holds the straightened
copies, so a photo that came in sideways is delivered upright.
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
import os
import re
import shutil
from typing import Callable, Dict, List, Optional, Sequence

from photo_shortlist import vertical
from wedding_album import config, organizer

from .engine import STEP_NAME, FunnelResult


ProgressFn = Optional[Callable[[str, int, int], None]]

PREFIX = config.OUTPUT_DIR_PREFIX        # "BestImages"
MANUAL_DIR = "Manually_Selected"
REJECTED_DIR = "Rejected"
VERTICAL_DIR = "Vertical"

INCLUDE_DEFAULTS = {
    "selects": True, "manual": True, "notes": True,
    "csv": True, "rejected": False, "vertical": False,
}

_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


class ExportError(Exception):
    """A problem the user can fix, reported as a message rather than a trace."""

    def __init__(self, message: str, suggestion: str = "") -> None:
        super().__init__(message)
        self.suggestion = suggestion


# ============================================================================
# DESTINATIONS AND NAMES
# ============================================================================

def event_name(source_dir: Optional[str]) -> str:
    """'2_Muhurtham' -> 'Muhurtham'. Falls back to 'Wedding' with no source."""
    if not source_dir:
        return "Wedding"
    base = os.path.basename(os.path.normpath(source_dir))
    base = re.sub(r"^[\d]+[\s_.\-]*", "", base).strip(" _.-")
    base = re.sub(r"\s+", "_", base)
    return sanitize(base) if base else "Wedding"


def sanitize(name: str) -> str:
    """
    A safe single folder name, or ExportError.

    Path separators are replaced rather than rejected, so pasting a name with
    a slash in it produces something usable instead of an error.
    """
    name = _BAD.sub("_", (name or "").strip()).strip(" .")
    if not name:
        raise ExportError("Folder name cannot be empty.")
    if name in (".", ".."):
        raise ExportError("That is not a usable folder name.")
    return name[:120]


def next_version(base: str) -> str:
    """The engine's own suggestion: the next free BestImages_vN in `base`."""
    if os.path.isdir(base):
        return organizer.next_version_name(base, PREFIX)
    return f"{PREFIX}_v1"


def name_suggestions(base: str, source_dir: Optional[str], n_selects: int) -> List[str]:
    event = event_name(source_dir)
    today = _dt.date.today().isoformat()
    names = [
        next_version(base),
        f"{event}_Selects",
        f"Selects_{today}",
        f"Top{max(1, n_selects)}_{event}",
        f"{event}_Final",
    ]
    seen, out = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def destinations(source_dir: Optional[str]) -> List[dict]:
    """
    The places a user is likely to want, most suitable first.

    "Inside the shoot folder" is only offered when the photos came from a
    folder on disk. A browser upload carries filenames but not where they came
    from, so there is no shoot folder to write next to.
    """
    home = os.path.expanduser("~")
    out = []
    if source_dir and os.path.isdir(source_dir):
        out.append({"key": "shoot", "label": "Inside the shoot folder",
                    "path": source_dir, "suggested": True})
        parent = os.path.dirname(os.path.normpath(source_dir))
        if parent and parent != source_dir:
            out.append({"key": "parent", "label": "Beside the shoot folder",
                        "path": parent, "suggested": False})
    pictures = os.path.join(home, "Pictures")
    if os.path.isdir(pictures):
        out.append({"key": "pictures", "label": "Pictures", "path": pictures,
                    "suggested": not out})
    desktop = os.path.join(home, "Desktop")
    if os.path.isdir(desktop):
        out.append({"key": "desktop", "label": "Desktop", "path": desktop,
                    "suggested": not out})
    out.append({"key": "custom", "label": "Somewhere else", "path": "",
                "suggested": False})
    return out


def pretty(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home) else path


def resolve_base(path: str) -> str:
    """An existing directory from user input, or ExportError."""
    if not path or not path.strip():
        raise ExportError("Choose a destination folder.")
    base = os.path.abspath(os.path.expanduser(path.strip()))
    if not os.path.isdir(base):
        raise ExportError(f"Folder not found: {pretty(base)}")
    if not os.access(base, os.W_OK):
        raise ExportError(f"Cannot write to {pretty(base)}")
    return base


def check_target(base: str, name: str, workdir: str) -> str:
    """The full target path if it is safe and free, otherwise ExportError."""
    target = os.path.join(base, sanitize(name))
    real_target = os.path.realpath(target)
    real_work = os.path.realpath(workdir)
    if real_target == real_work or real_target.startswith(real_work + os.sep):
        raise ExportError("Cannot export into the working directory.")
    if os.path.exists(target):
        raise ExportError(f"{name} already exists in {pretty(base)}. Nothing was overwritten.",
                          suggestion=next_version(base))
    return target


# ============================================================================
# WRITING
# ============================================================================

def _notes(result: FunnelResult, picks: Sequence[str]) -> str:
    lines = ["Photo Funnel - selected photos", ""]
    for i, name in enumerate(result.selects, 1):
        st = result.states[name]
        lines.append(f"{i:02d}_{name}   score {st.score * 100:.1f}   "
                     f"overall rank {st.rank} of {result.total}")
        lines.append(f"      {st.faces if st.faces >= 0 else '?'} face(s), "
                     f"{st.band} band, moment {st.group}")
        faults = [f.label for f in st.faults]
        lines.append("      " + ("faults: " + ", ".join(faults) if faults else "no faults found"))
        lines.append("")
    if picks:
        lines.append(f"Manually selected ({len(picks)}) - your choices, not ranked by the funnel:")
        for name in picks:
            st = result.states.get(name)
            why = f"removed at step {st.removed_by} ({STEP_NAME[st.removed_by]}): {st.reason}" \
                if st and st.removed_by else "also a funnel select" if name in result.selects else ""
            lines.append(f"  {name}   {why}")
        lines.append("")
    lines.append("Funnel")
    for s in result.steps:
        lines.append(f"  {s.n:>2} {s.name:<12} {s.before:>4} -> {s.after:<4}"
                     + ("" if s.available else f"  ({s.note})"))
    return "\n".join(lines) + "\n"


def ranking_csv(result: FunnelResult, picks: Sequence[str]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["rank", "photo", "score", "select", "manual_pick", "faces", "band",
                "moment", "removed_at_step", "removed_by", "reason", "faults"])
    picked = set(picks)
    for st in sorted(result.states.values(), key=lambda s: s.rank):
        w.writerow([
            st.rank, st.name, f"{st.score * 100:.1f}",
            st.pick_rank or "", "yes" if st.name in picked else "",
            st.faces if st.faces >= 0 else "", st.band, st.group or "",
            st.removed_by or "", STEP_NAME.get(st.removed_by, "") if st.removed_by else "",
            st.reason, "; ".join(f"{f.label} ({f.severity})" for f in st.faults),
        ])
    return buf.getvalue()


def plan(result: FunnelResult, picks: Sequence[str], include: Dict[str, bool]) -> List[tuple]:
    """(source, relative destination) pairs, without writing anything."""
    inc = {**INCLUDE_DEFAULTS, **(include or {})}
    out = []
    if inc["selects"]:
        for i, name in enumerate(result.selects, 1):
            out.append((result.states[name].record.path, f"{i:02d}_{name}"))
    if inc["manual"]:
        for name in picks:
            st = result.states.get(name)
            if st:
                out.append((st.record.path, os.path.join(MANUAL_DIR, name)))
    if inc["rejected"]:
        for st in result.states.values():
            if st.removed_by:
                folder = f"{st.removed_by:02d}_{STEP_NAME[st.removed_by].replace(' ', '_')}"
                out.append((st.record.path, os.path.join(REJECTED_DIR, folder, st.name)))
    return out


def write(result: FunnelResult, picks: Sequence[str], base: str, name: str,
          include: Dict[str, bool], workdir: str, progress: ProgressFn = None) -> dict:
    """Write the export. Returns a summary; raises ExportError before touching disk."""
    inc = {**INCLUDE_DEFAULTS, **(include or {})}
    target = check_target(resolve_base(base), name, workdir)
    copies = plan(result, picks, inc)
    verticals = result.selects if inc["vertical"] else []
    total = len(copies) + len(verticals) + 2

    os.makedirs(target)
    done, errors = 0, []

    def tick(stage: str) -> None:
        if progress:
            progress(stage, done, total)

    for src, rel in copies:
        dest = os.path.join(target, rel)
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(src, dest)
        except OSError as exc:
            errors.append(f"{rel}: {exc.strerror or exc}")
        done += 1
        tick("copying photos")

    for i, sel in enumerate(verticals, 1):
        st = result.states[sel]
        stem = os.path.splitext(sel)[0]
        dest = os.path.join(target, VERTICAL_DIR, f"{i:02d}_{stem}_9x16.jpg")
        faces = []
        if st.record.faces is not None and st.record.faces.ok:
            faces = sorted(st.record.faces.faces, key=lambda f: -f.size_ratio)
        conv = vertical.convert(st.record.path, dest, faces=faces)
        if not conv.ok:
            errors.append(f"{VERTICAL_DIR}/{os.path.basename(dest)}: {conv.error}")
        done += 1
        tick("making 9:16 versions")

    if inc["notes"]:
        with open(os.path.join(target, "why_these_photos.txt"), "w", encoding="utf-8") as fh:
            fh.write(_notes(result, picks))
    if inc["csv"]:
        with open(os.path.join(target, "ranking.csv"), "w", encoding="utf-8", newline="") as fh:
            fh.write(ranking_csv(result, picks))
    done = total
    tick("done")

    return {
        "path": target, "pretty": pretty(target),
        "photos": len(copies), "verticals": len(verticals) - sum(1 for e in errors if e.startswith(VERTICAL_DIR)),
        "errors": errors,
    }
