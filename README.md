# plan2rm

A Claude Code plugin that pushes every plan Claude writes to a reMarkable
tablet, as a PDF sized for the screen, **before** you are asked to approve it.

The idea is to read plans on e-ink instead of scrolling a terminal. Claude
calls `ExitPlanMode`; a `PreToolUse` hook intercepts it, renders the plan, and
uploads it; only then does the approval prompt appear in your terminal.

Install once, globally. After that it is automatic in every project on the
machine — nothing to configure per repo, nothing to invoke by hand.

## How it works

```
Claude finishes planning
        │
        ▼
  ExitPlanMode  ──►  PreToolUse hook (scripts/push_plan.py)
                          │
                          │  tool_input.plan  — the full markdown
                          │  H1 of the plan   — the document title
                          │  cwd → git root   — the tablet folder
                          ▼
                     pandoc ─► tectonic ─► PDF at 157 x 210 mm
                          │
                          ▼
                     rmapi put  ──►  /claude-plans/<project>/<date> <title>
        │
        ▼
  approval prompt appears in the terminal
```

The title is the plan's own H1, which Claude already writes in plain English
("Member-facing class booking"), so no extra model call is needed to name the
document.

Every revision of a plan is pushed, including ones you reject — those are
exactly the ones you are reading in order to decide. A revision replaces the
previous document rather than piling up copies, because the filename is derived
from the title.

## Install

Dependencies first — the plugin can ship scripts, but it cannot install
binaries for you:

```sh
brew install pandoc tectonic go
npm install -g @mermaid-js/mermaid-cli    # optional, for ```mermaid blocks

