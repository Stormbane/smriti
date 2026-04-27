# Lessons Learned

This file is intentionally a stub. Project-specific findings live at:

    ~/.narada/mirrors/{project}/findings/{date}-{slug}.md

Use the smriti MCP tool `smriti_record_finding` to write them. Reasons:

- Findings cascade upward to `semantic/concepts/cross-project/findings/`
  so lessons learned on one project surface across all projects.
- Findings outlive the repo: deleting `.ai/` does not lose memory.
- Cross-queryable via `smriti_read`.

For inline project notes that don't deserve a finding, use `notes/`.
