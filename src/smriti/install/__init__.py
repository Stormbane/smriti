"""Install steps separated by harness-neutrality.

``smriti.install.core`` — memory tree skeleton, wake-script
templates, mirror junctions, qmd recall daemon. Anything that should
run regardless of which agent harness (Claude Code, Cursor, custom
Python agent, ...) the user wires smriti into.

``smriti.install.templates`` — where the files it copies are found: the
package's own copies in a wheel, the checkout's in an editable install.

``smriti.install.privacy`` — the two questions every install asks
before it changes anything: capture conversations into the day-log, and
is the backup remote private. It writes the capture choice into
``daylog.json`` and prints what is captured, committed and pushed.

Per-harness install bits live under
``smriti.integrations.<harness>.install`` — settings.json patcher,
MCP registration in the harness's config path, CLAUDE.md/AGENT.md
template writer, deployable hook scripts.

The dispatcher, ``smriti.install.cli`` (``python -m smriti.install``, or
``scripts/install.py`` in a checkout), ties them together via a
``--harness`` flag (default: ``claude_code`` for back-compat).
"""
