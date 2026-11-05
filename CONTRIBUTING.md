# Contributing to smriti

smriti is a Python 3.11+ project. Keep patches small, explain the problem and the
resulting behaviour, and include regression tests for bug fixes. Use synthetic
examples (Ari and Sam); never commit personal memory, credentials, or audit notes.

## Development setup

From a clone, create and activate a virtual environment, then install:

```sh
python -m venv .venv
# POSIX: source .venv/bin/activate
# PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e ".[read,dev]"
```

The base package has no runtime dependencies. Put new optional dependencies in
the appropriate extra. Read [the conventions](.ai/knowledge/conventions.md) before
introducing a new pattern.

## Tests and checks

Run from the repository root. Set `PYTHONPATH=src`, especially in a worktree, so
tests exercise this checkout rather than a different editable install.

For the deterministic CI mode, no embedding model is required:

```sh
PYTHONPATH=src SMRITI_EMBED_PROVIDER=stub HF_HUB_OFFLINE=1 python -m pytest -q --ignore=tests/test_perf.py
```

In PowerShell:

```powershell
$env:PYTHONPATH = "src"
$env:SMRITI_EMBED_PROVIDER = "stub"
$env:HF_HUB_OFFLINE = "1"
python -m pytest -q --ignore=tests/test_perf.py
```

The stub hashes words into normalised 384-dimensional vectors. It tests indexing
and retrieval plumbing, not semantic similarity. Mark a test `needs_model` only
when its assertions require semantic quality; the collection hook skips those
tests when the stub is selected. Keep the override set throughout a stub-index
test: the indexer's persistent provider-key map currently recognises only ONNX
and local providers. Use disposable indexes, never a real memory index, for stub
runs. Offline mode also prevents the optional reranker from downloading a model.

To exercise the normal model path, unset `SMRITI_EMBED_PROVIDER` and run the same
pytest command. The selected provider needs its model installed or cached. Unset
`HF_HUB_OFFLINE` only when intentionally allowing a first model download. The
`slow` performance guards are excluded by default; run them with `pytest -m slow`.

**Tests must never touch the real memory tree.** The autouse fixture in
`tests/conftest.py` redirects both memory-root variables to a temporary directory
and resets metrics. Use `tmp_path` and explicit roots for new tests; mock external
services and side effects. Some legacy configuration lint tests inspect locally
installed harness configuration and skip when absent; use a disposable home when
you must avoid reading your own configuration, preserving a separate model cache
only for real-model runs.

Before submitting:

```sh
python -m ruff check src tests scripts
python -m mypy --strict path/to/changed_module.py
python scripts/doc_facts.py --write
```

Run tests with both the stub and the normal model for embedding-related changes.
The generated counts in README.md, AGENTS.md, and CLAUDE.md must travel with test or
CLI/MCP surface changes. Keep AGENTS.md and CLAUDE.md identical apart from their
orientation sentence. Repository-wide strict typing still has a backlog; keep new
and touched code clean. CI runs lint and stub-mode tests on Windows, Linux, and
macOS with Python 3.11 and 3.12.

## Commits and review

Make atomic commits: one logical change per commit, with a clear description of
the problem and fix. Include the checks run and any limitations in the pull
request. Hardening phases require a Codex diff review, fixes for findings, and one
recheck, as described in [the hardening plan](.ai/features/hardening.md).

The [quality bar](.ai/knowledge/quality-bar.md) calls for an outside review after
each significant stretch of work. Two fresh models review a fresh clone of the
public repository as engineers deciding whether to adopt it. They grade the code
with file-and-line evidence, without relying on README claims or our prior
context. Isolation is part of the method: a temporary config, empty home, no user
instructions, hooks, MCP servers, skills, or auto-memory, and deny rules outside
the clone. Prove that isolation with a canary before reviewing. Record the grades,
turn findings into prioritised work in `.ai/todo.md`, and check progress at the
next review. A reviewer that has seen the working conversation is not an outside
reviewer. The current isolation harness is private and machine-specific; its
general-purpose replacement remains planned.
