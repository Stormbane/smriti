"""Per-harness integration packages.

Smriti's core (memory tree, write pipeline, recall, LLM provider, MCP
server) is harness-agnostic. Code that knows about a *specific* agent
harness — Claude Code's hook events, Cursor's settings file, etc. —
lives here, one subpackage per harness.

Conventions for an integration ``smriti.integrations.<name>``:

    hooks/                  scripts that get deployed to the harness's
                            hook directory and parse its stdin payload
                            shape; they should be thin shims that
                            delegate to harness-neutral logic in
                            ``smriti.recall``, ``smriti.store``, etc.
    install.py              functions called by ``scripts/install.py``
                            that wire the harness up — patch its
                            settings file, register the MCP server in
                            its config path, drop in its CLAUDE.md
                            equivalent.
    payload.py (optional)   parser for the harness's hook stdin shape
                            so the recall hook entry point stays
                            harness-neutral.

See ``smriti.integrations._template`` for a stub showing the shape.
Adding a new harness: copy the template, fill in the four pieces, and
register a ``--harness`` flag value in ``scripts/install.py``.
"""
