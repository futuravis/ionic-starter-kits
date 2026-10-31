"""
The funnel: measure once, then decide in milliseconds.

Two halves, deliberately separated.

**Measurement** (Cache.scan) is the expensive part: decoding every photograph,
measuring it, and probing for faces. It runs once per file and is cached by
path, modification time and size. Rotating a file changes its modification
time, which invalidates exactly that entry and nothing else - so the cache is
self-correcting without anyone having to remember to clear it.

**Deciding** (run_funnel) is a pure function of the cached measurements and
the current parameters. It never touches the disk. That is what makes every
slider instant and stepping backwards free.

Three rules the funnel keeps, all agreed in the mockup:

* A photo is attributed to the **first** step that removes it. Later faults it
  would also fail are recorded as `also`, so a card can say "also fails:
  focus" without the photo being counted twice.
* Percentile thresholds are computed over the **whole batch**, not over
  whatever survives to that step, so a threshold means the same thing however
  the steps before it are configured.
* Manual picks are **not an input**. The funnel's result is reproducible from
  the photographs and the parameters alone; the user's own selections are a
  separate set that is delivered alongside it.
"""

from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from wedding_album import config, quality
from wedding_album.quality import phash_distance

from photo_shortlist import metrics as measurement
from photo_shortlist import diagnose, shortlist, vertical
from photo_shortlist.metrics import FaceInfo, FaceReading, PhotoMetrics


ProgressFn = Optional[Callable[[str, int, int], None]]


# ============================================================================
# STEPS AND PARAMETERS
# ============================================================================

STEPS: List[Tuple[int, str, str]] = [
    (1, "add", "Add"),
    (2, "straighten", "Straighten"),
    (3, "duplicates", "Duplicates"),
    (4, "exposure", "Exposure"),
    (5, "focus", "Focus"),
    (6, "subject", "Subject"),
    (7, "expressions", "Expressions"),
    (8, "grain", "Grain"),
    (9, "narrow", "Narrow"),
    (10, "people", "People"),
    (11, "moments", "Per moment"),
    (12, "best", "The best"),
]
STEP_NAME = {n: name for n, _, name in STEPS}

BANDS = ("solo", "duo", "small", "large", "none")
BAND_LABEL = {
    "solo": "One person", "duo": "Two people", "small": "Small group",
    "large": "Large group", "none": "Nobody in frame", "unknown": "Not checked",
}

# Defaults are the engine's own constants wherever one exists, so the funnel
# starts out agreeing with photo_shortlist rather than inventing new numbers.
DEFAULTS: Dict[str, object] = {
    "straighten_margin": vertical.UPRIGHT_MARGIN,

    "dup_threshold": config.DUPLICATE_HASH_THRESHOLD,
    "dup_keep": "face",

    "exp_blown_on": True, "exp_blown": 0.10,
    "exp_bright_on": True, "exp_bright": 202.0,
    "exp_dark_on": True, "exp_dark": 62.0,
    "exp_shadow_on": True, "exp_shadow": 0.18,
    "exp_hot_on": False, "exp_hot": 0.04,

    "focus_soft_on": True, "focus_soft": 22.0,
    "focus_slight_on": False, "focus_slight": 42.0,

    "subj_fsoft_on": True, "subj_fsoft": 0.35,
    "subj_fdark_on": True, "subj_fdark": 0.68,
    "subj_noface_on": False,
    "subj_turned_on": False, "subj_turned": 0.45,
    "face_min": config.FACE_MIN_SIZE_RATIO,

    "grain_on": False, "grain_pct": 78.0,
    "flat_on": False, "flat": 26.0,
    "noise_w": shortlist.QUALITY_WEIGHTS["noise"],

    "narrow_keep": 18,
    "penalty": shortlist.MAJOR_FAULT_PENALTY,

    "band_split": 6,
    "band_solo_on": True, "band_duo_on": True, "band_small_on": True,
    "band_large_on": True, "band_none_on": False,
    "q_solo": 2, "q_duo": 2, "q_small": 1, "q_large": 1, "q_none": 0,

    "shot_threshold": config.SHOT_GROUP_THRESHOLD,
    "prefer_clean": True,
}

