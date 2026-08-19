#!/usr/bin/env python3
"""SessionStart hook — warn early if plan2rm could not push a plan right now.

Silent when everything is healthy, so it costs you nothing on a normal session.
The check is rate-limited by a stamp file: the answer only changes when you
install or uninstall something, and `rmapi find` is a network round trip.
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import plan2rm_lib as lib  # noqa: E402

RECHECK_SECONDS = 6 * 60 * 60


def main():
    try:
        json.load(sys.stdin)
    except Exception:
        pass

    try:
        cfg = lib.load_config()
        if not cfg.get("enabled", True):
            sys.exit(0)
        lib.ensure_state()

        stamp = lib.PREFLIGHT_STAMP
        if stamp.exists() and time.time() - stamp.stat().st_mtime < RECHECK_SECONDS:
            sys.exit(0)

        ok, problems = lib.check_deps()
        if ok:
            stamp.touch()
            sys.exit(0)

        # Leave the stamp alone so a broken setup is re-reported next session
        # rather than going quiet for six hours.
        json.dump({
            "systemMessage": (
                "plan2rm is installed but cannot push plans: "
                + "; ".join(problems)
                + ". Run `/plan2rm doctor` for setup instructions."
            )
        }, sys.stdout)
        sys.stdout.write("\n")
    except Exception as exc:
        lib.log(f"preflight error: {exc}")

    sys.exit(0)


if __name__ == "__main__":
    main()
