# Coding Conventions

<!-- USAGE: Document patterns as they emerge — don't pre-populate with hypotheticals.
     Check here before introducing new patterns. If a pattern isn't here, that's a
     signal to either adopt it consciously (and document it) or avoid it. -->

## Python

- **`from __future__ import annotations`** at the top of every file (PEP 563).
- **`pathlib.Path`** for all path handling. Never raw string paths.
- **`log = logging.getLogger(__name__)`** at module level for logging.
- **Windows path compat**: use `.replace("\\", "/")` when displaying paths or
  building wikilinks. Test path operations on Windows.
- **UTF-8**: reconfigure stdin/stdout at CLI entry points for Windows.

## Type Checking & Linting

- **mypy strict** mode. Full PEP 484 type hints on all functions.
- **ruff** with line-length 100, target Python 3.11.
- **Dataclasses** for structured return values (SearchResult, JudgmentResult, etc.).
- **Uppercase strings** for enum-like values (KEEP, REVISE, PROMOTE, DISCARD).

## Dependencies

- **Zero runtime deps** in the base install. Everything optional goes in extras.
- Extras: `read` (sqlite-vec, onnxruntime), `api` (anthropic), `private`
  (cryptography), `dev` (all + pytest + ruff + mypy).
- **Vendor** rather than depend when the upstream has conflicting architectural
  choices. Preserve attribution (NOTICE.md).

## Error Handling

- **Non-fatal failures**: try-except around non-critical operations (reindex,
  cascade, metrics). A cascade error must not block a write.
- **Log levels**: WARNING for expected failures (missing index, FTS not available).
  ERROR for unexpected. Never swallow silently.
- **Fallback pattern**: primary method (Anthropic SDK) -> secondary (claude -p
  subprocess). Both paths tested.

## Config

- **Environment variables** for all config overrides: NARADA_ROOT,
  NARADA_PROTECTED_FILES, NARADA_LEAF_PREFIXES, NARADA_CLUSTER_THRESHOLD,
  SMRITI_MODEL, SMRITI_EXECUTOR_MODEL, ANTHROPIC_API_KEY.
- **No config files** in v0.1. Env vars only.
- **`root: Path | None = None`** parameter pattern -- most functions accept an
  optional root that defaults to `tree_root()`. This makes testing easy (pass
  tmp_path) without polluting the real tree.

## Functions & Modules

- **One function per concern**: judge functions judge, executor functions execute,
  writer functions write. Don't mix.
- **Return dataclasses** for complex results, not dicts or tuples.
- **Lazy imports** where needed to avoid circular deps (e.g. api_backend inside
  judge functions).
- **Singletons** for MetricsLogger and embedding provider (via factory function).

## CLI

- **argparse with subparsers**. Each command is a `_cmd_{name}(args)` function
  returning an int exit code.
- **User output to stdout**, errors to stderr.
- **`-v` flag** sets logging to INFO. Default is WARNING.

## Testing

- **pytest** with tmp_path fixtures for file isolation.
- **mini_tree fixtures** that create a narada-like tree in temp dirs.
- **Stub judge/executor** (judge_auto_keep, executor_echo) for tests that don't
  need real LLM calls. Use `--real` flag in eval for actual API calls.
- **No external service deps** in default test runs.

## Metrics

- **Structured JSONL** at `~/.narada/.smriti/metrics.jsonl`.
- **snake_case event names**: search_query, index_completed, write_entry,
  cascade_verdict, ingest_completed.
- **Every operation** produces a metrics event. This is not optional.
- **10 MB rotation**, 3 files kept.

## Git

- **Atomic commits** -- one logical change per commit.
- **Never commit** .claude/, memory data, .smriti/ runtime state, sqlite files.
