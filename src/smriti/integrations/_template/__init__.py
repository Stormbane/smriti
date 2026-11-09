"""Template for a new harness integration.

Copy this directory to ``smriti.integrations.<your_harness>`` and fill
in the pieces below. Replace this docstring with one describing
which harness this targets and what its hook/event surface looks like.

The shared building blocks live in ``smriti.integrations.common``.
Use them — don't re-implement. Anything genuinely harness-neutral
(MCP spec, AGENT.md composition, hook script deployment, wake
runner) belongs in ``common``, not in your adapter.

Required pieces:

1. ``install.py`` — exposes ``run_<harness>(memory_root, **opts)``.
   Responsibilities:
     - Patch the harness's settings/config file (JSON for Claude Code,
       TOML for Codex) to wire the SessionStart hook to the package's
       wake (``make_wake_hook_command``: ``python -P -m smriti.wake``)
       with the appropriate ``SMRITI_WAKE_FRAMING``
       (``raw`` for stdout-injection harnesses,
       ``codex-json`` for Codex's ``hookSpecificOutput`` shape).
     - Register the smriti MCP server using
       ``smriti.integrations.common.SMRITI_MCP_COMMAND`` as the
       canonical spec.
     - Write the harness's CLAUDE.md/AGENTS.md equivalent via
       ``smriti.integrations.common.compose_agent_doc(addendum=...,
       memory_rel=..., header=...)``. The addendum should describe
       the harness's hook mechanism and any quirks (file size caps,
       file precedence rules) — everything else comes from the shared
       template.
     - Idempotent. Re-running install must be a no-op except for
       drift-correction.

2. ``hooks/`` (only if the harness has events beyond SessionStart) —
   thin Python shims that delegate to a harness-neutral entry point
   under ``smriti.store``, ``smriti.wake``, etc. Use
   ``smriti.integrations.common.deploy_hook_scripts`` to drop them in
   place, and ``check_deployed_hooks`` in ``smriti doctor`` to catch
   drift. See ``smriti.integrations.claude_code.hooks`` for a deployed
   script.

3. ``payload.py`` (optional) — if the harness's hook stdin shape
   diverges meaningfully from Claude Code's and Codex's (``{"session_id":
   ..., "cwd": ..., "tool_name": ..., "tool_input": ...}``), put a parser
   here, so the hook's logic stays harness-neutral.

4. Tests under ``tests/integrations/<your_harness>/`` — at minimum a
   smoke test that the install function is idempotent against a
   tempdir.

Worked examples in this repo:
    - ``smriti.integrations.claude_code`` — JSON config
      (``~/.claude/settings.json``, ``~/.claude.json`` for MCP),
      two hook events (SessionStart, UserPromptSubmit). Uses
      raw-stdout wake framing.
    - ``smriti.integrations.codex`` — TOML config
      (``~/.codex/config.toml``), SessionStart hook with
      ``hookSpecificOutput.additionalContext`` JSON framing,
      AGENTS.md drop-in.

Register the new harness in ``scripts/install.py`` as a ``--harness``
flag value once the install function is in place.
"""
