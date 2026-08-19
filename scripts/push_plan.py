#!/usr/bin/env python3
"""PreToolUse hook for ExitPlanMode — push the plan to the reMarkable.

Runs *before* the approval prompt so the plan is on the tablet by the time you
are asked to approve it. That means this hook blocks for as long as the render
and upload take, which is the point: finishing early would put the prompt in
front of you before there was anything to read.

It never blocks the tool itself. Every failure path exits 0 with an explanatory
message, because a broken tablet sync is not a reason to stop you working.
"""

import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import plan2rm_lib as lib  # noqa: E402

# Claude sometimes re-issues ExitPlanMode with byte-identical content. Rendering
# is the slow part, so skip a repeat push of the same plan to the same project.
DEDUPE_PATH = lib.STATE_DIR / ".last-push.json"
DEDUPE_WINDOW_SECONDS = 900


def respond(message):
    json.dump({"systemMessage": message}, sys.stdout)
    sys.stdout.write("\n")
    sys.exit(0)


def recently_pushed(digest):
    try:
        state = json.loads(DEDUPE_PATH.read_text())
        return (state.get("digest") == digest
                and time.time() - state.get("at", 0) < DEDUPE_WINDOW_SECONDS)
    except Exception:
        return False


def remember(digest):
    try:
        DEDUPE_PATH.write_text(json.dumps({"digest": digest, "at": time.time()}))
    except Exception:
        pass


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # nothing sensible to say without a payload

    if payload.get("tool_name") != "ExitPlanMode":
        sys.exit(0)

    plan = (payload.get("tool_input") or {}).get("plan") or ""
    if not plan.strip():
        sys.exit(0)

    cwd = payload.get("cwd") or "."

    try:
        cfg = lib.load_config()
        lib.ensure_state()
    except Exception as exc:
        respond(f"plan2rm: could not read config ({exc}); plan not pushed.")

    if not cfg.get("enabled", True):
        sys.exit(0)

    if lib.is_denied(cwd, cfg):
        lib.log(f"skipped (denylisted): {cwd}")
        sys.exit(0)

    ok, problems = lib.check_deps()
    if not ok:
        lib.log("preflight failed: " + "; ".join(problems))
        respond(
            "plan2rm: not pushed to reMarkable — " + "; ".join(problems) + ". "
            "Run `/plan2rm doctor` for setup instructions."
        )

    digest = hashlib.sha256(
        (lib.project_name(cwd) + "\0" + plan).encode("utf-8")
    ).hexdigest()
    if recently_pushed(digest):
        title = lib.plan_title(plan)
        respond(f"plan2rm: “{title}” is unchanged since the last push; tablet already has it.")

    started = time.time()
    try:
        remote_path, title = lib.push_plan(plan, cwd, cfg)
    except Exception as exc:
        lib.log(f"push failed: {exc}")
        respond(
            f"plan2rm: failed to push this plan to the reMarkable — {exc}. "
            f"See {lib.LOG_PATH} for details."
        )

    remember(digest)
    elapsed = time.time() - started
    lib.log(f"pushed '{title}' -> {remote_path} ({elapsed:.1f}s)")
    respond(
        f"plan2rm: pushed “{title}” to reMarkable at {remote_path} "
        f"({elapsed:.0f}s). It appears on the tablet at its next wifi sync."
    )


if __name__ == "__main__":
    main()
