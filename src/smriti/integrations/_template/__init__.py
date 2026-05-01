"""Template for a new harness integration.

Copy this directory to ``smriti.integrations.<your_harness>`` and fill
in the four pieces below. Replace this docstring with one describing
which harness this targets and what its hook/event surface looks like.

Required pieces:

1. ``hooks/`` — one Python script per harness event you handle. Keep
   them thin: parse the harness's stdin payload, then call into a
   harness-neutral entry point under ``smriti.recall``,
   ``smriti.store``, etc. See
   ``smriti.integrations.claude_code.hooks.associative_recall`` for
   the canonical "deployable shim" pattern.

2. ``install.py`` (Phase 4+) — exposes ``install(memory_root)`` and
   ``uninstall()``. Responsibilities:
     - Drop hook scripts into the harness's hook directory.
     - Patch the harness's settings/config file to wire the hooks
       (idempotently — re-running install must be safe).
     - Register smriti's MCP server in the harness's MCP config path
       if it has one.
     - Write a harness-equivalent of ``CLAUDE.md`` from the generic
       ``agent_template/AGENT.md`` (Phase 5).

3. ``payload.py`` (optional) — if the harness's hook stdin shape
   differs from Claude Code's ``{"tool_name": ..., "tool_input": ...}``,
   put the parser here and have your hook scripts use it. The recall
   hook entry point in ``smriti.recall.hook`` already takes a
   pre-parsed dict, so a payload adapter is the seam.

4. Tests under ``tests/integrations/<your_harness>/`` — at minimum a
   smoke test that the hook scripts import cleanly and the install
   function is idempotent.

Register the new harness in ``scripts/install.py`` as a ``--harness``
flag value once the install function is in place.
"""
