"""
Photo Funnel: the rules agreed in the mockup, pinned.

  * every photo is accounted for: removed by exactly one step, or a select
  * a photo is attributed to the FIRST step that removes it; later faults it
    would also fail are recorded, not double-counted
  * switching a check off stops it removing anything
  * people bands follow face counts, quotas are honoured, and a band short of
    its quota passes the rest on rather than losing it
  * the funnel takes no manual picks as input, so picks can never change it
  * parameters are clamped and never raise on bad input
  * export never overwrites, sanitises names, and keeps picks out of the
    numbered selects

Face-driven behaviour uses hand-built FaceReadings, because committing a
photograph of a real person to the repository would be worse than the gap.

Run with:

    python3 -m tests.test_funnel
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from photo_funnel import engine, export  # noqa: E402
from photo_shortlist.metrics import FaceInfo, FaceReading  # noqa: E402
from tests.test_shortlist import write_batch  # noqa: E402


PASSED = FAILED = 0


def ok(label: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
    else:
        FAILED += 1
    print(f"  [{'pass' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


def scan(directory: str) -> engine.Cache:
    paths = sorted(os.path.join(directory, n) for n in os.listdir(directory)
                   if n.endswith(".jpg"))
    cache = engine.Cache()
    cache.scan(paths, workers=1, use_faces=False)
    return cache, paths


def with_faces(records, counts):
    """Attach synthetic face readings: counts[name] faces, all comfortably sized."""
    for r in records:
        n = counts.get(r.name, 0)
        faces = [FaceInfo(box=(10, 10, 80, 80), size_ratio=0.12 - i * 0.01, brightness=140.0,
                          sharpness=500.0, frontality=0.9) for i in range(n)]
        r.faces = FaceReading(filepath=r.path, faces=faces, frame_brightness=140.0,
                              frame_sharpness=400.0, ok=True)
    return records


def check_accounting(cache, paths) -> None:
    print("\naccounting")
    r = engine.run_funnel(cache.ordered(paths), {}, faces_probed=False)
    removed = [s for s in r.states.values() if s.removed_by]
    survivors = [s for s in r.states.values() if not s.removed_by]
    ok("every readable photo has a state", r.total == 14, f"{r.total}")
    ok("unreadable file reported, not ranked", len(r.failures) == 1)
    ok("removed + survivors = total", len(removed) + len(survivors) == r.total)
    ok("survivors are exactly the selects",
       sorted(s.name for s in survivors) == sorted(r.selects))
    ok("step counts chain: each step's before is the last one's after",
       all(r.steps[i].before == r.steps[i - 1].after for i in range(2, 11)))
    ok("removal lists add up",
       sum(len(s.removed) for s in r.steps[2:11]) == len(removed))
    ok("best is the top-scoring select",
       r.best == max(r.selects, key=lambda n: r.states[n].score))
    ok("expressions step is marked unavailable", r.steps[6].available is False)


def check_attribution(cache, paths) -> None:
    print("\nfirst step wins, later faults recorded")
    r = engine.run_funnel(cache.ordered(paths), {"focus_soft": 45}, faces_probed=False)
    blown = [s for s in r.states.values() if s.name.startswith("blown")]
    ok("blown frames go at exposure", all(s.removed_by == 4 for s in blown),
       str([s.removed_by for s in blown]))
    doomed = [s for s in blown if 5 in s.also]
    ok("blown frames that are also soft say so, not counted twice", bool(doomed))
    ok("focus step does not list them", not any(s.name in r.steps[4].removed for s in blown))


def check_toggles(cache, paths) -> None:
    print("\nswitching a check off")
    on = engine.run_funnel(cache.ordered(paths), {}, faces_probed=False)
    off = engine.run_funnel(cache.ordered(paths),
                            {"exp_blown_on": False, "exp_bright_on": False}, faces_probed=False)
    ok("exposure removes the blown frames when on", len(on.steps[3].removed) >= 2)
    ok("and removes none of them when off",
       not any(n.startswith("blown") for n in off.steps[3].removed))
    ok("the fault is still reported as a hit when off", off.steps[3].hits.get("blown", 0) >= 2)
    zero = engine.run_funnel(cache.ordered(paths), {"focus_soft": 0}, faces_probed=False)
    ok("a 0 percentile switches focus off", not zero.steps[4].removed)


def check_narrow_and_quotas(cache, paths) -> None:
    print("\nnarrow and quotas without faces")
    r = engine.run_funnel(cache.ordered(paths), {"narrow_keep": 3}, faces_probed=False)
    ok("narrow keeps exactly N", r.steps[8].after == min(3, r.steps[8].before))
    none = engine.run_funnel(cache.ordered(paths),
                             {f"q_{b}": 0 for b in engine.BANDS}, faces_probed=False)
    ok("all quotas 0 gives no selects and no best", not none.selects and none.best is None)


def check_people(cache, paths) -> None:
    print("\npeople bands with faces")
    recs = with_faces(cache.ordered(paths),
                      {"good1.jpg": 1, "good2.jpg": 1, "good3.jpg": 2, "good4.jpg": 3,
                       "good5.jpg": 7, "good6.jpg": 0})
    p = {"narrow_keep": 50, "shot_threshold": 0, "focus_soft": 0,
         "exp_blown_on": False, "exp_bright_on": False, "exp_dark_on": False}
    r = engine.run_funnel(recs, p, faces_probed=True)
    band = {n: r.states[n].band for n in ("good1.jpg", "good3.jpg", "good4.jpg", "good5.jpg", "good6.jpg")}
    ok("bands follow face counts",
       band == {"good1.jpg": "solo", "good3.jpg": "duo", "good4.jpg": "small",
                "good5.jpg": "large", "good6.jpg": "none"}, str(band))
    ok("nobody-in-frame band is off by default and removes",
       r.states["good6.jpg"].removed_by == 10)
    solos = [n for n in r.selects if r.states[n].band == "solo"]
    ok("solo quota of 2 is honoured", len(solos) == 2, str(solos))
    split = engine.run_funnel(recs, {**p, "band_split": 8}, faces_probed=True)
    ok("raising the split moves 7 faces into small", split.states["good5.jpg"].band == "small")
    short = engine.run_funnel(recs, {**p, "q_large": 3}, faces_probed=True)
    ok("a band short of quota passes the rest on",
       len(short.selects) == 2 + 2 + 1 + 3 or len(short.selects) == short.steps[10].before,
       f"{len(short.selects)} selects")
    big = with_faces(cache.ordered(paths), {"good1.jpg": 1})
    for rec in big:
        if rec.name == "good1.jpg":
            rec.faces.faces[0].size_ratio = 0.03
    tiny = engine.run_funnel(big, {**p, "face_min": 0.05}, faces_probed=True)
    ok("a face below the minimum size is not counted", tiny.states["good1.jpg"].faces == 0)


def check_purity(cache, paths) -> None:
    print("\nthe funnel ignores manual picks")
    import inspect
    sig = inspect.signature(engine.run_funnel)
    ok("run_funnel takes no picks argument", "picks" not in sig.parameters, str(list(sig.parameters)))
    a = engine.run_funnel(cache.ordered(paths), {}, faces_probed=False)
    b = engine.run_funnel(cache.ordered(paths), {}, faces_probed=False)
    ok("same inputs, same selects", a.selects == b.selects)


def check_params() -> None:
    print("\nparameters")
    p = engine.normalize({"dup_threshold": "9999", "focus_soft": "abc", "unknown": 1,
                          "dup_keep": "evil", "exp_blown_on": "false", "focus_slight": 5})
    ok("out-of-range values are clamped", p["dup_threshold"] == 200)
    ok("unparseable values fall back to the default", p["focus_soft"] == engine.DEFAULTS["focus_soft"])
    ok("unknown keys are ignored", "unknown" not in p)
    ok("choices outside the list are refused", p["dup_keep"] == "face")
    ok("string booleans parse", p["exp_blown_on"] is False)
    ok("slightly-soft never sits below out-of-focus", p["focus_slight"] >= p["focus_soft"])
    w = engine.quality_weights(0.4)
    ok("weights still sum to 1", abs(sum(w.values()) - 1) < 1e-9)


def check_cache(directory, paths) -> None:
    print("\ncache")
    c = engine.Cache()
    first = c.scan(paths, workers=1, use_faces=False)
    second = c.scan(paths, workers=1, use_faces=False)
    ok("cold scan measures everything", first == len(paths))
    ok("warm scan measures nothing", second == 0)
    os.utime(paths[0], ns=(os.stat(paths[0]).st_atime_ns, os.stat(paths[0]).st_mtime_ns + 10**9))
    ok("touching one file makes exactly that one stale", c.stale(paths) == [paths[0]])
    ok("removed files are pruned", c.scan(paths[1:], workers=1, use_faces=False) == 0
       and paths[0] not in c.records)


def check_export(cache, paths) -> None:
    print("\nexport")
    r = engine.run_funnel(cache.ordered(paths), {}, faces_probed=False)
    pick = next(s.name for s in r.states.values() if s.removed_by)
    with tempfile.TemporaryDirectory() as dest:
        out = export.write(r, [pick], dest, "My Selects", {"rejected": True}, workdir="/nonexistent")
        target = os.path.join(dest, "My Selects")
        files = sorted(os.listdir(target))
        numbered = [f for f in files if f[:2].isdigit()]
        ok("selects are numbered in order", numbered == [f"{i:02d}_{n}" for i, n in enumerate(r.selects, 1)])
        ok("manual pick is in its own folder",
           os.listdir(os.path.join(target, export.MANUAL_DIR)) == [pick])
        ok("manual pick is not among the numbered selects", not any(pick in f for f in numbered))
        ok("notes and csv written", "why_these_photos.txt" in files and "ranking.csv" in files)
        ok("rejected grouped by step", os.path.isdir(os.path.join(target, export.REJECTED_DIR)))
        try:
            export.write(r, [], dest, "My Selects", {}, workdir="/nonexistent")
            ok("a second export to the same name is refused", False)
        except export.ExportError as exc:
            ok("a second export to the same name is refused", "already exists" in str(exc))
        ok("summary counts photos", out["photos"] == len(r.selects) + 1 + sum(1 for s in r.states.values() if s.removed_by))
    ok("names are sanitised", export.sanitize("../../etc") == "_.._etc" and export.sanitize("a/b") == "a_b")
    try:
        export.sanitize("..")
        ok("'..' is refused", False)
    except export.ExportError:
        ok("'..' is refused", True)
    with tempfile.TemporaryDirectory() as work:
        try:
            export.check_target(os.path.dirname(work), os.path.basename(work), work)
            ok("cannot export into the working directory", False)
        except export.ExportError:
            ok("cannot export into the working directory", True)
    ok("event name strips the ordering prefix", export.event_name("/x/2_Muhurtham") == "Muhurtham")
    ok("no source folder means no shoot destination",
       not any(d["key"] == "shoot" for d in export.destinations(None)))


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="funnel_tests_") as d:
        write_batch(d)
        cache, paths = scan(d)
        check_accounting(cache, paths)
        check_attribution(cache, paths)
        check_toggles(cache, paths)
        check_narrow_and_quotas(cache, paths)
        check_people(cache, paths)
        check_purity(cache, paths)
        check_params()
        check_export(cache, paths)
        check_cache(d, paths)
    print(f"\n{PASSED}/{PASSED + FAILED} passed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
