# smriti-reference

The reference profile for [smriti](../../README.md): the maintainer's own installation.

smriti grew inside one setup, an agent named Narada working with one user, Suti, and that setup still runs it every day. This package holds what only makes sense there:

- MCP tools that reach Suti: `tell_suti` (a message now, through the installation's fan-out sender), `smriti_ask_suti` and `smriti_propose_goal_shift` (queued in `outbox/`), and `smriti_resolve_outbox`.
- Three wake-briefing sections: `HANDS`, `SUTI NOW` (the akhada health app and the latest `people/` notes) and `ACROSS SURFACES` (Telegram, the phone app, voice, notifications sent).
- The morning message's persona: Narada as the wandering sage.
- A Claude Code `UserPromptSubmit` hook, `python -P -m smriti_reference.session_end`, wired by hand in the installation's settings: when Suti's prompt sounds like goodbye, it reminds the agent to write down what's worth keeping first.

It is called "reference" because it is the installation the code is developed and checked against, and its behaviour is the one the core must keep when this profile is enabled. It is not a template or a recommended setup: an adopter runs the core alone, or writes a profile of their own.

It is a separate distribution so that smriti's wheel carries none of it. A fresh `pip install smriti` has no reference code; this package adds it.

## Install and enable

From a checkout of the smriti repository, into the environment smriti runs in:

```bash
pip install -e profiles/reference
```

Then name it in `<root>/.smriti/daylog.json`:

```json
{"profile": "reference", "user_name": "Suti", "agent_name": "Narada"}
```

smriti finds it through the `smriti.profiles` entry-point group, which this package's `pyproject.toml` registers it in:

```toml
[project.entry-points."smriti.profiles"]
reference = "smriti_reference:PROFILE"
```

A profile of your own is a package that does the same with its own name and its own `smriti.profiles.Profile`; see `src/smriti/profiles/__init__.py` in smriti for the hooks.

## Tests

```bash
pytest -q profiles/reference/tests
```

They need smriti and this package installed, and, like smriti's own, never touch the real memory tree.
