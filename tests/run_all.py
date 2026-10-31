"""
Run every test suite and report one total.

The suites are self-checking scripts rather than unittest classes, which is why
`python3 -m unittest discover` reports "Ran 0 tests - OK" and looks like a pass
when nothing ran. Use this instead:

    python3 -m tests.run_all
"""

from __future__ import annotations

import subprocess
import sys

SUITES = ["funnel", "shortlist", "selection", "accounting",
          "priority", "timeline", "overrides"]


def main() -> int:
    failed = []
    for name in SUITES:
        proc = subprocess.run([sys.executable, "-m", f"tests.test_{name}"],
                              capture_output=True, text=True)
        last = (proc.stdout.strip().splitlines() or ["(no output)"])[-1]
        status = "ok  " if proc.returncode == 0 else "FAIL"
        print(f"  {status} {name:<11} {last}")
        if proc.returncode != 0:
            failed.append(name)
            print(proc.stdout[-2000:], proc.stderr[-2000:], sep="\n")
    print(f"\n{len(SUITES) - len(failed)}/{len(SUITES)} suites passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
