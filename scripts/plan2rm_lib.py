"""Shared guts of plan2rm: config, tool discovery, rendering, upload.

Imported by push_plan.py (the PreToolUse hook), preflight.py (SessionStart)
and plan2rm.py (the /plan2rm CLI), so all three agree on where things live and
how a document is named.
"""

import fcntl
import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import date
from pathlib import Path
from xml.etree import ElementTree

# ---------------------------------------------------------------------------
# Paths
#
# State lives outside the plugin directory on purpose: a plugin install is a
# cache copy that gets replaced wholesale on update, which would take your
# config and mermaid cache with it.
# ---------------------------------------------------------------------------

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = PLUGIN_ROOT / "build"

STATE_DIR = Path(os.environ.get("PLAN2RM_STATE") or (Path.home() / ".plan2rm"))
CONFIG_PATH = STATE_DIR / "config.json"
LOG_PATH = STATE_DIR / "plan2rm.log"
LOCK_PATH = STATE_DIR / "upload.lock"
MERMAID_CACHE = STATE_DIR / "mermaid-cache"
PREFLIGHT_STAMP = STATE_DIR / ".preflight-ok"

DEFAULT_CONFIG = {
    "enabled": True,
    "remote_dir": "/claude-plans",
    "device": "rm2",
    # --force replaces the document outright, discarding handwriting on the
    # replaced pages. --content-only keeps annotations but they will not line
    # up once a plan is revised, which is the common case here.
    "put_flag": "--force",
    # Absolute path prefixes whose plans are never uploaded.
    "deny": [],
    # The date goes after the title, so the tablet's own list is alphabetical
    # by document rather than by day. Set false to leave it off entirely.
    "date_suffix": True,
}

# The reMarkable scales a PDF to the screen rather than reflowing it, so
# building at A4 yields unreadable text. Build at roughly physical screen size.
DEVICES = {
    "rm2": ("157mm", "210mm"),
    "rmpp": ("180mm", "239mm"),
}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        if CONFIG_PATH.exists():
            cfg.update(json.loads(CONFIG_PATH.read_text()))
    except Exception as exc:  # a broken config must not stop the hook
        log(f"config unreadable, using defaults: {exc}")
    if cfg.get("device") not in DEVICES:
        cfg["device"] = "rm2"
    return cfg


def ensure_state():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    MERMAID_CACHE.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n")


