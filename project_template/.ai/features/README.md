# Features

One file per feature. Lifecycle: active/ → shipped/ or abandoned/.

Each feature file is BOTH the planning doc and (once shipped) the
documentation. Don't write the same thing in two places.

## Frontmatter

```yaml
---
status: planning | building | reviewing | shipped | abandoned
opened: YYYY-MM-DD
shipped: YYYY-MM-DD       # set on transition to shipped/
owner: suti | narada | both
related_findings: [link]
related_decisions: [link]
---
```

## Body sections

- **Goal** — what this feature is for
- **Why now** — why this is the right time
- **Constraints** — must / must not
- **Plan** — step-by-step
- **Open questions** — tracks asks of Suti via outbox
- **Tests / acceptance criteria**
- **Implementation notes** — filled during build
- **Findings** — links to findings/ entries that came from this work
- **Documentation** — once shipped, this is the canonical doc

Use the smriti tools `smriti_record_feature` and
`smriti_update_feature_status` to manipulate these files.
