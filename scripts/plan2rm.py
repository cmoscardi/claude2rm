#!/usr/bin/env python3
"""plan2rm CLI — the manual half of the plugin.

    plan2rm doctor            check the toolchain and cloud pairing
    plan2rm status            list what is on the tablet
    plan2rm config            show (and locate) the config file
    plan2rm push <file.md>    render and upload a markdown file by hand
    plan2rm clean --yes       delete every pushed plan from the cloud
    plan2rm clean <project> --yes
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import plan2rm_lib as lib  # noqa: E402

INSTALL_HINTS = {
    "pandoc": "brew install pandoc",
    "tectonic": "brew install tectonic",
    "mmdc": "npm install -g @mermaid-js/mermaid-cli   (only needed for mermaid diagrams)",
    "rmapi": (
        "download the binary for your platform from\n"
        "       https://github.com/ddvk/rmapi/releases into ~/.local/bin"
    ),
}

PAIRING_HINT = """rmapi has no reMarkable cloud credentials yet. Pairing prompts on
    stdin, so it cannot be scripted — do it once, by hand:
      1. get an 8-character code from
         https://my.remarkable.com/device/browser/connect
      2. run `rmapi`, paste the code, then Ctrl-D"""


def cmd_doctor(args):
    print("plan2rm doctor\n")
    failed = False

    print("toolchain")
    for tool in lib.REQUIRED_TOOLS + lib.OPTIONAL_TOOLS:
        path = lib.find_tool(tool)
        required = tool in lib.REQUIRED_TOOLS
        if path:
            print(f"  ok       {tool:<10} {path}")
        else:
            mark = "MISSING " if required else "absent  "
            print(f"  {mark} {tool:<10} {INSTALL_HINTS.get(tool, '')}")
            failed = failed or required

    rmapi_path = lib.find_tool("rmapi")
    if rmapi_path:
        if lib.rmapi_is_patched(rmapi_path):
            print("  ok       rmapi is a patched build (can write to the cloud)")
        else:
            print("  WARNING  rmapi does not look like a patched build. Released rmapi")
            print("           (v0.0.34) cannot write to the reMarkable cloud at all —")
            print("           every put and mkdir fails with HTTP 400 (ddvk/rmapi#75,")
            print("           #76). Build a patched one with:")
            print(f"             bash {Path(__file__).resolve().parent / 'build_rmapi.sh'}")
            failed = True

    print("\nreMarkable cloud")
    if not rmapi_path:
        print("  skipped  (rmapi not installed)")
    elif lib.rmapi_paired():
        print("  ok       paired")
        cfg = lib.load_config()
        reachable = lib.remote_exists(cfg["remote_dir"])
        print(f"  ok       {cfg['remote_dir']} "
              f"{'exists' if reachable else 'not created yet (will be made on first push)'}")
    else:
        print("  MISSING  " + PAIRING_HINT)
        failed = True

    cfg = lib.load_config()
    print("\nconfig")
    print(f"  file     {lib.CONFIG_PATH}")
    print(f"  enabled  {cfg['enabled']}")
    print(f"  device   {cfg['device']}  ({' x '.join(lib.DEVICES[cfg['device']])})")
    print(f"  folder   {cfg['remote_dir']}/<project>")
    print(f"  deny     {cfg['deny'] or '(none)'}")
    print(f"  log      {lib.LOG_PATH}")

    print("\n" + ("FAILED — plans are not being pushed." if failed else "All good."))
    return 1 if failed else 0


def cmd_status(args):
    cfg = lib.load_config()
    rmapi = lib.find_tool("rmapi")
    if not rmapi:
        print("rmapi not installed; run `plan2rm doctor`.")
        return 1
    if not lib.remote_exists(cfg["remote_dir"]):
        print(f"{cfg['remote_dir']} does not exist yet — nothing has been pushed.")
        return 0

    entries = lib.remote_entries(cfg["remote_dir"])
    files = [p for kind, p in entries if kind == "f"]
    for kind, path in entries:
        print(f"  {'/' if kind == 'd' else ' '} {path}")
    if not files:
        print("  (no documents)")

    # A duplicate means an rmapi tree-cache race slipped through; it is the one
    # failure mode that silently piles up copies on the tablet.
    dupes = {n for n in files if files.count(n) > 1}
    if dupes:
        print("\nWARNING: duplicate documents:")
        for d in sorted(dupes):
            print(f"    {d}")
        print("  fix with: plan2rm clean --yes")
    return 0


def cmd_config(args):
    lib.ensure_state()
    print(f"# {lib.CONFIG_PATH}")
    print(lib.CONFIG_PATH.read_text().rstrip())
    return 0


def cmd_push(args):
    path = Path(args.file).expanduser()
    if not path.is_file():
        print(f"no such file: {path}")
        return 1
    text = path.read_text(encoding="utf-8")
    title = lib.plan_title(text, fallback=path.stem)
    print(f"rendering “{title}” ...")
    try:
        remote, title = lib.push_plan(text, os.getcwd())
    except Exception as exc:
        print(f"failed: {exc}")
        return 1
    print(f"pushed to {remote}")
    return 0


def cmd_clean(args):
    cfg = lib.load_config()
    target = cfg["remote_dir"].rstrip("/")
    if args.project:
        target = f"{target}/{lib.safe_filename(args.project, limit=40)}"

    if not args.yes:
        print(f"This deletes every document under {target} in the reMarkable cloud.")
        print("Re-run with --yes to confirm.")
        return 1

    if not lib.find_tool("rmapi"):
        print("rmapi not installed; run `plan2rm doctor`.")
        return 1

    with lib.Lock():
        ok = lib.remote_purge(target)
    return 0 if ok else 1


def main():
    parser = argparse.ArgumentParser(prog="plan2rm", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="check toolchain and cloud pairing")
    sub.add_parser("status", help="list pushed plans")
    sub.add_parser("config", help="show the config file")

    p_push = sub.add_parser("push", help="render and upload a markdown file")
    p_push.add_argument("file")

    p_clean = sub.add_parser("clean", help="delete pushed plans from the cloud")
    p_clean.add_argument("project", nargs="?", help="only this project's folder")
    p_clean.add_argument("--yes", action="store_true", help="confirm deletion")

    args = parser.parse_args()
    return {
        "doctor": cmd_doctor, "status": cmd_status, "config": cmd_config,
        "push": cmd_push, "clean": cmd_clean,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
