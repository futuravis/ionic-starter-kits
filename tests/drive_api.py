"""
Drives the web UI's HTTP API end to end, so the browser path is exercised
rather than assumed.

Checks the same collection the CLI was verified against and compares the
resulting album, since the UI and the CLI must not disagree.

Run with the server already up:
    python3 -m wedding_album serve --no-browser &
    python3 -m tests.drive_api
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("ALBUM_API", "http://127.0.0.1:8766")
SOURCE = os.path.expanduser("~/Desktop/A.4 Get Ready Outdoor")

EXPECTED = {"01_Bride": 10, "02_Groom": 7, "03_Couple": 8}


def get(path: str):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=60) as r:
        return json.loads(r.read())


def post(path: str, payload: dict):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{BASE}{path}", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read()), None
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read() or b"{}"), exc.code


def wait(label: str, timeout: int = 600) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        p = get("/api/progress")
        if not p["running"]:
            print(f"  {label}: {p['message'] or p['error'] or 'done'}")
            return p
        time.sleep(0.5)
    raise TimeoutError(f"{label} did not finish")


def reset_labels() -> None:
    """
    Strip saved roles so the run starts unlabeled.

    Touches people.json directly rather than going through the API, because
    clearing labels wholesale is a test concern and not something the UI should
    offer as a button.
    """
    path = os.path.join(SOURCE, ".album_state", "people.json")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        blob = json.load(fh)
    for person in blob.get("people", []):
        person["role"] = "unknown"
        person["name"] = ""
        person["note"] = ""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(blob, fh, indent=1)
    print("  reset saved labels so the run starts unlabeled")


def main() -> int:
    checks = []

    def check(ok: bool, label: str, detail: str = "") -> None:
        checks.append(ok)
        print(f"  [{'pass' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))

    print("\nstate")
    state = get("/api/state")
    check(state["models_present"], "models present")
    check(state["roles"][-1] == "ignore", "ignore role exposed to the UI")

    # Labels persist between runs by design - that is the whole point of
    # carry_labels - so an un-reset collection would already be labeled and the
    # "before labeling" check below would be meaningless. Clear them first so
    # this driver tests the same thing every time.
    reset_labels()

    print("\nanalyze at defaults")
    post("/api/analyze", {"source": SOURCE, "config": {"FACE_SCORE_THRESHOLD": 0.50}})
    wait("analyze")
    result = get("/api/result")
    ids = [p["id"] for p in result["people"]]
    check(len(ids) == 4, "four clusters found", str(ids))

    print("\nbefore labeling, nothing lands in a named section")
    before = {s["section"]: s["count"] for s in result["sections"]}
    check(set(before) <= {"07_Group", "00_Unsorted"}, "only Group/Unsorted", str(before))

    print("\nlabel through the API")
    for pid, role, name in (
        ("p01", "bride", "Rekha"),
        ("p02", "groom", "Siva"),
        ("p03", "ignore", ""),
        ("p04", "ignore", ""),
    ):
        data, code = post("/api/label", {
            "assignments": [{"id": pid, "role": role, "name": name}]
        })
        if code:
            check(False, f"label {pid}={role}", data.get("error", ""))
            continue
        counts = {s["section"]: s["count"] for s in data["sections"]}
        print(f"    {pid}={role:<7} -> {counts}")
    check(True, "labels applied")

    print("\nsections after labeling")
    result = get("/api/result")
    counts = {s["section"]: s["count"] for s in result["sections"]}
    check(counts == EXPECTED, "matches the CLI result", str(counts))

    print("\nshot groups and diversity picks")
    stats = result["stats"]
    check(stats["shot_groups"] > 0, f"{stats['shot_groups']} pose groups found")
    check(stats["picks"] == 9, f"3 picks per section = {stats['picks']}")

    for sec in result["sections"]:
        picks = [p["name"] for p in sec["picks"]]
        groups = {p["shot_group"] for p in sec["picks"]}
        # The whole point: picks must come from different pose groups, not from
        # whichever group happens to contain the top scores.
        check(len(groups) == len(picks),
              f"{sec['section']} picks are all from different groups",
              f"{picks} groups={sorted(groups)}")

    groom = next(s for s in result["sections"] if s["section"] == "02_Groom")
    naive = [p["name"] for p in sorted(
        (ph for g in groom["groups"] for ph in g["photos"]),
        key=lambda p: -p["score"])][:3]
    chosen = [p["name"] for p in groom["picks"]]
    check(naive != chosen,
          "diversity picks differ from naive top-3",
          f"naive={naive} chosen={chosen}")

    print("\nrelabeling is instant and reversible")
    t0 = time.time()
    data, _ = post("/api/label", {"assignments": [{"id": "p03", "role": "family",
                                                   "name": "Test", "note": "temp"}]})
    elapsed = time.time() - t0
    counts_family = {s["section"]: s["count"] for s in data["sections"]}
    check("04_Family" in counts_family, "idol as family moves 3 photos to Family",
          str(counts_family))
    check(elapsed < 3.0, f"no re-detection ({elapsed:.2f}s)")

    post("/api/label", {"assignments": [{"id": "p03", "role": "ignore"}]})
    counts_back = {s["section"]: s["count"] for s in get("/api/result")["sections"]}
    check(counts_back == EXPECTED, "reverts cleanly", str(counts_back))

    print("\nguards")
    data, code = post("/api/label", {"assignments": [{"id": "p03", "role": "bride"}]})
    check(code == 400 and "already" in data.get("error", ""),
          "second bride refused", data.get("error", ""))

    data, code = post("/api/analyze", {"source": SOURCE, "config": {"NOPE": 1}})
    check(code == 400, "unknown config key refused", data.get("error", ""))

    data, code = post("/api/analyze", {"source": "/no/such/folder"})
    check(code == 400, "bad source refused", data.get("error", ""))

    print("\nimages")
    thumb = urllib.parse.quote("7Y4A2094.JPG")
    with urllib.request.urlopen(f"{BASE}/api/thumb?p={thumb}&w=240") as r:
        data = r.read()
    check(data[:2] == b"\xff\xd8", f"thumbnail is JPEG ({len(data)} bytes)")

    face = result["people"][0]["faces"][0]
    q = urllib.parse.urlencode({"p": face["photo"], "box": face["box"]})
    with urllib.request.urlopen(f"{BASE}/api/face?{q}") as r:
        data = r.read()
    check(data[:2] == b"\xff\xd8", f"face crop is JPEG ({len(data)} bytes)")

    print("\norganize")
    name = get("/api/state")["suggested_name"]
    post("/api/organize", {"picks": 3, "skip_duplicates": False,
                           "output_name": name})
    wait("organize")

    album = os.path.join(SOURCE, name)
    check(os.path.isdir(album), f"wrote a fresh versioned folder: {name}")

    picks_on_disk, alts_on_disk = {}, {}
    for section in sorted(os.listdir(album)):
        sdir = os.path.join(album, section)
        if not os.path.isdir(sdir):
            continue
        picks_on_disk[section] = len([f for f in os.listdir(sdir)
                                      if f.upper().endswith(".JPG")])
        second = os.path.join(sdir, "2nd_Option")
        alts_on_disk[section] = (
            len([f for f in os.listdir(second) if f.upper().endswith(".JPG")])
            if os.path.isdir(second) else 0
        )

    check(all(v == 3 for v in picks_on_disk.values()),
          "3 picks in each section root", str(picks_on_disk))
    check(sum(alts_on_disk.values()) == 25 - 9,
          "the other 16 are alternatives", str(alts_on_disk))
    check(os.path.isfile(os.path.join(album, "_album_report.csv")), "report written")

    originals = len([f for f in os.listdir(SOURCE) if f.upper().endswith(".JPG")])
    check(originals == 25, f"originals untouched ({originals})")

    passed, total = sum(checks), len(checks)
    print(f"\n{passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