LIMITS: Dict[str, Tuple[float, float]] = {
    "straighten_margin": (0.0, 0.4),
    "dup_threshold": (0, 200),
    "exp_blown": (0.01, 0.5), "exp_bright": (120, 255), "exp_dark": (0, 140),
    "exp_shadow": (0.01, 0.6), "exp_hot": (0.005, 0.3),
    "focus_soft": (0, 60), "focus_slight": (0, 80),
    "subj_fsoft": (0.05, 1.0), "subj_fdark": (0.2, 1.0),
    "subj_turned": (0.05, 0.95),
    # Faces smaller than the probe's own floor were never recorded, so the
    # slider cannot go below it; it can only filter upwards.
    "face_min": (config.FACE_MIN_SIZE_RATIO, 0.3),
    "grain_pct": (40, 99.9), "flat": (5, 80), "noise_w": (0.0, 0.6),
    "narrow_keep": (1, 10000), "penalty": (0.3, 1.0),
    "band_split": (3, 20),
    "q_solo": (0, 50), "q_duo": (0, 50), "q_small": (0, 50),
    "q_large": (0, 50), "q_none": (0, 50),
    "shot_threshold": (0, 256),
}

CHOICES = {"dup_keep": ("face", "score")}


def normalize(params: Optional[dict]) -> Dict[str, object]:
    """
    Merge user parameters over the defaults, coerced and clamped.

    Never raises on bad input: an unknown key is ignored and an unparseable
    value falls back to the default, because a malformed slider value should
    produce a sensible funnel rather than a 400 mid-drag.
    """
    out: Dict[str, object] = dict(DEFAULTS)

    for key, value in (params or {}).items():
        if key not in DEFAULTS:
            continue
        default = DEFAULTS[key]
        try:
            if isinstance(default, bool):
                value = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
            elif isinstance(default, int):
                value = int(round(float(value)))
            elif isinstance(default, float):
                value = float(value)
            else:
                value = str(value)
                if key in CHOICES and value not in CHOICES[key]:
                    continue
        except (TypeError, ValueError):
            continue

        if key in LIMITS and not isinstance(value, bool):
            lo, hi = LIMITS[key]
            value = type(default)(min(hi, max(lo, value)))
        out[key] = value

    # "Slightly soft" sits above "out of focus" by definition.
    if out["focus_slight"] < out["focus_soft"]:
        out["focus_slight"] = out["focus_soft"]

    return out


def quality_weights(noise_weight: float) -> Dict[str, float]:
    """
    The scoring weights with noise set as asked and the rest scaled to fit.

    The other four keep their relative proportions, so moving the noise slider
    never quietly reorders sharpness against exposure.
    """
    base = dict(shortlist.QUALITY_WEIGHTS)
    others = {k: v for k, v in base.items() if k != "noise"}
    remaining = max(0.0, 1.0 - noise_weight)
    total = sum(others.values()) or 1.0
    weights = {k: v / total * remaining for k, v in others.items()}
    weights["noise"] = noise_weight
    return weights


# ============================================================================
# MEASUREMENT CACHE
# ============================================================================

@dataclass
class Record:
    """Everything measured about one photograph, valid for one file version."""

    path: str
    stamp: Tuple[int, int]                 # (mtime_ns, size)
    metrics: PhotoMetrics
    faces: Optional[FaceReading] = None    # None when no face probe ran

    @property
    def name(self) -> str:
        return os.path.basename(self.path)


def file_stamp(path: str) -> Tuple[int, int]:
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return (0, 0)


