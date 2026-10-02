"""
Runtime override checks.

The subtle part is process boundaries. On macOS a ProcessPoolExecutor spawns
fresh interpreters, so each worker imports wedding_album.config anew and starts
from the file's defaults. Rebinding a value in the parent therefore does not
reach the workers, and a threshold changed in the web UI would be quietly
ignored during the very stage it was meant to affect.

Run with:
    python3 -m tests.test_overrides
"""

from __future__ import annotations

import sys
from concurrent.futures import ProcessPoolExecutor

from wedding_album import config, pipeline


def peek(_ignored):
    """Read the values a worker actually sees."""
    return (
        config.FACE_SCORE_THRESHOLD,
        round(config.QUALITY_WEIGHTS["sharpness"], 4),
    )


def main() -> int:
    checks = []

    default_threshold = config.FACE_SCORE_THRESHOLD
    print(f"parent defaults: threshold={default_threshold} "
          f"sharpness={config.QUALITY_WEIGHTS['sharpness']}")

    # --- the trap, demonstrated -------------------------------------------
    config.apply_overrides({"FACE_SCORE_THRESHOLD": 0.91})
    print(f"parent now: threshold={config.FACE_SCORE_THRESHOLD}")

    with ProcessPoolExecutor(max_workers=2) as pool:
        naive = list(pool.map(peek, range(2)))[0]
    print(f"  plain pool worker sees: threshold={naive[0]}")
    ok = naive[0] == default_threshold
    checks.append(ok)
    print(f"  [{'pass' if ok else 'FAIL'}] a plain pool does NOT inherit the override")

    # --- the fix ----------------------------------------------------------
    overrides = {
        "FACE_SCORE_THRESHOLD": 0.91,
        "QUALITY_WEIGHTS": {"sharpness": 0.70},
    }
    config.apply_overrides(overrides)

    with pipeline._pool(2, overrides) as pool:
        seeded = list(pool.map(peek, range(2)))[0]
    print(f"  seeded pool worker sees: threshold={seeded[0]} sharpness={seeded[1]}")

    ok = seeded[0] == 0.91
    checks.append(ok)
    print(f"  [{'pass' if ok else 'FAIL'}] initializer carries the threshold into workers")

    ok = seeded[1] == round(config.QUALITY_WEIGHTS["sharpness"], 4)
    checks.append(ok)
    print(f"  [{'pass' if ok else 'FAIL'}] initializer carries the weights too")

    # --- weights are renormalized ----------------------------------------
    total = sum(config.QUALITY_WEIGHTS.values())
    ok = abs(total - 1.0) < 1e-9
    checks.append(ok)
    print(f"  [{'pass' if ok else 'FAIL'}] weights renormalized to 1.0 (got {total:.6f})")

    # --- bad input is refused --------------------------------------------
    for label, bad in (
        ("unknown key", {"NOT_A_SETTING": 1}),
        ("typo'd weight", {"QUALITY_WEIGHTS": {"shrapness": 0.5}}),
        ("zero weights", {"QUALITY_WEIGHTS": {k: 0.0 for k in config.QUALITY_WEIGHTS}}),
    ):
        try:
            config.apply_overrides(bad)
        except config.ConfigError as exc:
            checks.append(True)
            print(f"  [pass] refuses {label}: {exc}")
        else:
            checks.append(False)
            print(f"  [FAIL] accepted {label}")

    # --- restoring works --------------------------------------------------
    previous = config.apply_overrides({"FACE_SCORE_THRESHOLD": 0.42})
    config.apply_overrides(previous)
    ok = config.FACE_SCORE_THRESHOLD == 0.91
    checks.append(ok)
    print(f"  [{'pass' if ok else 'FAIL'}] previous values can be restored")

    passed, total_checks = sum(checks), len(checks)
    print(f"\n{passed}/{total_checks} passed")
    return 0 if passed == total_checks else 1


if __name__ == "__main__":
    sys.exit(main())
