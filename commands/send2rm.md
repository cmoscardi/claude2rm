---
description: Send a markdown, Word or PDF file (or several) to the reMarkable tablet — use for "send X.md to my reMarkable", "put this docx on my tablet", "sync this PDF to the reMarkable"
argument-hint: "<file> [more ...] [--title \"...\"] [--project name]"
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/plan2rm.py":*)
---

The user wants a document sent to their reMarkable. Work out which file(s) they
mean from `$ARGUMENTS` and from the conversation, then run:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/plan2rm.py" push <file> [<file> ...]
```

Rules:

- Resolve the file yourself before running. If the user named a file that is
  not in the working directory, search for it. If more than one file matches,
  ask which one. If nothing matches, say so — do not invent a path.
- Pass the files as they are. The command takes markdown, Word (`.docx`,
  `.doc`) and PDF. It converts the Word file itself and sends the PDF
  unchanged. Do not convert anything first. It does not accept source files.
- A PDF keeps the page size it was made at. The tablet scales it to the screen,
  so a letter-sized or A4 PDF reads smaller than a document plan2rm builds. Tell
  the user this only if they say the text is too small.
- The document is filed under the project folder for the *file's* repository,
  not the shell's directory. Add `--project <name>` only when the user asks for
  a different folder.
- The title comes from the file's own H1, then from the title Word recorded,
  then from the filename. A PDF always uses the filename, because there is no
  page to print a title on. Add `--title "..."` only when the user gives a
  title, or when a Word or PDF file has an unhelpful filename.
- The user may ask you to write a document and send it. Write the file first,
  then push it. A markdown file with an H1 renders best.
- Report the remote path the command prints. A push takes about 10-30 seconds
  per file, and less for a PDF; the document appears on the tablet at its next
  wifi sync.
- On failure, report the error and suggest `/plan2rm doctor`.