class Cache:
    """Measurements keyed by file, reused for as long as the file is unchanged."""

    def __init__(self) -> None:
        self.records: Dict[str, Record] = {}
        self.faces_probed = False

    def stale(self, paths: Sequence[str]) -> List[str]:
        """Paths that have never been measured, or have changed since."""
        out = []
        for path in paths:
            rec = self.records.get(path)
            if rec is None or rec.stamp != file_stamp(path):
                out.append(path)
        return out

    def scan(self, paths: Sequence[str], workers: int = 1,
             progress: ProgressFn = None, use_faces: bool = True) -> int:
        """
        Measure whatever is stale, prune whatever is gone. Returns the number
        of files actually measured, which is 0 on a warm cache.
        """
        wanted = set(paths)
        for path in list(self.records):
            if path not in wanted:
                del self.records[path]

        todo = self.stale(paths)
        probe = use_faces and measurement.faces_available()
        self.faces_probed = probe

        if todo:
            measured = measurement.measure_all(todo, workers=workers,
                                               progress=progress)
            readings: Dict[str, FaceReading] = {}
            if probe:
                ok_paths = [m.filepath for m in measured if m.ok]
                for reading in measurement.read_faces_all(
                        ok_paths, workers=workers, progress=progress):
                    readings[reading.filepath] = reading

            for m in measured:
                self.records[m.filepath] = Record(
                    path=m.filepath,
                    stamp=file_stamp(m.filepath),
                    metrics=m,
                    faces=readings.get(m.filepath),
                )

        return len(todo)

    def ordered(self, paths: Sequence[str]) -> List[Record]:
        return [self.records[p] for p in paths if p in self.records]


# ============================================================================
# STRAIGHTENING, WITH A TUNABLE MARGIN
# ============================================================================

def _init_straighten(margin: float) -> None:
    """
    Apply the confidence margin inside a worker process.

    Workers import the module afresh, so assigning the constant in the parent
    would be silently ignored by every one of them.
    """
    vertical.UPRIGHT_MARGIN = margin


def straighten(paths: Sequence[str], margin: float, workers: int = 1,
               progress: ProgressFn = None) -> List[vertical.Straightened]:
    """Stand a batch upright with the given margin. In place, quarter turns only."""
    total = len(paths)
    out: List[vertical.Straightened] = []

    def report() -> None:
        if progress:
            progress("standing photos upright", len(out), total)

    report()
    if workers <= 1 or total <= 2:
        previous = vertical.UPRIGHT_MARGIN
        vertical.UPRIGHT_MARGIN = margin
        try:
            for path in paths:
                out.append(vertical.straighten_file(path))
                report()
        finally:
            vertical.UPRIGHT_MARGIN = previous
        return out

    with ProcessPoolExecutor(max_workers=workers, initializer=_init_straighten,
                             initargs=(margin,)) as pool:
        for item in pool.map(vertical.straighten_file, paths):
            out.append(item)
            report()
    return out


# ============================================================================
# PER-PHOTO STATE
# ============================================================================

@dataclass
class Fault:
    """One way a photo fails one check."""

    step: int
    code: str
    label: str
    severity: str           # "major" or "minor"
    detail: str
    enabled: bool           # whether this check is switched on

    def payload(self) -> dict:
        return {"step": self.step, "code": self.code, "label": self.label,
                "severity": self.severity, "detail": self.detail,
                "enabled": self.enabled}


@dataclass
class PhotoState:
    """One photograph as the funnel sees it."""

    record: Record
    quality: float = 0.0            # measured, 0-1
    score: float = 0.0              # after the major-fault penalty
    rank: int = 0                   # overall, by score, 1 = best
    faces: int = -1                 # -1 when no probe ran
    main: Optional[FaceInfo] = None
    band: str = "unknown"
    faults: List[Fault] = field(default_factory=list)
    majors: int = 0                 # major faults at current thresholds
    breakdown: Dict[str, float] = field(default_factory=dict)

    removed_by: Optional[int] = None
    reason: str = ""
    also: List[int] = field(default_factory=list)
    group: int = 0                  # moment, assigned at step 11
    pick_rank: int = 0              # 1-based among selects, 0 if not one

    @property
    def name(self) -> str:
        return self.record.name

    def fails_at(self, step: int) -> List[Fault]:
        return [f for f in self.faults if f.step == step and f.enabled]

    @property
    def orientation(self) -> str:
        m = self.record.metrics
        if not m.width or not m.height:
            return "unknown"
        ratio = m.width / m.height
        if ratio > 1.02:
            return "landscape"
        if ratio < 0.98:
            return "portrait"
        return "square"


