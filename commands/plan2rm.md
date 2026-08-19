---
description: Check, inspect, or clean up the reMarkable plan sync
argument-hint: "doctor | status | config | push <file.md> | clean [project] --yes"
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/plan2rm.py":*)
---

Run the plan2rm CLI with the user's arguments and report what it says.

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/plan2rm.py" $ARGUMENTS
```

If no arguments were given, run `doctor`.

Notes for interpreting the output:

- `doctor` reports missing tools with the exact install command. reMarkable
  cloud pairing is interactive and cannot be scripted — if it is unpaired, tell
  the user to run `rmapi` themselves and paste the code, and suggest they use
  the `!` prefix in the prompt so the output lands in this conversation.
- `clean` is destructive and refuses to run without `--yes`. If the user asked
  to clean without confirming, show them what `status` lists first and ask
  before re-running with `--yes`.
- Plans push automatically on every plan Claude writes; the user never needs to
  invoke `push` by hand. It exists for re-sending a markdown file that was
  written some other way.
