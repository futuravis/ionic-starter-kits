"""
Drives batch mode over HTTP, the way the browser does.

Checks the real 1099-photo collection: every ceremony folder gets its own album
in place, sizing follows a percentage, duplicates are cleared first, and
relabelling one person refiles photos across every shoot at once.

Writing is skipped by default because copying a gigabyte through iCloud takes
minutes. Pass --write to include it.

    python3 -m wedding_album serve --no-browser &
    python3 -m tools.drive_batch
"""

from __future__ import annotations

import urllib.parse

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("ALBUM_API", "http://127.0.0.1:8766")
ROOT = os.path.expanduser("~/Desktop/candid marriage")


def get(path: str):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=120) as r:
        return json.loads(r.read())


def post(path: str, payload: dict):
    req = urllib.request.Request(
        f"{BASE}{path}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read()), None
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read() or b"{}"), exc.code


def wait(label: str, timeout: int = 2400) -> dict:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        p = get("/api/progress")
        if not p["running"]:
            print(f"  {label}: {p['message'] or p['error'] or 'done'}")
            return p
        if p["stage"] != last:
            last = p["stage"]
            print(f"    ... {last}")
        time.sleep(1.0)
    raise TimeoutError(label)


def main() -> int:
    write = "--write" in sys.argv
    checks = []

    def check(ok, label, detail=""):
        checks.append(ok)
        print(f"  [{'pass' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))

    print("\nshoot discovery")
    d = get("/api/shoots?path=" + urllib.parse.quote(ROOT))
    check(d["count"] == 36, f"{d['count']} shoot folders", "expected 36")
    check(d["total_photos"] == 1099, f"{d['total_photos']} photos", "expected 1099")
    check(not any(s["folder"].startswith("BestImages")
                  for s in d["shoots"]),
          "no album folder mistaken for a shoot")

    print("\nbatch analyze at 10%")
    post("/api/batch", {"source": ROOT, "percent": 10, "drop_duplicates": True})
    p = wait("analyze")
    check(not p["error"], "completed without error", p.get("error", ""))

    res = get("/api/result")
    check(res["mode"] == "batch", "server reports batch mode")
    shoots = res["shoots"]
    check(len(shoots) == 36, f"{len(shoots)} shoots in the result")

    total_photos = sum(s["photos"] for s in shoots)
    total_dupes = sum(s["duplicates"] for s in shoots)
    total_picks = sum(s["pick_count"] for s in shoots)
    check(total_photos == 1099, f"photos add up ({total_photos})")
    check(total_dupes > 0, f"{total_dupes} duplicates dropped")
    check(0 < total_picks < total_photos // 3,
          f"{total_picks} picks, a real reduction",
          f"{100 * total_picks / total_photos:.1f}% of everything")

    print("\nevery shoot with something pickable keeps at least one photo")
    # A shoot can legitimately yield nothing: 'Z Cake Cutting 2' holds a single
    # photo of the cake with nobody in it, so it goes to the no-face bin and
    # there is nothing left to choose from. Only a shoot that still had
    # candidates is required to produce a pick.
    empty = [
        s["folder"] for s in shoots
        if s["kept"] > 0 and not s["pick_count"]
    ]
    check(not empty, "no shoot with candidates came out empty", str(empty[:3]))

    barren = [s["folder"] for s in shoots if s["photos"] and not s["kept"]]
    if barren:
        print(f"    {len(barren)} shoot(s) had nothing pickable at all: "
              f"{', '.join(barren[:3])}")

    print("\npicks scale with shoot size")
    big = max(shoots, key=lambda s: s["photos"])
    small = min((s for s in shoots if s["photos"] >= 6), key=lambda s: s["photos"])
    check(big["pick_count"] > small["pick_count"],
          f"{big['folder']} ({big['photos']}) -> {big['pick_count']} picks, "
          f"{small['folder']} ({small['photos']}) -> {small['pick_count']}")

    print("\nper-shoot picks never exceed what the shoot holds")
    bad = [s["folder"] for s in shoots if s["pick_count"] > s["kept"]]
    check(not bad, "picks within kept photos", str(bad[:3]))

    print("\npeople are global, not per shoot")
    ppl = res["people"]
    check(len(ppl) > 1, f"{len(ppl)} people clustered across the whole collection")
    labelled = [p for p in ppl if p["labeled"]]
    check(any(p["role"] == "bride" for p in labelled), "bride label carried over")
    check(any(p["role"] == "groom" for p in labelled), "groom label carried over")

    bride = next(p for p in ppl if p["role"] == "bride")
    spread = sum(1 for s in shoots if "01_Bride" in s["sections"])
    check(spread > 1,
          f"bride ({bride['photo_count']} photos) appears in {spread} shoots' "
          f"Bride sections")

    print("\nrelabelling refiles across every shoot at once")
    groom = next(p for p in ppl if p["role"] == "groom")
    before = sum(s["sections"].get("02_Groom", 0) for s in shoots)
    t0 = time.time()
    data, code = post("/api/label", {
        "assignments": [{"id": groom["id"], "role": "family", "name": groom["name"]}]
    })
    elapsed = time.time() - t0
    if code:
        check(False, "relabel accepted", data.get("error", ""))
    else:
        after_groom = sum(s["sections"].get("02_Groom", 0) for s in data["shoots"])
        after_family = sum(s["sections"].get("04_Family", 0) for s in data["shoots"])
        check(after_groom < before and after_family > 0,
              f"groom -> family moved photos (Groom {before}->{after_groom}, "
              f"Family 0->{after_family})")
        check(elapsed < 20, f"no re-detection ({elapsed:.1f}s)")

    post("/api/label", {"assignments": [{"id": groom["id"], "role": "groom",
                                         "name": groom["name"]}]})
    restored = sum(s["sections"].get("02_Groom", 0) for s in get("/api/result")["shoots"])
    check(restored == before, f"reverts cleanly ({restored})")

    print("\nguards")
    data, code = post("/api/batch", {"source": "/no/such/folder"})
    check(code == 400, "bad source refused", data.get("error", ""))
    data, code = post("/api/batch_write", {})
    check(code == 400 and "unidentified" in data.get("error", ""),
          "refuses to write with unlabelled people", data.get("error", "")[:60])

    if write:
        print("\nwrite every album in place")
        post("/api/batch_write", {"clean": True, "allow_unlabeled": True})
        wait("write", timeout=3600)

        albums = []
        for current, dirs, _ in os.walk(ROOT):
            for name in dirs:
                if name.startswith("BestImages_v"):
                    albums.append(os.path.join(current, name))
        check(len(albums) == 36, f"{len(albums)} albums on disk")

        versions = {os.path.basename(a) for a in albums}
        check(versions == {"BestImages_v1"},
              "clean reused v1 rather than making v2", str(sorted(versions)))
    else:
        print("\n  (skipping the write stage; pass --write to include it)")

    passed, total = sum(checks), len(checks)
    print(f"\n{passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    import urllib.parse
    sys.exit(main())
