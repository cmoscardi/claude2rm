#!/usr/bin/env python3
"""plan2rm CLI — the manual half of the plugin.

    plan2rm doctor            check the toolchain and cloud pairing
    plan2rm status            list what is on the tablet
    plan2rm config            show (and locate) the config file
    plan2rm push <file>       render and upload a markdown or Word file
    plan2rm clean --yes       delete every pushed plan from the cloud
    plan2rm clean <project> --yes
"""

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import plan2rm_lib as lib  # noqa: E402

INSTALL_HINTS = {
    "pandoc": "brew install pandoc",
    "tectonic": "brew install tectonic",
    "mmdc": "npm install -g @mermaid-js/mermaid-cli   (only needed for mermaid diagrams)",
    "textutil": "ships with macOS   (converts old .doc files; soffice also does)",
    "soffice": "brew install --cask libreoffice   (only needed for old .doc files "
               "on a machine without textutil)",
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
    # The .doc converters are reported together below: either one will do, so
    # naming the absent one here would read as a problem when it is not.
    for tool in [t for t in lib.REQUIRED_TOOLS + lib.OPTIONAL_TOOLS
                 if t not in lib.DOC_CONVERTERS]:
        path = lib.find_tool(tool)
        required = tool in lib.REQUIRED_TOOLS
        if path:
            print(f"  ok       {tool:<10} {path}")
        else:
            mark = "MISSING " if required else "absent  "
            print(f"  {mark} {tool:<10} {INSTALL_HINTS.get(tool, '')}")
            failed = failed or required

    converter = next((t for t in lib.DOC_CONVERTERS if lib.find_tool(t)), None)
    if converter:
        print(f"  ok       .doc       {lib.find_tool(converter)}")
    else:
        print("  absent   .doc       old .doc files cannot be converted "
              "(.docx and markdown are fine).")
        print(f"                      {INSTALL_HINTS['soffice']}")

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
    """Send one or more documents through the same pipeline as a plan."""
    if args.title and len(args.file) > 1:
        print("--title takes one file at a time.")
        return 1

    paths = []
    for name in args.file:
        path = Path(name).expanduser()
        if not path.is_file():
            print(f"no such file: {path}")
            return 1
        paths.append(path)

    cfg = lib.load_config()
    failures = 0
    for path in paths:
        # A Word file becomes markdown first, and its images are extracted
        # into this directory. It must therefore outlive the render, which
        # reads those images by absolute path.
        with tempfile.TemporaryDirectory(prefix="plan2rm-src-") as workdir:
            try:
                text, fallback = lib.read_document(path, workdir)
            except Exception as exc:
                print(f"failed: {exc}")
                lib.log(f"manual push of {path} failed to convert: {exc}")
                failures += 1
                continue

            # The file's own H1 names it on the tablet. A document written as a
            # note rather than as a plan often has none — and a Word file
            # rarely has one — so fall back to the title Word recorded or to
            # the filename, not to the generic "Untitled plan".
            title = args.title or lib.plan_title(text, fallback=fallback)
            print(f"rendering “{title}” ...")
            try:
                # Render relative to the file, not to the shell's directory:
                # that is what files a document under the project it belongs to.
                remote, title = lib.push_plan(
                    text, str(path.resolve().parent), cfg,
                    title=title, project=args.project,
                )
            except Exception as exc:
                print(f"failed: {exc}")
                lib.log(f"manual push of {path} failed: {exc}")
                failures += 1
                continue
        lib.log(f"pushed '{title}' -> {remote} (manual)")
        print(f"pushed to {remote}")

    if failures:
        print(f"\n{failures} of {len(paths)} file(s) failed; see {lib.LOG_PATH}.")
        return 1
    print("It appears on the tablet at its next wifi sync.")
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

    p_push = sub.add_parser("push", help="render and upload markdown or Word files")
    p_push.add_argument("file", nargs="+", help="markdown, .docx or .doc file(s) to send")
    p_push.add_argument("--title", help="override the document title")
    p_push.add_argument("--project", help="file it under this folder name "
                                          "instead of the one derived from the repo")

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
