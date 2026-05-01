"""Install steps separated by harness-neutrality.

``smriti.install.core`` — memory tree skeleton, wake-script
templates, mirror junctions, qmd recall daemon. Anything that should
run regardless of which agent harness (Claude Code, Cursor, custom
Python agent, ...) the user wires smriti into.

Per-harness install bits live under
``smriti.integrations.<harness>.install`` — settings.json patcher,
MCP registration in the harness's config path, CLAUDE.md/AGENT.md
template writer, deployable hook scripts.

The dispatcher in ``scripts/install.py`` ties them together via a
``--harness`` flag (default: ``claude_code`` for back-compat).
"""
