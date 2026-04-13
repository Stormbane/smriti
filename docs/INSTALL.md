# smriti — Installation

> **Status**: smriti is in design phase. The Python package doesn't
> install yet. What you CAN install today is the **PreCompact capture
> hook** — a small standalone script that captures conversation turns
> from Claude Code sessions before context compaction destroys them.
> This is the pre-v0.1 backstop that seeds smriti's `events/{entity}/`
> namespace once the main package lands.

This document describes:

1. The PreCompact capture hook (installable today, ~10 minutes)
2. The smriti package itself (not yet — will fill in when v0.1 ships)

---

## 1. PreCompact capture hook (pre-v0.1 backstop)

### What it does

Claude Code compacts conversation context when it gets too large. The
compacted summary is lossy — details of earlier turns can be lost
forever. This hook captures the raw user/assistant turns as individual
markdown files *before* the compaction runs, so the raw conversation
is durable.

Every Claude Code session on your machine writes to a single
per-user staging directory. Events are namespaced by project:

```
~/.claude/narada-staging-events/
├── .markers/
│   └── {session_id}.json
├── svapna-narada/
│   └── 2026/04/2026-04-11/
│       ├── 6bd926fa-turn-0001.md
│       └── ...
├── beautiful-tree-narada/
│   └── 2026/04/2026-04-07/
│       └── ...
└── <any-other-project>-narada/
    └── ...
```

Entity names are derived from the working directory: the cwd basename
becomes `{basename}-narada`. `C:\Projects\svapna` → `svapna-narada`.
`C:\Projects\beautiful-tree` → `beautiful-tree-narada`. No per-project
configuration.

When smriti v0.1 ships, the staging tree migrates to
`~/.narada/memory/events/` with one `mv`, and smriti's cascading review
ingests the accumulated turns in bulk. Nothing is lost in the migration.

### Requirements

- Claude Code installed
- Python 3.11+
- Write access to `~/.claude/hooks/` and `~/.claude/narada-staging-events/`

### Install

**1. Fetch the hook script**:

Download `precompact_capture.py` from this repository
(`smriti/src/smriti/hooks/precompact_capture.py` once the reference
implementation is extracted from svapna; for now the canonical source
is inside the svapna repository) and save it to:

```
~/.claude/hooks/precompact_capture.py
```

Make sure Python can execute it:

```bash
python ~/.claude/hooks/precompact_capture.py < /dev/null
# Expected: [precompact_capture] no stdin payload; nothing to do
# Exit code 0
```

**2. Create the staging directory** (the script will create it on first
run, but creating it manually lets you set permissions):

```bash
mkdir -p ~/.claude/narada-staging-events/.markers
```

**3. Wire the hook in `~/.claude/settings.json`**:

Add a `PreCompact` entry under `hooks`. If you already have hooks
configured, add this alongside them — do not overwrite the file.

```json
{
  "hooks": {
    "PreCompact": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python ~/.claude/hooks/precompact_capture.py"
          }
        ]
      }
    ]
  }
}
```

**4. Verify**:

Start a Claude Code session in any project. Run `/compact` to trigger
a manual compaction. After compaction completes, check the staging
directory:

```bash
ls ~/.claude/narada-staging-events/{projectname}-narada/
```

You should see dated directories with markdown turn files.

### What gets captured

Per user/assistant turn, a markdown file with YAML frontmatter:

```markdown
---
session_id: <uuid>
turn_number: <integer, sequential per session>
role: user | assistant
timestamp: <UTC ISO-8601 from transcript>
captured_at: <UTC ISO-8601 at hook run time>
captured_by: precompact-hook
trigger: manual | auto
entity: <derived-from-cwd>
cwd: <absolute working directory>
---

<the turn content, verbatim>
```

### What is NOT captured

- **Thinking blocks**. Model internal reasoning is private and
  volatile, and persisting it would create large files that don't
  reflect anything the user can see.
- **System messages, permission prompts, file-history snapshots**.
  Only `role: user` and `role: assistant` turns are captured.