def log(msg):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with LOG_PATH.open("a") as fh:
            fh.write(f"{stamp}  {msg}\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tool discovery
#
# Hook processes are spawned by the harness, not by an interactive shell, so
# PATH is not guaranteed to include the places these tools actually land.
# rmapi in particular ships as a bare binary with no brew formula and usually
# sits in ~/.local/bin.
# ---------------------------------------------------------------------------

EXTRA_BIN_DIRS = [
    Path.home() / ".local" / "bin",
    Path("/opt/homebrew/bin"),
    Path("/usr/local/bin"),
    Path("/usr/bin"),
    Path("/bin"),
    Path.home() / "bin",
    Path("/opt/homebrew/opt/node/bin"),
    # LibreOffice is a .app bundle; its CLI is never on PATH.
    Path("/Applications/LibreOffice.app/Contents/MacOS"),
]

# Binaries plan2rm builds for itself. This wins over PATH: it exists because
# the released rmapi is broken (see scripts/build_rmapi.sh), so preferring
# PATH would mean preferring the broken one.
OWN_BIN_DIR = STATE_DIR / "bin"


def find_tool(name):
    """Absolute path to `name`: our own bin, then PATH, then usual install dirs."""
    override = os.environ.get(f"PLAN2RM_{name.upper()}")
    if override and Path(override).exists():
        return override
    own = OWN_BIN_DIR / name
    if own.is_file() and os.access(own, os.X_OK):
        return str(own)
    found = shutil.which(name)
    if found:
        return found
    for d in EXTRA_BIN_DIRS:
        cand = d / name
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def rmapi_paired():
    """True if rmapi already holds reMarkable cloud credentials.

    Pairing prompts on stdin for a one-time code, so it cannot be scripted;
    all we can do is detect that it has not happened yet and say so.
    """
    override = os.environ.get("RMAPI_CONFIG")
    candidates = [
        Path(override) if override else None,
        Path.home() / "Library" / "Application Support" / "rmapi" / "rmapi.conf",
        Path.home() / ".config" / "rmapi" / "rmapi.conf",
        Path.home() / ".rmapi",
    ]
    return any(p and p.is_file() for p in candidates)


PATCHED_RMAPI_MANIFEST = STATE_DIR / "patched-rmapi.sha256"


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def record_patched_rmapi(path):
    """Remember a build produced by build_rmapi.sh, by content hash."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    digest = file_sha256(path)
    known = set()
    if PATCHED_RMAPI_MANIFEST.exists():
        known = set(PATCHED_RMAPI_MANIFEST.read_text().split())
    known.add(digest)
    PATCHED_RMAPI_MANIFEST.write_text("\n".join(sorted(known)) + "\n")
    return digest


def rmapi_is_patched(path):
    """Whether `path` is a build known to be able to write to the cloud.

    Identified by content hash, not location: the natural thing to do with the
    patched binary is copy it over the one on your PATH, and a path-based check
    would then report your working rmapi as broken.
    """
    if not path:
        return False
    try:
        if PATCHED_RMAPI_MANIFEST.exists():
            if file_sha256(path) in PATCHED_RMAPI_MANIFEST.read_text().split():
                return True
    except Exception:
        pass
    return Path(path).parent == OWN_BIN_DIR


REQUIRED_TOOLS = ["pandoc", "tectonic", "rmapi"]
# mmdc renders mermaid diagrams; textutil and soffice each convert the old
# binary .doc format, which pandoc cannot read. Any one of them is enough.
OPTIONAL_TOOLS = ["mmdc", "textutil", "soffice"]
DOC_CONVERTERS = ["textutil", "soffice"]


def check_deps():
    """Returns (ok, list_of_problem_strings)."""
    problems = []
    for tool in REQUIRED_TOOLS:
        if not find_tool(tool):
            problems.append(f"missing `{tool}`")
    if find_tool("rmapi") and not rmapi_paired():
        problems.append("rmapi is not paired with the reMarkable cloud")
    return (not problems), problems


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------

H1_RE = re.compile(r"^\s{0,3}#\s+(.+?)\s*#*\s*$", re.MULTILINE)
# Inline markdown that would look like noise in a filename.
MD_INLINE_RE = re.compile(r"[`*_]+")
UNSAFE_FILENAME_RE = re.compile(r"[/\\\x00-\x1f]")


def plan_title(plan_md, fallback="Untitled plan"):
    """The plan's own H1, which Claude writes in plain English."""
    m = H1_RE.search(plan_md or "")
    if not m:
        return fallback
    title = MD_INLINE_RE.sub("", m.group(1)).strip()
    return title or fallback


def title_from_filename(path):
    """A readable title for a document whose markdown has no H1.

    `next-steps.md` reads better on the tablet as "Next steps" than as either
    the raw stem or "Untitled plan".
    """
    stem = Path(path).stem.strip()
    # A hyphen is a word separator in `next-steps.md` but part of the name in
    # `Int. No. 199-2.docx`, so only open up the ones between letters.
    words = re.sub(r"_+", " ", stem)
    words = re.sub(r"(?<=[^\W\d_])-+(?=[^\W\d_])", " ", words)
    words = re.sub(r"\s+", " ", words).strip()
    if not words:
        return "Untitled document"
    return words[0].upper() + words[1:]


def safe_filename(title, limit=80):
    name = UNSAFE_FILENAME_RE.sub(" ", title)
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    if len(name) > limit:
        name = name[:limit].rstrip() + "…"
    return name or "Untitled plan"


def project_name(cwd):
    """Name the tablet folder after the repo, not whatever directory you're in.

    Walking up to the git root means `cd frontend/src` still files the plan
    under the project. Directories that aren't in a repo — a scratch dir, or
    $HOME — fall into a shared bucket rather than creating junk folders.
    """
    try:
        cwd_path = Path(cwd).resolve()
    except Exception:
        return "misc"
    if not cwd_path.is_dir():
        return "misc"

    git = find_tool("git")
    if git:
        try:
            out = subprocess.run(
                [git, "rev-parse", "--show-toplevel"],
                cwd=str(cwd_path), capture_output=True, text=True,
                stdin=subprocess.DEVNULL, timeout=10,
            )
            if out.returncode == 0 and out.stdout.strip():
                root = Path(out.stdout.strip())
                return safe_filename(root.name, limit=40)
        except Exception:
            pass

    # Not a repo. $HOME and filesystem roots would produce a meaningless
    # folder name, so bucket those instead.
    if cwd_path in (Path.home(), Path("/")) or cwd_path.parent == Path("/"):
        return "misc"
    return safe_filename(cwd_path.name, limit=40)


def document_name(title, cfg, when=None):
    """The filename rmapi uploads under, which is what you read on the tablet.

    Title first: the reMarkable shows a truncated name in a narrow tile, so a
    leading date costs the ten characters that tell one document from another.
    `date_prefix` is the old name of the setting and is still honoured.
    """
    base = safe_filename(title)
    dated = cfg.get("date_suffix", cfg.get("date_prefix", True))
    if dated:
        return f"{base} ({(when or date.today()).isoformat()})"
    return base


def is_denied(cwd, cfg):
    try:
        resolved = str(Path(cwd).resolve())
    except Exception:
        return False
    for prefix in cfg.get("deny") or []:
        try:
            expanded = str(Path(prefix).expanduser().resolve())
        except Exception:
            continue
        if resolved == expanded or resolved.startswith(expanded.rstrip("/") + "/"):
            return True
    return False


# ---------------------------------------------------------------------------
# Source documents
#
# The renderer, the title logic and the ascii fallback all work on markdown, so
# a Word file is converted to markdown first and then takes exactly the same
# path as a plan. Conversion goes through pandoc, which is already required.
#
# A PDF is the exception: it is already the format the tablet reads, so it
# skips the reader and the renderer entirely and goes straight to push_pdf().
# ---------------------------------------------------------------------------

MARKDOWN_SUFFIXES = {".md", ".markdown", ".mdown", ".mkd", ".mdx", ".txt", ""}
WORD_SUFFIXES = {".docx", ".doc"}
PDF_SUFFIXES = {".pdf"}
# What read_document() can turn into markdown. A PDF is not in here.
RENDERABLE_SUFFIXES = MARKDOWN_SUFFIXES | WORD_SUFFIXES
SUPPORTED_SUFFIXES = RENDERABLE_SUFFIXES | PDF_SUFFIXES

# Pandoc's own markdown flavour, not gfm: it is the only writer whose output
# the reader in _pandoc_flags() reads back without loss. Word underlining, for
# one, round-trips as [text]{.underline} but as raw <u> HTML in gfm — and raw
# HTML is dropped silently on the way to LaTeX, which in a legal document
# means most of the text disappears.
DOCX_TO_MARKDOWN = [
    "--to=markdown",
    "--wrap=none",
    "--markdown-headings=atx",   # so the table-of-contents heuristic sees them
    "--track-changes=accept",
]

DC_TITLE = "{http://purl.org/dc/elements/1.1/}title"


def docx_title(path):
    """The title Word itself records, or None.

    Read straight out of the package rather than through pandoc: it is one
    small XML file, and most documents leave it empty, so it is not worth a
    second conversion run to find out.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            root = ElementTree.fromstring(zf.read("docProps/core.xml"))
    except Exception:
        return None
    node = root.find(DC_TITLE)
    if node is not None and (node.text or "").strip():
        return node.text.strip()
    return None


def doc_to_docx(path, workdir):
    """Old binary .doc -> .docx inside `workdir`. Raises if neither tool is there.

    Pandoc reads the XML formats only; .doc is a different, undocumented
    format. macOS ships textutil, which converts it without a launch of any
    application; LibreOffice covers everyone else.
    """
    out = workdir / "converted.docx"

    textutil = find_tool("textutil")
    if textutil:
        proc = subprocess.run(
            [textutil, "-convert", "docx", "-output", str(out), str(path)],
            capture_output=True, text=True,
            stdin=subprocess.DEVNULL, timeout=120,
        )
        if out.is_file():
            return out
        raise RuntimeError(f"textutil could not read {path.name}: "
                           f"{proc.stderr.strip()[:200]}")

    soffice = find_tool("soffice")
    if soffice:
        proc = subprocess.run(
            [soffice, "--headless", "--convert-to", "docx",
             "--outdir", str(workdir), str(path)],
            capture_output=True, text=True,
            stdin=subprocess.DEVNULL, timeout=300,
        )
        # LibreOffice names the result after the input and reports success even
        # when it wrote nothing, so find the file rather than trust the exit code.
        produced = workdir / (path.stem + ".docx")
        if produced.is_file():
            return produced
        raise RuntimeError(f"soffice could not read {path.name}: "
                           f"{proc.stderr.strip()[:200]}")

    raise RuntimeError(
        f"{path.name} is in the old binary .doc format, which pandoc cannot "
        "read. Install LibreOffice (`brew install --cask libreoffice`), or "
        "save the file as .docx."
    )


def word_to_markdown(path, workdir):
    """(markdown, title or None) for a .doc or .docx file.

    `workdir` must outlive the render: images are extracted into it and the
    markdown points at them by absolute path.
    """
    pandoc = find_tool("pandoc")
    if not pandoc:
        raise RuntimeError("pandoc is required to read Word documents")

    docx = path if path.suffix.lower() == ".docx" else doc_to_docx(path, workdir)

    proc = subprocess.run(
        [pandoc, *DOCX_TO_MARKDOWN,
         f"--extract-media={workdir / 'media'}", str(docx)],
        capture_output=True, text=True,
        stdin=subprocess.DEVNULL, timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"pandoc could not read {path.name}: "
                           f"{proc.stderr.strip()[:300]}")
    if not proc.stdout.strip():
        raise RuntimeError(f"{path.name} converted to an empty document")

    text, title = proc.stdout, docx_title(docx)
    # Pandoc lifts a paragraph in Word's "Title" style out of the body and into
    # the metadata, so a document titled that way would render with its title
    # missing from the page. Put it back as the H1 it stands for.
    if title and not H1_RE.search(text):
        text = f"# {title}\n\n{text}"
    return text, title


def read_document(path, workdir):
    """(markdown, fallback title) for any supported file.

    The fallback title is used only when the document has no H1 of its own:
    the title Word recorded, or a readable form of the filename.
    """
    path = Path(path)
    suffix = path.suffix.lower()

    if suffix in WORD_SUFFIXES:
        text, title = word_to_markdown(path, Path(workdir))
        return text, title or title_from_filename(path)

    if suffix in PDF_SUFFIXES:
        raise RuntimeError(
            f"{path.name} is a PDF and needs no conversion; send it with "
            "push_pdf() instead."
        )

    if suffix not in MARKDOWN_SUFFIXES:
        raise RuntimeError(
            f"{path.name}: plan2rm renders markdown and Word documents "
            f"({', '.join(sorted(s for s in RENDERABLE_SUFFIXES if s))}), "
            "and sends a PDF as it is. Convert the file first."
        )

    try:
        return path.read_text(encoding="utf-8"), title_from_filename(path)
    except UnicodeDecodeError:
        raise RuntimeError(f"{path.name} is not text; plan2rm renders markdown "
                           "and Word documents.")


# ---------------------------------------------------------------------------
# Markdown preprocessing
#
# xelatex drops any character with no glyph in the chosen fonts, silently. The
# preamble maps the mathematical and dingbat characters that matter; colour
# emoji cannot be embedded by xdvipdfmx at all, so they are stripped here.
# ---------------------------------------------------------------------------

EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"   # pictographs, transport, supplemental symbols
    "\U0001F000-\U0001F2FF"   # tiles, enclosed alphanumerics
    "\U0000FE0F"              # variation selector-16 (emoji presentation)
    "\U0000200D"              # zero-width joiner
    "\U0001F1E6-\U0001F1FF"   # regional indicators (flags)
    "]"
)


def preprocess(plan_md):
    # Only emoji come out. Do NOT tidy the whitespace an emoji leaves behind:
    # LaTeX already collapses runs of spaces in prose, and collapsing them here
    # would destroy indentation in code blocks and alignment in ASCII diagrams,
    # which is most of what makes a plan readable.
    return EMOJI_RE.sub("", plan_md)


def asciify(text):
    """Last-resort fallback: drop everything non-ASCII.

    Only used when a build has already failed once. A plan with mangled
    typography still beats no plan at all.
    """
    replacements = {
        "—": "--", "–": "-", "“": '"', "”": '"', "‘": "'", "’": "'",
        "→": "->", "←": "<-", "⇒": "=>", "↔": "<->", "•": "*", "…": "...",
        "✓": "[y]", "✗": "[n]", "≈": "~=", "≤": "<=", "≥": ">=", "≠": "!=",
        "▶": ">", "◀": "<", "★": "*", "⭐": "*", "│": "|", "─": "-",
        "☐": "[ ]", "☑": "[x]", "☒": "[x]",
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    return text.encode("ascii", "ignore").decode("ascii")


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def _pandoc_flags(cfg, title, toc):
    paper_w, paper_h = DEVICES[cfg["device"]]
    flags = [
        "--standalone",
        "--pdf-engine=xelatex",
        "--from=markdown+tex_math_dollars+pipe_tables+fenced_code_attributes",
        "-H", str(BUILD_DIR / "remarkable.tex"),
        "--lua-filter=" + str(BUILD_DIR / "filters" / "remarkable.lua"),
        "-V", f"geometry:paperwidth={paper_w}",
        "-V", f"geometry:paperheight={paper_h}",
        "-V", "geometry:top=10mm", "-V", "geometry:bottom=12mm",
        "-V", "geometry:left=10mm", "-V", "geometry:right=10mm",
        "-V", "fontsize=11pt",
        "-V", "linestretch=1.05",
        "-V", "monofont=Menlo", "-V", "monofontoptions=Scale=0.85",
        "-V", "colorlinks=true", "-V", "linkcolor=black",
        "-V", "urlcolor=black", "-V", "toccolor=black",
        # title-meta sets the PDF metadata title WITHOUT emitting \maketitle,
        # which would duplicate the plan's own H1 on a title page. Emoji are
        # stripped here too — this string reaches LaTeX, and an unsettable
        # glyph would fail the build before the ascii fallback could help.
        "-V", f"title-meta={EMOJI_RE.sub('', title).strip()}",
    ]
    if toc:
        flags += ["--toc", "--toc-depth=2"]
    return flags


def render_pdf(plan_md, title, out_pdf, cfg, resource_dir=None):
    """Markdown -> reMarkable-sized PDF at `out_pdf`. Raises on failure.

    `resource_dir` is the directory that relative paths in the markdown are
    written against — the directory holding the source file, or the project
    the plan is about. Pandoc runs there and copies every image it finds into
    the build directory, rewriting each path to an absolute one. Without that,
    `![](img.png)` is looked for beside the staged copy of the markdown, in a
    temporary directory that holds nothing else, and the build fails on an
    image that is plainly sitting next to the file.

    Rewriting is what does the work here, not the working directory: tectonic
    resolves a relative path against the .tex file it was given, so no choice
    of working directory would have let it find the image on its own.
    """
    pandoc = find_tool("pandoc")
    tectonic = find_tool("tectonic")
    if not pandoc or not tectonic:
        raise RuntimeError("pandoc and tectonic are both required to render")

    # A table of contents earns its place on a long plan and looks silly on a
    # short one. Section count alone is a poor proxy — a three-page plan can
    # have six headings — so require some actual bulk too.
    toc = (len(re.findall(r"^##\s", plan_md, re.MULTILINE)) >= 4
           and len(plan_md) > 6000)

    env = dict(os.environ)
    env["RMK_MERMAID_CACHE"] = str(MERMAID_CACHE)
    # The lua filter shells out to mmdc; make sure it can be found even when
    # the hook inherited a threadbare PATH.
    env["PATH"] = os.pathsep.join(
        [str(d) for d in EXTRA_BIN_DIRS if d.is_dir()] + [env.get("PATH", "")]
    )
    MERMAID_CACHE.mkdir(parents=True, exist_ok=True)

    # Relative paths belong to whoever wrote the markdown, so resolve them
    # where it was written. A directory that has gone away is not worth
    # failing over: everything except the images still renders without it.
    workdir = str(resource_dir) if resource_dir and Path(resource_dir).is_dir() else None

    def build(source):
        """Returns None on success (out_pdf written), or an error string."""
        with tempfile.TemporaryDirectory(prefix="plan2rm-") as tmp:
            tmp = Path(tmp)
            md_path, tex_path = tmp / "plan.md", tmp / "plan.tex"
            md_path.write_text(source, encoding="utf-8")

            proc = subprocess.run(
                [pandoc, *_pandoc_flags(cfg, title, toc),
                 f"--extract-media={tmp / 'media'}",
                 str(md_path), "-o", str(tex_path)],
                capture_output=True, text=True, env=env, cwd=workdir,
                stdin=subprocess.DEVNULL, timeout=120,
            )
            if proc.returncode != 0:
                return f"pandoc: {proc.stderr.strip()[:500]}"

            # A path that resolves to nothing is not fatal — pandoc puts the
            # image's description in its place — but it is the author's
            # mistake to hear about, and the PDF itself cannot show it.
            for line in proc.stderr.splitlines():
                if "Could not fetch resource" in line:
                    log(f"render '{title}': {line.strip()}")

            proc = subprocess.run(
                [tectonic, "-X", "compile", str(tex_path),
                 "--outdir", str(tmp), "--keep-logs"],
                capture_output=True, text=True, env=env,
                stdin=subprocess.DEVNULL, timeout=300,
            )
            if proc.returncode != 0 or not (tmp / "plan.pdf").exists():
                return f"tectonic: {proc.stderr.strip()[-800:]}"

            out_pdf.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(tmp / "plan.pdf", out_pdf)
            return None

    error = build(preprocess(plan_md))
    if error is None:
        return

    # Most failures here are a character no font in the chain can set, so retry
    # once with the typography flattened. A plan with mangled punctuation still
    # beats no plan at all.
    log(f"render failed, retrying in ascii — {error}")
    fallback_error = build(asciify(plan_md))
    if fallback_error is None:
        return

    raise RuntimeError(f"{error} (ascii retry also failed: {fallback_error})")


# ---------------------------------------------------------------------------
# Upload
#
# rmapi caches the remote tree. A mutation immediately before a put is what
# leaves the next rmapi process with a stale cache, and a stale cache creates a
# duplicate document instead of replacing the existing one. So: only mkdir what
# is actually missing, and hold a lock so concurrent Claude sessions cannot
# interleave their tree mutations.
# ---------------------------------------------------------------------------

class Lock:
    def __init__(self, path=LOCK_PATH, timeout=240):
        self.path, self.timeout, self.fh = path, timeout, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = self.path.open("w")
        deadline = time.time() + self.timeout
        while True:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.time() > deadline:
                    raise RuntimeError("timed out waiting for another plan2rm upload")
                time.sleep(0.5)

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()
        except Exception:
            pass


RETRYABLE_MARKERS = ("status 429", "status 500", "status 502", "status 503")
# rmapi's wording when a path genuinely is not there, as opposed to a lookup
# that could not be completed. Everything else is an error, not an absence.
MISSING_MARKERS = ("doesn't exist", "does not exist", "not found")


def _rmapi(args, timeout=120, retries=3):
    """Run rmapi, retrying transient server errors with backoff.

    Every read re-mirrors the document tree, so a burst of calls gets HTTP 429
    and rmapi exits non-zero. Retrying here keeps that from surfacing as a
    wrong answer upstream.
    """
    rmapi = find_tool("rmapi")
    if not rmapi:
        raise RuntimeError("rmapi not found")
    delay = 2.0
    for attempt in range(retries + 1):
        proc = subprocess.run(
            [rmapi, *args], capture_output=True, text=True,
            stdin=subprocess.DEVNULL, timeout=timeout,
        )
        if proc.returncode == 0 or attempt == retries:
            return proc
        if not any(m in proc.stderr for m in RETRYABLE_MARKERS):
            return proc
        log(f"rmapi {args[0]}: transient error, retrying in {delay:.0f}s")
        time.sleep(delay)
        delay *= 2.5
    return proc


def remote_exists(path):
    """True/False, or raises if the answer could not be established.

    Treating any non-zero exit as "absent" is how a rate-limited probe turns
    into a confident wrong answer — reporting a folder deleted when it is still
    there, or creating a duplicate of a directory that already exists.
    """
    proc = _rmapi(["find", path])
    if proc.returncode == 0:
        return True
    if any(m in proc.stderr for m in MISSING_MARKERS):
        return False
    raise RuntimeError(
        f"could not determine whether {path} exists: {proc.stderr.strip()[:200]}")


def remote_entries(target):
    """[(kind, absolute_path)] for everything under `target`, `target` included.

    `rmapi find X` prints paths starting at basename(X), not at the filesystem
    root — so `find /claude-plans/learn-tts` prints `learn-tts/...`. Prefixing
    those with "/" would name a completely different top-level folder, which is
    how a cleanup can delete the wrong thing. Rejoin against dirname(X).

    Document names contain spaces, so split off the [f]/[d] marker only.
    """
    proc = _rmapi(["find", target])
    if proc.returncode != 0:
        if any(m in proc.stderr for m in MISSING_MARKERS):
            return []
        raise RuntimeError(f"could not list {target}: {proc.stderr.strip()[:200]}")
    parent = posixpath.dirname(target.rstrip("/")) or "/"
    entries = []
    for line in proc.stdout.splitlines():
        line = line.rstrip()
        if not line.startswith("[") or "]" not in line:
            continue
        kind = line[1:line.index("]")]
        name = line[line.index("]") + 1:].lstrip()
        if not name:
            continue
        entries.append((kind, posixpath.join(parent, name)))
    return entries


def remote_rm(path):
    return _rmapi(["rm", path]).returncode == 0


def remote_purge(target, echo=print):
    """Delete `target` and everything under it. Returns True if fully gone.

    rmapi refuses to delete a non-empty directory, so: documents first (retried,
    since the cloud intermittently leaves stragglers), then directories deepest
    first, then the target itself.

    Listings are NOT preceded by an explicit refresh: `find` re-mirrors the tree
    itself, so refreshing first only doubles the API traffic and invites the
    HTTP 429 that made earlier versions of this report a removal that never
    happened. remote_exists/remote_entries raise rather than guess when a lookup
    fails, so a throttled call can no longer be mistaken for an empty folder.
    """
    if not remote_exists(target):
        echo(f"==> {target} does not exist, nothing to do")
        return True

    for attempt in range(1, 4):
        files = [p for kind, p in remote_entries(target) if kind == "f"]
        if not files:
            break
        if attempt > 1:
            echo(f"  retry {attempt} ({len(files)} left)")
        for path in files:
            echo(f"  rm     {path}")
            if not remote_rm(path):
                echo("         failed, will retry")

    leftover = [p for kind, p in remote_entries(target) if kind == "f"]
    if leftover:
        echo(f"WARNING: could not delete {len(leftover)} document(s):")
        for path in leftover:
            echo(f"    {path}")
        return False

    # Deepest first, so a directory is empty by the time it is attempted. Repeat
    # the pass: a parent can only go once the server agrees its children are
    # gone, which may not be true on the first sweep.
    for _ in range(3):
        dirs = sorted((p for kind, p in remote_entries(target) if kind == "d"),
                      key=lambda p: p.count("/"), reverse=True)
        if not dirs:
            break
        for path in dirs:
            echo(f"  rmdir  {path}")
            remote_rm(path)

    if remote_exists(target):
        echo(f"WARNING: {target} still exists on the server after cleanup")
        return False
    echo(f"==> {target} fully removed")
    return True


def ensure_remote_dir(path):
    """Create `path` and its parents, skipping any that already exist.

    rmapi's mkdir is not recursive, so walk down from the root.
    """
    parts = [p for p in path.strip("/").split("/") if p]
    current = ""
    for part in parts:
        current = f"{current}/{part}"
        if not remote_exists(current):
            proc = _rmapi(["mkdir", current])
            if proc.returncode != 0 and not remote_exists(current):
                raise RuntimeError(f"could not create {current}: {proc.stderr.strip()[:200]}")


def upload(pdf_path, remote_dir, put_flag="--force"):
    ensure_remote_dir(remote_dir)
    proc = _rmapi(["put", put_flag, str(pdf_path), remote_dir], timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(f"rmapi put failed: {proc.stderr.strip()[:300]}")


def destination(title, cwd, cfg, project=None):
    """(remote directory, document name) for a document with this title."""
    # A caller-supplied project name becomes a remote path segment, so it gets
    # the same sanitising as one derived from a directory name.
    project = safe_filename(project, limit=40) if project else project_name(cwd)
    return f"{cfg['remote_dir'].rstrip('/')}/{project}", document_name(title, cfg)


def push_plan(plan_md, cwd, cfg=None, title=None, project=None):
    """Render and upload one document. Returns (remote path, title).

    `title` overrides the document's own H1; `project` overrides the folder
    that would be derived from `cwd`. Both exist for the manual `push` path,
    where the file being sent need not be a plan and need not live in the
    repository it belongs to.
    """
    cfg = cfg or load_config()
    ensure_state()

    title = title or plan_title(plan_md)
    remote_dir, doc = destination(title, cwd, cfg, project)

    with tempfile.TemporaryDirectory(prefix="plan2rm-out-") as tmp:
        # rmapi names the document after the file, so the local filename is
        # what you will read on the tablet.
        pdf_path = Path(tmp) / f"{doc}.pdf"
        render_pdf(plan_md, title, pdf_path, cfg, resource_dir=cwd)
        with Lock():
            upload(pdf_path, remote_dir, cfg.get("put_flag", "--force"))

    return f"{remote_dir}/{doc}", title


def push_pdf(src, cwd, cfg=None, title=None, project=None):
    """Upload a PDF unchanged. Returns (remote path, title).

    Nothing is rendered or converted: a PDF is already what the tablet reads,
    and re-making it would only lose the typesetting it came with. It keeps
    whatever page size it was made at, so a letter-sized or A4 PDF reads
    smaller on the screen than one plan2rm builds for the device.

    The title names the file on the tablet and nothing else — there is no page
    to print it on — so it comes from the filename unless the caller gives one.
    """
    cfg = cfg or load_config()
    ensure_state()

    src = Path(src)
    # rmapi accepts anything and the tablet then shows an unopenable document,
    # so check the one byte-string that says this really is a PDF.
    try:
        header = src.open("rb").read(5)
    except OSError as exc:
        raise RuntimeError(f"could not read {src.name}: {exc}")
    if header != b"%PDF-":
        raise RuntimeError(f"{src.name} is named .pdf but is not a PDF file.")

    title = title or title_from_filename(src)
    remote_dir, doc = destination(title, cwd, cfg, project)

    with tempfile.TemporaryDirectory(prefix="plan2rm-out-") as tmp:
        # The upload is copied rather than sent from where it sits, because
        # rmapi names the document after the file and the source filename
        # carries neither the date suffix nor any sanitising.
        staged = Path(tmp) / f"{doc}.pdf"
        shutil.copyfile(src, staged)
        with Lock():
            upload(staged, remote_dir, cfg.get("put_flag", "--force"))

    return f"{remote_dir}/{doc}", title