def _band(faces: int, split: int) -> str:
    if faces < 0:
        return "unknown"
    if faces == 0:
        return "none"
    if faces == 1:
        return "solo"
    if faces == 2:
        return "duo"
    return "small" if faces < split else "large"


def _percentile(values: Sequence[float], pct: float) -> Optional[float]:
    """
    A batch percentile, or None when it would be meaningless.

    Below four values a percentile is a guess dressed as a measurement, and at
    0% the check is switched off by definition.
    """
    if pct <= 0 or len(values) < 4:
        return None
    return float(np.percentile(np.asarray(values, dtype=np.float64), pct))


def _faces_for(record: Record, face_min: float) -> Tuple[Optional[FaceInfo], int, List[FaceInfo]]:
    reading = record.faces
    if reading is None or not reading.ok:
        return None, -1, []
    kept = sorted((f for f in reading.faces if f.size_ratio >= face_min),
                  key=lambda f: -f.size_ratio)
    return (kept[0] if kept else None), len(kept), kept


# ============================================================================
# CHECKS
# ============================================================================

def _faults(st: PhotoState, p: Dict[str, object], cut: Dict[str, Optional[float]],
            faces_probed: bool) -> List[Fault]:
    """
    Every fault this photo has at the current thresholds, switched on or not.

    Disabled checks are still evaluated so the UI can say "would remove 9" and
    so the major-fault penalty does not depend on which steps are switched on.
    """
    m = st.record.metrics
    out: List[Fault] = []

    def add(step, code, label, sev, detail, on):
        out.append(Fault(step, code, label, sev, detail, bool(on)))

    # --- 4 exposure ---------------------------------------------------------
    if m.blown >= p["exp_blown"]:
        add(4, "blown", "Highlights blown out", "major",
            f"{m.blown * 100:.0f}% of the frame is pure white", p["exp_blown_on"])
    elif m.blown >= p["exp_hot"]:
        add(4, "hot", "Bright highlights", "minor",
            f"{m.blown * 100:.1f}% of the frame is pure white", p["exp_hot_on"])
    if m.brightness > p["exp_bright"]:
        add(4, "bright", "Washed out", "major",
            f"average brightness {m.brightness:.0f} of 255", p["exp_bright_on"])
    if m.brightness < p["exp_dark"]:
        add(4, "dark", "Low light", "major",
            f"average brightness {m.brightness:.0f} of 255", p["exp_dark_on"])
    if m.crushed >= p["exp_shadow"]:
        add(4, "shadow", "Shadow detail lost", "major",
            f"{m.crushed * 100:.0f}% of the frame is pure black", p["exp_shadow_on"])

    # --- 5 focus ------------------------------------------------------------
    soft, slight = cut["soft"], cut["slight"]
    if soft is not None and m.sharpness <= soft:
        add(5, "soft", "Out of focus", "major",
            f"among the softest {p['focus_soft']:.0f}% of the batch", p["focus_soft_on"])
    elif slight is not None and m.sharpness <= slight:
        add(5, "slight", "Slightly soft", "minor",
            f"softer than {100 - p['focus_slight']:.0f}% of the batch", p["focus_slight_on"])

    # --- 6 subject ----------------------------------------------------------
    reading = st.record.faces
    if faces_probed and reading is not None and reading.ok:
        main = st.main
        if main is not None:
            face_cut = cut["face_soft"]
            if (reading.frame_sharpness > 0 and face_cut is not None
                    and main.sharpness < reading.frame_sharpness * p["subj_fsoft"]
                    and main.sharpness <= face_cut):
                ratio = main.sharpness / reading.frame_sharpness
                add(6, "fsoft", "Face not in focus", "major",
                    f"face at {ratio * 100:.0f}% of the frame's sharpness",
                    p["subj_fsoft_on"])
            if (reading.frame_brightness > 0
                    and main.brightness < reading.frame_brightness * p["subj_fdark"]
                    and main.brightness < 95.0):
                add(6, "fdark", "Face in shadow", "major",
                    f"face {main.brightness:.0f} against frame {reading.frame_brightness:.0f}",
                    p["subj_fdark_on"])
            if main.frontality < p["subj_turned"]:
                add(6, "turned", "Head turned away", "minor",
                    f"frontality {main.frontality:.2f}", p["subj_turned_on"])
        elif st.faces == 0:
            add(6, "noface", "Nobody in frame", "minor",
                "no face found; reads as a venue or detail shot", p["subj_noface_on"])

    # --- 8 grain and clarity ------------------------------------------------
    grain, median = cut["grain"], cut["noise_median"]
    if grain is not None and m.noise >= grain and m.noise > median * 1.25:
        add(8, "grain", "Grainy", "minor",
            f"noise {m.noise / median:.1f}\u00d7 the batch median" if median else "grainy",
            p["grain_on"])
    if m.contrast < p["flat"]:
        add(8, "flat", "Flat, low contrast", "minor",
            f"intensity spread {m.contrast:.0f}", p["flat_on"])

    return out