mkdir -p ~/.local/bin
# grab the right build from https://github.com/ddvk/rmapi/releases
```

Pair `rmapi` with the reMarkable cloud once, by hand. It prompts on stdin, so
it cannot be scripted:

1. Get an 8-character code from
   <https://my.remarkable.com/device/browser/connect>
2. Run `rmapi`, paste the code, then `Ctrl-D`

### You need a patched rmapi

**The released rmapi (v0.0.34, the current latest) cannot write to the
reMarkable cloud at all.** Every `put` and `mkdir` fails with HTTP 400 "invalid
root schema": the cloud began requiring the root index to be sorted by document
ID, and rmapi appends new entries unsorted. Reads (`ls`, `find`, `get`) still
work, which is why the breakage is easy to miss. See upstream
[ddvk/rmapi#75](https://github.com/ddvk/rmapi/issues/75) and
[#76](https://github.com/ddvk/rmapi/issues/76); the fix is open as PR #77 but
unmerged.

`build/rmapi-sort-root-index.patch` is that fix — sorting the docs in
`HashTree.IndexReader()`, the single serialization point, so the uploaded body
and the hash computed over it stay consistent. Build it:

```sh
bash scripts/build_rmapi.sh
```

That clones upstream, applies the patch, runs the upstream test suite, and
installs to `~/.plan2rm/bin/rmapi`, which plan2rm prefers over whatever is on
your `PATH`. Your existing rmapi is left untouched.

Since the released rmapi is broken for *every* tool that shells out to it — not
just this one — you probably want the patched build everywhere:

```sh
cp "$(command -v rmapi)" "$(command -v rmapi).orig"   # keep a backup
cp ~/.plan2rm/bin/rmapi "$(command -v rmapi)"
rm -rf ~/.plan2rm/bin                                 # one binary, not two
```

`doctor` identifies a patched build by **content hash**, recorded in
`~/.plan2rm/patched-rmapi.sha256` when it is built — not by location — so it
keeps recognising the binary after you move it, and still flags a stock one.

Once upstream ships the fix, restore a released rmapi and delete the manifest.

Then install the plugin, from this directory:

```
/plugin marketplace add ~/plan2rm
/plugin install plan2rm@plan2rm
```

Verify with `/plan2rm doctor`.

## Usage

There is no usage. That is the point — plans push themselves.

The `/plan2rm` command exists for everything around that:

| Command | Effect |
| --- | --- |
| `/plan2rm doctor` | Check the toolchain, pairing, and config |
| `/plan2rm status` | List what is on the tablet, flag duplicates |
| `/plan2rm config` | Show the config file and its path |
| `/plan2rm push <file.md>` | Render and upload a markdown file by hand |
| `/plan2rm clean --yes` | Delete every pushed plan from the cloud |
| `/plan2rm clean <project> --yes` | Delete one project's folder |

## Config

`~/.plan2rm/config.json`, created on first run:

```json
{
  "enabled": true,
  "remote_dir": "/claude-plans",
  "device": "rm2",
  "put_flag": "--force",
  "deny": [],
  "date_prefix": true
}
```

- **`device`** — `rm2` (157 x 210 mm) or `rmpp` for the Paper Pro
  (180 x 239 mm). The reMarkable scales PDFs to the screen rather than
  reflowing them, so building at A4 would give you unreadably small text.
- **`put_flag`** — `--force` replaces the document outright, discarding
  handwriting on the replaced pages. `--content-only` keeps annotations, but
  they will not line up once a plan is revised.
- **`deny`** — absolute path prefixes whose plans are never uploaded. Global
  scope means plans from *every* project reach reMarkable's cloud; this is the
  escape hatch for anything you would rather keep off it.

State lives in `~/.plan2rm/` rather than inside the plugin, since a plugin
install is a cache copy that gets replaced wholesale on update. The log at
`~/.plan2rm/plan2rm.log` records every push, skip, and failure.

## Design notes

**Why a `PreToolUse` hook.** A hook is the only extension point the harness
fires deterministically — skills and MCP tools depend on Claude choosing to
invoke them, and slash commands depend on you remembering. `PreToolUse`
specifically, rather than `PostToolUse`, because the plan has to be readable
before you decide, and `PostToolUse` only fires after approval.

**Why it blocks.** Rendering and uploading takes ~10s. Detaching would put the
approval prompt in front of you while tectonic was still running, which defeats
the purpose. The hook holds the prompt back until the document is in the cloud.

**Why it never fails loudly enough to stop you.** Every error path exits 0 with
a message. A broken tablet sync is not a reason to block your work — but it is
silent-failure-prone, so problems are reported at `SessionStart` (rate-limited
to once every six hours) as well as at push time.

**Tool discovery.** Hook processes are spawned by the harness, not by an
interactive shell, so `PATH` is not reliable — `rmapi` in particular has no brew
formula and lands in `~/.local/bin`. `find_tool()` searches `PATH` and then the
usual install directories.

**Upload serialization.** `rmapi` caches the remote tree, and a mutation
immediately before a `put` leaves the next process with a stale cache, which
creates a *duplicate* document instead of replacing one. So directories are
only created when actually missing, and an `flock` keeps concurrent Claude
sessions from interleaving their tree mutations. `/plan2rm status` flags
duplicates if one slips through anyway.

**Failure is not absence.** `rmapi find` exits non-zero both when a path is
genuinely missing and when the lookup could not be completed. Every read
re-mirrors the whole document tree, so a burst of calls earns an HTTP 429 — and
code that reads "non-zero means absent" will then cheerfully report a folder
deleted while it is still sitting on the tablet. So `remote_exists()` and
`remote_entries()` distinguish rmapi's not-found wording from every other error
and **raise** rather than guess, and `_rmapi()` retries 429/5xx with backoff.
For the same reason nothing calls `rmapi refresh` before a listing: `find`
already re-mirrors, so refreshing first only doubles the traffic and causes the
throttling it was meant to work around.

**Remote path handling.** `rmapi find <dir>` prints paths relative to the
*parent* of the directory you searched — `find /claude-plans/learn-tts` prints
`learn-tts/...`. Prefixing those with `/` names a different top-level folder
entirely, so cleanup rejoins them against `dirname(target)` instead. Document
names also contain spaces, so parsing splits off the `[f]`/`[d]` marker only,
never on whitespace generally.

**Glyph coverage.** xelatex silently drops characters with no glyph in the font
chain. The LaTeX preamble maps the mathematical and dingbat characters plans
tend to use; colour emoji cannot be embedded at all, so they are stripped. If a
render still fails, it is retried once with the typography flattened to ASCII —
a plan with mangled punctuation beats no plan.

**Sync latency.** `rmapi` uploads to the reMarkable *cloud*. The tablet pulls on
its next wifi sync, typically 10-30s if it is awake. That lag is not something
this plugin can control.

## Developing

Installing copies this directory into
`~/.claude/plugins/cache/plan2rm/plan2rm/<version>/` and Claude Code runs the
copy — editing `~/plan2rm` does **not** take effect until you reinstall:

```sh
claude plugin uninstall plan2rm@plan2rm && claude plugin install plan2rm@plan2rm
```

Bump `version` in `.claude-plugin/plugin.json` when the change matters, since
the cache path contains it. State in `~/.plan2rm/` survives reinstalls by
design, so your config, log, and patched rmapi are not disturbed.

## Uninstall

```
/plan2rm clean --yes                    # wipe the documents off the tablet
/plugin uninstall plan2rm@plan2rm       # stop the hook firing
/plugin marketplace remove plan2rm      # forget the marketplace
rm -rf ~/plan2rm ~/.plan2rm             # remove the plugin, its state, its rmapi
```

Nothing is left behind in `~/.claude/settings.json`.

The render pipeline (`build/remarkable.tex`, `build/filters/remarkable.lua`) is
lifted from `learn-tts` and keeps its equation and mermaid handling. The
cleanup logic is *not* — the version there parses `rmapi` output with
`awk '{print $2}'`, which truncates any document name at its first space, and
prefixes subdirectory results with `/`, which points at the wrong folder. Plan
titles are full of spaces, so both had to go.
