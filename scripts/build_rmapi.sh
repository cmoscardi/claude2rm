#!/usr/bin/env bash
# Build a patched rmapi into ~/.plan2rm/bin/rmapi.
#
# Why this exists: rmapi v0.0.34 — the current release — cannot write to the
# reMarkable cloud at all. Every put and mkdir fails with HTTP 400 "invalid
# root schema", because the cloud now requires the root index sorted by
# document ID and rmapi appends new entries unsorted. Upstream issues
# ddvk/rmapi#75 and #76; the fix is open as PR #77 but unmerged.
#
# build/rmapi-sort-root-index.patch is that one-function fix. This script
# clones upstream, applies it, runs the upstream test suite, and installs the
# result somewhere only plan2rm looks — your existing rmapi is left alone.
#
# Delete ~/.plan2rm/bin once upstream ships the fix and plan2rm goes back to
# whichever rmapi is on your PATH.
#
# Usage: bash scripts/build_rmapi.sh

set -euo pipefail

PLUGIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PATCH="$PLUGIN_ROOT/build/rmapi-sort-root-index.patch"
DEST_DIR="${PLAN2RM_STATE:-$HOME/.plan2rm}/bin"
DEST="$DEST_DIR/rmapi"
REPO="https://github.com/ddvk/rmapi.git"

command -v go >/dev/null || {
  echo "error: go is required to build rmapi. Install it with: brew install go"
  exit 1
}
[ -f "$PATCH" ] || { echo "error: patch not found at $PATCH"; exit 1; }

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

echo "==> cloning rmapi"
git clone -q --depth 1 "$REPO" "$work/rmapi"

echo "==> applying $(basename "$PATCH")"
git -C "$work/rmapi" apply "$PATCH"

echo "==> running upstream tests"
(cd "$work/rmapi" && go test ./api/sync15/)

echo "==> building"
(cd "$work/rmapi" && go build -o rmapi-patched .)

mkdir -p "$DEST_DIR"
install -m 0755 "$work/rmapi/rmapi-patched" "$DEST"

# Record the build by content hash so `doctor` recognises it wherever it ends
# up — copying it over the rmapi on your PATH is the natural thing to do, and a
# location-based check would then call your working rmapi broken.
python3 -c "
import sys; sys.path.insert(0, '$PLUGIN_ROOT/scripts')
import plan2rm_lib as lib
print('    sha256', lib.record_patched_rmapi('$DEST'))
"

echo "==> installed $DEST"
echo "    plan2rm prefers this binary over the one on your PATH."
echo
echo "    To use it everywhere instead — fixing any other tool that shells out"
echo "    to rmapi — copy it over yours. doctor identifies it by hash, so it"
echo "    stays recognised:"
echo "      cp \"\$(command -v rmapi)\" \"\$(command -v rmapi).orig\""
echo "      cp \"$DEST\" \"\$(command -v rmapi)\""
echo
echo "    Verify with: /plan2rm doctor"