def _cluster(states: Sequence[PhotoState], threshold: int) -> List[List[PhotoState]]:
    """
    Group look-alike frames, strongest first.

    Each group forms around its best remaining photo, so a group's leader is
    always its strongest member.
    """
    groups: List[List[PhotoState]] = []
    for st in states:
        own = st.record.metrics.phash
        placed = False
        if own:
            for g in groups:
                lead = g[0].record.metrics.phash
                if lead and phash_distance(own, lead) <= threshold:
                    g.append(st)
                    placed = True
                    break
        if not placed:
            groups.append([st])
    return groups


def _round_robin(groups: Sequence[Sequence[PhotoState]], want: int,
                 taken: set, prefer_clean: bool) -> List[PhotoState]:
    """
    Take `want` photos, one per group before any group gives a second.

    This is why six selects are six different photographs rather than six
    frames of whichever pose was lit best.
    """
    lanes = []
    for g in groups:
        members = [m for m in g if m.name not in taken]
        if prefer_clean:
            members.sort(key=lambda m: (m.majors > 0, -m.score))
        if members:
            lanes.append(members)
    lanes.sort(key=lambda lane: -lane[0].score)

    out: List[PhotoState] = []
    depth = 0
    while len(out) < want:
        took = False
        for lane in lanes:
            if len(out) >= want:
                break
            if depth < len(lane):
                out.append(lane[depth])
                took = True
        if not took:
            break
        depth += 1
    return out


# ============================================================================
# THE FUNNEL
# ============================================================================

@dataclass
class StepResult:
    n: int
    key: str
    name: str
    before: int = 0
    removed: List[str] = field(default_factory=list)
    hits: Dict[str, int] = field(default_factory=dict)   # check -> fails in input
    available: bool = True
    note: str = ""

    @property
    def after(self) -> int:
        return self.before - len(self.removed)


@dataclass
class FunnelResult:
    params: Dict[str, object]
    states: Dict[str, PhotoState]
    steps: List[StepResult]
    selects: List[str]
    best: Optional[str]
    pairs: List[dict]
    bands: Dict[str, dict]
    moments: int
    failures: List[dict]
    faces_probed: bool
    total: int

    def step(self, n: int) -> StepResult:
        return self.steps[n - 1]