- **Sessions older than the last marker**. The hook is incremental —
  if it has run before on this session, it only captures new turns
  since the last run.

### Safety properties

- **Never blocks compaction.** The script exits 0 on any error
  (missing transcript, corrupt JSON, write failure). A hook failure
  cannot lock your Claude Code session.
- **Incremental.** Per-session markers at
  `~/.claude/narada-staging-events/.markers/{session_id}.json` track
  the last JSONL line processed. Repeated runs are O(new turns), not
  O(whole transcript).
- **Idempotent.** Running with no new turns is a no-op.
- **Gitignore your staging directory** if you put it inside a git
  repository. The reference install at `~/.claude/narada-staging-events/`
  is outside any repo by design.

### Uninstall

```bash
# Remove the hook wiring in ~/.claude/settings.json
# (manually edit the file and delete the PreCompact entry)

# Remove the script
rm ~/.claude/hooks/precompact_capture.py

# Remove the staging directory (CAUTION: this deletes all captured turns)
# rm -rf ~/.claude/narada-staging-events/
```

### Troubleshooting

**"transcript not found" in stderr logs**:
The script derives the transcript path from `session_id` + `cwd`.
If your Claude Code install puts transcripts in a non-standard
location, or if the project-hash rule differs on your OS, the
derivation may be wrong. File an issue with your OS, Claude Code
version, and an example of the actual transcript path on disk.

**No turns captured after compaction**:
Check stderr output in Claude Code's hook log. Common causes:
- Empty or missing `session_id` in the payload
- JSON parse error on stdin (script exits 0, logs the error to stderr)
- The session has no user/assistant turns yet (system/setup messages
  only)

**Entity name looks wrong**:
The entity is `Path(cwd).name + "-narada"` with non-alphanumeric chars
sanitized to dashes. If your project name contains unusual characters,
check the staging directory for the actual entity folder created.

### Future migration path

When smriti v0.1 ships, the staging tree becomes the initial content
of smriti's `events/` layer. The migration:

```bash
# 1. Install smriti (when available)
pip install smriti

# 2. Initialize the memory tree
smriti init

# 3. Import the staging events
smriti import-staging ~/.claude/narada-staging-events/

# 4. Remove the old staging tree
rm -rf ~/.claude/narada-staging-events/

# 5. Point the PreCompact hook at smriti's own capture command
# (the hook script is rewritten to use smriti.hooks.precompact)
```

After migration, captured turns flow directly through smriti's six-step
pipeline (CAPTURE → EXTRACT → JUDGE → WRITE → CROSSLINK → INDEX) and
trigger cascading reviews up the impact tree. The backstop becomes the
main ingest path.

---

## 2. smriti package (pre-alpha — not installable yet)

The reference implementation is being built inside
[svapna](https://github.com/). When smriti v0.1 is ready, this section
will describe:

- `pip install smriti`
- Initializing `~/.narada/memory/` or a custom memory root
- Wiring the pluggable `IdentityCore` (default: Qwen3-8B + per-entity
  LoRA adapter)
- Wiring the pluggable `Executor` (default: Claude Code headless mode
  via `claude -p`)
- Migrating staging events into the main pipeline
- Running the first backlog consolidation pass
- Verifying the cascade works by writing a test leaf and watching the
  review propagate

For now, if you're eager to follow along:

- Read [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) to understand the
  design
- Install the PreCompact hook above so you're not losing context to
  compaction in the meantime
- Watch this repository for v0.1 release notes

---

## Questions and issues

smriti is being built in public by two authors (Suti and Narada — see
the README). Questions and issues are welcome on this repository.
Please include:

- Your OS + Claude Code version
- A description of the project layout (where cwd lives, how it's named)
- Relevant stderr output from the hook log, if applicable
- Whether you are installing for a specific entity (a particular LoRA-
  tuned model) or using the fudge-layer install without an entity

🪔

*Om Namo Bhagavate Naradaya.*
