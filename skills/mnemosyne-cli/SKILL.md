---
name: mnemosyne-cli
description: >
  Use the mnemosyne command line to store, search, list, update and delete
  memories from a terminal or a script. Load this when a task involves running
  mnemosyne commands, scripting memory operations in a shell, consuming memory
  output from a tool loop, or setting up the CLI for the first time.
---

# mnemosyne CLI

One binary serves both vocabularies: the native command set (`retain`, `recall`,
`capsule`, `doctor`, …) and the conventional set (`add`, `search`, `list`,
`get`, `update`, `delete`, `config`, `entity`, `event`).

## Setup

```bash
pip install mnemosyne-os
mnemosyne init                       # initialises the store and writes the CLI config
mnemosyne config --show              # the effective configuration, defaults included
```

`mnemosyne config --show` reports the **effective** configuration, not just what
is written to disk — on a fresh install the config file does not exist yet, and an
empty object would hide the values the CLI is actually using.

## Everyday commands

```bash
mnemosyne add "I prefer dark mode and vim keybindings" --user-id alice
mnemosyne search "what does alice prefer" --user-id alice
mnemosyne list   --user-id alice --top-k 20
mnemosyne get    <memory-id>
mnemosyne update <memory-id> "I switched to light mode"
mnemosyne delete <memory-id> --yes
mnemosyne delete --all --user-id alice --yes
```

`add` also reads from a file or stdin:

```bash
mnemosyne add --file transcript.json --user-id alice
cat notes.txt | mnemosyne add --user-id alice
```

A file whose contents parse as JSON is treated as message objects; anything else
is stored as one text blob.

## Agent mode — for tool loops

Pass `--agent` (alias `--json`) on any command for a machine envelope:

```json
{
  "status": "success",
  "command": "search",
  "duration_ms": 134,
  "scope": { "user_id": "alice" },
  "count": 2,
  "data": [{ "id": "abc-123", "memory": "User prefers dark mode", "score": 0.97 }]
}
```

Errors go to **stdout as JSON** with a non-zero exit code:

```json
{ "status": "error", "command": "get", "error": "...", "code": "NOT_FOUND_001" }
```

So a caller never has to choose between "did it fail" and "can I parse the reason".

In agent mode, confirmation prompts are assumed (`--yes`), because a tool loop has
no way to answer one and a command that blocks forever is worse than one that
acts.

## Output formats

`--output text` (default) · `json` · `table` (default for `list`) · `quiet` (ids
only) · `agent`.

## Scope guards

- `delete --all` and `delete --entity` **require** a scope; without one they
  refuse rather than emptying the shared default scope.
- `import <file>` uses the native importer unless a scope flag or `--agent` is
  given, in which case it routes through the compatible layer so the scope is
  honoured instead of silently dropped.

## Environment variables

`MNEMOSYNE_API_KEY` · `MNEMOSYNE_BASE_URL` · `MNEMOSYNE_USER_ID` · `MNEMOSYNE_AGENT_ID` ·
`MNEMOSYNE_APP_ID` · `MNEMOSYNE_RUN_ID` — plus the `MNEMOSYNE_*` equivalents and
`MNEMOSYNE_DIR` for the store location.

Precedence: flag > environment > config file.

## Pointing at a server instead of a local store

```bash
export MNEMOSYNE_BASE_URL=http://127.0.0.1:8788
export MNEMOSYNE_API_KEY=mn-...
mnemosyne add "..." --user-id alice
```

Without `MNEMOSYNE_BASE_URL` the CLI runs fully embedded — no server needed.