def run_funnel(records: Sequence[Record], params: Optional[dict] = None,
               faces_probed: bool = True) -> FunnelResult:
    """
    Decide, step by step, which photographs survive. Pure; never touches disk.
    """
    p = normalize(params)
    usable = [r for r in records if r.metrics.ok]
    failures = [{"name": r.name, "error": r.metrics.error}
                for r in records if not r.metrics.ok]

    states: Dict[str, PhotoState] = {}
    if usable:
        prev = config.apply_overrides({"QUALITY_WEIGHTS": quality_weights(p["noise_w"])})
        try:
            measured = quality.score_collection([r.metrics for r in usable])
        finally:
            config.apply_overrides(prev)
        breakdowns = shortlist.breakdowns([r.metrics for r in usable])
    else:
        measured, breakdowns = {}, []

    for i, r in enumerate(usable):
        main, count, _ = _faces_for(r, p["face_min"])
        states[r.name] = PhotoState(
            record=r, quality=measured.get(r.path, 0.0), faces=count,
            main=main, band=_band(count, p["band_split"]),
            breakdown=breakdowns[i] if i < len(breakdowns) else {},
        )

    # Batch-wide thresholds: the same meaning however earlier steps are set.
    all_states = list(states.values())
    sharp = [s.record.metrics.sharpness for s in all_states]
    noise = [s.record.metrics.noise for s in all_states]
    face_sharp = [s.main.sharpness for s in all_states
                  if s.main is not None and s.main.sharpness > 0]
    cut = {
        "soft": _percentile(sharp, p["focus_soft"]),
        "slight": _percentile(sharp, p["focus_slight"]),
        "grain": _percentile(noise, p["grain_pct"]),
        "noise_median": float(np.median(noise)) if noise else 0.0,
        "face_soft": _percentile(face_sharp, float(diagnose.FACE_SOFT_PERCENTILE)),
    }

    for st in all_states:
        st.faults = _faults(st, p, cut, faces_probed)
        st.majors = sum(1 for f in st.faults if f.severity == "major")
        st.score = st.quality * (float(p["penalty"]) ** st.majors)

    ordered = sorted(all_states, key=lambda s: (-s.score, s.name))
    for i, st in enumerate(ordered, 1):
        st.rank = i

    steps = [StepResult(n, key, name) for n, key, name in STEPS]
    alive: List[PhotoState] = list(ordered)

    def remove(step: int, victims: Sequence[Tuple[PhotoState, str]]) -> None:
        nonlocal alive
        gone = set()
        for st, why in victims:
            st.removed_by = step
            st.reason = why
            gone.add(st.name)
            steps[step - 1].removed.append(st.name)
        alive = [s for s in alive if s.name not in gone]

    # 1 add, 2 straighten: nothing is removed here.
    for n in (1, 2):
        steps[n - 1].before = len(alive)

    # --- 3 duplicates -------------------------------------------------------
    steps[2].before = len(alive)
    pairs: List[dict] = []
    victims = []
    for g in _cluster(alive, p["dup_threshold"]):
        if len(g) < 2:
            continue
        if p["dup_keep"] == "face":
            keeper = max(g, key=lambda s: ((s.main.sharpness if s.main else -1.0), s.score))
        else:
            keeper = max(g, key=lambda s: s.score)
        lost = []
        for s in g:
            if s is keeper:
                continue
            d = phash_distance(s.record.metrics.phash, keeper.record.metrics.phash)
            victims.append((s, f"near-duplicate of {keeper.name}, distance {d} of 256"))
            lost.append({"name": s.name, "distance": d})
        pairs.append({"keeper": keeper.name, "removed": lost})
    remove(3, victims)

    # --- 4, 5, 6 faults; 7 expressions; 8 grain -----------------------------
    for n in (4, 5, 6, 7, 8):
        sr = steps[n - 1]
        sr.before = len(alive)
        if n == 7:
            sr.available = False
            sr.note = "expression model not installed"
            continue
        if n == 6 and not faces_probed:
            sr.available = False
            sr.note = "face model missing; subject checks skipped"
        for st in alive:
            for f in st.faults:
                if f.step == n:
                    sr.hits[f.code] = sr.hits.get(f.code, 0) + 1
        remove(n, [(st, st.fails_at(n)[0].detail) for st in alive if st.fails_at(n)])

    # --- 9 narrow -----------------------------------------------------------
    sr = steps[8]
    sr.before = len(alive)
    keep = max(1, int(p["narrow_keep"]))
    if len(alive) > keep:
        tail = alive[keep:]
        remove(9, [(st, f"rank {keep + i + 1} of {sr.before} by score")
                   for i, st in enumerate(tail)])

    # --- 10 people ----------------------------------------------------------
    sr = steps[9]
    sr.before = len(alive)
    bands: Dict[str, dict] = {}
    for b in BANDS + ("unknown",):
        here = [s for s in alive if s.band == b]
        bands[b] = {
            "label": BAND_LABEL[b],
            "count": len(here),
            "batch": sum(1 for s in all_states if s.band == b),
            "on": True if b == "unknown" else bool(p[f"band_{b}_on"]),
            "quota": 0 if b == "unknown" else int(p[f"q_{b}"]),
            "top": [s.name for s in here[:4]],
        }
    if not faces_probed:
        sr.available = False
        sr.note = "face model missing; every photo is unbanded"
    remove(10, [(st, f"{BAND_LABEL[st.band].lower()} band switched off")
                for st in alive if not bands[st.band]["on"]])

    # --- 11 one per moment, honouring quotas --------------------------------
    sr = steps[10]
    sr.before = len(alive)
    groups = _cluster(alive, p["shot_threshold"])
    for gi, g in enumerate(groups, 1):
        for st in g:
            st.group = gi

    taken: set = set()
    chosen: List[PhotoState] = []
    prefer = bool(p["prefer_clean"])

    if faces_probed:
        shortfall = 0
        for b in BANDS:
            if not bands[b]["on"]:
                continue
            want = bands[b]["quota"]
            lanes = [[s for s in g if s.band == b] for g in groups]
            got = _round_robin([l for l in lanes if l], want, taken, prefer)
            chosen += got
            taken |= {s.name for s in got}
            shortfall += max(0, want - len(got))
        if shortfall:
            got = _round_robin(groups, shortfall, taken, prefer)
            chosen += got
            taken |= {s.name for s in got}
    else:
        want = sum(int(p[f"q_{b}"]) for b in BANDS if p[f"band_{b}_on"])
        chosen = _round_robin(groups, want, taken, prefer)
        taken = {s.name for s in chosen}

    winner_of = {}
    for s in chosen:
        winner_of.setdefault(s.group, s)
    victims = []
    for st in alive:
        if st.name in taken:
            continue
        w = winner_of.get(st.group)
        if w is not None:
            victims.append((st, f"beaten within its moment by {w.name}"))
        elif not chosen:
            victims.append((st, "every band quota is 0"))
        else:
            victims.append((st, "its band's quota was already filled"))
    remove(11, victims)

    chosen.sort(key=lambda s: (-s.score, s.name))
    for i, s in enumerate(chosen, 1):
        s.pick_rank = i

    # --- 12 the best --------------------------------------------------------
    sr = steps[11]
    sr.before = len(alive)
    best = chosen[0].name if chosen else None
    sr.removed = [s.name for s in chosen[1:]]     # reported, not removed

    # Later faults a removed photo would also have failed: "doomed anyway".
    for st in all_states:
        if st.removed_by is None:
            continue
        st.also = sorted({f.step for f in st.faults
                          if f.enabled and f.step > st.removed_by})

    return FunnelResult(
        params=p, states=states, steps=steps,
        selects=[s.name for s in chosen], best=best, pairs=pairs,
        bands=bands, moments=len(groups), failures=failures,
        faces_probed=faces_probed, total=len(all_states),
    )

