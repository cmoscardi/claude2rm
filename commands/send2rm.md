---
description: Send a markdown file (or several) to the reMarkable tablet as a paper-sized PDF — use for "send X.md to my reMarkable", "put these notes on my tablet", "sync this doc to the reMarkable"
argument-hint: "<file.md> [more.md ...] [--title \"...\"] [--project name]"
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/plan2rm.py":*)
---

The user wants markdown sent to their reMarkable. Work out which file(s) they
mean from `$ARGUMENTS` and from the conversation, then run:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/plan2rm.py" push <file> [<file> ...]
```

Rules:

- Resolve the file yourself before running. If the user named a file that is
  not in the working directory, search for it. If more than one file matches,
  ask which one. If nothing matches, say so — do not invent a path.
- Pass the files as they are. The renderer takes markdown only; it does not
  accept PDF, plain text, or source files.
- The document is filed under the project folder for the *file's* repository,
  not the shell's directory. Add `--project <name>` only when the user asks for
  a different folder.
- The title comes from the file's own H1, or from the filename when it has
  none. Add `--title "..."` only when the user gives a title.
- The user may ask you to write a document and send it. Write the file first,
  then push it. A markdown file with an H1 renders best.
- Report the remote path the command prints. A push takes about 10-30 seconds
  per file; the document appears on the tablet at its next wifi sync.
- On failure, report the error and suggest `/plan2rm doctor`.
